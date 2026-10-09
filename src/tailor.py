"""Score and build one job: shared by the scraper run and pasted adverts (#20).

The scraper run (``src.main``) and ``python -m src.cli.tailor`` both turn a
parsed advert into a scored selection and an ``applied`` row through
:func:`score_job` and :func:`build_applied`, so a pasted advert is judged exactly
as a scraped one would be. Only the gates differ: the run applies the hard
filters and the apply threshold; :func:`tailor` reports what they would have
said and builds the resume anyway.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from src.config import settings

log = structlog.get_logger(__name__)

MANUAL_SITE = "manual"
UNKNOWN = "Unknown"


# ---------------------------------------------------------------------------
# Shared steps (scraper run and pasted adverts)
# ---------------------------------------------------------------------------


def compute_gap_skills(required: list[str], pool: list[str], bullets: tuple[str, ...] = ()) -> list[str]:
    """Required skills the operator shows NOWHERE: not in the skills pool, not in
    any bullet, in any spelling or family form (``keywords.matches``, #23).

    It used to be a one-way substring test against the pool alone, which called
    "CI/CD pipelines" a gap in 32 matched jobs although the profile states CI/CD
    throughout, and disagreed with coverage, which reads the bullets.
    """
    from src.scorer.keywords import matches, norm

    # Split "Python (NumPy, pandas)" into its parts, but keep a LEADING dot:
    # keywords.tokens_of strips it, which turns ".NET" into the plain word "net"
    # and lets any bullet saying "net" clear it.
    tokens = list(dict.fromkeys(
        p.strip(" ;:") for line in required for p in re.split(r"[,()]", line or "")
        if len(p.strip(" ;:")) >= 2
    ))
    evidence = [norm(s) for s in pool] + list(bullets)
    return [t for t in tokens if not any(matches(t, e) for e in evidence)]


def bullet_texts(profile) -> tuple[str, ...]:
    """Every bullet the operator could render, normalised, de-duplicated."""
    return tuple(dict.fromkeys(
        b.norm_text
        for e in (*profile.work, *profile.projects)
        for blk in e.blocks
        for b in blk.bullets
    ))


def score_job(profile, job, parsed, *, scrape_window_hours: float | None = None):
    """Layer 4: embed the parsed advert and score it against the profile."""
    from src.scorer.apply_decision import evaluate
    from src.scorer.keywords import jd_keywords
    from src.scorer.selector import build_jd_context

    # scraped_at + the window the run used let recency be inferred for the
    # listings that carry no posting date (almost all of them). Recency is
    # recorded, not scored; the applicant count is scored.
    jd_context = build_jd_context(
        parsed,
        posted_at=job.posted_at,
        scraped_at=job.scraped_at,
        scrape_window_hours=scrape_window_hours,
        applicants_count=job.applicants_count,
        jd_text=job.jd_text,
    )
    return evaluate(profile, jd_context, keywords=jd_keywords(parsed))


@dataclass
class Built:
    """Layer 5 output: the selection and the ``applied`` row that stores it."""

    selection: object
    applied: object
    gap_skills: list[str]
    title_alias: str


def build_applied(profile, job, parsed, result) -> Built | None:
    """Layer 5 + 7: build the selection and an (unsaved) ``applied`` row.

    None when the builder returns no selection; the caller records that as a
    BUILD_FAILURE.
    """
    from src.builder.llm_call import build as build_selection
    from src.state.models import Applied

    # Gap skills: JD required skills not in operator's pool
    skills_pool = [sc.skill for sc in profile.skills]
    gap_skills = compute_gap_skills(
        list(parsed.required_skills or []), skills_pool, bullet_texts(profile)
    )
    selection = build_selection(
        result=result,
        profile=profile,
        jd_role_summary=parsed.role_summary,
        jd_required_skills=list(parsed.required_skills or []),
        jd_team_or_product=parsed.team_or_product,
    )
    if selection is None:
        return None
    selection.job_id = job.job_id

    # The notification's display title comes from the first WORK entry, not
    # the first entry on the page. v3.2 merged work and projects into one
    # section ordered by match, so position 1 can be a project — and a
    # project has no title, only a name and an arbitrary alias list.
    title_alias = next(
        (e.title_alias for e in selection.entries if e.kind != "project"),
        job.role,
    )
    expected_salary = parsed.salary_max_lpa or float(settings.salary.default_expected_lpa)
    applied = Applied(
        job_id=job.job_id,
        selection_json=selection.model_dump(),
        template_version=selection.template_version,
        cover_letter_text=selection.cover_letter_text,
        expected_salary_lpa=expected_salary,
        fit_score=result.fit,
        success_prob=result.success_prob,
        recency_score=result.recency,
        final_score=result.final_score,
        gap_skills=gap_skills,
        user_status="pending",
    )
    return Built(selection, applied, gap_skills, title_alias)


# ---------------------------------------------------------------------------
# Pasted adverts (#20): no hard filters, no threshold
# ---------------------------------------------------------------------------


def manual_job_id(jd_text: str) -> str:
    """Stable id for a pasted advert, so pasting it twice reuses the stored job.

    Whitespace is collapsed first: the same advert copied from two places
    differs in line breaks far more often than in words.
    """
    canon = " ".join((jd_text or "").split())
    return "manual-" + hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def parsed_from_row(job):
    """Rebuild the parse stored on an ``all_jobs`` row, or None if it has none.

    Lets a re-pasted advert skip the LLM call: the row already carries every
    field :func:`src.parser.apply_to_row` wrote.
    """
    from src.llm.schemas import JDParsed

    if job.required_skills is None or not job.role_summary:
        return None
    try:
        parsed = JDParsed.model_validate(dict(
            role_summary=job.role_summary,
            role_category=job.role_category or "other",
            role_level=job.role_level,
            years_required=job.years_required or 0,
            required_skills=job.required_skills or [],
            nice_to_have=job.nice_to_have or [],
            responsibilities=job.responsibilities or [],
            team_or_product=job.team_or_product,
            job_type=job.job_type,
            location_type=job.location_type,
            salary_min_lpa=job.salary_min_lpa,
            salary_max_lpa=job.salary_max_lpa,
            salary_currency=job.salary_currency,
        ))
    except Exception as exc:  # a row written by an older schema
        log.warning("stored_parse_unusable", job_id=job.job_id, error=str(exc))
        return None
    # The stored lists are the FINAL ones: `parse` grounds them and adds pool
    # and vocabulary skills after validation. Validating them again drops some
    # of those additions ("Data pipelines" on the first pasted advert), which
    # moved the re-paste's score (0.599 -> 0.594), so restore them verbatim.
    parsed.required_skills = list(job.required_skills or [])
    parsed.nice_to_have = list(job.nice_to_have or [])
    return parsed


@dataclass
class FilterVerdict:
    name: str
    would_reject: bool
    detail: str


def filter_verdicts(session, job, parsed, now: datetime) -> list[FilterVerdict]:
    """What every hard filter WOULD have said. Information only: nothing here
    stops the resume being built."""
    from src.scorer.apply_decision import applicant_multiplier
    from src.scraper import filters

    cfg = settings
    ceiling = int(cfg.filters.years_ceiling)
    wanted_type = cfg.filters.get("job_type")
    regions = list(cfg.filters.disallowed_regions)
    blocklist = list(cfg.filters.get("company_blocklist") or [])
    title_patterns = list(cfg.filters.get("title_blocklist") or [])
    best_fit = float(cfg.filters.get("best_realistic_fit", 1.0))
    threshold = float(cfg.scoring.apply_threshold)
    cooldown_days = int(cfg.scraper.cooldown_days)
    last = filters.company_last_notified(session, job.company) if job.company != UNKNOWN else None

    return [
        FilterVerdict(
            "years ceiling",
            filters.exceeds_years_ceiling(parsed.years_required, ceiling),
            f"asks {parsed.years_required}, ceiling {ceiling}",
        ),
        FilterVerdict(
            "job type",
            filters.job_type_disallowed(parsed.job_type, wanted_type),
            f"{parsed.job_type or 'not stated'}, wanted {wanted_type or 'any'}",
        ),
        FilterVerdict(
            "location",
            filters.location_disallowed(job.location, regions),
            job.location or "not given",
        ),
        FilterVerdict(
            "company blocklist",
            filters.company_blocked(job.company, blocklist),
            job.company,
        ),
        FilterVerdict(
            "title blocklist",
            filters.title_disallowed(job.role, title_patterns),
            job.role,
        ),
        FilterVerdict(
            "too many applicants",
            filters.cannot_reach_threshold(job.applicants_count, best_fit, threshold),
            "unknown" if job.applicants_count is None
            else f"{job.applicants_count} (multiplier {applicant_multiplier(job.applicants_count):.3f})",
        ),
        FilterVerdict(
            "company cooldown",
            filters.company_in_cooldown(last, now, cooldown_days),
            "never notified" if last is None else f"last notified {last:%Y-%m-%d}",
        ),
    ]


@dataclass
class TailorResult:
    job: object
    parsed: object
    result: object
    built: Built
    verdicts: list[FilterVerdict]
    threshold: float
    reused_parse: bool
    files: dict[str, object] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)

    @property
    def below_threshold(self) -> bool:
        return self.result.final_score < self.threshold


def _apply_flags(job, flags: dict) -> None:
    """Flags the operator typed override what was stored or fetched."""
    for name, column in (("company", "company"), ("role", "role"), ("url", "job_url"),
                         ("location", "location"), ("applicants", "applicants_count")):
        if flags.get(name) is not None:
            setattr(job, column, flags[name])


def _job_from_text(session, jd_text: str, flags: dict, say, now: datetime):
    """The ``all_jobs`` row for pasted advert text, created on first paste."""
    from src.scorer.embeddings import embed_documents
    from src.state.models import AllJobs

    job_id = manual_job_id(jd_text)
    job = session.get(AllJobs, job_id)
    if job is not None:
        say(f"Seen this advert before: reusing {job_id}")
        _apply_flags(job, flags)
        return job

    say(f"New advert ({len(jd_text):,} characters): saving it as {job_id}")
    job = AllJobs(
        job_id=job_id, company=UNKNOWN, role=UNKNOWN, site=MANUAL_SITE,
        jd_text=jd_text, scraped_at=now, posted_at=now,
    )
    _apply_flags(job, flags)
    job.jd_embedding = embed_documents([jd_text])[0]
    session.add(job)
    return job


def _job_from_linkedin(session, li_id: str, flags: dict, fetch_fn, say, now: datetime):
    """The ``all_jobs`` row for a LinkedIn link.

    Keyed exactly as the scraper keys it (``linkedin-li-<id>``), so a posting
    the scraper already stored is reused rather than fetched, and one fetched
    here is recognised by later scrapes instead of arriving twice.
    """
    from src.scorer.embeddings import embed_documents
    from src.scraper.jobspy_wrapper import fetch_linkedin_job
    from src.state.models import AllJobs, NotApplied

    job_id = f"linkedin-li-{li_id}"
    job = session.get(AllJobs, job_id)
    if job is not None and (job.jd_text or "").strip():
        say(f"LinkedIn job {li_id} is already stored from a scrape: reusing {job_id}"
            f" ({job.role} at {job.company})")
        skipped = session.get(NotApplied, job_id)
        if skipped is not None:
            # Choosing it by hand overrides the scorer's skip, and keeps the
            # job out of the Skipped view now that it is under Matches.
            say(f"  it had been skipped by the run ({skipped.reason_category});"
                " moving it to Matches")
            session.delete(skipped)
        _apply_flags(job, flags)
        return job

    say(f"Fetching LinkedIn job {li_id}...")
    info = (fetch_fn or fetch_linkedin_job)(li_id)
    say(f"  {info['title'] or 'untitled'} at {info['company'] or 'unknown company'},"
        f" {info['location'] or 'no location'}; {info['applicants_text'] or 'no applicant count'};"
        f" {len(info['description']):,} characters of description")
    if info.get("closed"):
        say("  LinkedIn says this posting no longer accepts applications; building anyway")
    if job is None:
        job = AllJobs(job_id=job_id, company=UNKNOWN, role=UNKNOWN, site="linkedin",
                      scraped_at=now)
        session.add(job)
    job.company = info["company"] or job.company or UNKNOWN
    job.role = info["title"] or job.role or UNKNOWN
    job.location = info["location"] or job.location
    job.job_url = info["url"]
    job.jd_text = info["description"]
    job.applicants_text = info["applicants_text"]
    job.applicants_count = info["applicants_count"]
    _apply_flags(job, flags)
    job.jd_embedding = embed_documents([job.jd_text])[0]
    return job


def tailor(
    session,
    jd_text: str,
    *,
    company: str | None = None,
    role: str | None = None,
    applicants: int | None = None,
    url: str | None = None,
    location: str | None = None,
    parse_fn=None,
    fetch_fn=None,
    now: datetime | None = None,
    progress=None,
) -> TailorResult:
    """Parse, score and build a resume for a pasted advert, whatever it scores.

    ``jd_text`` is the advert text, or just a LinkedIn job link: a link reuses
    the job if a scrape already stored it, and otherwise reads the posting from
    LinkedIn's public page (``fetch_fn``, for tests).

    Writes the ``all_jobs`` and ``applied`` rows (so the dashboard, the render
    cache and "Mark applied" work unchanged) but never a company cooldown: a
    pasted advert is the operator's own choice, not a notification.

    Raises ``ValueError`` for an empty advert and lets the parser's
    ``LLMError`` through, so the caller can show it instead of a silent skip.

    ``progress(message)``, if given, is called at every step with a one-line
    account of what the backend is doing and what it found, so the CLI and the
    Tailor page can show the work as it happens.
    """
    from src.llm.client import observe
    from src.parser import apply_to_row, parse
    from src.scraper.jobspy_wrapper import linkedin_job_id
    from src.state import master_profile
    from src.state.models import Applied, RenderCache

    if not (jd_text or "").strip():
        raise ValueError("the advert is empty")
    now = now or datetime.now(timezone.utc)
    parse_fn = parse_fn or parse
    say = progress or (lambda _msg: None)
    flags = dict(company=company, role=role, url=url, location=location,
                 applicants=applicants)

    li_id = linkedin_job_id(jd_text)
    if li_id:
        job = _job_from_linkedin(session, li_id, flags, fetch_fn, say, now)
    else:
        job = _job_from_text(session, jd_text, flags, say, now)
    job_id = job.job_id

    # End the transaction before the LLM call: Neon kills a session left idle
    # inside one for 5 minutes, and a provider fallback can take that long.
    session.commit()

    parsed = parsed_from_row(job)
    reused = parsed is not None
    if parsed is None:
        say("Reading the advert with the AI parser...")

        def on_llm(event, f):
            if event == "trying":
                say(f"  asking {f['provider']} ({f['model']})")
            elif event == "fallback":
                say(f"  {f['from_provider']} failed, falling back to {f['to_provider']}:"
                    f" {f['reason'][:120]}")
            elif event == "served":
                say(f"  {f['provider']} answered in {f['seconds']:.1f}s")

        with observe(on_llm):
            parsed = parse_fn(job)
        apply_to_row(job, parsed)
        say("Parsed")
    else:
        say("Reusing the stored parse: no AI call")
    say(f"  {parsed.role_level} level, {parsed.years_required} years asked,"
        f" {len(parsed.required_skills)} required skills,"
        f" {len(parsed.nice_to_have)} nice to have")
    say("  required: " + (", ".join(parsed.required_skills) or "none"))

    profile = master_profile.load_profile(session)
    say(f"Loaded the profile: {len(profile.work)} jobs, {len(profile.projects)} projects")
    say("Scoring every entry against the advert...")
    result = score_job(profile, job, parsed)
    say(f"  score {result.final_score:.3f} (fit {result.fit:.3f} x applicants"
        f" {result.success_prob:.3f}), keyword coverage {result.keyword_coverage:.2f}")
    say("Building the resume selection...")
    built = build_applied(profile, job, parsed, result)
    if built is None:
        raise RuntimeError(f"the builder returned no selection for {job_id}")
    bullets = sum(len(e.bullets) for e in result.entries)
    say(f"  {len(result.entries)} entries, {bullets} bullets;"
        f" lead: {result.entries[0].header_left if result.entries else 'none'}")

    existing = session.get(Applied, job_id)
    if existing is None:
        session.add(built.applied)
    else:
        if existing.selection_json != built.applied.selection_json:
            # The render cache is keyed on job + template version, not on the
            # selection, so a changed selection must drop the old render.
            for row in session.query(RenderCache).filter(RenderCache.job_id == job_id):
                session.delete(row)
        # Keep the operator's own status; refresh everything the scorer owns.
        for name in ("selection_json", "template_version", "cover_letter_text",
                     "expected_salary_lpa", "fit_score", "success_prob",
                     "recency_score", "final_score", "gap_skills"):
            setattr(existing, name, getattr(built.applied, name))
        built.applied = existing
    job.outcome = "matched"
    job.outcome_at = now
    session.commit()
    say("Saved: it is now under Matches on the dashboard")

    return TailorResult(
        job=job,
        parsed=parsed,
        result=result,
        built=built,
        verdicts=filter_verdicts(session, job, parsed, now),
        threshold=float(settings.scoring.apply_threshold),
        reused_parse=reused,
    )


def breakdown(t: TailorResult) -> dict:
    """The score breakdown as plain data: what the CLI prints and the
    dashboard's Tailor page shows, so the two cannot disagree."""
    r = t.result
    kws = r.jd_keywords
    shown: set[str] = set().union(*(e.covered for e in r.entries)) if r.entries else set()
    required = [k.token for k in kws if k.weight >= 1.0]
    nice = [k.token for k in kws if k.weight < 1.0]

    entries, seen = [], set()
    for e in r.entries:
        entries.append({
            "header": e.header_left,
            "kind": e.kind,
            "score": e.score,
            "bullets": len(e.bullets),
            "adds": sorted(e.covered - seen),
        })
        seen |= e.covered

    return {
        "job_id": t.job.job_id,
        "company": t.job.company,
        "role": t.job.role,
        "reused_parse": t.reused_parse,
        "role_level": t.parsed.role_level,
        "years_required": t.parsed.years_required,
        "final_score": r.final_score,
        "threshold": t.threshold,
        "below_threshold": t.below_threshold,
        "fit": r.fit,
        "lead_entry": r.lead_entry,
        "similarity": r.similarity_scaled,
        "lead_coverage": r.lead_entry_coverage,
        "keyword_coverage": r.keyword_coverage,
        "repetition": r.keyword_repetition,
        "applicant_multiplier": r.success_prob,
        "applicants": t.job.applicants_count,
        "required_total": len(required),
        "required_shown": [k for k in required if k in shown],
        "required_missing": [k for k in required if k not in shown],
        "nice_shown": [k for k in nice if k in shown],
        "nice_total": len(nice),
        "not_in_profile": list(t.built.gap_skills),
        "entries": entries,
        "filters": [
            {"name": v.name, "would_reject": v.would_reject, "detail": v.detail}
            for v in t.verdicts
        ],
    }
