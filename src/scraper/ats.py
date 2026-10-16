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
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from src.state.models import AllJobs

ATSES = ("greenhouse", "lever", "ashby", "smartrecruiters")

# SmartRecruiters pages hold at most 100 postings.
SR_PAGE = 100


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
        return f"{self.ats}-{self.board_id}-{self.posting_id}"


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
    """The endpoint that lists a board's open postings."""
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
    raise ValueError(f"no adapter for {ats}")


def detail_url(posting: Posting) -> str | None:
    """Where the description is, for lists that leave it out; else None."""
    if posting.description is not None:
        return None
    b = quote(posting.board_id, safe="")
    if posting.ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{b}/jobs/{posting.posting_id}"
    if posting.ats == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{b}/postings/{posting.posting_id}"
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


_PARSERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
            "smartrecruiters": _smartrecruiters}


def parse_list(ats: str, board_id: str, body: str) -> list[Posting]:
    """A board's postings from its list response. Raises ValueError when the
    body is not the expected JSON, so a changed API fails loudly per board."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ValueError(f"{ats}/{board_id}: not JSON") from exc
    expected = list if ats == "lever" else dict
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


# A region lock written in the title (#114): "(US-Based)", "US only",
# "Remote - Canada", "Americas". APAC and Asia are not locks: India is in them.
_LOCK_REGION = r"(?:us|usa|u\.s\.?|united states|uk|united kingdom|canada|eu|europe|emea|latam|americas?|" \
               r"north america|mexico|brazil|germany|france|spain|poland|australia|japan|singapore|philippines)"
TITLE_LOCK = re.compile(
    rf"\(\s*{_LOCK_REGION}(?:[- ]?(?:based|only|remote))?\s*\)"
    rf"|\b{_LOCK_REGION}[- ](?:based|only)\b"
    rf"|\bremote\s*[-,(/:]?\s*{_LOCK_REGION}\b"
    rf"|\b{_LOCK_REGION}\s*[-,(/:]?\s*remote\b"
    r"|\b(?:must|need to) be (?:based|located) in (?!india)", re.I)


def title_locked(title: str | None) -> bool:
    """The title ties the job to a region outside India."""
    return bool(title and TITLE_LOCK.search(title) and not in_india(title))


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
        if title_locked(p.title):
            return "remote_locked"
        if OPEN_REGION.search(loc) or NO_REGION.match(loc) or VAGUE.match(loc):
            return "remote_open"
        return "remote_locked"
    if VAGUE.match(loc):
        return "elsewhere" if title_locked(p.title) else "unknown"
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
