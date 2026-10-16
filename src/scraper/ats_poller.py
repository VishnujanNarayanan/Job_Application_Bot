"""Layer 2 — fetching ATS job boards on a schedule (#97).

Transport and bookkeeping only; what to keep is decided by the caller
(``src.ats_pipeline``):

- :func:`due_boards`: boards marked ``poll`` whose ``next_poll_at`` has come,
  oldest first.
- :func:`fetch_lists` / :func:`fetch_details`: one paced lane per ATS.
- :class:`Ledger`: ``ats_postings`` bookkeeping (seen, closed, first-poll
  baseline) and each board's tier and next poll time, in memory, written in
  bulk.

Pacing (hard rule #4): one lane per ATS, one request at a time, ``delay``
seconds apart (0.5 s, #111). A 429 slows that lane to one request a second
for the rest of the run and pauses it for 2 minutes, a second one for 10, and
a third stops it for the run. Lanes run in parallel, so no ATS sees more than
two requests a second. A lane stops starting boards once its time budget is
spent; the boards not reached stay due and go first next run.

What a response may change: only a 200 with the expected JSON marks postings
closed. 429 / 403 / 5xx / anything else changes nothing and the board is
retried in an hour. A 404 / 410 means the board is gone: retried after a day,
and after three in a row it stops being polled (``poll_error`` says why, for
a human to check); its postings are not closed. A board that suddenly lists
nothing is held open once and closed only if it is still empty next poll.
Each run starts with a canary per ATS (a board that cannot exist): anything
but the usual "no such board" answer means the ATS is throttling us, and it
is skipped for the run.

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
SAFE_DELAY = 1.0  # a lane's delay after any 429
TIMEOUT = 40
TIER_HOURS = {"hot": 1.0, "active": 3.0, "quiet": 12.0}
# A board whose fetch failed is retried after this long.
RETRY_HOURS = 1.0
# 404 / 410: the board is gone. Retried after GONE_HOURS; polling stops after
# GONE_LIMIT in a row.
GONE = ("HTTP 404", "HTTP 410")
GONE_HOURS = 24.0
GONE_LIMIT = 3
# poll_error of a board held open after it suddenly listed nothing.
EMPTY_ONCE = "empty list (held open once)"
# A board id no ATS can have, asked for at the start of each run.
CANARY = "zzqx-no-such-board-7f3k"


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

    def get(self, url: str, *, patient: bool = True) -> tuple[int, str]:
        """(status, body); status -1 on a network error. ``patient=False``
        returns a 429 instead of pausing and retrying."""
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
            self.delay = max(self.delay, SAFE_DELAY)
            if not patient:
                return status, body
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
    tier: str | None = None
    # The last poll's error: counts 404s in a row, and marks a board held
    # open after an empty list.
    poll_error: str | None = None

    @classmethod
    def of(cls, row) -> "Board":
        return cls(row.ats, row.board_id, row.board_name or row.company, row.company_key,
                   row.polled_at, row.fits_7d, row.target_posted_7d, row.tier, row.poll_error)

    @property
    def gone_count(self) -> int:
        """404 / 410s in a row so far, read from ``poll_error``."""
        e = self.poll_error or ""
        if not e.startswith(GONE):
            return 0
        match = re.search(r"gone x(\d+)", e)
        return int(match.group(1)) if match else 1

    @property
    def priority(self) -> str:
        """The tier it is polled in: a board never polled is hot when the
        measurement saw fitting jobs on it, else "new"."""
        if self.tier:
            return self.tier
        return "hot" if (self.fits_7d or 0) > 0 or (self.target_posted_7d or 0) > 0 else "new"


@dataclass
class BoardResult:
    board: Board
    postings: list = field(default_factory=list)
    # False when the list was cut short (SmartRecruiters page cap), so
    # absence from it does not mean closed.
    complete: bool = True
    error: str | None = None
    # Listed nothing although postings were open: held open this once.
    held_empty: bool = False


# --- Fetching ------------------------------------------------------------------------

#: Polling order within a run (#110): hot boards always go first, so a run
#: that hits its time budget still reaches them; then boards never polled,
#: then active, then quiet. Within a tier, the longest overdue first.
PRIORITY = {"hot": 0, "new": 1, "active": 2, "quiet": 3}


def due_boards(session, now: datetime, atses) -> list[Board]:
    """Boards to poll now, hot first, then by how long they have been due."""
    from sqlalchemy import or_, select

    from src.state.models import AtsBoard

    rows = session.scalars(
        select(AtsBoard)
        .where(AtsBoard.poll.is_(True), AtsBoard.ats.in_(list(atses)))
        .where(or_(AtsBoard.next_poll_at.is_(None), AtsBoard.next_poll_at <= now))
        .order_by(AtsBoard.next_poll_at.asc().nullsfirst(), AtsBoard.polled_at.asc().nullsfirst())
    )
    boards = [Board.of(r) for r in rows]
    # Stable sort: the overdue order from the query is kept within a tier.
    return sorted(boards, key=lambda b: PRIORITY[b.priority])


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


def canary_ok(lane: Lane) -> bool:
    """Ask for a board that cannot exist. Greenhouse, Lever and Ashby answer
    404; SmartRecruiters answers 200 with nothing found (checked 2026-10-17).
    Anything else (429, 403, a 200 with jobs, a network error) means the ATS
    is not answering normally."""
    status, body = lane.get(ats.list_url(lane.name, CANARY), patient=False)
    if lane.name == "smartrecruiters":
        return status == 200 and ats.sr_total(body) == 0 and '"content":[]' in body.replace(" ", "")
    return status == 404


def fetch_lists(boards: list[Board], lanes: dict[str, Lane], *, budget_s: float,
                sr_max_pages: int = 30, delay: float = 1.0, stats=None) -> list[BoardResult]:
    """Each ATS's boards; an ATS whose canary fails is skipped for the run
    (its boards stay due)."""
    by_ats: dict[str, list] = {}
    for b in boards:
        by_ats.setdefault(b.ats, []).append(b)
    for name in list(by_ats):
        lanes.setdefault(name, Lane(name, delay))
        if not canary_ok(lanes[name]):
            log.error("ats_canary_failed", ats=name, boards_left_due=len(by_ats[name]))
            if stats is not None:
                stats.atses_skipped.append(name)
            del by_ats[name]
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
    gone_stopped: int = 0
    held_empty: int = 0
    atses_skipped: list = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


@dataclass
class Known:
    """One ``ats_postings`` row, as much of it as the bookkeeping needs."""
    first_seen_at: datetime
    skipped: str | None
    closed_at: datetime | None


class Ledger:
    """Bookkeeping for one poll, in memory, written in a few bulk statements.

    It used to be several Neon round trips per board (load its postings,
    insert, flush, load the board row, a tier query): the first full sweep
    (2,695 boards, 2026-10-16) spent 21 minutes writing after a 20-minute list
    phase (#109). Now the known postings of every fetched board are loaded in
    one query (:meth:`load`), :meth:`record` and :meth:`schedule` only work
    in memory, and :meth:`flush` writes inserts, seen/closed updates and the
    boards' schedules in bulk.
    """

    def __init__(self, now: datetime, known: dict | None = None, tier_hours: dict | None = None):
        self.now = now
        self.known: dict[tuple[str, str], dict[str, Known]] = known or {}
        self.hours = {**TIER_HOURS, **(tier_hours or {})}
        self.inserts: list[dict] = []
        self.seen: list[tuple[str, str, str]] = []
        self.closed: list[tuple[str, str, str]] = []
        self.boards: list[dict] = []
        self._sent: set[tuple[str, str]] = set()

    @classmethod
    def load(cls, session, boards: list[Board], now: datetime, tier_hours: dict | None = None) -> "Ledger":
        from sqlalchemy import select, tuple_

        from src.state.models import AtsPosting

        known: dict[tuple[str, str], dict[str, Known]] = {(b.ats, b.board_id): {} for b in boards}
        keys = list(known)
        for i in range(0, len(keys), 1000):
            rows = session.execute(
                select(AtsPosting.ats, AtsPosting.board_id, AtsPosting.posting_id, AtsPosting.first_seen_at,
                       AtsPosting.skipped, AtsPosting.closed_at)
                .where(tuple_(AtsPosting.ats, AtsPosting.board_id).in_(keys[i:i + 1000])))
            for a, b, pid, first, skipped, closed in rows:
                known[(a, b)][pid] = Known(first, skipped, closed)
        return cls(now, known, tier_hours)

    def record(self, res: BoardResult, classify: Callable, *, baseline_hours: float,
               stats: PollStats) -> list[ats.Posting]:
        """New postings to send on (not skipped, not a first poll's
        baseline); queues what to write."""
        board, now = res.board, self.now
        known = self.known.setdefault((board.ats, board.board_id), {})
        if (not res.postings and board.poll_error != EMPTY_ONCE
                and any(k.closed_at is None for k in known.values())):
            # Could be a glitch or quiet throttling: close nothing until a
            # second poll also lists nothing.
            res.held_empty = True
            stats.held_empty += 1
            log.warning("ats_board_empty_held", ats=board.ats, board=board.board_id,
                        open=sum(k.closed_at is None for k in known.values()))
            return []
        first_poll = board.polled_at is None or not known
        send, kept = [], set()
        for p in res.postings:
            reason = classify(p)
            if reason == DROP:
                continue
            kept.add(p.posting_id)
            if p.posting_id in known:
                self.seen.append((p.ats, p.board_id, p.posting_id))
                continue
            if reason:
                stats.skip(reason)
            elif first_poll and not (p.posted_at and now - p.posted_at <= timedelta(hours=baseline_hours)):
                reason = "baseline"
                stats.baseline += 1
            self.inserts.append(dict(
                ats=p.ats, board_id=p.board_id, posting_id=p.posting_id, job_id=p.job_id,
                title=p.title[:500], location=(p.location or "")[:500], place=ats.place(p),
                posted_at=p.posted_at, first_seen_at=now, last_seen_at=now, skipped=reason))
            known[p.posting_id] = Known(now, reason, None)
            if reason is None:
                send.append(p)
                self._sent.add((board.ats, board.board_id))
        if res.complete:
            for pid, row in known.items():
                if pid not in kept and row.closed_at is None:
                    self.closed.append((board.ats, board.board_id, pid))
                    row.closed_at = now
                    stats.closed += 1
        return send

    def tier(self, res: BoardResult) -> str:
        """hot / active / quiet, from what this board has shown."""
        board, week = res.board, self.now - timedelta(days=7)
        known = self.known.get((board.ats, board.board_id), {})
        sent = (board.ats, board.board_id) in self._sent or any(
            k.skipped is None and k.first_seen_at >= week for k in known.values())
        if sent or (board.polled_at is None and ((board.fits_7d or 0) > 0 or (board.target_posted_7d or 0) > 0)):
            return "hot"
        if any(p.posted_at and p.posted_at >= week for p in res.postings):
            return "active"
        return "quiet"

    def schedule(self, res: BoardResult, stats: PollStats | None = None) -> None:
        """Queue the board's poll stamp and next due time. A failed board
        keeps its tier and is retried after RETRY_HOURS; a gone one (404 /
        410) after GONE_HOURS, and stops being polled after GONE_LIMIT."""
        error, stop = res.error, False
        if res.error and res.error.startswith(GONE):
            n = res.board.gone_count + 1
            stop = n >= GONE_LIMIT
            error = f"{res.error} (gone x{n}{', polling stopped' if stop else ''})"
            tier, due = None, self.now + timedelta(hours=GONE_HOURS)
            if stop:
                log.warning("ats_board_gone", ats=res.board.ats, board=res.board.board_id, error=res.error)
                if stats is not None:
                    stats.gone_stopped += 1
        elif res.error:
            tier, due = None, self.now + timedelta(hours=RETRY_HOURS)
        elif res.held_empty:
            error, tier, due = EMPTY_ONCE, None, self.now + timedelta(hours=RETRY_HOURS)
        else:
            tier = self.tier(res)
            # A few minutes early, so a board due hourly is not pushed past
            # the next run by the minutes this one took.
            due = self.now + timedelta(hours=float(self.hours[tier])) - timedelta(minutes=10)
        self.boards.append(dict(ats=res.board.ats, board_id=res.board.board_id, error=error,
                                tier=tier, due=due, stop=stop))

    def flush(self, session) -> int:
        """Write everything queued; returns the number of statements sent."""
        from sqlalchemy import insert, text

        from src.state.models import AtsPosting

        statements = 0
        for i in range(0, len(self.inserts), 1000):
            session.execute(insert(AtsPosting), self.inserts[i:i + 1000])
            statements += 1
        for keys, sql in ((self.seen, _SEEN_SQL), (self.closed, _CLOSED_SQL)):
            for i in range(0, len(keys), 5000):
                chunk = keys[i:i + 5000]
                session.execute(text(sql), {"now": self.now, "a": [k[0] for k in chunk],
                                            "b": [k[1] for k in chunk], "c": [k[2] for k in chunk]})
                statements += 1
        for i in range(0, len(self.boards), 5000):
            chunk = self.boards[i:i + 5000]
            session.execute(text(_BOARDS_SQL), {
                "now": self.now, "a": [r["ats"] for r in chunk], "b": [r["board_id"] for r in chunk],
                "err": [r["error"] for r in chunk], "tier": [r["tier"] for r in chunk],
                "due": [r["due"] for r in chunk], "stop": [r["stop"] for r in chunk]})
            statements += 1
        self.inserts, self.seen, self.closed, self.boards = [], [], [], []
        return statements


_KEYS = ("FROM unnest(CAST(:a AS text[]), CAST(:b AS text[]), CAST(:c AS text[])) AS u(ats, board_id, posting_id) "
         "WHERE p.ats = u.ats AND p.board_id = u.board_id AND p.posting_id = u.posting_id")
_SEEN_SQL = f"UPDATE ats_postings AS p SET last_seen_at = :now, closed_at = NULL {_KEYS}"
_CLOSED_SQL = f"UPDATE ats_postings AS p SET closed_at = :now {_KEYS}"
_BOARDS_SQL = (
    "UPDATE ats_boards AS x SET polled_at = :now, poll_error = u.err, "
    "tier = COALESCE(u.tier, x.tier), next_poll_at = u.due, poll = x.poll AND NOT u.stop "
    "FROM unnest(CAST(:a AS text[]), CAST(:b AS text[]), CAST(:err AS text[]), CAST(:tier AS text[]), "
    "CAST(:due AS timestamptz[]), CAST(:stop AS boolean[])) AS u(ats, board_id, err, tier, due, stop) "
    "WHERE x.ats = u.ats AND x.board_id = u.board_id")


def mark_skipped(session, p: ats.Posting, reason: str) -> None:
    """Record a reason found after the list stage (description, duplicate)."""
    from src.state.models import AtsPosting

    row = session.get(AtsPosting, (p.ats, p.board_id, p.posting_id))
    if row is not None:
        row.skipped = reason


def forget(session, p: ats.Posting) -> None:
    """Drop a posting's row so the next poll treats it as new (its
    description could not be fetched)."""
    from sqlalchemy import delete

    from src.state.models import AtsPosting

    session.execute(delete(AtsPosting).where(AtsPosting.ats == p.ats, AtsPosting.board_id == p.board_id,
                                             AtsPosting.posting_id == p.posting_id))


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


def ats_index(session, now: datetime, days: int) -> dict[str, list[tuple[str, str]]]:
    """ATS jobs from the last ``days`` by company key: {key: [(job_id, title)]}.
    For the JobSpy run, to spot a LinkedIn/Naukri copy of a job the ATS poll
    already found."""
    from sqlalchemy import select

    from src.state.models import AllJobs

    index: dict[str, list[tuple[str, str]]] = {}
    rows = session.execute(
        select(AllJobs.job_id, AllJobs.company, AllJobs.role)
        .where(AllJobs.site.like("ats:%"), AllJobs.scraped_at >= now - timedelta(days=days)))
    for job_id, company, role in rows:
        index.setdefault(ats.key(company or ""), []).append((job_id, role))
    return index


def ats_copy(index: dict, company: str | None, title: str | None) -> str | None:
    """The job_id of the ATS job this portal listing duplicates, if any."""
    want = _title_key(title or "")
    for job_id, role in index.get(ats.key(company or ""), []):
        if difflib.SequenceMatcher(None, _title_key(role), want).ratio() >= 0.9:
            return job_id
    return None
