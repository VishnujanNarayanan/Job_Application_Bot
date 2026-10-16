"""Layer 2 — fetching ATS job boards on a schedule (#97).

Transport and bookkeeping only; what to keep is decided by the caller
(``src.ats_pipeline``):

- :func:`due_boards`: boards marked ``poll`` whose ``next_poll_at`` has come,
  oldest first.
- :func:`fetch_lists` / :func:`fetch_details`: one paced lane per ATS.
- :func:`record`: ``ats_postings`` bookkeeping (seen, closed, first-poll
  baseline) for one board.
- :func:`schedule`: the board's tier and next poll time.

Pacing (hard rule #4): one lane per ATS, one request at a time, ``delay``
seconds apart. A 429 pauses that lane for 2 minutes, a second one for 10, and
a third stops it for the run. Lanes run in parallel, so no ATS sees more than
one request a second. A lane stops starting boards once its time budget is
spent; the boards not reached stay due and go first next run.

Tiers: ``hot`` (a posting sent on in the last 7 days, or the measurement saw
fitting jobs), ``active`` (anything posted in the last 7 days), ``quiet``
(nothing). Each tier has its own interval (``ats_poll.tier_hours``).
"""

from __future__ import annotations

import difflib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests
import structlog

from src.scraper import ats

log = structlog.get_logger(__name__)

UA = "Mozilla/5.0 (job-application-bot; personal use)"
PAUSES = (120, 600)  # seconds after the 1st and 2nd 429; the 3rd stops the lane
TIMEOUT = 40
TIER_HOURS = {"hot": 1.0, "active": 3.0, "quiet": 12.0}
# A board whose fetch failed is retried after this long.
RETRY_HOURS = 1.0


class LaneStopped(Exception):
    """Too many 429s: this ATS is left alone for the rest of the run."""


class Lane:
    """One ATS's paced HTTP client."""

    def __init__(self, name: str, delay: float, sleep=time.sleep, get=None):
        self.name = name
        self.delay = delay
        self.sleep = sleep
        self.requests = 0
        self.throttled = 0
        self._last = 0.0
        if get is None:
            session = requests.Session()
            session.headers["User-Agent"] = UA
            get = session.get
        self._get = get

    def get(self, url: str) -> tuple[int, str]:
        """(status, body); status -1 on a network error."""
        while True:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                r = self._get(url, timeout=TIMEOUT)
                status, body = r.status_code, r.text
            except requests.RequestException:
                return -1, ""
            if status != 429:
                return status, body
            self.throttled += 1
            if self.throttled > len(PAUSES):
                raise LaneStopped(self.name)
            log.warning("ats_throttled", ats=self.name, pause_s=PAUSES[self.throttled - 1])
            self.sleep(PAUSES[self.throttled - 1])


@dataclass(frozen=True)
class Board:
    """What the poll needs of an ``ats_boards`` row, as plain values: the
    fetch threads must not touch ORM objects, which load lazily through the
    session."""
    ats: str
    board_id: str
    name: str | None = None
    company_key: str | None = None
    polled_at: datetime | None = None
    # Seeds for the first tier, from the measurement.
    fits_7d: int | None = None
    target_posted_7d: int | None = None

    @classmethod
    def of(cls, row) -> "Board":
        return cls(row.ats, row.board_id, row.board_name or row.company, row.company_key,
                   row.polled_at, row.fits_7d, row.target_posted_7d)


@dataclass
class BoardResult:
    board: Board
    postings: list = field(default_factory=list)
    # False when the list was cut short (SmartRecruiters page cap), so
    # absence from it does not mean closed.
    complete: bool = True
    error: str | None = None


# --- Fetching ------------------------------------------------------------------------

def due_boards(session, now: datetime, atses) -> list[Board]:
    """Boards to poll now, least recently due first."""
    from sqlalchemy import or_, select

    from src.state.models import AtsBoard

    rows = session.scalars(
        select(AtsBoard)
        .where(AtsBoard.poll.is_(True), AtsBoard.ats.in_(list(atses)))
        .where(or_(AtsBoard.next_poll_at.is_(None), AtsBoard.next_poll_at <= now))
        .order_by(AtsBoard.next_poll_at.asc().nullsfirst(), AtsBoard.polled_at.asc().nullsfirst())
    )
    return [Board.of(r) for r in rows]


