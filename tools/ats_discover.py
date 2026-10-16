"""Find which ATS each company's job board is on, by guessing board ids (#97).

For every company name it builds a few candidate board ids ("Acme Technologies
Pvt Ltd" -> acme, acmetechnologies, acme-technologies, ...) and asks each ATS in
``config/ats_patterns.yaml`` (the `guessable`, `confirmed` ones) whether that
board exists, using the hit/miss signals recorded there.

Rate limiting: one worker per ATS, one request at a time, ``--delay`` seconds
apart (default 1.0). A 429 pauses that ATS for 2 minutes, a second one for 10,
a third stops it. Different ATSes run in parallel; none sees more than one
request per second.

Output (all under data/ats_probe/, gitignored - the company list is private):

    probes.jsonl   one line per request, append-only; a rerun skips any
                   (ats, id) already probed, so the run can be stopped and
                   resumed, and new id rules only cost the new ids
    progress.log   per-ATS progress
    boards.json    written by --report: every hit, grouped by company

USAGE
-----
    python -m tools.ats_discover                 # probe everything
    python -m tools.ats_discover --ats lever --limit 20
    python -m tools.ats_discover --report        # summarise probes.jsonl
    python -m tools.ats_discover --commoncrawl   # probe ids from tools.ats_commoncrawl
    python -m tools.ats_discover --upload --dry-run [--include-commoncrawl]
    python -m tools.ats_discover --upload [--include-commoncrawl]

--upload writes ats_probes and ats_boards to Neon in one batch. Each board
gets ``poll`` (India jobs or remote jobs), ``jobs_url``, ``company_key``,
``agency`` and, when data/ats_probe/measure/jobs.json exists, its recent
posting and fit counts - the inputs of the hourly poller.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests

OUT = Path("data/ats_probe")
PROBES = OUT / "probes.jsonl"
# Common Crawl ids are probed into their own file, so a guessing run and a
# Common Crawl run can go at the same time without writing into one file.
PROBE_FILES = (PROBES, OUT / "probes_commoncrawl.jsonl")
UA = "Mozilla/5.0 (job-application-bot; personal use)"
MAX_BODY = 300_000

# Trailing words that are not part of a board id.
SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated",
    "corp", "corporation", "co", "company", "plc", "gmbh", "ag", "sa", "bv",
    "technologies", "technology", "solutions", "services", "systems",
    "software", "labs", "lab", "india", "global", "group", "consulting",
    "international", "in", "of", "the", "and", "center", "centre",
    "acceleration", "north", "america", "research", "development", "com", "io",
}
# Legal-form words only: stripped from the full-name variant.
LEGAL = {"pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated",
         "corp", "corporation", "co", "plc", "gmbh", "ag", "sa", "bv"}
# A first word this generic is not tried as a board id on its own.
GENERIC = {
    "tata", "global", "digital", "smart", "first", "new", "the", "data",
    "tech", "info", "cloud", "american", "national", "united", "general",
    "open", "next", "blue", "red", "green", "bank", "capital", "india",
    "indian", "go", "my", "one", "pay", "jack", "zoho",
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


def squashed(name: str) -> str:
    """The name with punctuation dropped instead of spelled out: S&P -> sp."""
    kept = [w for w in name.split() if re.sub(r"[^a-z]", "", w.lower()) not in LEGAL]
    return re.sub(r"[^a-z0-9]", "", " ".join(kept).lower())


def variants(name: str) -> list[list[str]]:
    """Candidate ids in tiers: tier 0 is the likeliest, probed for every
    company before tier 1 starts."""
    full, core = words(name), core_words(name)
    if not core:
        return [[], []]
    joined = "".join(core)
    legal = list(full)
    while len(legal) > 1 and legal[-1] in LEGAL:
        legal.pop()
    t0 = [joined, "-".join(core), "".join(legal), squashed(name)]
    t1 = []
    if len(core) > 1 and len(core[0]) >= 4 and core[0] not in GENERIC:
        t1.append(core[0])
    if len(core) > 2:
        t1.append("".join(x[0] for x in core))
    t1 += [joined + s for s in ("inc", "hq", "india", "careers", "1", "2")]

    def ok(v: str) -> bool:
        return len(v) >= 3 or (len(v) >= 2 and v == joined)

    seen: set[str] = set()
    tiers = []
    for tier in (t0, t1):
        out = []
        for v in tier:
            if ok(v) and v not in seen:
                seen.add(v)
                out.append(v)
        tiers.append(out)
    return tiers


# --- ATS probes ------------------------------------------------------------
# Each returns (hit, board_name_or_None). hit is True/False, or None when the
# answer says nothing (5xx, timeout) and the id should be retried later.

def _json(body: str):
    try:
        return json.loads(body)
    except ValueError:
        return None


def p_status(hit_codes=(200,), miss_codes=(404,)):
    def f(status, body, final_url, ident):
        if status in hit_codes:
            return True, None
        if status in miss_codes:
            return False, None
        return None, None
    return f


def p_greenhouse(status, body, final_url, ident):
    if status == 200:
        j = _json(body) or {}
        return True, j.get("name")
    return (False, None) if status == 404 else (None, None)


def p_workable(status, body, final_url, ident):
    if status == 200:
        j = _json(body) or {}
        return True, j.get("name")
    return (False, None) if status == 404 else (None, None)


def p_smartrecruiters(status, body, final_url, ident):
    if status == 200:
        j = _json(body) or {}
        found = j.get("totalFound", 0) > 0
        name = None
        if found and j.get("content"):
            name = (j["content"][0].get("company") or {}).get("name")
        return found, name
    return None, None


def p_recruitee(status, body, final_url, ident):
    if status == 403 and "Public API disabled" in body:
        return True, None
    if status == 200 and '"offers"' in body:
        name = None
        j = _json(body) or {}
        if j.get("offers"):
            name = j["offers"][0].get("company_name")
        return True, name
    return (False, None) if status == 404 else (None, None)


def p_redirect_miss(status, body, final_url, ident):
    if status == 200:
        return True, None
    if 300 <= status < 400:
        return False, None
    return (False, None) if status == 404 else (None, None)


def p_breezy(status, body, final_url, ident):
    if status == 200 and body.lstrip().startswith("["):
        j = _json(body) or []
        name = (j[0].get("company") or {}).get("name") if j else None
        return True, name
    return (False, None) if status == 404 else (None, None)


def p_teamtailor(status, body, final_url, ident):
    if status == 200 and "<rss" in body:
        m = re.search(r"<channel>\s*<title>([^<]*)</title>", body)
        return True, m.group(1).strip() if m else None
    return (False, None) if status == 404 else (None, None)


def p_eightfold(status, body, final_url, ident):
    if status == 200:
        j = _json(body)
        return (j is not None and "positions" in j), None
    if status == 403 and "Not authorized" in body:
        return True, None
    return (False, None) if status in (404, -1) else (None, None)


ATS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{id}", p_greenhouse, True),
    "lever": ("https://api.lever.co/v0/postings/{id}?limit=1&mode=json", p_status(), True),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{id}", p_status(), True),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=1", p_smartrecruiters, True),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{id}", p_workable, True),
    "recruitee": ("https://{id}.recruitee.com/api/offers", p_recruitee, True),
    "bamboohr": ("https://{id}.bamboohr.com/careers/list", p_redirect_miss, False),
    "breezy": ("https://{id}.breezy.hr/json", p_breezy, True),
    "teamtailor": ("https://{id}.teamtailor.com/jobs.rss", p_teamtailor, True),
    "jazzhr": ("https://{id}.applytojob.com/apply", p_redirect_miss, False),
    "rippling": ("https://api.rippling.com/platform/api/ats/v1/board/{id}/jobs", p_status(), True),
    "manatal": ("https://api.manatal.com/open/v3/career-page/{id}/jobs/", p_status(), True),
    "eightfold": ("https://{id}.eightfold.ai/api/apply/v2/jobs?domain={id}.com&num=1", p_eightfold, True),
}

# Subdomain ATSes: an id must be a valid DNS label.
SUBDOMAIN = {"recruitee", "bamboohr", "breezy", "teamtailor", "jazzhr", "eightfold"}


# --- Job lists -----------------------------------------------------------------
# Once a board exists its job list is fetched once: (url, path to the job list,
# path to a total count when the list is paged).
JOBS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{id}/jobs", ["jobs"], None),
    "lever": ("https://api.lever.co/v0/postings/{id}?mode=json", [], None),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{id}", ["jobs"], None),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100", ["content"], ["totalFound"]),
    "workable": ("https://apply.workable.com/api/v1/widget/accounts/{id}", ["jobs"], None),
    "recruitee": ("https://{id}.recruitee.com/api/offers", ["offers"], None),
    "bamboohr": ("https://{id}.bamboohr.com/careers/list", ["result"], None),
    "breezy": ("https://{id}.breezy.hr/json", [], None),
    "teamtailor": ("https://{id}.teamtailor.com/jobs.rss", None, None),
    "jazzhr": ("https://{id}.applytojob.com/apply", None, None),
    "rippling": ("https://api.rippling.com/platform/api/ats/v1/board/{id}/jobs", [], None),
    "manatal": ("https://api.manatal.com/open/v3/career-page/{id}/jobs/", ["results"], ["count"]),
    "eightfold": ("https://{id}.eightfold.ai/api/apply/v2/jobs?domain={id}.com&num=100", ["positions"], ["count"]),
}
# SmartRecruiters filters by country server-side, so its India count is exact
# even past the first 100 jobs.
SR_INDIA = "https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=1&country=in"
INDIA = re.compile(r"\b(india|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurgaon|gurugram|"
                   r"noida|delhi|kolkata|ahmedabad|kochi|coimbatore|jaipur|chandigarh|"
                   r"thiruvananthapuram|trivandrum|indore|mysuru|mysore|visakhapatnam)\b", re.I)
JOBS_BODY = 20_000_000
LOC_KEYS = re.compile(r"location|country|city|state|region|address|office", re.I)
REMOTE_KEYS = re.compile(r"remote|telecommut|workplace|location_type|work_location", re.I)


def _dig(j, path):
    for k in path:
        j = j.get(k) if isinstance(j, dict) else None
    return j


def _strings(obj, keyed=False, depth=0):
    """Every string under location-like keys of one job (recursive)."""
    out = []
    if depth > 4:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "question" in k:
                continue
            hit = keyed or bool(LOC_KEYS.search(k))
            if isinstance(v, str) and hit:
                out.append(v)
            elif isinstance(v, (dict, list)):
                out += _strings(v, hit, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, str) and keyed:
                out.append(v)
            else:
                out += _strings(v, keyed, depth + 1)
    return out


def _is_remote(job) -> bool:
    if not isinstance(job, dict):
        return False
    for k, v in job.items():
        if REMOTE_KEYS.search(k):
            if v is True or (isinstance(v, str) and "remote" in v.lower()):
                return True
            if isinstance(v, dict) and _is_remote(v):
                return True
    return any("remote" in s.lower() for s in _strings(job))


# The ATS's own "first posted" field, in preference order. Never an updated-at
# field: an edit would make an old job look new.
DATE_KEYS = ("first_published", "publishedAt", "published_at", "releasedDate",
             "createdAt", "created_at", "t_create", "datePosted")
COUNTRY_KEYS = re.compile(r"^(country|country_code|countryCode|addressCountry)$")


def _posted(job) -> datetime | None:
    for k in DATE_KEYS:
        v = job.get(k) if isinstance(job, dict) else None
        if v in (None, ""):
            continue
        try:
            if isinstance(v, (int, float)):
                return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" UTC", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except (ValueError, OSError, OverflowError):
            continue
    return None


def _country_india(obj, depth=0) -> bool:
    """A structured country field saying India ("IN", "IND", "India")."""
    if depth > 4:
        return False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if COUNTRY_KEYS.match(k) and isinstance(v, str) and v.strip().lower() in ("in", "ind", "india"):
                return True
            if isinstance(v, (dict, list)) and _country_india(v, depth + 1):
                return True
    elif isinstance(obj, list):
        return any(_country_india(v, depth + 1) for v in obj)
    return False


def job_records(ats: str, body: str) -> list[dict] | None:
    """Per job: location text, remote (None = unknown), india, posted time.
    None when the list can't be read."""
    if ats == "teamtailor":
        out = []
        for it in re.findall(r"<item>(.*?)</item>", body, re.S):
            locs = re.findall(r"<tt:(?:city|country|name)>([^<]*)</tt:", it)
            text = ", ".join(locs) or " ".join(re.findall(r"<title>([^<]*)</title>", it))
            m = re.search(r"<pubDate>([^<]*)</pubDate>", it)
            posted = None
            if m:
                try:
                    posted = parsedate_to_datetime(m.group(1))
                except (TypeError, ValueError):
                    pass
            # The feed says "remote" for every job, so remote is unknown.
            out.append({"loc": text, "remote": None, "india": bool(INDIA.search(text)), "posted": posted})
        return out
    path = JOBS[ats][1]
    if path is None:
        return None
    jobs = _dig(_json(body), path)
    if not isinstance(jobs, list):
        return None
    out = []
    for j in jobs:
        text = ", ".join(dict.fromkeys(s.strip() for s in _strings(j) if s.strip()))[:200]
        out.append({"loc": text, "remote": _is_remote(j),
                    "india": bool(INDIA.search(text)) or _country_india(j), "posted": _posted(j)})
    return out


