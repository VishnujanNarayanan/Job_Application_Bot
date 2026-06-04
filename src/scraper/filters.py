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

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.state.models import AllJobs, CompanyCooldown


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
    """Subset of ``job_ids`` already present in ``all_jobs`` (duplicate check)."""
    ids = list(job_ids)
    if not ids:
        return set()
    rows = session.scalars(
        select(AllJobs.job_id).where(AllJobs.job_id.in_(ids))
    ).all()
    return set(rows)


def company_last_notified(session: Session, company: str) -> datetime | None:
    """Last time we notified for ``company``, or None (cooldown lookup)."""
    return session.scalar(
        select(CompanyCooldown.last_applied_at).where(
            CompanyCooldown.company == company
        )
    )
