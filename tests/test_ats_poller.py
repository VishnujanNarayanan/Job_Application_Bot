"""#97 — the hourly ATS poll (src/scraper/ats_poller.py). Network and Neon are faked."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.scraper import ats
from src.scraper import ats_poller as poller
from src.state.models import AtsPosting

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)


class FakeGet:
    """Answers queued (status, body) pairs and records the URLs asked for."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls = []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        status, body = self.answers.pop(0)
        return SimpleNamespace(status_code=status, text=body)


def _lane(*answers, delay=0.0):
    sleeps = []
    get = FakeGet(*answers)
    return poller.Lane("lever", delay, sleep=sleeps.append, get=get), get, sleeps


# --- pacing --------------------------------------------------------------------------

def test_a_429_pauses_the_lane_then_retries():
    lane, get, sleeps = _lane((429, ""), (200, "[]"))
    assert lane.get("u") == (200, "[]")
    assert sleeps == [120]
    assert get.urls == ["u", "u"]


def test_the_third_429_stops_the_lane():
    lane, _get, sleeps = _lane((429, ""), (429, ""), (429, ""))
    with pytest.raises(poller.LaneStopped):
        lane.get("u")
    assert sleeps == [120, 600]


def test_requests_are_spaced_by_the_delay():
    lane, _get, sleeps = _lane((200, "[]"), (200, "[]"), delay=1.0)
    lane.get("a")
    lane.get("b")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0


# --- one board -----------------------------------------------------------------------

def _board(ats_name="lever", board_id="acme", polled_at=None):
    return poller.Board(ats_name, board_id, "Acme", "acme", polled_at)


def test_a_failing_board_is_reported_not_raised():
    lane, *_ = _lane((503, "busy"))
    res = poller.fetch_board(lane, _board())
    assert res.error == "HTTP 503" and res.postings == []
    lane, *_ = _lane((200, "<html>moved</html>"))
    assert "not JSON" in poller.fetch_board(lane, _board()).error


def _sr_page(ids, total):
    return json.dumps({"totalFound": total, "content": [
        {"id": i, "name": f"Job {i}", "location": {"country": "in", "remote": False, "fullLocation": "Pune, India"}}
        for i in ids]})


def test_smartrecruiters_pages_through_india_jobs():
    lane, get, _ = _lane((200, _sr_page(range(100), 150)), (200, _sr_page(range(100, 150), 150)))
    res = poller.fetch_board(lane, _board("smartrecruiters"))
    assert len(res.postings) == 150 and res.complete
    assert all("country=in" in u for u in get.urls)
    assert "offset=100" in get.urls[1]


def test_smartrecruiters_page_cap_marks_the_list_incomplete():
    lane, *_ = _lane((200, _sr_page(range(100), 500)))
    res = poller.fetch_board(lane, _board("smartrecruiters"), sr_max_pages=1)
    assert not res.complete  # so nothing is marked closed from a partial list


# --- bookkeeping ---------------------------------------------------------------------

def _posting(pid="1", title="Backend Engineer", location="Bengaluru", remote=None, posted=None, level=None):
    return ats.Posting(ats="lever", board_id="acme", posting_id=pid, title=title, location=location,
                       remote=remote, posted_at=posted, level=level, description="JD")


def test_a_portal_job_with_the_same_company_and_title_is_a_duplicate():
    portal = [("linkedin-li-1", "Acme Technologies Pvt Ltd", "Backend Engineer (Python)"),
              ("linkedin-li-2", "Other Co", "Backend Engineer")]
    assert poller.portal_duplicate(portal, "acme", "Backend Engineer") == "linkedin-li-1"
    assert poller.portal_duplicate(portal, "acme", "Frontend Engineer") is None


# --- recording -----------------------------------------------------------------------

class FakeSession:
    def __init__(self, known=(), board_row=None, recent_sent=None):
        self.known = list(known)
        self.added = []
        self.board_row = board_row
        self.recent_sent = recent_sent

    def scalar(self, _stmt):
        return self.recent_sent

    def scalars(self, _stmt):
        return list(self.known)

    def get(self, _model, _key):
        return self.board_row

    def add(self, row):
        self.added.append(row)

    def flush(self):
        pass


