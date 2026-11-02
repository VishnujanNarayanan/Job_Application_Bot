"""Layer 2 — ATS job boards read directly (#97).

Companies' own job boards on Greenhouse, Lever, Ashby and SmartRecruiters
have public JSON endpoints that need no login (hard rule #4 / FR-15). A job
found there has a live, direct application link, and appears there before it
reaches LinkedIn or Naukri.

Each adapter turns one board's response into :class:`Posting`\\ s, and
:func:`to_job` maps a posting to the same ``AllJobs`` row JobSpy produces, so
the filters, parser, scorer and builder run on it unchanged. Everything here
is pure (body in, postings out); fetching and pacing live in the poller.

Lists that carry no description (Greenhouse, SmartRecruiters) are fetched
light, and :func:`detail_url` / :func:`parse_detail` add the description only
for the postings that survive the cheap checks, so an hourly poll stays small.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from src.state.models import AllJobs

ATSES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workday",
         "recruitee", "rippling", "workable", "bamboohr", "eightfold")

# SmartRecruiters pages hold at most 100 postings.
SR_PAGE = 100
# Eightfold returns 10 postings a page.
EIGHTFOLD_PAGE = 10


@dataclass
class Posting:
    ats: str
    board_id: str
    posting_id: str
    title: str
    location: str
    # Structured fields, when the ATS gives them: ISO country code, and
    # remote True/False (None = the ATS does not say).
    country: str | None = None
    remote: bool | None = None
    posted_at: datetime | None = None
    url: str | None = None
    apply_url: str | None = None
    description: str | None = None
    company: str | None = None
    employment_type: str | None = None
    level: str | None = None

    @property
    def job_id(self) -> str:
        # Workday board ids are tenant/wdN/site: no slashes in a job id.
        return f"{self.ats}-{self.board_id.replace('/', '_')}-{self.posting_id}"


# --- Company names ----------------------------------------------------------------
# One key per company, so a board, a LinkedIn/Naukri listing and a company
# list can be matched ("Acme Technologies Pvt Ltd" -> "acme").

# Trailing words that are not part of a board id.
SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated",
    "corp", "corporation", "co", "company", "plc", "gmbh", "ag", "sa", "bv",
    "technologies", "technology", "solutions", "services", "systems",
    "software", "labs", "lab", "india", "global", "group", "consulting",
    "international", "in", "of", "the", "and", "center", "centre",
    "acceleration", "north", "america", "research", "development", "com", "io",
}
def words(name: str) -> list[str]:
    s = name.lower().replace("&", " and ").replace("+", " plus ")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return s.split()


def core_words(name: str) -> list[str]:
    w = [x for x in words(name)]
    while len(w) > 1 and w[-1] in SUFFIXES:
        w.pop()
    while len(w) > 1 and w[0] == "the":
        w.pop(0)
    return w


def key(name: str) -> str:
    return "".join(core_words(name))


# --- URLs -------------------------------------------------------------------------

def list_url(ats: str, board_id: str, *, country: str | None = None, offset: int = 0) -> str:
    """The endpoint that lists a board's open postings. Workday's is POSTed
    (:func:`workday_search`)."""
    if ats == "workday":
        tenant, shard, site = workday_parts(board_id)
        return f"{workday_host(board_id)}/wday/cxs/{tenant}/{site}/jobs"
    b = quote(board_id, safe="")
    if ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{b}/jobs"
    if ats == "lever":
        return f"https://api.lever.co/v0/postings/{b}?mode=json"
    if ats == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{b}"
    if ats == "smartrecruiters":
        url = f"https://api.smartrecruiters.com/v1/companies/{b}/postings?limit={SR_PAGE}&offset={offset}"
        return f"{url}&country={country}" if country else url
    # #160: the next ATSes. Eightfold needs the company's web domain too;
    # every board seen so far is {id}.com (hsbc, netapp, netflix, insight).
    if ats == "recruitee":
        return f"https://{b}.recruitee.com/api/offers"
    if ats == "rippling":
        return f"https://api.rippling.com/platform/api/ats/v1/board/{b}/jobs"
    if ats == "workable":
        return f"https://apply.workable.com/api/v1/widget/accounts/{b}"
    if ats == "bamboohr":
        return f"https://{b}.bamboohr.com/careers/list"
    if ats == "eightfold":
        return (f"https://{b}.eightfold.ai/api/apply/v2/jobs?domain={b}.com"
                f"&location=India&num={EIGHTFOLD_PAGE}&start={offset}")
    raise ValueError(f"no adapter for {ats}")


# --- Boards from apply links (#158) -------------------------------------------------
# A LinkedIn / Naukri job's own apply link names the company's ATS board, so
# every portal job can teach us a board. Hosts where the board id is a path
# segment, and hosts where it is the subdomain.

_PATH_HOSTS = {
    "boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse", "job-boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever", "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "jobs.smartrecruiters.com": "smartrecruiters", "careers.smartrecruiters.com": "smartrecruiters",
    "apply.workable.com": "workable",
    "ats.rippling.com": "rippling",
}
_SUBDOMAIN_HOSTS = {
    "recruitee.com": "recruitee", "bamboohr.com": "bamboohr", "breezy.hr": "breezy",
    "teamtailor.com": "teamtailor", "applytojob.com": "jazzhr", "darwinbox.in": "darwinbox",
    "keka.com": "keka", "freshteam.com": "freshteam", "zohorecruit.com": "zoho_recruit",
    "zohorecruit.in": "zoho_recruit", "eightfold.ai": "eightfold",
}
# Path segments that are pages of the ATS itself, not a board.
_NOT_BOARDS = {"embed", "api", "v1", "jobs", "job_app", "o", "j", "careers", ""}
_WORKDAY = re.compile(r"^(?P<tenant>[a-z0-9-]+)\.(?P<shard>wd\d+)\.myworkdayjobs\.com$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# Generic subdomains that are never a customer's board.
_NOT_TENANTS = {"www", "api", "app", "careers", "jobs", "help", "support"}


def board_from_url(url: str | None) -> tuple[str, str] | None:
    """(ats, board_id) named by a job's apply link, or None. Workday's id is
    ``tenant/wdN/site``; every other id is what ``list_url`` takes."""
    from urllib.parse import parse_qs, urlsplit

    if not url:
        return None
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    if host in _PATH_HOSTS:
        ats = _PATH_HOSTS[host]
        # boards.greenhouse.io/embed/job_app?for=acme&token=...
        if ats == "greenhouse" and parts[:1] == ["embed"]:
            board = (parse_qs(u.query).get("for") or [""])[0]
        else:
            board = parts[0] if parts else ""
        if board.lower() in _NOT_BOARDS:
            return None
        return ats, board.lower() if ats in ("smartrecruiters", "greenhouse") else board
    # SuccessFactors: career{n}.successfactors.com/career?company=acme (#160).
    if host.endswith((".successfactors.com", ".successfactors.eu")) and host.startswith("career"):
        company = (parse_qs(u.query).get("company") or [""])[0].strip()
        return ("successfactors", company) if company else None
    # iCIMS: the whole host is the board (careers-acme.icims.com) (#160).
    if host.endswith(".icims.com"):
        sub = host[: -len(".icims.com")]
        return ("icims", sub) if sub and sub not in _NOT_TENANTS else None
    m = _WORKDAY.match(host)
    if m:
        site = next((p for p in parts if not _LOCALE.match(p)), None)
        if not site or site in ("wday", "job"):
            return None
        return "workday", f"{m['tenant']}/{m['shard']}/{site}"
    for suffix, ats in _SUBDOMAIN_HOSTS.items():
        if host.endswith("." + suffix):
            tenant = host[: -len(suffix) - 1].split(".")[-1]
            if tenant and tenant not in _NOT_TENANTS:
                return ats, tenant
    return None


# --- Workday (#103) ---------------------------------------------------------------
# A board is {tenant}.{wdN}.myworkdayjobs.com/{site}; its id here is
# "tenant/wdN/site". The search is an unofficial JSON POST that returns 20
# postings a page plus location facets; India is a facet value whose id
# differs per tenant, so the first page (unfiltered) is read for it.

WORKDAY_PAGE = 20


def workday_parts(board_id: str) -> tuple[str, str, str]:
    tenant, shard, site = board_id.split("/", 2)
    return tenant, shard, site


def workday_host(board_id: str) -> str:
    tenant, shard, _site = workday_parts(board_id)
    return f"https://{tenant}.{shard}.myworkdayjobs.com"


def workday_search(offset: int = 0, facets: dict | None = None) -> dict:
    return {"appliedFacets": facets or {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": ""}


def workday_india_facets(data: dict) -> dict[str, list[str]]:
    """{facet parameter: [ids]} that select India, from a search response.
    A country-level value ("India") is used when there is one; otherwise every
    site whose name is in India ("India, Bengaluru", "Pune"). Empty = the
    board has no India postings."""
    countries: dict[str, list[str]] = {}
    sites: dict[str, list[str]] = {}

    def walk(facet: dict, param: str | None) -> None:
        param = facet.get("facetParameter") or param
        for v in facet.get("values") or []:
            if v.get("values"):
                walk(v, param)
                continue
            name = (v.get("descriptor") or "").strip()
            if not v.get("id") or not param:
                continue
            if name.lower() == "india":
                countries.setdefault(param, []).append(v["id"])
            elif in_india(name):
                sites.setdefault(param, []).append(v["id"])

    for f in data.get("facets") or []:
        walk(f, None)
    if countries:
        # One country facet is enough; several would AND together.
        param = next(iter(countries))
        return {param: countries[param]}
    if sites:
        param = max(sites, key=lambda k: len(sites[k]))
        return {param: sites[param]}
    return {}


_POSTED_DAYS = re.compile(r"(\d+)\+?\s+days?\s+ago", re.I)


def _workday_posted(text: str | None, now: datetime) -> datetime | None:
    """"Posted Today" / "Posted Yesterday" / "Posted 3 Days Ago" /
    "Posted 30+ Days Ago" -> an approximate time; the detail's startDate
    replaces it."""
    t = (text or "").lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = _POSTED_DAYS.search(t)
    if m:
        return now - timedelta(days=int(m.group(1)) + (1 if "+" in t else 0))
    return None


def _workday(board_id: str, data: dict, now: datetime | None = None) -> list[Posting]:
    """Postings of an India-filtered page: every one is in India (country IN),
    even when its location says "3 Locations"."""
    now = now or datetime.now(timezone.utc)
    host = workday_host(board_id)
    _tenant, _shard, site = workday_parts(board_id)
    out = []
    for j in data.get("jobPostings") or []:
        path = j.get("externalPath") or ""
        if not path:
            continue
        ref = (j.get("bulletFields") or [None])[0] or path.rsplit("_", 1)[-1]
        loc = j.get("locationsText") or ""
        out.append(Posting(
            ats="workday", board_id=board_id, posting_id=str(ref), title=(j.get("title") or "").strip(),
            location=loc, country="IN",
            remote=True if re.search(r"\bremote\b", loc, re.I) else None,
            posted_at=_workday_posted(j.get("postedOn"), now),
            url=f"{host}/{site}{path}", apply_url=f"{host}/{site}{path}",
        ))
    return out


def detail_url(posting: Posting) -> str | None:
    """Where the description is, for lists that leave it out; else None."""
    if posting.description is not None:
        return None
    if posting.ats == "workday":
        tenant, _shard, site = workday_parts(posting.board_id)
        host = workday_host(posting.board_id)
        path = (posting.url or "").split(f"/{site}", 1)[-1]
        return f"{host}/wday/cxs/{tenant}/{site}{path}"
    b = quote(posting.board_id, safe="")
    if posting.ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{b}/jobs/{posting.posting_id}"
    if posting.ats == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{b}/postings/{posting.posting_id}"
    if posting.ats == "rippling":
        return f"https://api.rippling.com/platform/api/ats/v1/board/{b}/jobs/{posting.posting_id}"
    if posting.ats == "workable":
        return f"https://apply.workable.com/api/v2/accounts/{b}/jobs/{posting.posting_id}"
    if posting.ats == "bamboohr":
        return f"https://{b}.bamboohr.com/careers/{posting.posting_id}/detail"
    if posting.ats == "eightfold":
        return f"https://{b}.eightfold.ai/api/apply/v2/jobs/{posting.posting_id}?domain={b}.com"
    return None


# --- Parsing ----------------------------------------------------------------------

def html_to_text(raw: str | None) -> str | None:
    """HTML (possibly entity-escaped, as Greenhouse sends it) to the markdown
    JobSpy stores in ``jd_text``."""
    if not raw:
        return None
    from jobspy.util import markdown_converter

    text = markdown_converter(html.unescape(raw))
    return (text or "").strip() or None


def _when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OSError, OverflowError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _country(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in ("in", "ind", "india"):
        return "IN"
    if len(text) == 2 and text.isalpha():
        return text.upper()
    # A country name, not a placeholder such as "Remote".
    return None if text in ("", "remote", "anywhere", "global") else text.title()


def _greenhouse(board_id: str, data: dict) -> list[Posting]:
    out = []
    for j in data.get("jobs") or []:
        loc = (j.get("location") or {}).get("name") or ""
        out.append(Posting(
            ats="greenhouse", board_id=board_id, posting_id=str(j["id"]),
            title=j.get("title") or "", location=loc,
            remote=True if re.search(r"\bremote\b", loc, re.I) else None,
            # first_published, never updated_at: an edit must not make an old
            # job look new.
            posted_at=_when(j.get("first_published")),
            url=j.get("absolute_url"), apply_url=j.get("absolute_url"),
            description=html_to_text(j["content"]) if j.get("content") else None,
            company=j.get("company_name"),
        ))
    return out


def _lever(board_id: str, data: list) -> list[Posting]:
    out = []
    for j in data or []:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        parts = [j.get("descriptionPlain") or ""]
        for block in j.get("lists") or []:
            parts.append(block.get("text") or "")
            parts.append(html_to_text(block.get("content")) or "")
        parts.append(j.get("additionalPlain") or "")
        wt = (j.get("workplaceType") or "").lower()
        out.append(Posting(
            ats="lever", board_id=board_id, posting_id=str(j["id"]),
            title=j.get("text") or "", location=" / ".join(x for x in locs if x),
            country=_country(j.get("country")) if j.get("country") else None,
            remote=True if wt == "remote" else (False if wt in ("onsite", "on-site", "hybrid") else None),
            posted_at=_when(j.get("createdAt")),
            url=j.get("hostedUrl"), apply_url=j.get("applyUrl") or j.get("hostedUrl"),
            description="\n\n".join(p.strip() for p in parts if p and p.strip()) or None,
            employment_type=cats.get("commitment"),
        ))
    return out


def _ashby(board_id: str, data: dict) -> list[Posting]:
    out = []
    for j in data.get("jobs") or []:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [x.get("location") or "" for x in j.get("secondaryLocations") or []]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        countries = {_country(addr.get("addressCountry"))} | {
            _country(((x.get("address") or {}).get("postalAddress") or {}).get("addressCountry"))
            for x in j.get("secondaryLocations") or []}
        out.append(Posting(
            ats="ashby", board_id=board_id, posting_id=str(j["id"]),
            title=(j.get("title") or "").strip(), location=" / ".join(x for x in locs if x),
            country="IN" if "IN" in countries else _country(addr.get("addressCountry")),
            remote=bool(j.get("isRemote")) or (j.get("workplaceType") or "").lower() == "remote",
            posted_at=_when(j.get("publishedAt")),
            url=j.get("jobUrl"), apply_url=j.get("applyUrl") or j.get("jobUrl"),
            description=(j.get("descriptionPlain") or "").strip() or html_to_text(j.get("descriptionHtml")),
            employment_type=j.get("employmentType"),
        ))
    return out


def _smartrecruiters(board_id: str, data: dict) -> list[Posting]:
    out = []
    for j in data.get("content") or []:
        loc = j.get("location") or {}
        out.append(Posting(
            ats="smartrecruiters", board_id=board_id, posting_id=str(j["id"]),
            title=j.get("name") or "",
            location=loc.get("fullLocation") or ", ".join(
                str(loc[k]) for k in ("city", "region", "country") if loc.get(k)),
            country=_country(loc.get("country")) if loc.get("country") else None,
            remote=bool(loc.get("remote")) if "remote" in loc else None,
            posted_at=_when(j.get("releasedDate")),
            url=f"https://jobs.smartrecruiters.com/{quote(board_id, safe='')}/{j['id']}",
            company=(j.get("company") or {}).get("name"),
            employment_type=(j.get("typeOfEmployment") or {}).get("label"),
            level=(j.get("experienceLevel") or {}).get("label"),
        ))
    return out


def _recruitee(board_id: str, data: dict) -> list[Posting]:
    out = []
    for j in data.get("offers") or []:
        locs = j.get("locations") or []
        codes = {(x.get("country_code") or "").upper() for x in locs}
        desc = "\n\n".join(t for t in (html_to_text(j.get("description")), html_to_text(j.get("requirements"))) if t)
        out.append(Posting(
            ats="recruitee", board_id=board_id, posting_id=str(j["id"]), title=(j.get("title") or "").strip(),
            location=j.get("location") or " / ".join(x.get("name") or "" for x in locs),
            country="IN" if "IN" in codes else _country(j.get("country_code") or j.get("country")),
            remote=bool(j.get("remote")) or None,
            posted_at=_when((j.get("published_at") or j.get("created_at") or "").replace(" UTC", "+00:00")),
            url=j.get("careers_url"), apply_url=j.get("careers_apply_url") or j.get("careers_url"),
            description=desc or None, company=j.get("company_name"),
            employment_type=j.get("employment_type_code"),
        ))
    return out


def _rippling(board_id: str, data: list) -> list[Posting]:
    out = []
    for j in data or []:
        loc = (j.get("workLocation") or {}).get("label") or ""
        out.append(Posting(
            ats="rippling", board_id=board_id, posting_id=str(j["uuid"]), title=(j.get("name") or "").strip(),
            location=loc, remote=True if re.search(r"\bremote\b", loc, re.I) else None,
            url=j.get("url"), apply_url=j.get("url"),
        ))
    return out


def _workable(board_id: str, data: dict) -> list[Posting]:
    out = []
    for j in data.get("jobs") or []:
        locs = j.get("locations") or []
        codes = {(x.get("countryCode") or "").upper() for x in locs}
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        out.append(Posting(
            ats="workable", board_id=board_id, posting_id=str(j["shortcode"]), title=(j.get("title") or "").strip(),
            location=loc, country="IN" if "IN" in codes else _country(j.get("country")),
            remote=True if j.get("telecommuting") else None,
            posted_at=_when(j.get("published_on")),
            url=j.get("url"), apply_url=j.get("application_url") or j.get("url"),
            employment_type=j.get("employment_type"),
        ))
    return out


def _bamboohr(board_id: str, data: dict) -> list[Posting]:
    out = []
    for j in data.get("result") or []:
        loc = j.get("location") or {}
        ats_loc = j.get("atsLocation") or {}
        place_text = ", ".join(x for x in (loc.get("city") or ats_loc.get("city"), loc.get("state") or ats_loc.get("state"),
                                            ats_loc.get("country")) if x)
        out.append(Posting(
            ats="bamboohr", board_id=board_id, posting_id=str(j["id"]), title=(j.get("jobOpeningName") or "").strip(),
            location=place_text, country=_country(ats_loc.get("country")) if ats_loc.get("country") else None,
            remote=True if j.get("isRemote") else None,
            url=f"https://{board_id}.bamboohr.com/careers/{j['id']}",
            apply_url=f"https://{board_id}.bamboohr.com/careers/{j['id']}",
            employment_type=j.get("employmentType"),
        ))
    return out


def _eightfold(board_id: str, data: dict) -> list[Posting]:
    out = []
    for j in data.get("positions") or []:
        locs = j.get("locations") or [j.get("location") or ""]
        out.append(Posting(
            ats="eightfold", board_id=board_id, posting_id=str(j["id"]),
            title=(j.get("posting_name") or j.get("name") or "").strip(), location=" / ".join(x for x in locs if x),
            remote=True if (j.get("work_location_option") or "").lower() == "remote" else None,
            posted_at=_when(j.get("t_create")),
            url=j.get("canonicalPositionUrl"), apply_url=j.get("canonicalPositionUrl"),
        ))
    return out


_PARSERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
            "smartrecruiters": _smartrecruiters, "workday": _workday, "recruitee": _recruitee,
            "rippling": _rippling, "workable": _workable, "bamboohr": _bamboohr, "eightfold": _eightfold}


def parse_list(ats: str, board_id: str, body: str) -> list[Posting]:
    """A board's postings from its list response. Raises ValueError when the
    body is not the expected JSON, so a changed API fails loudly per board."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ValueError(f"{ats}/{board_id}: not JSON") from exc
    expected = list if ats in ("lever", "rippling") else dict
    if not isinstance(data, expected):
        raise ValueError(f"{ats}/{board_id}: unexpected {type(data).__name__}")
    return _PARSERS[ats](board_id, data)


