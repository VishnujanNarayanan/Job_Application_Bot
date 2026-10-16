"""#97 — finding companies' ATS boards (tools/ats_discover.py, tools/ats_commoncrawl.py)."""

from __future__ import annotations

import pytest

from tools import ats_commoncrawl as cc
from tools import ats_discover as d


def test_board_ids_are_guessed_from_the_name_likeliest_first():
    tiers = d.variants("Acme Technologies Pvt Ltd")
    assert tiers[0] == ["acme", "acmetechnologies"]
    assert "acmeindia" in tiers[1]


def test_company_key_drops_legal_and_filler_words():
    assert d.key("Acme Technologies Pvt Ltd") == "acme"
    assert d.squashed("S&P Global") == "spglobal"


@pytest.mark.parametrize("company,board_name,expected", [
    ("Acme Corp", "Acme", "strong"),       # the board names the company
    ("Acme Corp", "Zeta Labs", "weak"),    # the id exists but is someone else's
    (None, "Acme", None),                  # Common Crawl ids come with no company
])
def test_a_hit_is_only_strong_when_the_board_names_the_company(company, board_name, expected):
    assert d.confirm(company, "acme", board_name, "") == expected


@pytest.mark.parametrize("url,expected", [
    ("https://jobs.ashbyhq.com/acme/123", "acme"),
    ("https://boards.greenhouse.io/embed/job_board?for=Acme", "acme"),
    ("https://jobs.lever.co/robots.txt", None),
])
def test_common_crawl_urls_give_the_board_id(url, expected):
    assert cc.board_id(url) == expected


def test_jobs_url_is_the_endpoint_that_lists_open_jobs():
    assert d.jobs_url("greenhouse", "acme") == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    assert d.jobs_url("recruitee", "acme") == "https://acme.recruitee.com/api/offers"
    assert d.jobs_url("nosuchats", "acme") is None


@pytest.mark.parametrize("loc,open_to_india", [
    ("Remote", True),
    ("Remote, APAC", True),
    ("Remote - Anywhere", True),
    ("Remote - US", False),
    ("Seoul, South Korea", False),
])
def test_remote_is_open_to_india_only_when_it_says_so_or_names_no_region(loc, open_to_india):
    assert bool(d.OPEN_REMOTE.search(loc) or d.NO_REGION.match(loc)) is open_to_india


@pytest.mark.parametrize("name,agency", [
    ("Prestige Staffing", True),
    ("GenZS Recruitment solutions", True),
    ("VITRU | International Executive Recruitment", True),
    ("ID.me University Recruiting", False),   # the employer's own campus board
    ("Placements.io", False),
])
def test_agency_boards_are_flagged(name, agency):
    assert (bool(d.AGENCY.search(name)) and not d.NOT_AGENCY.search(name)) is agency
