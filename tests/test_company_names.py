"""Company names for boards whose list names none (#182): Oracle pods showed
as "Fa Etvl Saasfaprod1" (Zensar) and "Hcbt" (Kotak), Workday tenants as
"Wf"/"Pwc", and Avanade's jobs as "Accenture"."""

from __future__ import annotations

import json

import pytest

from src import ats_pipeline as pipe
from src.scraper import ats
from src.scraper import ats_poller as poller


@pytest.mark.parametrize("body,name", [
    # Real pages, trimmed (2026-11-05).
    ('<meta property="og:title" content="Zensar Careers"/><meta property="og:site_name" content="Zensar"/>'
     "<title>Zensar</title>", "Zensar"),
    ('<meta property="og:site_name" content="Kotak Additional Career Site"/>', "Kotak"),
    ('<meta property="og:site_name" content="DTCC Candidate Experience Site"/>', "DTCC"),
    ('<meta property="og:site_name" content="Cummins Talent Acquisition"/>', "Cummins"),
    ('<title></title><meta name="title" property="og:title" content="Avanade Careers">', "Avanade"),
    ('<title></title><meta name="title" property="og:title" content="Wolters Kluwer">', "Wolters Kluwer"),
    ("<title>Careers at Acme &amp; Co</title>", "Acme & Co"),
    ("<title>G2i Inc. Jobs</title>", "G2i Inc."),
    ('<meta property="og:site_name" content="Verisk Careers | Verisk"/>', "Verisk"),
    ('<meta property="og:site_name" content="Career Site(Servify)"/>', "Servify"),
    ('<meta property="og:site_name" content="Jobs onsemi"/>', "onsemi"),
    ('<meta property="og:site_name" content="Job Listings at Liquidity Services Inc."/>', "Liquidity Services Inc."),
    ('<meta property="og:site_name" content="Legrand Group Opportunities - Join us"/>', "Legrand Group"),
    ('<meta property="og:site_name" content="ENBD Recruitment Team"/>', "ENBD"),
    ('<meta property="og:site_name" content="SAEL_Career Site_V1"/>', "SAEL"),
    ('<meta property="og:site_name" content="HR"/>', None),
    # Pages that name nobody.
    ('<meta property="og:site_name" content="Candidate Experience site"/><title>Candidate Experience site</title>', None),
    ('<title></title><meta name="title" property="og:title">', None),
    ("<title>Jobs</title>", None),
    ("", None),
])
def test_page_name(body, name):
    assert ats.page_name(body) == name


def test_an_oracle_site_with_a_generic_title_is_named_by_its_logo_domain():
    body = ('<meta property="og:image" content="https://www.kotak.com/bank/mailers/Kotak-Logo.png"/>'
            '<meta property="og:site_name" content="Candidate Experience site"/>')
    assert ats.board_page_name("oracle", body) == "Kotak"
    tradeweb = ('<meta property="og:image" content="https://cdn.tradeweb.com/sites/logo.png"/>'
                '<meta property="og:site_name" content="Candidate Experience site"/>')
    assert ats.board_page_name("oracle", tradeweb) == "Tradeweb"
    oracle_logo = '<meta property="og:image" content="https://static.oracle.com/logo.png"/>'
    assert ats.board_page_name("oracle", oracle_logo) is None
    assert ats.board_page_name("workday", body) is None   # only Oracle


def test_curated_names_win_for_unreadable_tenants_and_pods():
    assert ats.known_name("workday", "wf/wd1/WellsFargoJobs") == "Wells Fargo"
    assert ats.known_name("workday", "PwC/wd3/Global_Experienced_Careers") == "PwC"   # keys are lower case
    assert ats.known_name("oracle", "jpmc.fa.oraclecloud.com/CX_2001") == "JPMorganChase"
    assert ats.known_name("workday", "accenture/wd103/AccentureCareers") is None   # readable as it is
    assert ats.known_name("lever", "wf") is None
    p = ats.Posting(ats="workday", board_id="wf/wd1/WellsFargoJobs", posting_id="1", title="x", location=None)
    assert pipe.company_name(poller.Board("workday", p.board_id), p) == "Wells Fargo"