def sr_total(body: str) -> int:
    """SmartRecruiters' total for the query, to page through it."""
    try:
        return int(json.loads(body).get("totalFound") or 0)
    except (ValueError, AttributeError):
        return 0


def parse_detail(posting: Posting, body: str) -> None:
    """Fill ``posting`` from its detail response: description, and the apply
    link SmartRecruiters only gives there."""
    data = json.loads(body)
    if posting.ats == "greenhouse":
        posting.description = html_to_text(data.get("content"))
        # Structured offices: a vague board location ("Hybrid") often names
        # the office here.
        offices = [o.get("location") or o.get("name") or "" for o in data.get("offices") or []]
        if any(INDIA.search(o) for o in offices):
            posting.country = "IN"
    elif posting.ats == "smartrecruiters":
        sections = (data.get("jobAd") or {}).get("sections") or {}
        parts = []
        for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
            sec = sections.get(key) or {}
            text = html_to_text(sec.get("text"))
            if text:
                parts.append(f"## {sec.get('title') or key}\n\n{text}")
        posting.description = "\n\n".join(parts) or None
        posting.apply_url = data.get("applyUrl") or posting.apply_url
        posting.url = data.get("postingUrl") or posting.url
    elif posting.ats == "rippling":
        d = data.get("description") or {}
        posting.description = "\n\n".join(t for t in (html_to_text(d.get("role")), html_to_text(d.get("company"))) if t) or None
        posting.posted_at = _when(data.get("createdOn")) or posting.posted_at
        posting.employment_type = (data.get("employmentType") or {}).get("id") or posting.employment_type
        locs = data.get("workLocations") or []
        if locs and not posting.location:
            posting.location = " / ".join(locs)
    elif posting.ats == "workable":
        posting.description = "\n\n".join(
            t for t in (html_to_text(data.get("description")), html_to_text(data.get("requirements"))) if t) or None
        posting.posted_at = _when(data.get("published")) or posting.posted_at
        if data.get("remote"):
            posting.remote = True
    elif posting.ats == "bamboohr":
        job = (data.get("result") or {}).get("jobOpening") or {}
        posting.description = html_to_text(job.get("description"))
        posting.posted_at = _when(job.get("datePosted")) or posting.posted_at
        country = ((job.get("atsLocation") or {}).get("country"))
        if country:
            posting.country = _country(country)
        # "India - Salaried" says where the role is when the location does not.
        if in_india(job.get("employmentStatusLabel")):
            posting.country = "IN"
    elif posting.ats == "eightfold":
        posting.description = html_to_text(data.get("job_description"))
        posting.apply_url = data.get("apply_redirect_url") or posting.apply_url
    elif posting.ats == "workday":
        info = data.get("jobPostingInfo") or {}
        posting.description = html_to_text(info.get("jobDescription"))
        # The real first-posted date, in place of "Posted 3 Days Ago".
        posting.posted_at = _when(info.get("startDate")) or posting.posted_at
        posting.employment_type = info.get("timeType") or posting.employment_type
        posting.location = info.get("location") or posting.location
        posting.apply_url = info.get("externalUrl") or posting.apply_url
        country = (info.get("country") or {}).get("descriptor")
        if country:
            posting.country = _country(country)