def board_details(session, ats, ident, follow, probe_status, delay):
    """State, job counts and a location breakdown for a board that exists."""
    if probe_status == 403:
        return {"state": "no_api"}
    url, _, total_path = JOBS[ats]
    status, body, _ = fetch(session, _url(url, ats, ident), follow, JOBS_BODY)
    if status != 200:
        return {"state": "unknown", "jobs_status": status}
    locs = job_records(ats, body)
    if locs is None:  # JazzHR (HTML): count job links, India by mention
        n = len(set(re.findall(r"/apply/([A-Za-z0-9]{6,})/", body)))
        india = 1 if INDIA.search(body) else 0
        details = {"job_count": n, "india_job_count": None, "remote_job_count": None,
                   "locations": None}
    else:
        total = _dig(_json(body), total_path) if total_path else None
        n = total if isinstance(total, int) else len(locs)
        india = sum(1 for j in locs if j["india"])
        counts: dict[str, int] = defaultdict(int)
        for j in locs:
            counts[j["loc"] or "(none)"] += 1
        now = datetime.now(timezone.utc)

        def recent(sub):
            ages = [(now - j["posted"]).total_seconds() / 3600 for j in sub if j["posted"]]
            return {"24h": sum(a <= 24 for a in ages), "48h": sum(a <= 48 for a in ages),
                    "7d": sum(a <= 168 for a in ages), "dated": len(ages)}

        details = {"job_count": n, "india_job_count": india,
                   "remote_job_count": None if ats == "teamtailor" else sum(1 for j in locs if j["remote"]),
                   # Jobs posted recently, by the ATS's own first-posted date.
                   "posted": recent(locs),
                   "posted_india": recent([j for j in locs if j["india"]]),
                   "posted_remote": recent([j for j in locs if j["remote"]]),
                   # Jobs the counts were read from; below job_count when the
                   # ATS pages its list (Eightfold, SmartRecruiters, Manatal).
                   "jobs_sampled": len(locs),
                   "locations": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:25])}
        if ats == "smartrecruiters" and n > len(locs):
            time.sleep(delay)
            s2, b2, _ = fetch(session, _url(SR_INDIA, ats, ident), follow)
            t2 = _dig(_json(b2), ["totalFound"]) if s2 == 200 else None
            if isinstance(t2, int):
                india = details["india_job_count"] = t2
    remote = details.get("remote_job_count") or 0
    if not n:
        state = "empty"
    elif india:
        state = "active_india"
    elif remote:
        state = "active_remote"  # no India jobs, but remote ones (any country)
    else:
        state = "active_other"
    details["state"] = state
    return details


