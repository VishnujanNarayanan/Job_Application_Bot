"""The operator's decision on a matched job: pending, applied or dismissed.

One code path for every place that records it -- the dashboard's status
endpoint and the Telegram buttons (#15) -- so the two can never disagree about
what "applied" writes.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

STATUSES = ("pending", "applied", "dismissed")


class UnknownJobError(LookupError):
    """No ``applied`` row exists for the job id."""


def set_job_status(session: Session, job_id: str, status: str) -> tuple[str | None, datetime | None]:
    """Set ``applied.user_status`` for ``job_id`` and commit.

    Returns ``(previous_status, recorded_at)``. Returning a job to pending
    clears the action date rather than leaving a stale one behind, so
    ``recorded_at`` is None for pending. Setting the status a job already has
    is harmless: it rewrites the same value (and refreshes the date).
    """
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")

    from src.state.models import Applied

    row = session.get(Applied, job_id)
    if row is None:
        raise UnknownJobError(job_id)

    previous = row.user_status
    row.user_status = status
    row.user_status_at = None if status == "pending" else datetime.now(timezone.utc)
    session.commit()
    return previous, row.user_status_at