# --- Where a posting is -------------------------------------------------------------

INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|hyderabad|secunderabad|pune|chennai|mumbai|navi mumbai|thane|"
    r"gurgaon|gurugram|noida|delhi|ncr|kolkata|ahmedabad|gandhinagar|vadodara|surat|kochi|cochin|"
    r"kerala|thiruvananthapuram|trivandrum|kozhikode|calicut|thrissur|coimbatore|madurai|"
    r"jaipur|chandigarh|mohali|indore|bhopal|nagpur|nashik|lucknow|bhubaneswar|mysuru|mysore|"
    r"mangalore|mangaluru|visakhapatnam|vizag|vijayawada|hubli|goa|karnataka|maharashtra|"
    r"tamil nadu|telangana|andhra pradesh|gujarat|haryana|uttar pradesh|west bengal|odisha)\b",
    re.I)
# The country code, upper case only ("Mohali, IND"); "ind" in lower case is a
# word fragment ("Ind. Estate").
INDIA_CODE = re.compile(r"\bIND\b")
# Philippine "NCR" (Metro Manila) and Indiana.
NOT_INDIA = re.compile(r"\bph\b|philippines|indiana", re.I)
# Remote that names a region India is part of.
OPEN_REGION = re.compile(r"india|apac|asia|anywhere|global|worldwide|world-wide|international", re.I)
# Remote that names no region at all.
NO_REGION = re.compile(r"^[\s,;/()|-]*((fully|100%)?\s*remote|anywhere|wfh|work from home)?[\s,;/()|-]*$", re.I)
# A location that does not say where: decided from the description instead.
VAGUE = re.compile(r"^[\s,;/()|-]*(hybrid|on-?site|in-?office|office|headquarters|hq|multiple( locations)?|"
                   r"various( locations)?|flexible|global|apac|apj|asia( pacific)?|any|tbd|n/?a|"
                   r"locations?|)[\s,;/()|-]*$", re.I)