def test_an_unknown_initialism_is_upper_case():
    p = ats.Posting(ats="workday", board_id="gh/wd1/x", posting_id="1", title="x", location=None)
    assert pipe.company_name(poller.Board("workday", "gh/wd1/x"), p) == "GH"


def test_workday_legal_entities_are_not_used_as_names():
    # "IND201 Refinitiv India Shared Services" for LSEG, "VISCO ..." for
    # Vanguard: worse than the tenant (survey of 169 boards, 2026-11-05).
    p = ats.Posting(ats="workday", board_id="lseg/wd3/careers", posting_id="1", title="x", location=None,
                    url="https://lseg.wd3.myworkdayjobs.com/careers/job/x")
    ats.parse_detail(p, json.dumps({"jobPostingInfo": {"jobDescription": "<p>d</p>"},
                                    "hiringOrganization": {"name": "IND201 Refinitiv India Shared Services"}}))
    assert pipe.company_name(poller.Board("workday", p.board_id), p) == "LSEG"


def test_workable_postings_carry_the_account_name():
    body = json.dumps({"name": "Acme Labs", "jobs": [{"shortcode": "AB1", "title": "SWE", "country": "India",
                                                      "url": "u", "locations": [{"countryCode": "IN"}]}]})
    assert ats.parse_list("workable", "acme", body)[0].company == "Acme Labs"


def test_name_pages():
    assert ats.name_page_url("oracle", "hcbt.fa.em2.oraclecloud.com/CX_1001") == \
        "https://hcbt.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"
    assert ats.name_page_url("workday", "accenture/wd103/AvanadeCareers") == \
        "https://accenture.wd103.myworkdayjobs.com/AvanadeCareers"
    assert ats.name_page_url("lever", "cin7") == "https://jobs.lever.co/cin7"
    assert ats.name_page_url("greenhouse", "acme") is None   # its list names the company


class _Lane:
    def __init__(self, name, pages):
        self.name, self.delay, self.pages, self.calls = name, 0, pages, []

    def get(self, url, patient=True):
        self.calls.append(url)
        return (200, self.pages[url]) if url in self.pages else (404, "")


def test_fetch_names_reads_only_unnamed_boards_with_a_page():
    url = ats.name_page_url("oracle", "hcbt.fa.em2.oraclecloud.com/CX_1001")
    lane = _Lane("oracle", {url: '<meta property="og:site_name" content="Kotak Mahindra Bank"/>'})
    boards = [poller.Board("oracle", "hcbt.fa.em2.oraclecloud.com/CX_1001"),
              poller.Board("oracle", "jpmc.fa.oraclecloud.com/CX_1", name="JPMorganChase"),
              poller.Board("greenhouse", "acme")]
    boards.append(poller.Board("oracle", "hdpc.fa.us2.oraclecloud.com/CampusHiring"))   # curated
    names = poller.fetch_names(boards, {"oracle": lane})
    assert names == {("oracle", "hcbt.fa.em2.oraclecloud.com/CX_1001"): "Kotak Mahindra Bank",
                     ("oracle", "hdpc.fa.us2.oraclecloud.com/CampusHiring"): "Goldman Sachs"}
    assert lane.calls == [url]   # the curated one needs no request


def test_a_named_board_gets_the_name_and_its_key():
    b = pipe._named(poller.Board("oracle", "hcbt.fa.em2.oraclecloud.com/CX"), "Kotak Mahindra Bank")
    assert (b.name, b.company_key) == ("Kotak Mahindra Bank", ats.key("Kotak Mahindra Bank"))
    assert pipe.company_name(b, ats.Posting(ats="oracle", board_id=b.board_id, posting_id="1", title="x",
                                            location=None)) == "Kotak Mahindra Bank"