def _row(pid, closed=None):
    return AtsPosting(ats="lever", board_id="acme", posting_id=pid, job_id=f"lever-acme-{pid}", title="t",
                      place="india", first_seen_at=NOW - timedelta(days=3),
                      last_seen_at=NOW - timedelta(hours=1), closed_at=closed)


def _classify(p):
    where = ats.place(p)
    if where == "elsewhere":
        return poller.DROP
    return "title" if "Intern" in p.title else None


def _record(session, board, postings, complete=True):
    stats = poller.PollStats()
    res = poller.BoardResult(board, postings, complete)
    sent = poller.record(session, res, NOW, _classify, baseline_hours=24, stats=stats)
    return [p.posting_id for p in sent], stats


def test_first_poll_sends_only_the_last_day_and_records_the_rest():
    session = FakeSession()
    sent, stats = _record(session, _board(), [
        _posting("new", posted=NOW - timedelta(hours=3)),
        _posting("old", posted=NOW - timedelta(days=9)),
        _posting("undated"),
        _posting("berlin", location="Berlin"),
    ])
    assert sent == ["new"]
    assert stats.baseline == 2
    assert {r.posting_id: r.skipped for r in session.added} == {"new": None, "old": "baseline", "undated": "baseline"}


def test_later_polls_send_everything_new_and_close_what_left_the_feed():
    session = FakeSession(known=[_row("a"), _row("gone")])
    sent, stats = _record(session, _board(polled_at=NOW - timedelta(hours=1)), [
        _posting("a"), _posting("b", posted=NOW - timedelta(days=9)), _posting("i", title="Data Intern")])
    assert sent == ["b"]  # old date, but the board has been polled before: it is new to us
    assert stats.closed == 1 and stats.skipped == {"title": 1}
    a, gone = session.known
    assert a.last_seen_at == NOW and a.closed_at is None
    assert gone.closed_at == NOW


def test_a_partial_list_closes_nothing():
    session = FakeSession(known=[_row("gone")])
    _sent, stats = _record(session, _board(polled_at=NOW), [_posting("a")], complete=False)
    assert stats.closed == 0 and session.known[0].closed_at is None


def test_a_posting_back_in_the_feed_is_reopened():
    session = FakeSession(known=[_row("a", closed=NOW - timedelta(hours=2))])
    sent, _stats = _record(session, _board(polled_at=NOW), [_posting("a")])
    assert sent == [] and session.known[0].closed_at is None


# --- tiers ---------------------------------------------------------------------------

def _schedule(session, board, postings=(), error=None):
    row = SimpleNamespace(polled_at=None, poll_error=None, tier=None, next_poll_at=None)
    session.board_row = row
    poller.schedule(session, poller.BoardResult(board, list(postings), error=error), NOW)
    return row


def test_a_board_that_sent_a_job_this_week_is_hot_and_due_within_the_hour():
    row = _schedule(FakeSession(recent_sent="p1"), _board(polled_at=NOW))
    assert row.tier == "hot" and row.next_poll_at == NOW + timedelta(minutes=50)


def test_the_measurement_seeds_the_first_tier():
    board = poller.Board("lever", "acme", fits_7d=3)
    assert _schedule(FakeSession(), board).tier == "hot"


def test_a_board_posting_other_jobs_is_active_and_a_silent_one_quiet():
    week_old = [_posting(posted=NOW - timedelta(days=2))]
    assert _schedule(FakeSession(), _board(polled_at=NOW), week_old).tier == "active"
    row = _schedule(FakeSession(), _board(polled_at=NOW), [_posting(posted=NOW - timedelta(days=40))])
    assert row.tier == "quiet" and row.next_poll_at == NOW + timedelta(hours=12, minutes=-10)


def test_a_failed_board_is_retried_next_run():
    row = _schedule(FakeSession(), _board(), error="HTTP 503")
    assert row.poll_error == "HTTP 503" and row.next_poll_at == NOW + timedelta(hours=1)