def in_india(text: str | None) -> bool:
    text = text or ""
    return bool((INDIA.search(text) or INDIA_CODE.search(text)) and not NOT_INDIA.search(text))


def place(p: Posting) -> str:
    """india | remote_open | remote_locked | unknown | elsewhere.

    India on the structured country first, then on the location text. Remote
    counts as open to India only when it says so or names no region; a remote
    job tied to another region is ``remote_locked``, and a foreign city that
    is not remote is ``elsewhere``. A location that says nothing ("Hybrid",
    "Multiple locations", empty) is ``unknown``: its description decides
    (:func:`mentions_india`).
    """
    loc = p.location or ""
    if p.country == "IN" or in_india(loc):
        return "india"
    if p.remote:
        if OPEN_REGION.search(loc) or NO_REGION.match(loc) or VAGUE.match(loc):
            return "remote_open"
        return "remote_locked"
    if VAGUE.match(loc):
        return "unknown"
    return "elsewhere"


def mentions_india(description: str | None) -> bool:
    """The description names India or an Indian city."""
    return in_india(description)


# --- Fields the pipeline expects in JobSpy's form --------------------------------------

_TYPES = (
    (re.compile(r"intern", re.I), "internship"),
    (re.compile(r"part[- ]?time", re.I), "parttime"),
    (re.compile(r"contract|freelance|consult", re.I), "contract"),
    (re.compile(r"temp|seasonal|fixed[- ]?term", re.I), "temporary"),
    (re.compile(r"full[- ]?time|permanent|regular", re.I), "fulltime"),
)


