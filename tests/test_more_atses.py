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
        self.calls.append((url, json) if json is not None else url)
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


def test_oracle_canary_asks_a_real_site():
    lane, http = _lane("oracle", (200, json.dumps({"items": [{"TotalJobsCount": 5}]})))
    assert poller.canary_ok(lane)
    assert "eeho.fa.us2.oraclecloud.com" in http.calls[0] and "siteNumber=CX_45001" in http.calls[0]
    for bad in ((-1, ""), (503, ""), (200, "<html>maintenance</html>")):
        lane, _ = _lane("oracle", bad)
        assert not poller.canary_ok(lane)


def test_recruitee_keeps_one_lane():
    lanes = {}
    work = poller.split_by_host({"recruitee": [poller.Board("recruitee", f"t{i}") for i in range(20)]},
                                lanes, parallel=8, delay=0.5)
    assert list(work) == ["recruitee"]   # rate-limited across tenants (2026-11-03)


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


# --- SuccessFactors career sites (#160) --------------------------------------------

def _sf_page(rows, total):
    return json.dumps({"totalJobs": total, "jobSearchResult": [{"response": r} for r in rows]})


def test_successfactors_reads_newest_india_postings_and_stops_at_a_week():
    from datetime import timedelta
    today = datetime.now(timezone.utc).strftime("%-m/%-d/%y")
    old = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%-m/%-d/%y")
    page0 = _sf_page([
        {"id": "216316", "urlTitle": "Process-Associate", "unifiedStandardTitle": "Data Engineer",
         "unifiedStandardStart": today, "jobLocationShort": ["Kolkata, IND-19, IND, 700091<br/>"],
         "jobLocationCountry": ["India"]},
        {"id": "9", "urlTitle": "X", "unifiedStandardTitle": "Banker", "unifiedStandardStart": today,
         "jobLocationShort": ["Kwun Tong, HKG"]},
        {"id": "179408", "urlTitle": "Senior-Technical-Architect", "unifiedStandardTitle": "Senior Technical Architect",
         "unifiedStandardStart": today, "custCountryRegion": ["India"], "custprimecity": "Bangalore"},
    ] + [{"id": str(100 + i), "urlTitle": "Y", "unifiedStandardTitle": "Old", "unifiedStandardStart": old,
          "jobLocationCountry": ["India"]} for i in range(7)], 5000)
    lane, http = _lane("successfactors", (200, page0))
    res = poller.fetch_board(lane, poller.Board("successfactors", "careers.wipro.com"))
    assert res.error is None and not res.complete and len(http.calls) == 1   # stopped: page all older than a week
    ids = [p.posting_id for p in res.postings]
    assert "216316" in ids and "179408" in ids and "9" not in ids            # Hong Kong dropped
    p = res.postings[0]
    assert p.url == "https://careers.wipro.com/job/Process-Associate/216316-en_US/"
    assert ats.place(p) == "india" and ats.detail_url(p) == p.url
    ats.parse_detail(p, '<span itemprop="title">Data Engineer</span>'
                        '<span itemprop="description"><p>Build <b>Spark</b> pipelines.</p></span>'
                        '<span itemprop="description"><p>Mandatory: Python</p></span>')
    assert "Spark" in p.description and "Python" in p.description


def test_successfactors_canary_is_skipped():
    lane, _ = _lane("successfactors")
    assert poller.canary_ok(lane)


# --- Avature career portals (#160) --------------------------------------------------

AV_PAGE = """
<a href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/528135" data-au="ag-a-10"> Data Engineer </a>
<span>Thane</span>, <span>Maharashtra</span>, <span>India</span> &bull; Job ID: 528135
<a href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/528135" aria-label="Learn more"> Learn more </a>
<a href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/528116" data-au="ag-a-10"> Power Electronics Engineer </a>
<span>Suzhou</span>, <span>China</span> &bull; Job ID: 528116
"""


