"""Dashboard status clicks, recorded at once and written to Postgres later.

A dashboard click used to wait on ~6 round trips to Neon (~110 ms each)
before the card moved. Now the click is appended to a local file and the
endpoint answers immediately; the next pipeline run (or a GitHub dispatch
from the dashboard) flushes the file into ``applied.user_status`` through
``set_job_status`` -- the same path as Telegram taps.

Until then the dashboard overlays the queue on what it reads from Postgres
(:func:`pending`), so a reload already shows the job in its new tab.

The file is JSON lines under ``data/``, which the endpoint and the pipeline
share (bind mount in Docker, same checkout locally). A GitHub Actions run can
not see it; that is why the dashboard flushes before dispatching one.
"""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import structlog

log = structlog.get_logger(__name__)

_DATA = Path(__file__).resolve().parents[2] / "data"
QUEUE_PATH = _DATA / "status_queue.jsonl"
# Held for a whole flush, so two flushes (pipeline + dashboard) never both
# trim the file and drop a click appended between them.
_FLUSH_LOCK_PATH = _DATA / "status_queue.flush.lock"


@contextmanager
def _locked(path: Path, mode: str, flags: int = fcntl.LOCK_EX) -> Iterator:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode) as fh:
        fcntl.flock(fh, flags)
        try:
            yield fh
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def enqueue(job_id: str, status: str, at: datetime | None = None) -> datetime:
    """Record a click. Returns the time it was recorded with."""
    at = at or datetime.now(timezone.utc)
    line = json.dumps({"job_id": job_id, "status": status, "at": at.isoformat()})
    with _locked(QUEUE_PATH, "a") as fh:
        fh.write(line + "\n")
        fh.flush()
    return at


def _parse(lines: list[str]) -> dict[str, tuple[str, datetime]]:
    """``job_id -> (status, at)``, the last click per job winning."""
    out: dict[str, tuple[str, datetime]] = {}
    for raw in lines:
        try:
            entry = json.loads(raw)
            out[entry["job_id"]] = (entry["status"], datetime.fromisoformat(entry["at"]))
        except (ValueError, KeyError, TypeError):
            log.warning("status_queue_bad_line", line=raw[:200])
    return out


def pending() -> dict[str, tuple[str, datetime]]:
    """Clicks not yet in Postgres: ``job_id -> (status, clicked_at)``."""
    if not QUEUE_PATH.exists():
        return {}
    with _locked(QUEUE_PATH, "r", fcntl.LOCK_SH) as fh:
        return _parse([line for line in fh.read().splitlines() if line.strip()])


def flush() -> int:
    """Write queued clicks to Postgres. Returns how many were written.

    Never raises: a click that fails to write stays queued for the next flush.
    A click older than the job's recorded action (say a later Telegram tap) is
    dropped rather than overwriting it.
    """
    try:
        with _locked(_FLUSH_LOCK_PATH, "a", fcntl.LOCK_EX | fcntl.LOCK_NB):
            return _flush()
    except BlockingIOError:
        log.info("status_queue_flush_skipped", reason="flush_in_progress")
        return 0
    except Exception as exc:
        log.error("status_queue_flush_failed", error=str(exc))
        return 0


def _flush() -> int:
    if not QUEUE_PATH.exists():
        return 0
    with _locked(QUEUE_PATH, "r") as fh:
        text = fh.read()
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 0

    from src.state.db import session_scope
    from src.state.job_status import UnknownJobError, set_job_status
    from src.state.models import Applied

    written = 0
    failed: list[str] = []
    for job_id, (status, at) in _parse(lines).items():
        try:
            with session_scope() as session:
                row = session.get(Applied, job_id)
                if row is None:
                    raise UnknownJobError(job_id)
                if row.user_status_at and row.user_status_at > at:
                    log.info("status_queue_click_superseded", job_id=job_id, status=status)
                    continue
                previous, _ = set_job_status(session, job_id, status, at=at)
            written += 1
            log.info("job_status_changed", job_id=job_id, was=previous, now=status,
                     via="dashboard_queue")
        except UnknownJobError:
            log.warning("status_queue_unknown_job", job_id=job_id)
        except Exception as exc:
            log.warning("status_queue_write_failed", job_id=job_id, error=str(exc))
            failed.append(json.dumps({"job_id": job_id, "status": status,
                                      "at": at.isoformat()}))

    # Keep clicks appended while we were writing, plus any that failed.
    with _locked(QUEUE_PATH, "r+") as fh:
        newer = fh.read()[len(text):]
        fh.seek(0)
        fh.write("".join(line + "\n" for line in failed) + newer)
        fh.truncate()
    return written