# --- Confirmation ------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def confirm(company: str | None, ident: str, board_name: str | None, body: str) -> str | None:
    """strong: the board names this company; weak: the id exists but nothing
    ties it to the company (e.g. a short first-word id). None for Common Crawl
    ids, which come with no company."""
    if not company:
        return None
    ck = key(company)
    if board_name:
        bk = key(board_name)
        if bk and (bk == ck or difflib.SequenceMatcher(None, bk, ck).ratio() >= 0.85
                   or (len(ck) >= 5 and (ck in bk or bk in ck))):
            return "strong"
        return "weak"
    text = _norm(body[:MAX_BODY])
    if len(ck) >= 4 and ck in text:
        return "strong"
    if ident == ck and len(ck) >= 6:
        return "strong"
    return "weak"


# --- Local store ----------------------------------------------------------------
# Everything is written to data/ats_probe/ first. Neon is written only by
# --upload, in one batch, after the run (free-tier storage and compute).

def load_companies() -> list[dict]:
    seen: dict[str, dict] = {}
    db = OUT / "companies_db.json"
    if db.exists():
        for r in json.loads(db.read_text()):
            k = key(r["name"])
            if k and k not in seen:
                seen[k] = {"name": r["name"], "source": "scraped", "jobs": r["jobs"]}
    extra = OUT / "companies_extra.txt"
    if extra.exists():
        for line in extra.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k = key(line)
            if k and k not in seen:
                seen[k] = {"name": line, "source": "extra", "jobs": 0}
    return list(seen.values())


