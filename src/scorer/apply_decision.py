"""Layer 4 — final scoring + apply/skip decision.

Combines the deterministic selections (selector.py) into the final score and
the build-or-skip decision. Pure: ``evaluate(profile, jd, now=...)`` takes the
candidate pool + JD context and returns a :class:`SelectionResult` — no DB,
LLM, model, or network. The orchestrator loads the profile, calls this, and
(for every match >= threshold) hands the result to Layer 5; there are NO
quotas and NO top-N picking (CLAUDE.md hard rule #14).

Formulas (PIVOT_V3.md D6 + config.scoring):

    fit          = lead_entry*0.45 + keyword_coverage*0.35 + keyword_repetition*0.20
    success_prob = applicant score, banded on LinkedIn's applicant count
    final        = fit*0.60 + success_prob*0.40
    apply        = final >= scoring.apply_threshold

Each factor is counted once (issue #13). Recency used to enter both inside
success_prob and as its own 0.10 term -- an undeclared 22% of the score -- and the
1-hour scrape window then made it identical for every job. It is still computed
and recorded (``recency``), but no longer scored. success_prob also carried a
role_level seniority term, removed so the years ceiling is the only experience
gate.

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
    gated_terms,
    select_top,
    select_entry_bullets,
)


@dataclass
class SelectionResult:
    """Everything Layer 5 needs to build a selection_json, plus the scores."""

    apply: bool
    final_score: float
    fit: float
    success_prob: float
    #: Time since posting, banded. RECORDED, NOT SCORED (see module docstring).
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
    #: Mean over covered required keywords of min(entries showing it, cap) / cap.
    keyword_repetition: float = 0.0


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


def applicant_score(count: int | None) -> float:
    """Band LinkedIn's applicant count: fewer applicants scores higher.

    Bands come from ``scoring.success_prob.applicant_bands`` (first ``under``
    that the count is below wins); 200+ scores ``default``. No count at all --
    a non-LinkedIn portal, a page without the caption, or a row from before
    capture existed -- scores ``unknown``, a neutral midpoint, because an absent
    measurement is not a crowded posting.
    """
    cfg = settings.scoring.success_prob
    if count is None:
        return float(cfg.unknown)
    for band in sorted(cfg.applicant_bands, key=lambda b: int(b["under"])):
        if count < int(band["under"]):
            return float(band["score"])
    return float(cfg.default)


def repetition_score(entries: list[SelectedEntry], keywords: tuple[Keyword, ...]) -> float:
    """How many roles on the page show each required keyword the page covers.

    A required skill demonstrated in three roles is stronger evidence than the
    same skill in one. Per covered required keyword: ``min(entries showing it,
    cap) / cap``, averaged. Keywords the page does not cover are left out on
    purpose -- coverage already scores their absence, and counting it again here
    would double-charge it. With no required keywords in the checklist, every
    keyword stands in.

    ``entry.covered`` is what that entry's SELECTED bullets were credited with,
    so a filler word held by ``capped_keywords`` stops counting here exactly
    where it stops counting for coverage.
    """
    cap = max(1, int(settings.scoring.fit.repetition_cap))
    required = {k.token for k in keywords if k.weight >= 1.0} or {
        k.token for k in keywords
    }
    counts = {t: sum(1 for e in entries if t in e.covered) for t in required}
    shown = [n for n in counts.values() if n > 0]
    if not shown:
        return 0.0
    return sum(min(n, cap) / cap for n in shown) / len(shown)


def evaluate(
    profile: Profile,
    jd: JDContext,
    *,
    keywords: tuple[Keyword, ...] = (),
    now: datetime | None = None,
) -> SelectionResult:
    """Score one job against the profile and decide build-or-skip."""
    now = now or datetime.now(timezone.utc)

    # One pool, one ranking (v3.4). Work, freelance and projects are scored the
    # same way and compete for the same `selection.top_n` slots -- which is what
    # the merged section already renders. No per-kind threshold decides who gets
    # on the page, because a threshold is a percentile of a distribution that
    # stops existing whenever the formula moves. `select_top` guarantees the
    # salaried job a slot; `order_entries` guarantees it position 1 or 2.
    entries = order_entries(select_top(profile, jd, keywords, now=now))
    work = [e for e in entries if e.kind != "project"]
    projects = [e for e in entries if e.kind == "project"]

    # The cross-entry keyword ceiling applies to what actually RENDERS, so it runs
    # here rather than inside scoring: entries were ranked on their own merits,
    # and only now is it known which ones survived and in what order. Re-selecting
    # in render order means the best-placed entry keeps a contested keyword and
    # later entries give it up.
    kw_cap = int(getattr(settings.selection.bullets, "max_keyword_renders", 0) or 0)
    if kw_cap or gated_terms():
        by_id = {e.id: e for e in (*profile.work, *profile.projects)}
        kw_ledger: dict[str, int] = {}
        # Page-level ledger for the gated families (AI coding assistants): once one
        # entry renders that line, no later entry may repeat it.
        gated_ledger: list[str] = []
        rebuilt: list[SelectedEntry] = []
        for se in entries:
            cand = by_id.get(se.id)
            if cand is None:
                rebuilt.append(se)
                continue
            fresh = select_entry_bullets(
                cand, jd, keywords, now=now, rendered_keywords=kw_ledger,
                rendered_gated=gated_ledger,
            )
            # Keep the ranking decided above; only the bullets are re-picked.
            fresh.score, fresh.similarity = se.score, se.similarity
            rebuilt.append(fresh)
        entries = rebuilt
        work = [e for e in entries if e.kind != "project"]
        projects = [e for e in entries if e.kind == "project"]

    # The entry that leads the page sets the experience score, whatever its kind.
    # It used to be salaried employment only, but a project led 70 of 84 stored
    # resumes (2026-10-04): fit was graded on an entry sitting second or lower
    # while the one a recruiter reads first went uncounted. order_entries sorts by
    # score and only ever promotes the job to position 2, so entries[0] is also
    # the best-scoring entry of any kind.
    best_experience = entries[0].score if entries else 0.0
    # Reported for the logs only; it no longer enters the score (the lead entry
    # already is the best project whenever a project leads).
    best_project = max((e.score for e in projects), default=0.0)

    # The union of the per-entry covered sets, not a re-scan of the text: an
    # entry's set is by construction exactly what its SELECTED bullets hit, and
    # re-deriving it here would be a second definition of "covered" to keep in
    # step with the first.
    union: set[str] = set().union(*(e.covered for e in entries)) if entries else set()
    keyword_coverage = coverage_of(union, keywords)
    lead_entry_coverage = entries[0].coverage if entries else 0.0

    fit_cfg = settings.scoring.fit
    keyword_repetition = repetition_score(entries, keywords)
    fit = (
        fit_cfg.best_experience * best_experience
        + fit_cfg.keyword_coverage * keyword_coverage
        + fit_cfg.keyword_repetition * keyword_repetition
    )

    # Recorded for every job, never scored.
    recency = recency_score(
        jd.posted_at,
        now,
        scraped_at=jd.scraped_at,
        window_hours=jd.scrape_window_hours,
    )
    success_prob = applicant_score(jd.applicants_count)

    final_cfg = settings.scoring.final
    final_score = (
        final_cfg.fit * fit
        + final_cfg.success_prob * success_prob
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
        keyword_repetition=keyword_repetition,
        jd_keywords=keywords,
        reason_category=None if apply else LOW_SCORE,
    )