def employment_type(raw: str | None) -> str | None:
    """An ATS employment type in JobSpy's words, or None when not stated."""
    for pattern, value in _TYPES:
        if raw and pattern.search(raw):
            return value
    return None


_SPLIT = re.compile(r"\s*(?:/|;|\|)\s*")


def location_parts(location: str | None) -> list[str]:
    return [x for x in _SPLIT.split(location or "") if x.strip()]


def location_disallowed(p: Posting, regions) -> bool:
    """The operator's excluded regions (Delhi, Gurgaon...) for an ATS posting:
    excluded only when it is not remote and EVERY location it lists is in an
    excluded region. "Pune / Gurugram" is kept; a remote job with a Gurugram
    office is kept."""
    if p.remote:
        return False
    parts = location_parts(p.location)
    regions = [r.casefold() for r in regions]

    def excluded(part: str) -> bool:
        low = part.casefold()
        if not any(r in low for r in regions):
            return False
        # A comma list can name another city too: "Bengaluru, Karnataka,
        # India, Delhi, India" is not Delhi-only.
        others = {m.group(0).casefold() for m in INDIA.finditer(part)} - _NOT_CITIES
        return not any(not any(r in c for r in regions) for c in others)

    return bool(parts) and all(excluded(part) for part in parts)


# Names INDIA matches that are a country or state, not another city.
_NOT_CITIES = {"india", "ncr", "kerala", "karnataka", "maharashtra", "tamil nadu", "telangana",
               "andhra pradesh", "gujarat", "haryana", "uttar pradesh", "west bengal", "odisha", "goa"}