def load_rows() -> list[dict]:
    rows = []
    for f in PROBE_FILES:
        if f.exists():
            rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    return rows


def _current(r: dict) -> bool:
    """A hit recorded by this version: it has job counts, or it is a board
    whose API is off (which has none to count)."""
    return "india_job_count" in r or r.get("state") == "no_api"


def load_done() -> dict[tuple[str, str], dict]:
    """(ats, id) already answered. A hit only counts once it has the current
    details (india_job_count), so older hits are re-probed."""
    done = {}
    for r in load_rows():
        if r["hit"] is False or (r["hit"] and _current(r)):
            done[(r["ats"], r["id"])] = r
    return done


_lock = threading.Lock()
_target = PROBES  # set to the Common Crawl file by --commoncrawl


def record(row: dict) -> None:
    with _lock, _target.open("a") as f:
        f.write(json.dumps(row) + "\n")


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    with _lock, (OUT / "progress.log").open("a") as f:
        f.write(line + "\n")


def _url(template: str, ats: str, ident: str) -> str:
    return template.format(id=ident if ats in SUBDOMAIN else quote(ident, safe=""))


def fetch(session, url, follow, cap=MAX_BODY):
    try:
        r = session.get(url, timeout=30, allow_redirects=follow, stream=True)
        body = r.raw.read(cap, decode_content=True).decode("utf-8", "replace")
        r.close()
        return r.status_code, body, r.url
    except requests.exceptions.ConnectionError:
        return -1, "", url
    except requests.exceptions.RequestException:
        return -2, "", url


