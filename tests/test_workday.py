"""#103 — Workday boards in the ATS poll. Responses are trimmed copies of a
real board's shape (2026-11-02); the network is faked."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.scraper import ats
from src.scraper import ats_poller as poller

BOARD = "acme/wd5/AcmeCareers"
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=timezone.utc)

FACETS = [
    {"facetParameter": "jobFamilyGroup", "values": [{"descriptor": "Engineering", "id": "fam1", "count": 9}]},
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationHierarchy1", "descriptor": "Locations", "values": [
            {"descriptor": "Germany", "id": "de1", "count": 5},
            {"descriptor": "India", "id": "in1", "count": 21},
        ]},
        {"facetParameter": "locations", "descriptor": "Sites", "values": [
            {"descriptor": "India, Bengaluru", "id": "blr", "count": 15},
            {"descriptor": "India, Pune", "id": "pnq", "count": 6},
            {"descriptor": "Germany, Berlin", "id": "ber", "count": 5},
        ]},
    ]},
]


def _job(n, loc="India, Bengaluru", posted="Posted 3 Days Ago"):
    return {"title": f"Data Engineer {n}", "externalPath": f"/job/India-Bengaluru/Data-Engineer_JR{n}",
            "locationsText": loc, "postedOn": posted, "bulletFields": [f"JR{n}"]}


class FakeHttp:
    """Queued (status, body) answers for POSTs (and GETs); records each call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, timeout=None, headers=None, json=None):
        self.calls.append((url, json))
        status, body = self.answers.pop(0)
        return SimpleNamespace(status_code=status, text=body, headers={})


def _lane(*answers):
    http = FakeHttp(*answers)
    return poller.Lane("workday", 0.0, sleep=lambda _s: None, get=http, post=http), http


def test_board_urls():
    assert ats.list_url("workday", BOARD) == "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeCareers/jobs"
    p = ats.parse_list("workday", BOARD, json.dumps({"total": 1, "jobPostings": [_job(1)]}))[0]
    assert p.url == "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/India-Bengaluru/Data-Engineer_JR1"
    assert ats.detail_url(p) == ("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/AcmeCareers"
                                 "/job/India-Bengaluru/Data-Engineer_JR1")
    assert p.job_id == "workday-acme_wd5_AcmeCareers-JR1"   # no slashes
    assert p.country == "IN" and ats.place(p) == "india"


def test_india_facet_prefers_the_country_value():
    assert ats.workday_india_facets({"facets": FACETS}) == {"locationHierarchy1": ["in1"]}


def test_india_facet_falls_back_to_indian_sites():
    sites_only = [{"facetParameter": "locations", "values": [
        {"descriptor": "Bengaluru", "id": "blr"}, {"descriptor": "Hyderabad, Telangana", "id": "hyd"},
        {"descriptor": "Indianapolis, IN", "id": "indy"}, {"descriptor": "London", "id": "lon"}]}]
    assert ats.workday_india_facets({"facets": sites_only}) == {"locations": ["blr", "hyd"]}


def test_no_india_facet_means_no_india_postings():
    assert ats.workday_india_facets({"facets": [{"facetParameter": "locations", "values": [
        {"descriptor": "Germany, Berlin", "id": "ber"}]}]}) == {}


@pytest.mark.parametrize("text,days", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 3 Days Ago", 3), ("Posted 30+ Days Ago", 31),
])
def test_posted_on(text, days):
    assert ats._workday_posted(text, NOW) == NOW - timedelta(days=days)


def test_posted_on_unknown_is_none():
    assert ats._workday_posted("Recently", NOW) is None


def test_fetch_pages_through_the_india_filter():
    first = json.dumps({"total": 30, "jobPostings": [_job(99, "Germany, Berlin")], "facets": FACETS})
    page1 = json.dumps({"total": 25, "jobPostings": [_job(i) for i in range(20)]})
    page2 = json.dumps({"total": 0, "jobPostings": [_job(i) for i in range(20, 25)]})  # later total: 0
    lane, http = _lane((200, first), (200, page1), (200, page2))
    res = poller.fetch_board(lane, poller.Board("workday", BOARD))
    assert res.error is None and res.complete
    assert [p.posting_id for p in res.postings] == [f"JR{i}" for i in range(25)]
    assert http.calls[0][1]["appliedFacets"] == {}
    assert http.calls[1][1] == {"appliedFacets": {"locationHierarchy1": ["in1"]}, "limit": 20,
                                "offset": 0, "searchText": ""}
    assert http.calls[2][1]["offset"] == 20


