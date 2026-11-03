"""#160 — Recruitee, Rippling, Workable, BambooHR and Eightfold boards.
Shapes trimmed from real responses (2026-11-02); the network is faked."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from src.scraper import ats
from src.scraper import ats_poller as poller


class FakeHttp:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, timeout=None, headers=None, json=None):
        self.calls.append(url)
        status, body = self.answers.pop(0)
        return SimpleNamespace(status_code=status, text=body, headers={})


def _lane(name, *answers):
    http = FakeHttp(*answers)
    return poller.Lane(name, 0.0, sleep=lambda _s: None, get=http, post=http), http


def test_recruitee_list_has_the_description_and_india():
    body = json.dumps({"offers": [{
        "id": 1395, "title": "Backend Engineer - SRE", "location": "Bangalore, Karnataka, India",
        "locations": [{"name": "Bangalore", "country_code": "IN"}], "remote": False,
        "created_at": "2026-10-23 06:24:47 UTC", "published_at": "2026-10-23 06:30:59 UTC",
        "careers_url": "https://lenskartcareers.recruitee.com/o/backend-engineer-sre",
        "careers_apply_url": "https://lenskartcareers.recruitee.com/o/backend-engineer-sre/c/new",
        "description": "<p>Run <b>Kubernetes</b>.</p>", "requirements": "<p>Python</p>",
        "employment_type_code": "fulltime_permanent"}]})
    (p,) = ats.parse_list("recruitee", "lenskartcareers", body)
    assert ats.place(p) == "india" and p.posted_at == datetime(2026, 10, 23, 6, 30, 59, tzinfo=timezone.utc)
    assert "Kubernetes" in p.description and "Python" in p.description
    assert ats.detail_url(p) is None and ats.employment_type(p.employment_type) == "fulltime"
    assert ats.list_url("recruitee", "lenskartcareers") == "https://lenskartcareers.recruitee.com/api/offers"


def test_rippling_list_then_detail():
    body = json.dumps([{"uuid": "u1", "name": " Data Engineer ", "workLocation": {"label": "Bangalore, India"},
                        "url": "https://ats.rippling.com/rippling/jobs/u1"},
                       {"uuid": "u2", "name": "AE", "workLocation": {"label": "Remote (Utah, US)"}, "url": "x"}])
    a, b = ats.parse_list("rippling", "rippling", body)
    assert a.title == "Data Engineer" and ats.place(a) == "india" and ats.place(b) == "remote_locked"
    assert ats.detail_url(a) == "https://api.rippling.com/platform/api/ats/v1/board/rippling/jobs/u1"
    ats.parse_detail(a, json.dumps({"description": {"role": "<p>Spark</p>", "company": "<p>Us</p>"},
                                    "createdOn": "2026-10-06T16:36:07.947000-07:00",
                                    "employmentType": {"id": "Salaried, full-time"}}))
    assert a.description.startswith("Spark") and a.posted_at.year == 2026
    assert ats.employment_type(a.employment_type) == "fulltime"


def test_workable_list_then_detail():
    body = json.dumps({"jobs": [{"title": "Senior DevOps Engineer", "shortcode": "AB12", "country": "India",
                                 "city": "Bengaluru", "state": "Karnataka", "telecommuting": False,
                                 "published_on": "2026-09-21", "url": "https://apply.workable.com/j/AB12",
                                 "application_url": "https://apply.workable.com/j/AB12/apply",
                                 "employment_type": "Full-time",
                                 "locations": [{"country": "India", "countryCode": "IN"}]}]})
    (p,) = ats.parse_list("workable", "youtrip", body)
    assert p.country == "IN" and ats.place(p) == "india"
    assert ats.detail_url(p) == "https://apply.workable.com/api/v2/accounts/youtrip/jobs/AB12"
    ats.parse_detail(p, json.dumps({"description": "<p>Terraform</p>", "requirements": "<p>AWS</p>",
                                    "published": "2026-09-21T00:00:00.000Z"}))
    assert "Terraform" in p.description and "AWS" in p.description


def test_bamboohr_list_detail_and_gone():
    body = json.dumps({"result": [{"id": "595", "jobOpeningName": "Solutions Architect",
                                   "location": {"city": "Bengaluru", "state": "Karnataka"},
                                   "atsLocation": {"country": None}, "isRemote": None}]})
    (p,) = ats.parse_list("bamboohr", "atlas", body)
    assert ats.place(p) == "india" and p.url == "https://atlas.bamboohr.com/careers/595"
    assert ats.detail_url(p) == "https://atlas.bamboohr.com/careers/595/detail"
    ats.parse_detail(p, json.dumps({"result": {"jobOpening": {
        "description": "<p>Design APIs</p>", "datePosted": "2026-10-28", "employmentStatusLabel": "India - Salaried",
        "atsLocation": {"country": None}}}}))
    assert p.description == "Design APIs" and p.country == "IN"
    # A company that left: redirected to bamboohr.com's home page.
    lane, _ = _lane("bamboohr", (200, "<!doctype html><html>BambooHR</html>"))
    res = poller.fetch_board(lane, poller.Board("bamboohr", "gone"))
    assert res.error == "HTTP 404"


def test_eightfold_pages_through_india():
    def page(ids, count=12):
        return json.dumps({"count": count, "positions": [
            {"id": i, "name": f"SWE {i}", "locations": ["Bangalore, India Office (BANGALORE)"],
             "t_create": 1783660979, "canonicalPositionUrl": f"https://netapp.eightfold.ai/careers/job/{i}"}
            for i in ids]})
    lane, http = _lane("eightfold", (200, page(range(10))), (200, page(range(10, 12))))
    res = poller.fetch_board(lane, poller.Board("eightfold", "netapp"))
    assert res.error is None and res.complete and len(res.postings) == 12
    assert "location=India" in http.calls[0] and "start=10" in http.calls[1]
    p = res.postings[0]
    assert ats.place(p) == "india" and p.posted_at.year == 2026
    ats.parse_detail(p, json.dumps({"job_description": "<p>Go and C++</p>",
                                    "apply_redirect_url": "https://jobs.netapp.com/job-invite/1"}))
    assert "Go and C++" in p.description and p.apply_url.endswith("/1")


def test_canaries():
    for name, answer, ok in [("recruitee", (404, "{}"), True), ("rippling", (404, "{}"), True),
                             ("workable", (404, "Not Found"), True), ("bamboohr", (200, "<html>"), True),
                             ("bamboohr", (200, '{"result": []}'), False), ("eightfold", (404, ""), True),
                             ("recruitee", (200, '{"offers": []}'), False)]:
        lane, _ = _lane(name, answer)
        assert poller.canary_ok(lane) is ok, (name, answer)


# --- Oracle HCM -------------------------------------------------------------------

ORACLE = "jpmc.fa.oraclecloud.com/CX_1001"


def _oracle_page(ids, total, facets=None):
    return json.dumps({"items": [{"TotalJobsCount": total, "locationsFacet": facets or [],
                                  "requisitionList": [
                                      {"Id": str(i), "Title": f"Software Engineer {i}", "PostedDate": "2026-11-02",
                                       "PrimaryLocation": "Mumbai, Maharashtra, India",
                                       "PrimaryLocationCountry": "IN", "WorkplaceType": "",
                                       "secondaryLocations": [], "JobSchedule": "Full time"} for i in ids]}]})


def test_oracle_board_from_url_and_urls():
    assert ats.board_from_url(
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210722431") == ("oracle", ORACLE)
    assert ats.board_from_url("https://jpmc.fa.oraclecloud.com/fscmUI/faces/x") is None
    url = ats.oracle_search_url(ORACLE, offset=25, location_id="300")
    assert url.startswith("https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?")
    assert "siteNumber=CX_1001" in url and "offset=25" in url and "locationId=300" in url


def test_oracle_india_facet_then_pages():
    facets = [{"Id": 1, "Name": "United States"}, {"Id": 300000000289360, "Name": "India"},
              {"Id": 2, "Name": "Karnataka, India"}]
    lane, http = _lane("oracle", (200, _oracle_page([], 7077, facets)), (200, _oracle_page(range(25), 30)),
                       (200, _oracle_page(range(25, 30), 30)))
    res = poller.fetch_board(lane, poller.Board("oracle", ORACLE))
    assert res.error is None and res.complete and len(res.postings) == 30
    assert "locationId=300000000289360" in http.calls[1] and "offset=25" in http.calls[2]
    p = res.postings[0]
    assert ats.place(p) == "india" and p.url.endswith("/sites/CX_1001/job/0")
    assert ats.detail_url(p).endswith('finder=ById;Id="0",siteNumber=CX_1001')
    ats.parse_detail(p, json.dumps({"items": [{"ExternalDescriptionStr": "<p>Java</p>",
                                               "ExternalResponsibilitiesStr": "<p>Build</p>",
                                               "ExternalPostedStartDate": "2026-11-02T07:58:57+00:00",
                                               "JobSchedule": "Full time", "PrimaryLocationCountry": "IN"}]}))
    assert "Java" in p.description and p.posted_at.hour == 7
    assert ats.employment_type(p.employment_type) == "fulltime"


def test_oracle_site_without_india():
    lane, http = _lane("oracle", (200, _oracle_page([], 50, [{"Id": 1, "Name": "United States"}])))
    res = poller.fetch_board(lane, poller.Board("oracle", ORACLE))
    assert res.error is None and res.complete and res.postings == [] and len(http.calls) == 1


def test_oracle_canary():
    lane, http = _lane("oracle", (-1, ""))
    assert poller.canary_ok(lane)


# --- Amazon's own site (#161) -----------------------------------------------------

def _amazon_page(ids, hits):
    return json.dumps({"hits": hits, "jobs": [
        {"id_icims": str(i), "title": f"SDE {i}", "normalized_location": "Bengaluru, Karnataka, IND",
         "country_code": "IND", "posted_date": "November  2, 2026", "job_path": f"/en/jobs/{i}/sde",
         "description": "<p>Build services</p>", "basic_qualifications": "- Java", "preferred_qualifications": "",
         "company_name": "Amazon Dev Center India", "job_schedule_type": "full-time"} for i in ids]})


def test_amazon_pages_and_parses():
    lane, http = _lane("amazon", (200, _amazon_page(range(100), 150)), (200, _amazon_page(range(100, 150), 150)))
    res = poller.fetch_board(lane, poller.Board("amazon", "amazon"))
    assert res.error is None and res.complete and len(res.postings) == 150
    assert "country=IND" in http.calls[0] and "offset=100" in http.calls[1]
    p = res.postings[0]
    assert ats.place(p) == "india" and p.posted_at == datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert "Build services" in p.description and "Java" in p.description
    assert p.url == "https://www.amazon.jobs/en/jobs/0/sde" and ats.detail_url(p) is None
    assert ats.employment_type(p.employment_type) == "fulltime"


def test_amazon_canary():
    lane, _ = _lane("amazon", (200, '{"hits": 5, "jobs": []}'))
    assert poller.canary_ok(lane)
    lane, _ = _lane("amazon", (503, ""))
    assert not poller.canary_ok(lane)


# --- Keka (#160) ------------------------------------------------------------------

KEKA_PAGE = '<script>fetch("/ats/documents/7159a120-94d0-4bec-83fe-30b283210841/careerportal/x.html")</script>'


def test_keka_org_then_jobs():
    jobs = json.dumps([{"id": "167256", "title": "Data Engineer", "description": "<div>Spark and Airflow</div>",
                        "jobLocations": [{"city": "Hyderabad", "state": "TG", "countryCode": "IN"}],
                        "publishedOn": "2026-10-21T05:58:00.08Z", "experience": "2 - 4 Years"},
                       {"id": "2", "title": "Marketer", "description": "<div>Chennai office</div>",
                        "jobLocations": [], "publishedOn": "2026-10-29T13:44:20.557Z"}])
    lane, http = _lane("keka", (200, KEKA_PAGE), (200, jobs))
    res = poller.fetch_board(lane, poller.Board("keka", "aidenai"))
    assert res.error is None and len(res.postings) == 2
    assert http.calls == ["https://aidenai.keka.com/careers/",
                          "https://aidenai.keka.com/careers/api/embedjobs/default/active/"
                          "7159a120-94d0-4bec-83fe-30b283210841"]
    a, b = res.postings
    assert ats.place(a) == "india" and "Spark" in a.description and a.posted_at.day == 21
    assert a.url == "https://aidenai.keka.com/careers/jobdetails/167256" and ats.detail_url(a) is None
    assert ats.place(b) == "unknown"   # the description decides


def test_keka_missing_tenant_is_gone_and_canary():
    lane, _ = _lane("keka", (200, "<html>Tenant not found</html>"))
    assert poller.fetch_board(lane, poller.Board("keka", "nope")).error == "HTTP 404"
    lane, _ = _lane("keka", (200, "<html>Tenant not found</html>"))
    assert poller.canary_ok(lane)
    lane, _ = _lane("keka", (200, KEKA_PAGE))
    assert not poller.canary_ok(lane)