def fetch_board(lane: Lane, board: Board, *, sr_max_pages: int = 30) -> BoardResult:
    """One board's postings. Never raises except LaneStopped."""
    res = BoardResult(board)
    try:
        if board.ats == "smartrecruiters":
            # India through the country filter, all pages. SmartRecruiters
            # boards can hold thousands of jobs, most of them elsewhere.
            offset = 0
            for _page in range(sr_max_pages):
                status, body = lane.get(ats.list_url(board.ats, board.board_id, country="in", offset=offset))
                if status != 200:
                    res.error = f"HTTP {status}"
                    return res
                res.postings += ats.parse_list(board.ats, board.board_id, body)
                offset += ats.SR_PAGE
                if offset >= ats.sr_total(body):
                    break
            else:
                res.complete = False
        else:
            status, body = lane.get(ats.list_url(board.ats, board.board_id))
            if status != 200:
                res.error = f"HTTP {status}"
                return res
            res.postings = ats.parse_list(board.ats, board.board_id, body)
    except LaneStopped:
        raise
    except Exception as exc:  # noqa: BLE001 - one board must not stop the run
        res.error = f"{type(exc).__name__}: {exc}"[:300]
    return res


def fetch_detail(lane: Lane, posting: ats.Posting) -> bool:
    """Add the description from the detail endpoint. False when it failed."""
    url = ats.detail_url(posting)
    if url is None:
        return True
    status, body = lane.get(url)
    if status != 200:
        return False
    try:
        ats.parse_detail(posting, body)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def run_lanes(lanes: dict[str, Lane], work: dict[str, list], fn: Callable, budget_s: float | None) -> list:
    """``fn(lane, item)`` over each ATS's items, one thread per ATS; returns
    ``(item, result)`` pairs. Items a lane did not reach are left out."""
    out: dict[str, list] = {name: [] for name in work}
    started = time.monotonic()

    def run(name: str) -> None:
        lane = lanes[name]
        for item in work[name]:
            if budget_s is not None and time.monotonic() - started > budget_s:
                log.info("ats_lane_budget_reached", ats=name, done=len(out[name]), of=len(work[name]))
                break
            try:
                out[name].append((item, fn(lane, item)))
            except LaneStopped:
                log.error("ats_lane_stopped", ats=name, reason="429")
                break

    threads = [threading.Thread(target=run, args=(n,), daemon=True) for n in work]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [pair for name in work for pair in out[name]]


def fetch_lists(boards: list[Board], lanes: dict[str, Lane], *, budget_s: float,
                sr_max_pages: int = 30, delay: float = 1.0) -> list[BoardResult]:
    by_ats: dict[str, list] = {}
    for b in boards:
        by_ats.setdefault(b.ats, []).append(b)
    for name in by_ats:
        lanes.setdefault(name, Lane(name, delay))
    pairs = run_lanes(lanes, by_ats, lambda lane, b: fetch_board(lane, b, sr_max_pages=sr_max_pages), budget_s)
    return [res for _b, res in pairs]


def fetch_details(postings: list[ats.Posting], lanes: dict[str, Lane], *, delay: float = 1.0) -> dict[str, bool]:
    """job_id -> whether the description was fetched."""
    work: dict[str, list] = {}
    for p in postings:
        work.setdefault(p.ats, []).append(p)
    for name in work:
        lanes.setdefault(name, Lane(name, delay))
    return {p.job_id: ok for p, ok in run_lanes(lanes, work, fetch_detail, None)}


# --- Bookkeeping -------------------------------------------------------------------

#: ``classify(posting)`` returns None to send a posting on, a reason to record
#: it as skipped, or DROP to neither record nor send it.
DROP = "drop"


@dataclass
class PollStats:
    boards: int = 0
    failed: int = 0
    postings: int = 0
    new: int = 0
    baseline: int = 0
    closed: int = 0
    skipped: dict = field(default_factory=dict)
    duplicates: int = 0
    no_description: int = 0
    requests: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def record(session, res: BoardResult, now: datetime, classify: Callable, *, baseline_hours: float,
           stats: PollStats) -> list[ats.Posting]:
    """Update ``ats_postings`` for one fetched board; return the postings to
    send on (new, not skipped, and not part of a first poll's baseline)."""
    from sqlalchemy import select

    from src.state.models import AtsPosting

    board = res.board
    known = {r.posting_id: r for r in session.scalars(
        select(AtsPosting).where(AtsPosting.ats == board.ats, AtsPosting.board_id == board.board_id))}
    first_poll = board.polled_at is None or not known
    send, kept = [], set()
    for p in res.postings:
        reason = classify(p)
        if reason == DROP:
            continue
        kept.add(p.posting_id)
        row = known.get(p.posting_id)
        if row is not None:
            row.last_seen_at = now
            row.closed_at = None
            continue
        if reason:
            stats.skip(reason)
        elif first_poll and not (p.posted_at and now - p.posted_at <= timedelta(hours=baseline_hours)):
            reason = "baseline"
            stats.baseline += 1
        session.add(AtsPosting(
            ats=p.ats, board_id=p.board_id, posting_id=p.posting_id, job_id=p.job_id,
            title=p.title[:500], location=(p.location or "")[:500], place=ats.place(p),
            posted_at=p.posted_at, first_seen_at=now, last_seen_at=now, skipped=reason))
        if reason is None:
            send.append(p)
    if res.complete:
        for pid, row in known.items():
            if pid not in kept and row.closed_at is None:
                row.closed_at = now
                stats.closed += 1
    session.flush()
    return send


