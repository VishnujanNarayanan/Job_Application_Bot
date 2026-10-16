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

    assert threshold_for("ats:greenhouse") == 0.40
    assert threshold_for("linkedin") == float(settings.scoring.apply_threshold)
    assert threshold_for(None) == float(settings.scoring.apply_threshold)
