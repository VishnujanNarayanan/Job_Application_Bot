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
    """Answers queued (status, body[, etag]) and records the URLs and
    headers asked with."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls = []
        self.headers = []

    def __call__(self, url, timeout=None, headers=None):
        self.urls.append(url)
        self.headers.append(headers)
        status, body, *etag = self.answers.pop(0)
        return SimpleNamespace(status_code=status, text=body, headers={"ETag": etag[0]} if etag else {})


def _lane(*answers, delay=0.0):
    sleeps = []
    get = FakeGet(*answers)
    return poller.Lane("lever", delay, sleep=sleeps.append, get=get), get, sleeps


# --- pacing --------------------------------------------------------------------------

def test_a_429_pauses_the_lane_then_retries():
    lane, get, sleeps = _lane((429, ""), (200, "[]"))
    assert lane.get("u") == (200, "[]")
    assert sleeps[0] == 120
    assert get.urls == ["u", "u"]


def test_a_429_slows_the_lane_to_one_request_a_second():
    lane, _get, _sleeps = _lane((429, ""), (200, "[]"), delay=0.5)
    lane.get("u")
    assert lane.delay == poller.SAFE_DELAY == 1.0


def test_the_third_429_stops_the_lane():
    lane, _get, sleeps = _lane((429, ""), (429, ""), (429, ""))
    with pytest.raises(poller.LaneStopped):
        lane.get("u")
    assert [s for s in sleeps if s >= 120] == [120, 600]


def test_requests_are_spaced_by_the_delay():
    lane, _get, sleeps = _lane((200, "[]"), (200, "[]"), delay=1.0)
    lane.get("a")
    lane.get("b")
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0


# --- one board -----------------------------------------------------------------------

def _board(ats_name="lever", board_id="acme", polled_at=None, poll_error=None, etag=None, tier=None):
    return poller.Board(ats_name, board_id, "Acme", "acme", polled_at, poll_error=poll_error, etag=etag,
                        tier=tier)


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


# --- recording (Ledger, in memory) ---------------------------------------------------------

def _known(*pids, closed=None, skipped=None, first=None, posted=None):
    return {pid: poller.Known(first or NOW - timedelta(days=3), skipped, closed, posted) for pid in pids}


def _classify(p):
    where = ats.place(p)
    if where == "elsewhere":
        return poller.DROP
    return "title" if "Intern" in p.title else None


def _record(board, postings, known=None, complete=True):
    ledger = poller.Ledger(NOW, {(board.ats, board.board_id): known} if known is not None else {})
    stats = poller.PollStats()
    sent = ledger.record(poller.BoardResult(board, postings, complete), _classify, baseline_hours=24, stats=stats)
    return ledger, [p.posting_id for p in sent], stats


def test_first_poll_sends_only_the_last_day_and_records_the_rest():
    ledger, sent, stats = _record(_board(), [
        _posting("new", posted=NOW - timedelta(hours=3)),
        _posting("old", posted=NOW - timedelta(days=9)),
        _posting("undated"),
        _posting("berlin", location="Berlin"),
    ])
    assert sent == ["new"]
    assert stats.baseline == 2
    assert {r["posting_id"]: r["skipped"] for r in ledger.inserts} == {"new": None, "old": "baseline", "undated": "baseline"}


def test_later_polls_send_everything_new_and_close_what_left_the_feed():
    ledger, sent, stats = _record(_board(polled_at=NOW - timedelta(hours=1)), [
        _posting("a"), _posting("b", posted=NOW - timedelta(days=9)), _posting("i", title="Data Intern")],
        known=_known("a", "gone"))
    assert sent == ["b"]  # old date, but the board has been polled before: it is new to us
    assert stats.closed == 1 and stats.skipped == {"title": 1}
    assert ledger.seen == [("lever", "acme", "a")]
    assert ledger.closed == [("lever", "acme", "gone")]


def test_a_partial_list_closes_nothing():
    ledger, _sent, stats = _record(_board(polled_at=NOW), [_posting("a")], known=_known("gone"), complete=False)
    assert stats.closed == 0 and ledger.closed == []


def test_a_posting_back_in_the_feed_is_reopened():
    ledger, sent, _stats = _record(_board(polled_at=NOW), [_posting("a")],
                                   known=_known("a", closed=NOW - timedelta(hours=2)))
    assert sent == [] and ledger.seen == [("lever", "acme", "a")]  # the seen update clears closed_at


# --- tiers ---------------------------------------------------------------------------

def _tier(board, postings=(), known=None, error=None):
    ledger = poller.Ledger(NOW, {(board.ats, board.board_id): known or {}})
    ledger.schedule(poller.BoardResult(board, list(postings), error=error))
    return ledger.boards[-1]


def test_a_board_that_sent_a_job_this_week_is_hot_and_due_within_the_hour():
    row = _tier(_board(polled_at=NOW), known=_known("p1", first=NOW - timedelta(days=2)))
    assert row["tier"] == "hot" and row["due"] == NOW + timedelta(minutes=50)


def test_a_board_whose_only_recent_postings_were_skipped_is_not_hot():
    row = _tier(_board(polled_at=NOW), known=_known("p1", skipped="role:nontech", first=NOW - timedelta(days=1)))
    assert row["tier"] == "quiet"


def test_the_measurement_seeds_the_first_tier():
    assert _tier(poller.Board("lever", "acme", fits_7d=3))["tier"] == "hot"


def test_a_board_posting_other_jobs_is_active_and_a_silent_one_quiet():
    assert _tier(_board(polled_at=NOW), [_posting(posted=NOW - timedelta(days=2))])["tier"] == "active"
    row = _tier(_board(polled_at=NOW), [_posting(posted=NOW - timedelta(days=40))])
    assert row["tier"] == "quiet" and row["due"] == NOW + timedelta(hours=12, minutes=-10)


def test_a_failed_board_keeps_its_tier_and_is_retried_next_run():
    row = _tier(_board(), error="HTTP 503")
    assert row["error"] == "HTTP 503" and row["tier"] is None  # COALESCE keeps the stored tier
    assert row["due"] == NOW + timedelta(hours=1)
    assert row["stop"] is False


# --- what a response may change (comment on #97) -------------------------------------------

def test_a_gone_board_is_retried_after_a_day_and_stopped_after_three():
    row = _tier(_board(), error="HTTP 404")
    assert row["error"] == "HTTP 404 (gone x1)" and row["due"] == NOW + timedelta(hours=24)
    assert row["stop"] is False
    row = _tier(_board(poll_error="HTTP 404 (gone x2)"), error="HTTP 404")
    assert row["error"] == "HTTP 404 (gone x3, polling stopped)" and row["stop"] is True


def test_a_gone_board_closes_nothing():
    # A failed fetch never reaches record(): only the schedule is written.
    ledger = poller.Ledger(NOW, {("lever", "acme"): _known("a", "b")})
    stats = poller.PollStats()
    ledger.schedule(poller.BoardResult(_board(poll_error="HTTP 404 (gone x2)"), error="HTTP 404"), stats)
    assert ledger.closed == [] and stats.gone_stopped == 1


def test_a_board_that_answers_again_starts_counting_afresh():
    assert _board(poll_error="HTTP 503").gone_count == 0
    assert _tier(_board(poll_error="HTTP 404 (gone x2)"))["error"] is None


def test_a_sudden_empty_list_is_held_open_once():
    board = _board(polled_at=NOW - timedelta(hours=1))
    ledger, sent, stats = _record(board, [], known=_known("a", "b"))
    assert sent == [] and ledger.closed == [] and stats.closed == 0 and stats.held_empty == 1
    res = poller.BoardResult(board, [])
    ledger.record(res, _classify, baseline_hours=24, stats=stats)
    ledger.schedule(res)
    assert ledger.boards[-1]["error"] == poller.EMPTY_ONCE
    assert ledger.boards[-1]["due"] == NOW + timedelta(hours=1)


def test_a_second_empty_list_closes_the_postings():
    board = _board(polled_at=NOW - timedelta(hours=1), poll_error=poller.EMPTY_ONCE)
    ledger, _sent, stats = _record(board, [], known=_known("a", "b"))
    assert stats.closed == 2 and stats.held_empty == 0


def test_an_empty_board_with_nothing_open_is_not_held():
    ledger, _sent, stats = _record(_board(polled_at=NOW), [], known=_known("a", closed=NOW - timedelta(days=1)))
    assert stats.held_empty == 0


@pytest.mark.parametrize("name,answer,ok", [
    ("lever", (404, '{"ok":false}'), True),
    ("greenhouse", (404, "{}"), True),
    ("ashby", (404, "Not Found"), True),
    ("lever", (429, ""), False),
    ("lever", (403, ""), False),
    ("lever", (200, "[]"), False),
    ("smartrecruiters", (200, '{"offset":0,"limit":100,"totalFound":0,"content":[]}'), True),
    ("smartrecruiters", (200, '{"totalFound":3,"content":[{}]}'), False),
    ("smartrecruiters", (429, ""), False),
])
def test_the_canary_expects_the_usual_no_such_board_answer(name, answer, ok):
    sleeps = []
    lane = poller.Lane(name, 0.0, sleep=sleeps.append, get=FakeGet(answer))
    assert poller.canary_ok(lane) is ok
    assert 120 not in sleeps  # a 429 at the canary is not waited out


def test_an_ats_whose_canary_fails_is_skipped_and_its_boards_stay_due():
    lanes = {"lever": poller.Lane("lever", 0.0, sleep=lambda _s: None, get=FakeGet((429, "")))}
    stats = poller.PollStats()
    assert poller.fetch_lists([_board()], lanes, budget_s=60, stats=stats) == []
    assert stats.atses_skipped == ["lever"]


def test_a_healthy_canary_lets_the_boards_be_polled():
    lanes = {"lever": poller.Lane("lever", 0.0, sleep=lambda _s: None, get=FakeGet((404, ""), (200, "[]")))}
    results = poller.fetch_lists([_board()], lanes, budget_s=60)
    assert len(results) == 1 and results[0].error is None


# --- writing -------------------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.calls = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params))


def test_a_whole_poll_is_written_in_a_few_bulk_statements():
    boards = [poller.Board("lever", f"b{i}", polled_at=NOW - timedelta(hours=1)) for i in range(300)]
    ledger = poller.Ledger(NOW, {(b.ats, b.board_id): _known("old", "gone") for b in boards})
    stats = poller.PollStats()
    for b in boards:
        res = poller.BoardResult(b, [ats.Posting("lever", b.board_id, "old", "Backend Engineer", "Pune"),
                                     ats.Posting("lever", b.board_id, "new", "Backend Engineer", "Pune")])
        ledger.record(res, _classify, baseline_hours=24, stats=stats)
        ledger.schedule(res)
    session = _Recorder()
    statements = ledger.flush(session)
    # 300 inserts, 300 seen, 300 closed, 300 boards: one statement each kind.
    assert statements == 4 == len(session.calls)
    assert len(session.calls[1][1]["a"]) == 300  # the seen update carries every key
    assert "unnest" in session.calls[3][0] and "COALESCE(u.tier" in session.calls[3][0]
    assert "poll = x.poll AND NOT u.stop" in session.calls[3][0]


# --- order within a run (#110) ----------------------------------------------------------

def _row(board_id, tier=None, fits=None, due=None):
    return SimpleNamespace(ats="lever", board_id=board_id, board_name=None, company=None, company_key=None,
                           polled_at=None if tier is None else NOW, fits_7d=fits, target_posted_7d=None,
                           tier=tier, next_poll_at=due, poll_error=None, etag=None)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self, _stmt):
        return self.rows


def test_hot_boards_are_polled_first_then_new_then_active_then_quiet():
    rows = [_row("quiet1", "quiet"), _row("active1", "active"), _row("new1"), _row("hot1", "hot"),
            _row("seeded", fits=2), _row("quiet2", "quiet"), _row("hot2", "hot")]
    order = [b.board_id for b in poller.due_boards(_Rows(rows), NOW, ["lever"])]
    assert order == ["hot1", "seeded", "hot2", "new1", "active1", "quiet1", "quiet2"]


# --- the JobSpy side of the dedupe --------------------------------------------------------

def test_a_linkedin_listing_of_a_job_the_ats_found_is_a_duplicate():
    index = {"acme": [("greenhouse-acme-1", "Backend Engineer (Python)"), ("greenhouse-acme-2", "Data Analyst")]}
    assert poller.ats_copy(index, "Acme Technologies Pvt Ltd", "Backend Engineer") == "greenhouse-acme-1"
    assert poller.ats_copy(index, "Acme", "Frontend Engineer") is None
    assert poller.ats_copy(index, "Other Co", "Backend Engineer") is None
    assert poller.ats_copy({}, "Acme", "Backend Engineer") is None


# --- conditional polls (#112) ----------------------------------------------------------

def test_a_board_with_an_etag_is_asked_if_none_match_and_a_304_is_unchanged():
    lane, get, _ = _lane((304, ""))
    res = poller.fetch_board(lane, _board(etag='W/"v1"'))
    assert get.headers == [{"If-None-Match": 'W/"v1"'}]
    assert res.not_modified and res.error is None and res.postings == []


def test_a_200_keeps_the_new_etag_and_a_first_poll_sends_none():
    lane, get, _ = _lane((200, "[]", 'W/"v2"'))
    res = poller.fetch_board(lane, _board())
    assert get.headers == [None] and res.etag == 'W/"v2"' and not res.not_modified


def test_only_a_one_page_smartrecruiters_board_keeps_an_etag():
    lane, *_ = _lane((200, _sr_page(range(10), 10), 'W/"one"'))
    assert poller.fetch_board(lane, _board("smartrecruiters")).etag == 'W/"one"'
    lane, get, _ = _lane((200, _sr_page(range(100), 150), 'W/"p1"'), (200, _sr_page(range(100, 150), 150), 'W/"p2"'))
    assert poller.fetch_board(lane, _board("smartrecruiters")).etag is None


def test_a_304_records_and_closes_nothing_and_keeps_the_etag():
    board = _board(polled_at=NOW - timedelta(hours=1), etag='W/"v1"')
    ledger = poller.Ledger(NOW, {("lever", "acme"): _known("a", "b")})
    stats = poller.PollStats()
    res = poller.BoardResult(board, [], not_modified=True)
    assert ledger.record(res, _classify, baseline_hours=24, stats=stats) == []
    ledger.schedule(res)
    assert ledger.closed == [] and ledger.inserts == [] and ledger.seen == []
    assert stats.not_modified == 1 and stats.held_empty == 0
    assert ledger.boards[-1]["etag"] == 'W/"v1"' and ledger.boards[-1]["error"] is None


def test_a_304_board_is_tiered_from_its_open_postings():
    board = _board(polled_at=NOW - timedelta(hours=3), etag='W/"v1"', tier="active")
    ledger = poller.Ledger(NOW, {("lever", "acme"): _known("a", skipped="baseline", posted=NOW - timedelta(days=2))})
    ledger.schedule(poller.BoardResult(board, [], not_modified=True))
    assert ledger.boards[-1]["tier"] == "active"
    ledger = poller.Ledger(NOW, {("lever", "acme"): _known("a", skipped="baseline", posted=NOW - timedelta(days=40))})
    ledger.schedule(poller.BoardResult(board, [], not_modified=True))
    assert ledger.boards[-1]["tier"] == "quiet"


@pytest.mark.parametrize("res", [
    poller.BoardResult(_board(etag='W/"old"'), error="HTTP 503", etag=None),
    poller.BoardResult(_board(etag='W/"old"'), held_empty=True, etag='W/"empty"'),
])
def test_a_failed_or_held_empty_poll_keeps_the_old_etag(res):
    ledger = poller.Ledger(NOW, {})
    ledger.schedule(res)
    assert ledger.boards[-1]["etag"] == 'W/"old"'


def test_a_full_read_stores_the_new_etag():
    ledger = poller.Ledger(NOW, {})
    ledger.schedule(poller.BoardResult(_board(etag='W/"old"'), [], etag='W/"new"'))
    assert ledger.boards[-1]["etag"] == 'W/"new"'


def test_the_lane_counts_the_characters_it_reads():
    lane, *_ = _lane((200, "[]"), (304, ""))
    lane.get("a")
    lane.fetch("b", etag="x")
    assert lane.bytes == 2 and lane.requests == 2
