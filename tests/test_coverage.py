"""#163 — share of a portal run's jobs from companies the ATS poll covers."""

from __future__ import annotations

from types import SimpleNamespace

from src.notifications import run_summary_text
from src.scraper import ats_poller as poller


class _Session:
    def __init__(self, rows):
        self.rows, self.stmt = rows, None

    def execute(self, stmt):
        self.stmt = stmt
        return SimpleNamespace(all=lambda: self.rows)


def _job(company, link=None):
    return SimpleNamespace(company=company, job_url_direct=link)


def test_coverage_by_company_key_or_apply_link():
    session = _Session([("greenhouse", "acme", "acme"), ("workday", "nv/wd5/Ext", "nvidia")])
    jobs = [
        _job("Acme Technologies Pvt Ltd"),                       # key matches
        _job("Unknown Name", "https://nv.wd5.myworkdayjobs.com/Ext/job/x"),  # link names a polled board
        _job("Citi"), _job("Citi"),
        _job("Wells Fargo", "https://wf.wd1.myworkdayjobs.com/WF/job/y"),  # link known, board not polled
    ]
    c = poller.coverage(session, jobs)
    assert (c["jobs"], c["covered"], c["pct"]) == (5, 2, 40)
    assert c["uncovered"] == [("Citi", 2, None), ("Wells Fargo", 1, "workday")]
    # Empty and no-API guesses never count as coverage.
    assert "'empty'" in str(session.stmt.compile(compile_kwargs={"literal_binds": True}))


def test_coverage_of_no_jobs():
    assert poller.coverage(_Session([]), []) == {"jobs": 0, "covered": 0, "pct": 0, "uncovered": []}


def test_summary_shows_coverage_line():
    text = run_summary_text(scraped=5, skipped=3, applied=1, dry_run=False, coverage={
        "jobs": 5, "covered": 2, "pct": 40, "uncovered": [("Citi", 2, None), ("Wells Fargo", 1, "workday")]})
    assert "ATS coverage: 2/5 (40%) from companies we poll" in text
    assert "Not covered: Citi (2), Wells Fargo (1, workday)" in text


def test_summary_without_coverage_is_unchanged():
    assert "ATS coverage" not in run_summary_text(scraped=5, skipped=3, applied=1)
