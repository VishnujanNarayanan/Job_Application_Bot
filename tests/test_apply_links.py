"""#158 — portal jobs' apply links (Naukri gives the employer's own link;
LinkedIn's public pages no longer carry it, 2026-11-03) mapped to ATS
boards, and the new boards added for the ATS poll."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.scraper import ats
from src.scraper import ats_poller as poller


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/taketwo/jobs/8075058", ("greenhouse", "taketwo")),
    ("https://boards.greenhouse.io/Zscaler/jobs/5255850007?gh_src=x", ("greenhouse", "zscaler")),
    ("https://job-boards.eu.greenhouse.io/bradken/jobs/4943118101", ("greenhouse", "bradken")),
    ("https://boards.greenhouse.io/embed/job_app?for=acme&token=123", ("greenhouse", "acme")),
    ("https://jobs.lever.co/cred/2f1c-aa/apply", ("lever", "cred")),
    ("https://jobs.eu.lever.co/acme/123", ("lever", "acme")),
    ("https://jobs.ashbyhq.com/telus-digital/104b1b56/application", ("ashby", "telus-digital")),
    ("https://jobs.smartrecruiters.com/NielsenIQ/744000158440229", ("smartrecruiters", "nielseniq")),
    ("https://wellsfargo.wd1.myworkdayjobs.com/en-US/WellsFargoJobs/job/Bengaluru/Engineer_R-1",
     ("workday", "wellsfargo/wd1/WellsFargoJobs")),
    ("https://accenture.wd103.myworkdayjobs.com/AccentureCareers/job/Pune/X_R1",
     ("workday", "accenture/wd103/AccentureCareers")),
    ("https://acme.recruitee.com/o/backend-engineer", ("recruitee", "acme")),
    ("https://zeta.keka.com/careers/jobdetails/123", ("keka", "zeta")),
    ("https://razorpay.darwinbox.in/ms/candidate/careers/123", ("darwinbox", "razorpay")),
    ("https://apply.workable.com/acme/j/ABC123/", ("workable", "acme")),
])
def test_board_from_url(url, expected):
    assert ats.board_from_url(url) == expected


@pytest.mark.parametrize("url", [
    None, "", "not a url", "https://www.okta.com/company/careers/123",
    "https://boards.greenhouse.io/embed/job_app?token=123",   # no board named
    "https://jobs.lever.co/", "https://www.myworkdayjobs.com/x",
    "https://www.keka.com/careers",
])
def test_no_board(url):
    assert ats.board_from_url(url) is None


# --- learn_boards -------------------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, known):
        self.known, self.inserted = known, []

    def execute(self, stmt):
        if stmt.is_select:
            return _Result(self.known)
        self.inserted.append(stmt)
        return _Result([])


def _job(company, link):
    return SimpleNamespace(company=company, job_url_direct=link)


def _rows(stmt):
    # The VALUES of the multi-row insert, one dict per row, keyed by column.
    return [{(c if isinstance(c, str) else c.key): v for c, v in row.items()}
            for row in stmt._multi_values[0]]


def test_learn_boards_adds_new_boards_and_polls_supported_atses():
    jobs = [
        _job("Take-Two", "https://job-boards.greenhouse.io/taketwo/jobs/1"),
        _job("Take-Two", "https://job-boards.greenhouse.io/taketwo/jobs/2"),  # same board once
        _job("Wells Fargo", "https://wellsfargo.wd1.myworkdayjobs.com/WellsFargoJobs/job/x"),
        _job("Known Co", "https://jobs.lever.co/known/1"),
        _job("Easy Apply Co", None),
        _job("Own Site", "https://careers.example.com/job/1"),
    ]
    session = _Session(known=[("lever", "known")])
    stats = poller.learn_boards(session, jobs)
    assert stats == {"links": 5, "boards": 3, "new": 2, "new_polled": 2}
    (stmt,) = session.inserted
    rows = {(r["ats"], r["board_id"]): r for r in _rows(stmt)}
    assert set(rows) == {("greenhouse", "taketwo"), ("workday", "wellsfargo/wd1/WellsFargoJobs")}
    gh = rows[("greenhouse", "taketwo")]
    assert gh["poll"] is True and gh["source"] == poller.APPLY_LINK
    assert gh["jobs_url"] == "https://boards-api.greenhouse.io/v1/boards/taketwo/jobs"
    assert gh["company_key"] == ats.key("Take-Two")
    wd = rows[("workday", "wellsfargo/wd1/WellsFargoJobs")]
    assert wd["poll"] is True   # Workday has an adapter (#103)
    assert wd["jobs_url"] == "https://wellsfargo.wd1.myworkdayjobs.com/wday/cxs/wellsfargo/WellsFargoJobs/jobs"


def test_learn_boards_with_no_links_writes_nothing():
    session = _Session(known=[])
    assert poller.learn_boards(session, [_job("A", None)]) == {"links": 0, "boards": 0, "new": 0, "new_polled": 0}
    assert session.inserted == []
