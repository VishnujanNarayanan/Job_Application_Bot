"""Layer 2 — hard filters applied at scrape time (architecture §4 / §7.4).

The predicates are pure (data in, bool out) so they unit-test without a DB.
The DB-backed lookup (`existing_job_ids`) is a thin wrapper the
orchestrator calls to gather the data the predicates need. The years-ceiling predicate is shared with Layer 3, which re-checks
it on the Gemini-structured field.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from src.config import settings
from src.state.models import AllJobs, Applied


def location_disallowed(location: str | None, disallowed_regions: Iterable[str]) -> bool:
    """True if the location string contains any disallowed region (ci substring)."""
    if not location:
        return False
    loc = location.casefold()
    return any(region.casefold() in loc for region in disallowed_regions)


def exceeds_years_ceiling(years_required: int | None, ceiling: int) -> bool:
    """True if the JD demands more years than the operator's ceiling."""
    if years_required is None:
        return False
    return years_required > ceiling


def experience_floor(experience_range: str | None) -> int | None:
    """The lower bound of a portal experience band: "3-6 Yrs" -> 3, "5+ Yrs"
    -> 5, "Fresher" -> 0. None when the text holds no number."""
    import re

    if not experience_range:
        return None
    if "fresher" in experience_range.casefold():
        return 0
    m = re.search(r"\d+", experience_range)
    return int(m.group()) if m else None


def experience_exceeds_ceiling(experience_range: str | None, ceiling: int) -> bool:
    """True if the portal's experience band STARTS above the years ceiling.

    Read before the parse: Naukri states a band on every listing, and its
    keyword search is loose -- "backend developer" returned 12-20 and 10-18
    year roles -- so most of what it returns would otherwise cost an LLM call
    only to fail the same ceiling at Layer 3. The lower bound is used, matching
    that check's "more years than this" rule: a 5-8 band passes a ceiling of 6.
    """
    floor = experience_floor(experience_range)
    return floor is not None and floor > ceiling


def job_level_disallowed(job_level: str | None, blocklist: Iterable[str]) -> bool:
    """True if the portal's seniority label is on the blocklist (case-insensitive,
    whole label). LinkedIn files most experienced-hire roles, including plain
    "Java Developer" and "Software Engineer II", as "mid-senior level", so only
    labels that are senior without exception belong on the list."""
    if not job_level:
        return False
    level = job_level.strip().casefold()
    return any(level == str(b).strip().casefold() for b in blocklist)


def job_type_disallowed(job_type: str | None, wanted: str | None) -> bool:
    """True if the JD's employment type is not the one the operator wants.

    `filters.job_type` sat in config.yaml unread since the config was written:
    "Fulltime only" is a stated operator rule, but nothing enforced it, and by
    2026-08-19 the database held 14 contract, 9 internship and 2 part-time
    listings that had been scored and notified like any other job.

    An unknown type passes. The parser leaves `job_type` null on roughly a
    quarter of ads — usually because the ad never says — and rejecting those
    would discard far more real full-time jobs than the handful of contracts
    it would catch.
    """
    if not wanted or job_type is None:
        return False
    return str(job_type).strip().casefold() != str(wanted).strip().casefold()


def company_blocked(company: str | None, blocklist: Iterable[str]) -> bool:
    """True if the company is on the operator's blocklist (case-insensitive,
    whole name). `filters.company_blocklist` sat in config unread until
    2026-10-05: a company added there was still scraped, parsed and notified."""
    if not company:
        return False
    name = company.strip().casefold()
    return any(name == str(b).strip().casefold() for b in blocklist)


def title_disallowed(title: str | None, patterns: Iterable[str]) -> bool:
    """True if the job TITLE names an employment type the operator rejects.

    Checked before the parse, and it catches what the parse misses: across
    1,221 stored jobs the parser called "Software Developer Intern" full-time,
    and two internships were notified as matches. A title that says "Intern"
    is not ambiguous; the description often is.
    """
    import re

    if not title:
        return False
    return any(re.search(p, title, re.IGNORECASE) for p in patterns)


def cannot_reach_threshold(
    applicants_count: int | None, best_fit: float, threshold: float
) -> bool:
    """True if even a ``best_fit`` match could not clear ``threshold`` at this
    applicant count.

    final_score = fit x applicant multiplier, so a crowded posting needs a fit
    no stored job has reached: at 200+ applicants the multiplier is 0.5 and a
    0.45 threshold needs fit 0.90, against a best-ever fit of 0.728. Parsing
    those spent ~1 in 5 LLM calls on jobs that could not match. Derived from the
    live threshold and multiplier, so retuning either moves the cut-off with it.
    An unknown count never qualifies -- an absent measurement is not a crowd.
    """
    if applicants_count is None:
        return False
    from src.scorer.apply_decision import applicant_multiplier

    return best_fit * applicant_multiplier(applicants_count) < threshold


# --- DB-backed lookups (thin; the predicates above do the deciding) --------


def existing_job_ids(session: Session, job_ids: Iterable[str]) -> set[str]:
    """Subset of ``job_ids`` that should NOT be processed again.

    Only jobs already **notified** qualify — a row in ``applied``. Everything
    else is re-parsed and re-scored on every run.

    That is deliberate now that inference is local and unmetered. Skipping on
    mere presence in ``all_jobs`` cost 176 of 745 jobs (24%): every scraped job
    is written there before any is parsed, so a run abandoned partway through
    left its remainder sitting with no verdict, and the next run dismissed all
    of them as duplicates — scraped, never scored, and never would be.

    Re-scoring is also worth having on its own: scoring changes (whole-document
    embeddings, unclipped descriptions) never reached anything already seen,
    so a job judged by an older, worse scorer kept that judgement forever.

    ``applied`` stays excluded because re-notifying is not free in the way
    compute is — it costs the operator's attention, and the same job arriving
    on Telegram every run is worse than useless. Set
    ``scraper.rescore_notified`` to re-examine even those.
    """
    ids = list(job_ids)
    if not ids:
        return set()

    if bool(settings.scraper.get("rescore_notified", False)):
        return set()

    rows = session.scalars(
        select(AllJobs.job_id).where(
            AllJobs.job_id.in_(ids),
            exists().where(Applied.job_id == AllJobs.job_id),
        )
    ).all()
    return set(rows)