def clean_location(p: Posting) -> str | None:
    """A readable location: empty parts dropped ("bengaluru, , India" ->
    "Bengaluru, India"), duplicates removed, "(Remote)" added when remote."""
    out = []
    for part in location_parts(p.location):
        bits = [b.strip() for b in part.split(",") if b.strip()]
        # SmartRecruiters repeats the address and coordinates; keep the
        # leading place names only.
        bits = [b for b in bits if not re.fullmatch(r"-?\d+(\.\d+)?", b)][:3]
        text = ", ".join(b[:1].upper() + b[1:] if b.islower() else b for b in bits)
        if text and text not in out:
            out.append(text)
    loc = " / ".join(out)
    if p.remote and "remote" not in loc.lower():
        loc = f"{loc} (Remote)" if loc else "Remote"
    return loc or None


# --- To the pipeline's row ------------------------------------------------------------

def to_job(p: Posting, company: str | None = None) -> AllJobs:
    """The ``AllJobs`` row for a posting, shaped like a JobSpy one. ``site`` is
    ``ats:<name>`` so reports can tell the channels apart."""
    return AllJobs(
        job_id=p.job_id,
        company=company or p.company or p.board_id,
        role=p.title.strip(),
        site=f"ats:{p.ats}",
        location=clean_location(p),
        job_url=p.url,
        job_url_direct=p.apply_url or p.url,
        posted_at=p.posted_at,
        jd_text=p.description,
        job_type=employment_type(p.employment_type) or p.employment_type,
        job_level=p.level,
    )