# --- Probe loop -------------------------------------------------------------------

def guess_pairs(companies: list[dict]) -> list[list[tuple[dict, str]]]:
    return [[(c, v) for c in companies for v in variants(c["name"])[t]] for t in (0, 1)]


def crawl_pairs(ats: str) -> list[list[tuple[dict, str]]]:
    """Common Crawl ids for this ATS, from data/ats_probe/commoncrawl_ids.jsonl."""
    f = OUT / "commoncrawl_ids.jsonl"
    if not f.exists():
        return [[]]
    ids = sorted({r["id"] for r in map(json.loads, f.read_text().splitlines()) if r["ats"] == ats})
    return [[({"name": None, "source": "commoncrawl"}, i) for i in ids]]


def worker(ats: str, tiers, done: dict, delay: float, limit: int | None):
    template, parse, follow = ATS[ats]
    session = requests.Session()
    session.headers["User-Agent"] = UA
    strong_found: set[str] = {r["company_key"] for (a, _), r in done.items()
                              if a == ats and r.get("confidence") == "strong"
                              and str(r.get("state", "")).startswith("active")}
    pauses, n, hits = 0, 0, 0
    for t, pairs in enumerate(tiers):
        log(f"{ats}: tier {t}, {len(pairs)} candidates")
        for company, ident in pairs:
            ck = key(company["name"]) if company["name"] else None
            if (ck and ck in strong_found) or (ats, ident) in done:
                continue
            if ats in SUBDOMAIN and not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", ident):
                continue
            if limit is not None and n >= limit:
                return
            status, body, final = fetch(session, _url(template, ats, ident), follow)
            n += 1
            if status == 429:
                pauses += 1
                if pauses >= 3:
                    log(f"{ats}: third 429 - stopping this ATS")
                    return
                wait = 120 if pauses == 1 else 600
                log(f"{ats}: 429 #{pauses}, pausing {wait}s")
                time.sleep(wait)
                continue
            if ats != "eightfold" and status == -1:
                hit, name = None, None
            else:
                hit, name = parse(status, body, final, ident)
            conf = confirm(company["name"], ident, name, body) if hit else None
            details = {}
            if hit:
                time.sleep(delay)
                details = board_details(session, ats, ident, follow, status, delay)
                n += 1
            row = {"ats": ats, "id": ident, "company": company["name"], "company_key": ck,
                   "source": company["source"], "tier": t, "status": status, "hit": hit,
                   "board_name": name, "confidence": conf, **details,
                   "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            record(row)
            if hit is not None:
                done[(ats, ident)] = row
            if hit:
                hits += 1
                if ck and conf == "strong" and details.get("state", "").startswith("active"):
                    strong_found.add(ck)
            if n % 200 == 0:
                log(f"{ats}: {n} requests, {hits} hits")
            time.sleep(delay)
    log(f"{ats}: finished, {n} requests, {hits} hits")


# --- Report -----------------------------------------------------------------------

STATES = ("active_india", "active_remote", "active_other", "empty", "no_api", "unknown")
USABLE = ("active_india", "active_remote")


def latest(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """The last answer per (ats, id); hits only once they carry details."""
    out = {}
    for r in rows:
        if r["hit"] is None or (r["hit"] and not _current(r)):
            continue
        out[(r["ats"], r["id"])] = r
    return out


def report() -> None:
    """Coverage per ATS and board state, ATSes with no usable boards, and
    companies on several ATSes. "usable" = the board names this company
    (strong) and has open jobs in India."""
    companies = load_companies()
    rows = load_rows()
    last = latest(rows)
    hits = [r for r in last.values() if r["hit"]]
    by_company: dict[str, list[dict]] = defaultdict(list)
    for r in hits:
        if r.get("company_key"):
            by_company[r["company_key"]].append(r)

    def usable(x):
        return x.get("confidence") == "strong" and x["state"] in USABLE

    def pct(n, d):
        return f"{n} ({100 * n / d:.1f}%)" if d else "0"

    boards = {c["name"]: by_company[key(c["name"])] for c in companies if by_company.get(key(c["name"]))}
    print("Companies with a usable board (names the company, open jobs in India or remote):")
    for src in ("all", "scraped", "extra"):
        names = {c["name"] for c in companies if src == "all" or c["source"] == src}
        u = {n for n, b in boards.items() if n in names and any(usable(x) for x in b)}
        active = {n for n, b in boards.items() if n in names
                  and any(x.get("confidence") == "strong" and x["state"].startswith("active") for x in b)}
        print(f"  {src:8} {len(names):5} companies   usable {pct(len(u), len(names)):>12}"
              f"   active anywhere {pct(len(active), len(names)):>12}")

    probed = defaultdict(int)
    for r in rows:
        probed[r["ats"]] += 1
    print(f"\n{'ats':16}{'requests':>9}{'usable':>8}" + "".join(f"{s:>13}" for s in STATES)
          + f"{'india jobs':>12}")
    for ats in ATS:
        hs = [h for h in hits if h["ats"] == ats and h["source"] != "commoncrawl"]
        u = {h["company_key"] for h in hs if usable(h)}
        cells = "".join(f"{sum(1 for h in hs if h['state'] == s):>13}" for s in STATES)
        ij = sum(h.get("india_job_count") or 0 for h in hs if usable(h))
        print(f"{ats:16}{probed[ats]:>9}{len(u):>8}{cells}{ij:>12}")

    cc = [h for h in hits if h["source"] == "commoncrawl"]
    if cc:
        print(f"\nCommon Crawl boards: {len(cc)}")
        for ats in sorted({h['ats'] for h in cc}):
            hs = [h for h in cc if h["ats"] == ats]
            cells = "  ".join(f"{s} {sum(1 for h in hs if h['state'] == s)}" for s in STATES)
            ij = sum(h.get("india_job_count") or 0 for h in hs)
            print(f"  {ats:16}{len(hs):>6}  {cells}  india jobs {ij}")

    multi = {n: sorted({x["ats"] for x in b if usable(x)}) for n, b in boards.items()}
    multi = {n: a for n, a in multi.items() if len(a) > 1}
    print(f"\ncompanies with usable boards on more than one ATS: {len(multi)}")
    for n, a in sorted(multi.items()):
        print(f"  {n}: {', '.join(a)}")


# --- Upload (run only after the user approves) ----------------------------------------

MEASURE = OUT / "measure" / "jobs.json"
TECH = {"software", "backend", "frontend", "fullstack", "mobile", "data", "ml", "devops", "sdet", "quant"}
# Remote open to India: says so, or names no region at all.
OPEN_REMOTE = re.compile(r"india|apac|asia|anywhere|global|worldwide|world-wide|international", re.I)
NO_REGION = re.compile(r"^[\s,;/()|-]*((fully|100%)?\s*remote|anywhere|wfh|work from home)?[\s,;/()|-]*$", re.I)
AGENCY = re.compile(r"staffing|recruitment|\brecruit\b|recruit solve|crisp recruit|boutique recruiting|manpower|"
                    r"headhunt|outsourc|endeavor it solution", re.I)
# An employer's own campus or university board, not an agency.
NOT_AGENCY = re.compile(r"campus|university|\binterns?\b|internship|external care", re.I)


def measured() -> tuple[dict[tuple[str, str], dict], datetime | None]:
    """Per-board counts from the last measurement run (jobs.json), if any."""
    if not MEASURE.exists():
        return {}, None
    at = datetime.fromtimestamp(MEASURE.stat().st_mtime, timezone.utc)
    stats: dict[tuple[str, str], dict] = {}
    for j in json.loads(MEASURE.read_text()):
        s = stats.setdefault((j["ats"], j["board"]), {
            "remote_open_jobs": 0, "remote_locked_jobs": 0, "posted_24h": 0, "posted_7d": 0,
            "target_posted_7d": 0, "fits_7d": 0, "last_posted_at": None})
        target = bool(j["india"])
        if j["remote"] and not j["india"]:
            if OPEN_REMOTE.search(j["loc"] or "") or NO_REGION.match(j["loc"] or ""):
                s["remote_open_jobs"] += 1
                target = True
            else:
                s["remote_locked_jobs"] += 1
        age = j["age_h"]
        if j["posted"] and (s["last_posted_at"] is None or j["posted"] > s["last_posted_at"]):
            s["last_posted_at"] = j["posted"]
        if age is None or age > 168:
            continue
        s["posted_7d"] += 1
        s["posted_24h"] += age <= 24
        if target:
            s["target_posted_7d"] += 1
            if j["family"] in TECH and (j["years"] is None or j["years"] <= 6):
                s["fits_7d"] += 1
    return stats, at


def jobs_url(ats: str, ident: str) -> str | None:
    return _url(JOBS[ats][0], ats, ident) if ats in JOBS else None


def upload(dry_run: bool, include_commoncrawl: bool) -> None:
    """One batch upsert of the probes and boards into Neon. Common Crawl rows
    are left out unless asked for: they can be far larger than the guesses,
    so their size is checked (--dry-run) and approved first."""
    last = {k: r for k, r in latest(load_rows()).items()
            if include_commoncrawl or r["source"] != "commoncrawl"}
    probes = [{"ats": r["ats"], "board_id": r["id"], "source": r["source"] if r["source"] == "commoncrawl" else "guess",
               "company": r["company"], "hit": r["hit"], "http_status": r["status"],
               "probed_at": r["ts"]} for r in last.values()]
    boards = [{"ats": r["ats"], "board_id": r["id"], "source": r["source"] if r["source"] == "commoncrawl" else "guess",
               "board_name": r["board_name"], "company": r["company"], "confidence": r["confidence"],
               "state": r["state"], "job_count": r.get("job_count"), "india_job_count": r.get("india_job_count"),
               "remote_job_count": r.get("remote_job_count"), "locations": r.get("locations"),
               "checked_at": r["ts"]} for r in last.values() if r["hit"]]
    stats, at = measured()
    for b in boards:
        name = b["board_name"] or b["company"] or b["board_id"]
        b["company_key"] = key(name) or b["board_id"]
        b["jobs_url"] = jobs_url(b["ats"], b["board_id"])
        b["poll"] = bool(b["india_job_count"] or b["remote_job_count"]) and b["state"] != "no_api"
        b["agency"] = bool(AGENCY.search(name)) and not NOT_AGENCY.search(name)
        m = stats.get((b["ats"], b["board_id"]))
        b.update(m or dict.fromkeys(("remote_open_jobs", "remote_locked_jobs", "posted_24h", "posted_7d",
                                     "target_posted_7d", "fits_7d", "last_posted_at"), None))
        b["measured_at"] = at if m else None
    print(f"poll: {sum(b['poll'] for b in boards)}   measured: {sum(b['measured_at'] is not None for b in boards)}"
          f"   agency: {sum(b['agency'] for b in boards)}   fits_7d: {sum(b['fits_7d'] or 0 for b in boards)}")
    size = len(json.dumps(probes)) + len(json.dumps(boards, default=str))
    print(f"ats_probes rows: {len(probes)}   ats_boards rows: {len(boards)}   ~{size / 1e6:.1f} MB as JSON")
    if dry_run:
        return
    from sqlalchemy.dialects.postgresql import insert

    from src.state.db import session_scope
    from src.state.models import AtsBoard, AtsProbe

    def upsert(session, model, rows, keys):
        for i in range(0, len(rows), 1000):
            chunk = rows[i:i + 1000]
            stmt = insert(model).values(chunk)
            cols = {c: stmt.excluded[c] for c in chunk[0] if c not in keys}
            session.execute(stmt.on_conflict_do_update(index_elements=keys, set_=cols))

    with session_scope() as s:
        upsert(s, AtsProbe, probes, ["ats", "board_id"])
        upsert(s, AtsBoard, boards, ["ats", "board_id"])
    print("uploaded")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", nargs="*", default=list(ATS))
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=None, help="max requests per ATS")
    ap.add_argument("--commoncrawl", action="store_true",
                    help="probe ids from commoncrawl_ids.jsonl instead of guessing")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--upload", action="store_true", help="write everything to Neon in one batch")
    ap.add_argument("--dry-run", action="store_true", help="with --upload: print sizes only")
    ap.add_argument("--include-commoncrawl", action="store_true",
                    help="with --upload: include Common Crawl rows (check the size first)")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.report:
        report()
        return
    if a.upload:
        upload(a.dry_run, a.include_commoncrawl)
        return
    if a.delay < 1.0:
        raise SystemExit("--delay below 1.0s is not allowed")
    if a.commoncrawl:
        global _target
        _target = PROBE_FILES[1]
    done = load_done()
    companies = [] if a.commoncrawl else load_companies()
    log(f"start: {'commoncrawl' if a.commoncrawl else f'{len(companies)} companies'}, "
        f"{len(done)} probes cached, ats={a.ats}")
    threads = [threading.Thread(target=worker, args=(
        x, crawl_pairs(x) if a.commoncrawl else guess_pairs(companies), done, a.delay, a.limit))
        for x in a.ats]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log("all done")


if __name__ == "__main__":
    main()
