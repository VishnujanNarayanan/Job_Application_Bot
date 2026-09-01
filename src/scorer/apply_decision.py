"""Layer 4 — final scoring + apply/skip decision.

Combines the deterministic selections (selector.py) into the final score and
the build-or-skip decision. Pure: ``evaluate(profile, jd, now=...)`` takes the
candidate pool + JD context and returns a :class:`SelectionResult` — no DB,
LLM, model, or network. The orchestrator loads the profile, calls this, and
(for every match >= threshold) hands the result to Layer 5; there are NO
quotas and NO top-N picking (CLAUDE.md hard rule #14).

Formulas (PIVOT_V3.md D6 + config.scoring):

    fit          = best_experience*0.55 + keyword_coverage*0.45
    success_prob = seniority*0.60 + recency*0.40
    recency      = banded on hours since posted
    final        = fit*0.55 + success_prob*0.30 + recency*0.10 + project*0.05
    apply        = final >= 0.50

``keyword_coverage`` replaced ``selected_summary*0.20 + avg_skill_pool_match*0.30``.
Both of those measured cosine against content the Headless template does not put on
the page — a summary paragraph and a skills list that no longer exist — so half the
fit score was grading material no recruiter would read. Coverage instead measures
the weighted fraction of the JD's own stated qualifications that the bullets we
ACTUALLY SELECTED literally contain.

Note the ordering that implies: selection runs first, and the score is computed on
its output. The two numbers are therefore not independent, which is exactly why
``scoring.apply_threshold`` has to be re-measured against a real corpus before it
means anything (PIVOT_V3.md Stage 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import settings
from src.reasons import LOW_SCORE
from src.scorer.keywords import Keyword, coverage_of
from src.scorer.ordering import order_entries
from src.scorer.selector import (
    JDContext,
    Profile,
    SelectedEntry,
    select_entries,
)


@dataclass
class SelectionResult:
    """Everything Layer 5 needs to build a selection_json, plus the scores."""

    apply: bool
    final_score: float
    fit: float
    success_prob: float
    recency: float
    project_score: float
    #: Work entries then project entries, in render order.
    entries: list[SelectedEntry]
    work: list[SelectedEntry]
    projects: list[SelectedEntry]
    #: Weighted fraction of the JD checklist covered by the UNION of all entries.
    keyword_coverage: float
    #: The same for the first entry alone. This is the number the method actually
    #: grades on — "the first entry must tick every box by itself" — because a
    #: token in the last bullet of the last entry is a token nobody read.
    lead_entry_coverage: float
    jd_keywords: tuple[Keyword, ...] = ()
    reason_category: str | None = None


def seniority_score(role_level: str | None) -> float:
    """Map a Gemini-parsed role_level to its seniority weight (config)."""
    scores = settings.scoring.success_prob.seniority_scores.as_dict()
    # YAML `null:` parses to a None key — used when role_level is unknown.
    return float(scores.get(role_level, scores.get(None, 0.80)))


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _unknown_recency(cfg) -> float:
    """Recency for a listing with NO timestamp of any kind.

    Not ``default``. ``default`` means "measured, and older than every band" —
    a verdict. This case has no measurement at all, and scoring an absence as
    though it were the worst observation penalises a job for its portal's
    metadata rather than for anything about the job. Absence of evidence is
    not evidence of staleness.

    So it scores neutral: the midpoint of the configured range, which is what
    an average posting would earn. ``scoring.recency_score.unknown`` overrides
    it; otherwise it is derived from the bands so that re-tuning them carries
    the neutral point along instead of stranding a stale literal.
    """
    configured = cfg.get("unknown")
    if configured is not None:
        return float(configured)
    scores = [float(b["score"]) for b in cfg.bands] + [float(cfg.default)]
    return (min(scores) + max(scores)) / 2.0


def recency_score(
    posted_at: datetime | None,
    now: datetime,
    *,
    scraped_at: datetime | None = None,
    window_hours: float | None = None,
) -> float:
    """Band the hours since posting into a recency score.

    Both the band edges and their scores come from
    ``scoring.recency_score.bands`` — they used to be hardcoded at 1/3/6/12
    hours here, which meant the bands could not be resized when the scrape
    window changed.

    **Undated listings.** LinkedIn supplies ``date_posted`` on 0.2% of
    listings (1 of 472, measured 2026-08-09) and it is currently the only
    enabled source, so "no timestamp" is the common path rather than the
    exception. Scoring those at ``default`` — the band meaning "older than
    every band we have" — was flatly wrong: JobSpy is asked for postings
    younger than ``scraper.hours_old``, so an undated listing is known to have
    been inside that window when it was scraped. Every job scored on the live
    run of 2026-08-09 reported recency 0.30 for this reason, and two of the
    seven (BCG X 0.465, Ecolab 0.437) would have crossed the 0.50 threshold
    without the penalty.

    ``scraped_at`` is what makes the inference sound, and it is used as a
    BOUND, not as a substitute posted_at: the listing appeared somewhere in
    ``[scraped_at - window, scraped_at]``, so its expected age now is
    ``(now - scraped_at) + window/2``. Taking the midpoint rather than either
    edge keeps this honest in both directions — a live run scores an undated
    job as roughly half a window old (0.60 on the configured bands) instead of
    0.30, while a backfill of a six-week-old row still lands in ``default``,
    because the elapsed term dominates. Substituting ``scraped_at`` outright
    would have driven every live job to 1.00 and matched 20 of 20 on a sample
    whose recorded verdict was 0 of 20 — saturation, not accuracy.

    With no timestamp of any kind, nothing is inferred and the job scores
    neutral (see :func:`_unknown_recency`) rather than being penalised for
    metadata its portal never supplied.
    """
    cfg = settings.scoring.recency_score
    now = _as_utc(now)

    if posted_at is not None:
        hours = (now - _as_utc(posted_at)).total_seconds() / 3600.0
    elif scraped_at is not None:
        window = (
            float(window_hours)
            if window_hours is not None
            else float(settings.scraper.hours_old.peak)
        )
        hours = (now - _as_utc(scraped_at)).total_seconds() / 3600.0 + window / 2.0
    else:
        return _unknown_recency(cfg)

    # Ascending by edge, so a mis-ordered config still behaves sanely.
    for band in sorted(cfg.bands, key=lambda b: float(b["under_hours"])):
        if hours < float(band["under_hours"]):
            return float(band["score"])
    return float(cfg.default)


def evaluate(
    profile: Profile,
    jd: JDContext,
    *,
    keywords: tuple[Keyword, ...] = (),
    now: datetime | None = None,
) -> SelectionResult:
    """Score one job against the profile and decide build-or-skip."""
    now = now or datetime.now(timezone.utc)

    # Three groups, not two. Employment is force-included -- a resume without the
    # operator's actual job is not a resume. Freelance engagements are separate
    # entries that compete on merit exactly as projects do: any, all or none may
    # appear on a given resume. They still render under Work History, because
    # that is what they are.
    employment = [e for e in profile.work if e.employment_type != "freelance"]
    freelance = [e for e in profile.work if e.employment_type == "freelance"]

    jobs = select_entries(employment, jd, keywords, kind="work", now=now)
    gigs = select_entries(freelance, jd, keywords, kind="freelance", now=now)
    projects = sorted(
        select_entries(profile.projects, jd, keywords, kind="project", now=now),
        key=lambda e: e.score,
        reverse=True,
    )
    # Employment and freelance are selected against different bars -- a job is
    # force-included, a gig has to earn its slot -- but once selected they are
    # ORDERED together on merit. Employment is not pinned to the top: if a
    # freelance engagement matches this JD better, it leads, and the job moves
    # down. Recency still breaks near-ties (order_entries).
    work = order_entries([*jobs, *gigs])
    entries = [*work, *projects]

    # Only real employment sets the experience score. A freelance engagement that
    # happens to match well should not stand in for having held the job.
    best_experience = max((e.score for e in jobs), default=0.0)
    best_project = max((e.score for e in projects), default=0.0)

    # The union of the per-entry covered sets, not a re-scan of the text: an
    # entry's set is by construction exactly what its SELECTED bullets hit, and
    # re-deriving it here would be a second definition of "covered" to keep in
    # step with the first.
    union: set[str] = set().union(*(e.covered for e in entries)) if entries else set()
    keyword_coverage = coverage_of(union, keywords)
    lead_entry_coverage = entries[0].coverage if entries else 0.0

    fit_cfg = settings.scoring.fit
    fit = (
        fit_cfg.best_experience * best_experience
        + fit_cfg.keyword_coverage * keyword_coverage
    )

    sp_cfg = settings.scoring.success_prob
    recency = recency_score(
        jd.posted_at,
        now,
        scraped_at=jd.scraped_at,
        window_hours=jd.scrape_window_hours,
    )
    success_prob = (
        sp_cfg.weight_seniority * seniority_score(jd.role_level)
        + sp_cfg.weight_recency * recency
    )

    final_cfg = settings.scoring.final
    final_score = (
        final_cfg.fit * fit
        + final_cfg.success_prob * success_prob
        + final_cfg.recency * recency
        + final_cfg.project * best_project
    )

    apply = final_score >= settings.scoring.apply_threshold
    return SelectionResult(
        apply=apply,
        final_score=final_score,
        fit=fit,
        success_prob=success_prob,
        recency=recency,
        project_score=best_project,
        entries=entries,
        work=work,
        projects=projects,
        keyword_coverage=keyword_coverage,
        lead_entry_coverage=lead_entry_coverage,
        jd_keywords=keywords,
        reason_category=None if apply else LOW_SCORE,
    )