def test_avature_list_keeps_india_and_reads_titles():
    lane, http = _lane("avature", (200, AV_PAGE), (200, "<html>no results</html>"))
    res = poller.fetch_board(lane, poller.Board("avature", "jobs.siemens.com/en_US/externaljobs"))
    assert res.error is None and not res.complete
    assert [(p.posting_id, p.title) for p in res.postings] == [("528135", "Data Engineer")]   # China dropped
    assert http.calls[0] == "https://jobs.siemens.com/en_US/externaljobs/SearchJobs?jobOffset=0"
    assert http.calls[1].endswith("jobOffset=10")
    p = res.postings[0]
    assert ats.place(p) == "india" and ats.detail_url(p) == p.url


def test_avature_detail_from_json_ld_or_sections():
    p = ats.Posting(ats="avature", board_id="b", posting_id="1", title="t", location="India", url="u")
    ats.parse_detail(p, '<script type="application/ld+json">{"@type": "JobPosting", "datePosted": "2026-11-03",'
                        ' "description": "<p>Build Spark pipelines</p>"}</script>')
    assert p.description == "Build Spark pipelines" and p.posted_at.day == 3
    q = ats.Posting(ats="avature", board_id="b", posting_id="2", title="t", location="India", url="u")
    ats.parse_detail(q, '<span>Posted since</span> <span>03-Nov-2026</span>'
                        '<div id="section1__content"><p>Python and SQL daily</p></div>'
                        '<div class="x" id="section2__content">Apply</div>')
    assert "Python and SQL" in q.description and q.posted_at.month == 11


# --- Phenom career sites (#160) -----------------------------------------------------

def _ph_page(ids, total):
    return json.dumps({"refineSearch": {"status": 200, "totalHits": total, "data": {"jobs": [
        {"jobId": str(i), "title": f"Data Engineer {i}", "location": "Chennai, IN-TN, India", "country": "India",
         "postedDate": "2026-11-03T05:52:26.000+0000", "jobSeqNo": f"SEQ{i}", "type": "Full-Time"} for i in ids]}}})


def test_phenom_pages_india_and_posts_for_detail():
    lane, http = _lane("phenom", (200, _ph_page(range(100), 130)), (200, _ph_page(range(100, 130), 130)),
                       (200, json.dumps({"jobDetail": {"data": {"job": {
                           "description": "<p>Spark and Python</p>", "companyName": "NTT DATA"}}}})))
    res = poller.fetch_board(lane, poller.Board("phenom", "careers.nttdata.com/en_global"))
    assert res.error is None and res.complete and len(res.postings) == 130
    assert http.calls[0][0] == "https://careers.nttdata.com/widgets"
    assert http.calls[0][1]["selected_fields"] == {"country": ["India"]} and http.calls[1][1]["from"] == 100
    p = res.postings[0]
    assert ats.place(p) == "india" and p.url == "https://careers.nttdata.com/global/en/job/0" and p.ref == "SEQ0"
    assert poller.fetch_detail(lane, p)
    assert http.calls[2][1]["ddoKey"] == "jobDetail" and http.calls[2][1]["jobSeqNo"] == "SEQ0"
    assert "Spark" in p.description and p.company == "NTT DATA"


# --- XML job feeds (#161) -----------------------------------------------------------

FEED = """<?xml version="1.0" encoding="utf-8"?><source><publisher>Cognizant</publisher>
<job><title><![CDATA[Data Engineer]]></title><date><![CDATA[Tue, 03 Nov 2026 05:06:46 GMT]]></date>
<referencenumber><![CDATA[00069818612]]></referencenumber>
<url><![CDATA[https://careers.cognizant.com/global-en/jobs/00069818612/data-engineer/]]></url>
<company><![CDATA[Cognizant]]></company><city><![CDATA[Chennai]]></city><state><![CDATA[Tamil Nadu]]></state>
<country><![CDATA[India]]></country><description><![CDATA[<p>Spark and <b>Python</b></p>]]></description></job>
<job><title><![CDATA[Banker]]></title><date><![CDATA[Tue, 03 Nov 2026 04:00:00 GMT]]></date>
<referencenumber><![CDATA[2]]></referencenumber><url><![CDATA[https://x/2]]></url><city><![CDATA[Dallas]]></city>
<country><![CDATA[United States]]></country><description><![CDATA[x]]></description></job></source>"""


