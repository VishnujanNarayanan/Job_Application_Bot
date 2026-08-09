"""Layer 2 — hard filters applied at scrape time (architecture §4 / §7.4).

The predicates are pure (data in, bool out) so they unit-test without a DB.
The DB-backed lookups (`existing_job_ids`, `company_last_notified`) are
thin wrappers the orchestrator calls to gather the data the predicates
need. The years-ceiling predicate is shared with Layer 3, which re-checks
it on the Gemini-structured field.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from src.config import settings
from src.state.models import AllJobs, Applied, CompanyCooldown


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


def company_in_cooldown(
    last_notified_at: datetime | None,
    now: datetime,
    cooldown_days: int,
) -> bool:
    """True if the company was notified within the cooldown window."""
    if last_notified_at is None:
        return False
    return now - last_notified_at < timedelta(days=cooldown_days)


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


def company_last_notified(session: Session, company: str) -> datetime | None:
    """Last time we notified for ``company``, or None (cooldown lookup)."""
    return session.scalar(
        select(CompanyCooldown.last_applied_at).where(
            CompanyCooldown.company == company
        )
    )
