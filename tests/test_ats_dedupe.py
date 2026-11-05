"""Duplicate ATS matches (#186): repeat requisitions dropped, sibling
openings grouped into one message, late siblings edit it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import ats_dedupe as d
from src import ats_pipeline as pipe
from src.scraper import ats
from src.scraper import ats_poller as poller

NOW = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)

JD = ("We are hiring a Business Analyst in {city} to own reporting for Global Solutions. "
      "You will build dashboards in SQL and Python, partner with finance, and present to leaders. "
      "Minimum 3 years of experience. Based in our {city} office.")


def _job(job_id, city, text=None, role="Business Analyst", company="Amazon"):
    return SimpleNamespace(job_id=job_id, company=company, role=role, location=f"{city}, India",
                           jd_text=(text or JD).format(city=city), job_url=f"https://x/{job_id}",
                           job_url_direct=None)


# --- telling siblings from different jobs ---------------------------------------------

def test_the_same_description_in_another_city_is_a_sibling():
    assert d.are_siblings(JD.format(city="Pune"), "Pune, India", JD.format(city="Hyderabad"), "Hyderabad, India")


@pytest.mark.parametrize("change", [
    ("Minimum 3 years", "Minimum 6 years"),       # Accenture: same template, other years
    ("SQL and Python", "SQL and Scala"),          # another must-have skill
])
def test_one_changed_requirement_makes_a_different_job(change):
    other = JD.replace(*change)
    assert not d.are_siblings(JD.format(city="Pune"), "Pune", other.format(city="Pune"), "Pune")


def test_an_empty_description_is_never_a_sibling():
    assert not d.are_siblings("", "Pune", "", "Pune")


@pytest.mark.parametrize("pid,base", [("153353", "153353"), ("153353_3", "153353"), ("R270615", "r270615"),
                                      ("", "")])
def test_base_requisition(pid, base):
    assert d.base_requisition(pid) == base


def test_cluster_keeps_order_and_leaves_different_jobs_apart():
    jobs = [_job("a1", "Pune"), _job("b1", "Pune", JD.replace("Python", "Scala")),
            _job("a2", "Hyderabad"), _job("a3", "Pune"), _job("c1", "Pune", role="Data Engineer")]
    clusters = d.cluster(jobs, key=pipe._role_key)
    assert [(c.head.job_id, [s.job_id for s in c.siblings]) for c in clusters] == \
        [("a1", ["a2", "a3"]), ("b1", []), ("c1", [])]


# --- the grouped message -----------------------------------------------------------------

def test_sibling_lines_list_other_cities_and_more_openings():
    lines = d.sibling_lines("Pune, India", [("Hyderabad, India", "https://x/h"), ("Pune, India", "https://x/p2"),
                                            ("Pune, India", "https://x/p3")])
    assert lines == ["2 more openings in Pune, India: [1](https://x/p2) [2](https://x/p3)",
                     "Also open in: [Hyderabad, India](https://x/h)"]


def test_sibling_lines_never_break_telegram_markdown():
    lines = d.sibling_lines("Pune", [("Bengaluru_[KA]", "https://x/a(b)")])
    assert lines == ["Also open in: BengaluruKA"]   # no stray _ or [, and a ")" url is not linked


def test_a_long_list_is_cut():
    many = [(f"City{i}", f"https://x/{i}") for i in range(20)]
    assert d.sibling_lines("Pune", many)[0].endswith("+8 more")


def test_with_sibling_lines_keeps_the_text_when_there_are_none():
    from src.notifications import with_sibling_lines

    assert with_sibling_lines("Match", "Pune", []) == "Match"
    assert with_sibling_lines("Match", "Pune", [("Delhi", "https://x/d")]) == \
        "Match\n\nAlso open in: [Delhi](https://x/d)"


# --- kind 1: the same requisition on another board ----------------------------------------

class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def execute(self, sql, params=None):
        self.calls.append(params)
        return _Rows(self.rows)


def _cand(ats_name, board_id, pid, title="Data Engineer", key="goldmansachs"):
    p = ats.Posting(ats_name, board_id, pid, title, "Bengaluru")
    return p, poller.Board(ats_name, board_id, name="Goldman Sachs", company_key=key)


def test_a_requisition_seen_on_another_board_is_a_repeat():
    first = _cand("goldman", "higher.gs.com", "177617")
    session = _Session([("goldmansachs", "177617", "Data Engineer", first[0].job_id)])
    again = [_cand("oracle", "hdpc.fa.us2.oraclecloud.com/CX_3002", "177617"),
             _cand("oracle", "hdpc.fa.us2.oraclecloud.com/CampusHiring", "177617")]
    out = d.repeat_requisitions(session, again, NOW)
    assert out == {p.job_id: first[0].job_id for p, _b in again}


def test_two_copies_in_one_run_keep_the_first():
    a = _cand("oracle", "eeho.fa.us2.oraclecloud.com/CX_1", "346063", key="oracle")
    b = _cand("oracle", "eeho.fa.us2.oraclecloud.com/CX_2", "346063", key="oracle")
    c = _cand("oracle", "x.fa.ocs.oraclecloud.com/CX_1", "153353_3", key="oracle")   # another req
    assert d.repeat_requisitions(_Session([]), [a, b, c], NOW) == {b[0].job_id: a[0].job_id}


def test_the_same_number_with_another_title_or_company_is_not_a_repeat():
    first = _cand("greenhouse", "acme", "5470904008")
    session = _Session([("goldmansachs", "5470904008", "Sales Manager", first[0].job_id)])
    assert d.repeat_requisitions(session, [_cand("lever", "acme", "5470904008")], NOW) == {}
    other_company = _cand("lever", "acme", "5470904008", key="acme")
    assert d.repeat_requisitions(_Session([]), [first, other_company], NOW) == {}


def test_a_board_without_a_company_key_is_left_alone():
    a = _cand("lever", "x", "1", key=None)
    b = _cand("lever", "y", "1", key=None)
    assert d.repeat_requisitions(_Session([]), [a, b], NOW) == {}


# --- late siblings ----------------------------------------------------------------------

def _parent(**kw):
    base = dict(job_id="amazon-1", company="Amazon", role="Business Analyst", location="Pune, India",
                jd_text=JD.format(city="Pune"), user_status="pending",
                telegram_message={"id": 77, "text": "Match", "markup": None})
    base.update(kw)
    return SimpleNamespace(**base)


class _LateSession:
    """not_applied SIBLING rows of the parent, after the new ones are saved."""
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        return _Rows([SimpleNamespace(job_id=j, location=loc, url=f"https://x/{j}") for j, loc in self.rows])


@pytest.fixture
def telegram(monkeypatch):
    import src.notifications as notif

    calls = {"edit": [], "note": []}
    monkeypatch.setattr(notif, "update_match_siblings",
                        lambda stored, loc, openings: calls["edit"].append((stored["id"], openings)) or True)
    monkeypatch.setattr(notif, "send_quiet_note", lambda text, reply_to=None: calls["note"].append((text, reply_to)))
    return calls


_LOG = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)


def test_a_late_sibling_in_a_new_city_edits_the_message_and_adds_a_silent_reply(telegram):
    late = _job("amazon-2", "Hyderabad")
    pipe._tell_late_siblings(_LateSession([("amazon-2", "Hyderabad, India")]), _parent(), [late], _LOG)
    assert telegram["edit"] == [(77, [("Hyderabad, India", "https://x/amazon-2")])]
    assert telegram["note"] == [("Same role, 1 more city\nAlso open in: [Hyderabad, India](https://x/amazon-2)", 77)]


def test_a_late_opening_in_the_same_city_only_edits_the_message(telegram):
    late = _job("amazon-3", "Pune")
    pipe._tell_late_siblings(_LateSession([("amazon-3", "Pune, India")]), _parent(), [late], _LOG)
    assert len(telegram["edit"]) == 1 and telegram["note"] == []


def test_a_city_already_listed_gets_no_second_note(telegram):
    late = _job("amazon-5", "Hyderabad")
    rows = [("amazon-2", "Hyderabad, India"), ("amazon-5", "Hyderabad, India")]   # amazon-2 came earlier
    pipe._tell_late_siblings(_LateSession(rows), _parent(), [late], _LOG)
    assert len(telegram["edit"]) == 1 and telegram["note"] == []


def test_a_dismissed_match_hears_nothing_more(telegram):
    pipe._tell_late_siblings(_LateSession([("amazon-2", "Delhi")]), _parent(user_status="dismissed"),
                             [_job("amazon-2", "Delhi")], _LOG)
    assert telegram == {"edit": [], "note": []}


def test_an_applied_match_still_hears_of_a_new_city(telegram):
    pipe._tell_late_siblings(_LateSession([("amazon-2", "Delhi, India")]), _parent(user_status="applied"),
                             [_job("amazon-2", "Delhi")], _LOG)
    assert len(telegram["note"]) == 1


def test_a_match_sent_before_messages_were_kept_gets_a_standalone_note(telegram):
    pipe._tell_late_siblings(_LateSession([("amazon-2", "Delhi, India")]), _parent(telegram_message=None),
                             [_job("amazon-2", "Delhi")], _LOG)
    assert telegram["edit"] == []
    (text, reply_to), = telegram["note"]
    assert reply_to is None and "*Business Analyst* at *Amazon*" in text


class _ParentSession:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        return _Rows([SimpleNamespace(_mapping=r, **r) for r in self.rows])


def test_sent_parents_finds_the_earlier_match_with_the_same_description():
    sent = dict(job_id="amazon-1", company="Amazon", role="Business Analyst", location="Pune, India",
                jd_text=JD.format(city="Pune"), job_url="u", job_url_direct=None, user_status="pending",
                telegram_message=None, built_at=NOW - timedelta(days=2))
    other = dict(sent, job_id="amazon-9", jd_text=JD.replace("Python", "Scala").format(city="Pune"))
    clusters = d.cluster([_job("amazon-2", "Hyderabad"), _job("amazon-7", "Pune", JD.replace("3 years", "8 years"))],
                         key=pipe._role_key)
    parents = pipe._sent_parents(_ParentSession([other, sent]), clusters, NOW)
    assert {k: v.job_id for k, v in parents.items()} == {"amazon-2": "amazon-1"}


def test_run_groups_before_parsing_and_saves_siblings_with_their_match():
    """Order inside run(): roles are formed before any parse, and a match's
    sibling verdicts are queued before the commit that precedes the message."""
    import inspect

    src = inspect.getsource(pipe.run)
    assert src.index("ats_dedupe.cluster(") < src.index("def _parse_ahead")
    matched = src.index("session.add(built.applied)")
    assert matched < src.index("queue.extend(_share(job, siblings_of, SIBLING))", matched) \
        < src.index("_safe_commit(session, log)", matched) < src.index("send_match_notification(", matched)