def test_xmlfeed_reads_the_whole_feed_with_descriptions():
    board = "careers.cognizant.com/global-en/jobs/xml/?rss=true"
    lane, http = _lane("xmlfeed", (200, FEED))
    res = poller.fetch_board(lane, poller.Board("xmlfeed", board))
    assert res.error is None and res.complete and len(res.postings) == 2
    assert http.calls == ["https://careers.cognizant.com/global-en/jobs/xml/?rss=true"]
    p, q = res.postings
    assert ats.place(p) == "india" and ats.place(q) == "elsewhere"
    assert "Spark" in p.description and ats.detail_url(p) is None and p.posted_at.hour == 5
    assert p.job_id == "xmlfeed-careers.cognizant.com_global-en_jobs_xml_rss_true-00069818612"   # URL-safe


def test_xmlfeed_that_is_not_xml_is_an_error():
    lane, _ = _lane("xmlfeed", (200, "<html>maintenance</html"))
    assert poller.fetch_board(lane, poller.Board("xmlfeed", "x/feed")).error


# --- Goldman Sachs (#161) ------------------------------------------------------------

def test_goldman_graphql_pages_and_role_page_description():
    page = json.dumps({"data": {"roleSearch": {"totalCount": 2, "items": [
        {"roleId": "187879_GS_MID_CAREER", "jobTitle": "Data Engineer", "corporateTitle": "Associate",
         "locations": [{"city": "Bengaluru", "state": "Karnataka", "country": "India"}],
         "externalSource": {"sourceId": "187879"}},
        {"roleId": "2", "jobTitle": "Trader", "locations": [{"city": "London", "country": "United Kingdom"}],
         "externalSource": {"sourceId": "2"}}]}}})
    lane, http = _lane("goldman", (200, page))
    res = poller.fetch_board(lane, poller.Board("goldman", "higher.gs.com"))
    assert res.error is None and res.complete and len(res.postings) == 2 and len(http.calls) == 1
    url, body = http.calls[0]
    assert url == "https://api-higher.gs.com/gateway/api/v1/graphql"
    assert body["variables"]["searchQueryInput"]["filters"][0]["filters"][0]["filter"] == "India"
    p = res.postings[0]
    assert ats.place(p) == "india" and p.url == "https://higher.gs.com/roles/187879"
    ats.parse_detail(p, '<script id="__NEXT_DATA__" type="application/json">'
                        '{"props": {"role": {"descriptionHtml": "<p>Build <b>Python</b> services</p>"}}}</script>')
    assert "Python" in p.description


def test_employment_type_accepts_a_json_ld_list():
    """A JSON-LD employmentType list crashed every ATS run for six hours (2026-11-04)."""
    assert ats.employment_type(["FULL_TIME"]) == "fulltime"
    assert ats.employment_type(("PART_TIME", None)) == "parttime"
    assert ats.employment_type(None) is None and ats.employment_type([]) is None
    p = ats.Posting(ats="avature", board_id="b", posting_id="1", title="Data Engineer", location="Bengaluru, India",
                    url="u", description="Spark")
    ats.parse_detail(p, '<script type="application/ld+json">{"@type": "JobPosting", "datePosted": "2026-11-03",'
                        ' "employmentType": ["FULL_TIME"], "description": "<p>Spark</p>"}</script>')
    assert p.employment_type == "FULL_TIME"
    job = ats.to_job(p, company="Deloitte")
    assert job.job_type == "fulltime"


# --- date-only sources are flagged (#180) -------------------------------------------------

def test_date_only_sources_are_flagged():
    assert isinstance(ats._sf_date("11/3/26"), ats.PostedDate)
    assert isinstance(ats._amazon_date("November 3, 2026"), ats.PostedDate)
    (o,) = ats.parse_list("oracle", ORACLE, _oracle_page([1], 1))
    assert isinstance(o.posted_at, ats.PostedDate)
    # Workable's detail writes the list's date as a midnight timestamp.
    assert isinstance(ats._when("2026-09-21T00:00:00.000Z"), ats.PostedDate)
    # Real times, and epoch numbers, are not dates.
    assert not isinstance(ats._when("2026-09-21T09:30:00Z"), ats.PostedDate)
    assert not isinstance(ats._when(1_790_000_000_000), ats.PostedDate)
