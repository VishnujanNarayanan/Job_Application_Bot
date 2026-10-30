"""#97 — the ATS run's own front stages (src/ats_pipeline.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src import ats_pipeline as pipe
from src.scraper import ats
from src.scraper import ats_poller as poller

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
RULES = {
    "record_places": ["remote_locked"],
    "job_type": "fulltime",
    "title_patterns": [r"\bintern(s|ship)?\b"],
    "level_blocklist": ["director"],
    "disallowed_regions": ["Delhi", "Gurgaon", "Gurugram", "Noida"],
    "min_pool_skills": 3,
}


def _p(title="Backend Engineer", location="Bengaluru", remote=None, etype=None, level=None, desc=None):
    return ats.Posting(ats="lever", board_id="acme", posting_id="1", title=title, location=location,
                       remote=remote, employment_type=etype, level=level, description=desc)


@pytest.mark.parametrize("title,family", [
    ("Senior Backend Engineer", "tech"),        # no early cut on "senior"
    ("Software Engineering Support Technician II", "tech"),
    ("Sales Executive", "nontech"),
    ("Clinical Pharmacist", "nontech"),
    ("Director of Engineering", "exec"),
    ("Analyst", "unclear"),
    ("Engineering Manager", "manager"),
    ("Social Media Escalations (Support) - Team Lead", "unclear"),  # not "scala"
    ("Process Trainer - TL - Lending", "nontech"),
    ("Customer Success Manager", "nontech"),
    ("Developer Support Engineer", "tech"),
    ("Scala Developer", "tech"),
    ("Node.js Engineer", "tech"),
    ("Quantitative Researcher", "tech"),
    ("Assistant Manager - Business Finance", "nontech"),
    ("Senior BizOps Analyst - Product Operations", "nontech"),
    ("Program Operations Specialist", "nontech"),
    ("Service Desk Technician", "nontech"),
    ("Finance Data Engineer", "tech"),        # a tech word wins
    ("Cloud Operations Engineer", "unclear"),  # not a business-operations title
    ("QA/QC Engineer", "nontech"),
    ("Senior Engineer-Dry Utility(Infrastructure)", "nontech"),
    ("PLC Automation Engineer-ECF2", "nontech"),
    ("Hydraulic Modeller - Water Supply", "nontech"),
    ("Software Engineer - Water Utility Platform", "tech"),  # a software word wins
    ("Principal Engineer (AI, Office of the CEO)", "tech"),
    ("Chief Executive Officer", "exec"),
    ("Senior IT Manager", "manager"),
    ("Senior Project Manager", "manager"),
    ("Engineering Manager - Backend", "tech"),
])
def test_role_family(title, family):
    assert pipe.role_family(title) == family


@pytest.mark.parametrize("posting,reason", [
    (_p(), None),
    (_p(location="Remote", remote=True), None),
    (_p(location="Hybrid"), None),                              # unknown: the description decides
    (_p(location="Berlin"), poller.DROP),
    (_p(location="Remote - US", remote=True), "place:remote_locked"),
    (_p(etype="Contract"), "type:contract"),
    (_p(etype="FullTime"), None),
    (_p(title="Data Engineering Intern"), "title"),
    (_p(level="Director"), "level"),
    (_p(location="Gurugram"), "location"),
    (_p(location="Pune / Gurugram"), None),
    (_p(title="Account Executive"), "role:nontech"),
    (_p(title="VP Engineering"), "role:exec"),
    (_p(title="Senior Project Manager"), "role:manager"),
])
def test_classify(posting, reason):
    assert pipe.classify(posting, RULES) == reason


def test_remote_locked_is_dropped_when_not_recorded():
    rules = {**RULES, "record_places": []}
    assert pipe.classify(_p(location="Remote - US", remote=True), rules) == poller.DROP


def test_an_unknown_place_needs_india_in_the_description(monkeypatch):
    assert pipe.decide(_p(location="Hybrid", desc="Join our Pune team. Python, SQL."), RULES) is None
    assert pipe.decide(_p(location="Hybrid", desc="Join our Berlin team."), RULES) == "place:unknown"


def test_an_unclear_title_needs_the_operators_skills_in_the_description(monkeypatch):
    monkeypatch.setattr("src.parser._pool_terms", lambda: ("Python", "SQL", "Airflow", "fintech"))
    assert pipe.decide(_p(title="Analyst", desc="Python, SQL and Airflow daily."), RULES) is None
    assert pipe.decide(_p(title="Analyst", desc="Python and fintech."), RULES) == "relevance:skills=2"
    assert pipe.decide(_p(title="Analyst", desc="Excel and PowerPoint."), RULES) == "relevance:skills=0"
    # A tech title is never held back by the skills check.
    assert pipe.decide(_p(title="Backend Engineer", desc="Excel."), RULES) is None


def test_company_name_falls_back_to_a_tidied_board_id():
    assert pipe.company_name(poller.Board("lever", "acme-labs"), _p()) == "Acme Labs"
    assert pipe.company_name(poller.Board("lever", "acme", name="Acme Inc"), _p()) == "Acme Inc"


# --- collect(), end to end with the network and Neon faked --------------------------------

class _Session:
    def __init__(self):
        self.added, self.got = [], {}

    def commit(self):
        pass

    def get(self, _model, key):
        return self.got.get(key)


def test_collect_runs_the_stages_in_order(monkeypatch):
    fresh = NOW - timedelta(hours=2)
    board = poller.Board("lever", "acme", polled_at=NOW - timedelta(hours=1))
    postings = [
        ats.Posting("lever", "acme", "ok", "Backend Engineer", "Pune", posted_at=fresh, description="Python"),
        ats.Posting("lever", "acme", "dup", "Data Engineer", "Pune", posted_at=fresh, description="Python"),
        ats.Posting("lever", "acme", "nodesc", "ML Engineer", "Pune", posted_at=fresh, description=None),
        ats.Posting("lever", "acme", "sales", "Sales Manager", "Pune", posted_at=fresh, description="x"),
    ]
    marked, forgotten = {}, []
    monkeypatch.setattr(poller, "due_boards", lambda *_a, **_k: [board])
    monkeypatch.setattr(pipe, "log_db_size", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(poller, "fetch_lists", lambda boards, lanes, **_k: [poller.BoardResult(board, postings)])
    class _Ledger:
        def record(self, res, classify, **_k):
            return [p for p in res.postings if classify(p) is None]

        def schedule(self, res, stats=None):
            pass

        def flush(self, session):
            return 0

    monkeypatch.setattr(poller.Ledger, "load", classmethod(lambda cls, *_a, **_k: _Ledger()))
    monkeypatch.setattr(poller, "portal_jobs", lambda *_a: [("linkedin-li-9", "Acme", "Data Engineer")])
    monkeypatch.setattr(poller, "fetch_details", lambda ps, lanes, **_k: {p.job_id: p.description is not None for p in ps})
    monkeypatch.setattr(poller, "mark_skipped", lambda s, p, reason: marked.__setitem__(p.posting_id, reason))
    monkeypatch.setattr(poller, "forget", lambda s, p: forgotten.append(p.posting_id))
    monkeypatch.setattr(poller, "prune", lambda *_a, **_k: 0)
    monkeypatch.setattr(poller, "requeue_pending", lambda *_a, **_k: 0)
    cfg = SimpleNamespace(filters=SimpleNamespace(get=lambda k, d=None: {
        "job_type": "fulltime", "title_blocklist": [], "job_level_blocklist": [], "disallowed_regions": []}.get(k, d)),
        get=lambda k, d=None: {"ats_poll": {}}.get(k, d))
    stats = poller.PollStats()

    jobs = pipe.collect(_Session(), cfg, now=NOW, stats=stats, lanes={})

    assert [j.job_id for j in jobs] == ["lever-acme-ok"]
    assert jobs[0].company == "Acme" and jobs[0].site == "ats:lever"
    assert marked == {"dup": "duplicate:linkedin-li-9"}
    assert forgotten == ["nodesc"]  # retried next poll
    assert stats.duplicates == 1 and stats.no_description == 1 and stats.new == 1


def test_ats_jobs_have_their_own_apply_threshold(monkeypatch):
    from src.config import settings
    from src.scorer.apply_decision import threshold_for

    assert threshold_for("ats:greenhouse") == 0.47
    assert threshold_for("linkedin") == float(settings.scoring.apply_threshold)
    assert threshold_for(None) == float(settings.scoring.apply_threshold)


def test_the_database_size_warns_above_the_limit():
    class _Session:
        def execute(self, _sql):
            return SimpleNamespace(one=lambda: (350_000_000, 30_000_000))

    calls = []
    log = SimpleNamespace(info=lambda e, **kw: calls.append(("info", kw)),
                          warning=lambda e, **kw: calls.append(("warning", kw)))
    assert pipe.log_db_size(_Session(), log, warn_mb=300) == 350.0
    assert calls == [("warning", {"mb": 350.0, "ats_postings_mb": 30.0, "warn_mb": 300})]
    pipe.log_db_size(_Session(), log, warn_mb=400)
    assert calls[-1][0] == "info"


def _report(monkeypatch, *, dry_run, new=3, matched=1, aborted=False, failed=0):
    """Run _report_end_of_run with Telegram replaced; return what was sent, in order."""
    import src.notifications as notif

    sent = []
    monkeypatch.setattr(notif, "send_status", lambda text: sent.append(("status", text)))
    monkeypatch.setattr(notif, "send_dry_run_summary", lambda **kw: sent.append(("summary", kw)))
    stats = SimpleNamespace(new=new, boards=5, duplicates=0, closed=0, failed=failed,
                            gone_stopped=0, atses_skipped=[])
    logged = []
    log = SimpleNamespace(info=lambda e, **kw: logged.append((e, kw)),
                          error=lambda e, **kw: logged.append((e, kw)))
    pipe._report_end_of_run(stats, new=new, backlog=0, matched=matched, skipped=new - matched,
                            aborted=aborted, outcomes={}, dry_run=dry_run, log=log)
    return sent, logged


def test_the_poll_line_and_summary_are_sent_at_the_end_in_order(monkeypatch):
    sent, _ = _report(monkeypatch, dry_run=False)
    assert [kind for kind, _ in sent] == ["status", "summary"]
    assert sent[0][1].startswith("ATS poll: 3 new jobs from 5 boards")


def test_a_dry_run_sends_nothing_to_telegram(monkeypatch):
    sent, logged = _report(monkeypatch, dry_run=True)
    assert sent == []
    assert logged[0][0] == "dry_run_telegram_suppressed"
    assert logged[0][1]["poll_line"].startswith("ATS poll:")


def test_a_quiet_run_stays_quiet(monkeypatch):
    sent, _ = _report(monkeypatch, dry_run=False, new=0, matched=0)
    assert sent == []


def test_a_failed_board_is_reported_even_without_matches(monkeypatch):
    sent, _ = _report(monkeypatch, dry_run=False, new=0, matched=0, failed=2)
    assert [kind for kind, _ in sent] == ["status"]
    assert "2 boards failed" in sent[0][1]


# --- posting age and processing order ------------------------------------------------

def test_a_posting_older_than_the_limit_is_skipped_at_classify():
    rules = {**RULES, "oldest": NOW - timedelta(days=7)}
    old = ats.Posting("lever", "acme", "o", "Backend Engineer", "Pune", posted_at=NOW - timedelta(days=9))
    new = ats.Posting("lever", "acme", "n", "Backend Engineer", "Pune", posted_at=NOW - timedelta(days=2))
    undated = ats.Posting("lever", "acme", "u", "Backend Engineer", "Pune")
    assert pipe.classify(old, rules) == "age"
    assert pipe.classify(new, rules) is None
    assert pipe.classify(undated, rules) is None, "nothing to judge an undated posting by"


def test_no_age_limit_when_unset():
    assert pipe.too_old(NOW - timedelta(days=400), None) is False


def test_rules_from_turns_the_day_limit_into_a_cutoff():
    cfg = SimpleNamespace(filters=SimpleNamespace(get=lambda k, d=None: d),
                          get=lambda k, d=None: {"ats_poll": {"max_posting_age_days": 7}}.get(k, d))
    assert pipe.rules_from(cfg, now=NOW)["oldest"] == NOW - timedelta(days=7)


def test_jobs_are_processed_newest_first_with_undated_last():
    jobs = [SimpleNamespace(job_id=n, posted_at=t) for n, t in (
        ("old", NOW - timedelta(days=5)), ("none", None), ("new", NOW - timedelta(hours=1)),
        ("mid", NOW - timedelta(days=1)))]
    assert [j.job_id for j in pipe.newest_first(jobs)] == ["new", "mid", "old", "none"]