def mark_skipped(session, p: ats.Posting, reason: str) -> None:
    """Record a reason found after the list stage (description, duplicate)."""
    from src.state.models import AtsPosting

    row = session.get(AtsPosting, (p.ats, p.board_id, p.posting_id))
    if row is not None:
        row.skipped = reason


def forget(session, p: ats.Posting) -> None:
    """Drop a posting's row so the next poll treats it as new (its
    description could not be fetched)."""
    from src.state.models import AtsPosting

    row = session.get(AtsPosting, (p.ats, p.board_id, p.posting_id))
    if row is not None:
        session.delete(row)


def tier_of(session, res: BoardResult, now: datetime) -> str:
    """hot / active / quiet, from what this board has shown."""
    from sqlalchemy import select

    from src.state.models import AtsPosting

    week = now - timedelta(days=7)
    board = res.board
    sent = session.scalar(
        select(AtsPosting.posting_id)
        .where(AtsPosting.ats == board.ats, AtsPosting.board_id == board.board_id)
        .where(AtsPosting.skipped.is_(None), AtsPosting.first_seen_at >= week).limit(1))
    if sent or (board.polled_at is None and ((board.fits_7d or 0) > 0 or (board.target_posted_7d or 0) > 0)):
        return "hot"
    if any(p.posted_at and p.posted_at >= week for p in res.postings):
        return "active"
    return "quiet"


def schedule(session, res: BoardResult, now: datetime, tier_hours: dict | None = None) -> None:
    """Stamp the board's poll and set when it is next due."""
    from src.state.models import AtsBoard

    hours = {**TIER_HOURS, **(tier_hours or {})}
    row = session.get(AtsBoard, (res.board.ats, res.board.board_id))
    row.polled_at = now
    row.poll_error = res.error
    if res.error:
        row.next_poll_at = now + timedelta(hours=RETRY_HOURS)
        return
    row.tier = tier_of(session, res, now)
    # A few minutes early, so a board due hourly is not pushed past the next
    # run by the minutes this one took.
    row.next_poll_at = now + timedelta(hours=float(hours[row.tier])) - timedelta(minutes=10)


def prune(session, *, now: datetime | None = None, days: int = 60) -> int:
    """Forget postings closed more than ``days`` ago."""
    from sqlalchemy import delete

    from src.state.models import AtsPosting

    now = now or datetime.now(timezone.utc)
    result = session.execute(delete(AtsPosting).where(AtsPosting.closed_at < now - timedelta(days=days)))
    return result.rowcount or 0


def closed_job_ids(session, job_ids: list[str]) -> set[str]:
    """Of ``job_ids``, the ATS jobs whose posting has left its board."""
    from sqlalchemy import select

    from src.state.models import AtsPosting

    if not job_ids:
        return set()
    return set(session.scalars(
        select(AtsPosting.job_id).where(AtsPosting.job_id.in_(job_ids), AtsPosting.closed_at.isnot(None))))


# --- Dedupe against LinkedIn / Naukri ---------------------------------------------------

_TITLE_NOISE = re.compile(r"\(.*?\)|\[.*?\]|[^a-z0-9+# ]+")


def _title_key(title: str) -> str:
    return " ".join(_TITLE_NOISE.sub(" ", (title or "").lower()).split())


def portal_duplicate(candidates: list, company_key: str, title: str) -> str | None:
    """The job_id of a LinkedIn/Naukri job that is this posting: same company
    key and near-identical title. ``candidates``: (job_id, company, role)."""
    want = _title_key(title)
    for job_id, company, role in candidates:
        if ats.key(company or "") != company_key:
            continue
        if difflib.SequenceMatcher(None, _title_key(role), want).ratio() >= 0.9:
            return job_id
    return None


def portal_jobs(session, now: datetime, days: int) -> list:
    """(job_id, company, role) of LinkedIn/Naukri jobs from the last ``days``."""
    from sqlalchemy import select

    from src.state.models import AllJobs

    return session.execute(
        select(AllJobs.job_id, AllJobs.company, AllJobs.role)
        .where(AllJobs.site.in_(["linkedin", "naukri"]))
        .where(AllJobs.scraped_at >= now - timedelta(days=days))
    ).all()