def test_a_board_without_india_is_an_empty_complete_list():
    lane, http = _lane((200, json.dumps({"total": 3, "jobPostings": [], "facets": []})))
    res = poller.fetch_board(lane, poller.Board("workday", BOARD))
    assert res.error is None and res.complete and res.postings == []
    assert len(http.calls) == 1


def test_a_missing_tenant_is_gone():
    lane, _ = _lane((422, '{"errorCode":"HTTP_422"}'))
    res = poller.fetch_board(lane, poller.Board("workday", BOARD))
    assert res.error == "HTTP 422" and res.error.startswith(poller.GONE)


def test_page_cap_marks_the_list_incomplete():
    first = json.dumps({"total": 100, "jobPostings": [], "facets": FACETS})
    full = json.dumps({"total": 100, "jobPostings": [_job(i) for i in range(20)]})
    lane, _ = _lane((200, first), (200, full), (200, full))
    res = poller.fetch_board(lane, poller.Board("workday", BOARD), sr_max_pages=2)
    assert not res.complete and len(res.postings) == 40


def test_detail_fills_description_date_type_and_apply_link():
    p = ats.parse_list("workday", BOARD, json.dumps({"jobPostings": [_job(1, "3 Locations")]}))[0]
    ats.parse_detail(p, json.dumps({"jobPostingInfo": {
        "jobDescription": "<p>Build <b>Spark</b> pipelines.</p>", "startDate": "2026-10-30",
        "timeType": "Full time", "location": "India, Bengaluru",
        "externalUrl": "https://acme.wd5.myworkdayjobs.com/AcmeCareers/job/x_JR1",
        "country": {"descriptor": "India", "id": "c1"}}}))
    assert "Spark" in p.description
    assert p.posted_at == datetime(2026, 10, 30, tzinfo=timezone.utc)
    assert ats.employment_type(p.employment_type) == "fulltime"
    assert p.location == "India, Bengaluru" and p.country == "IN"
    assert p.apply_url.endswith("x_JR1")


def test_canary_accepts_workdays_unknown_tenant_answer():
    lane, http = _lane((422, ""))
    assert poller.canary_ok(lane)
    assert http.calls[0][0].startswith(f"https://{poller.CANARY}.wd5.myworkdayjobs.com/")
    lane, _ = _lane((200, '{"total": 5}'))
    assert not poller.canary_ok(lane)


# --- myworkdaysite.com (Workday's second domain) --------------------------------------

def test_myworkdaysite_links_and_urls():
    url = "https://wd1.myworkdaysite.com/en-US/recruiting/wf/WellsFargoJobs/job/Bengaluru-India/X_R-1"
    assert ats.board_from_url(url) == ("workday", "wf/site.wd1/WellsFargoJobs")   # "wf" is not a locale
    assert ats.board_from_url("https://wd3.myworkdaysite.com/recruiting/avnet/External") == \
        ("workday", "avnet/site.wd3/External")
    assert ats.board_from_url("https://wd1.myworkdaysite.com/en-US/") is None
    board = "wf/site.wd1/WellsFargoJobs"
    assert ats.list_url("workday", board) == "https://wd1.myworkdaysite.com/wday/cxs/wf/WellsFargoJobs/jobs"
    p = ats.parse_list("workday", board, json.dumps({"jobPostings": [_job(7)]}))[0]
    assert p.url == "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/India-Bengaluru/Data-Engineer_JR7"
    assert ats.detail_url(p) == ("https://wd1.myworkdaysite.com/wday/cxs/wf/WellsFargoJobs"
                                 "/job/India-Bengaluru/Data-Engineer_JR7")
    assert p.job_id == "workday-wf_site.wd1_WellsFargoJobs-JR7"
