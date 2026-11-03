"""Board candidates from VC portfolio job boards run on Getro (#162).

Many VC funds publish their portfolio companies' jobs on a Getro board
(jobs.accel.com, ...). Each job links to the company's own ATS posting, so
an India-filtered sweep of a board yields startups' ATS boards that hire in
India. Board ids come from src.scraper.ats.board_from_url and go to
data/ats_probe/extra_ids.jsonl for tools.ats_measure; links that are not a
known ATS are tallied by host in data/ats_probe/getro_other_hosts.json (the
next adapters to consider). Nothing touches the database.

One request at a time, --delay seconds apart (default 1.5).

USAGE
-----
    python -m tools.ats_getro                  # every board in BOARDS
    python -m tools.ats_getro --boards jobs.accel.com careers.peakxv.com
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import requests

OUT = Path("data/ats_probe")
EXTRA = OUT / "extra_ids.jsonl"
OTHER = OUT / "getro_other_hosts.json"
UA = "Mozilla/5.0 (job-application-bot; personal use)"
API = "https://api.getro.com/api/v2/collections/{id}/search/jobs"
PAGE = 100
# Getro boards of funds with India portfolios first, then large global funds.
# A board that is not on Getro (or moved) is skipped with a note.
BOARDS = (
    "careers.peakxv.com", "jobs.blume.vc", "jobs.elevationcapital.com", "jobs.z47.com",
    "jobs.matrixpartners.in", "jobs.nexusvp.com", "jobs.kalaari.com", "jobs.chiratae.com",
    "jobs.3one4capital.com", "jobs.lightspeedvp.com", "jobs.accel.com", "jobs.sequoiacap.com",
    "jobs.a16z.com", "jobs.indexventures.com", "jobs.generalcatalyst.com", "jobs.gv.com",
    "jobs.bvp.com", "jobs.insightpartners.com", "jobs.tigerglobal.com", "jobs.sapphireventures.com",
    "jobs.foundersfund.com", "jobs.greylock.com", "jobs.kleinerperkins.com", "jobs.nea.com",
    "jobs.redpoint.com", "jobs.felicis.com", "jobs.ivp.com", "jobs.battery.com", "jobs.iconiqcapital.com",
    "jobs.northzone.vc", "jobs.balderton.com", "jobs.atomico.com", "jobs.antler.co", "jobs.500.co",
    "jobs.techstars.com", "jobs.saastr.com", "jobs.lsvp.com", "jobs.khoslaventures.com",
)


def network_id(session, board: str) -> str | None:
    """The Getro collection id from a board's page (Next.js data)."""
    try:
        r = session.get(f"https://{board}/jobs", timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return None
    try:
        net = json.loads(m.group(1))["props"]["pageProps"]["network"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(net["id"]) if isinstance(net, dict) and net.get("id") else None


def sweep(session, board: str, cid: str, delay: float, max_pages: int) -> list[dict]:
    jobs, page = [], 0
    while page < max_pages:
        body = {"hitsPerPage": PAGE, "page": page, "filters": {"searchable_locations": ["India"]}, "query": ""}
        try:
            r = session.post(API.format(id=cid), json=body, timeout=60)
        except requests.RequestException:
            break
        time.sleep(delay)
        if r.status_code != 200:
            print(f"  {board}: HTTP {r.status_code} on page {page}", flush=True)
            break
        res = r.json().get("results") or {}
        batch = res.get("jobs") or []
        jobs += batch
        if not batch or len(jobs) >= int(res.get("count") or 0):
            break
        page += 1
    return jobs


def main() -> None:
    from src.scraper.ats import board_from_url

    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", nargs="+", default=list(BOARDS))
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--max-pages", type=int, default=100)
    a = ap.parse_args()
    if a.delay < 1.0:
        raise SystemExit("--delay below 1.0s is not allowed")
    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/json"})
    seen = set()
    if EXTRA.exists():
        seen = {(r["ats"], r["id"].lower()) for r in map(json.loads, EXTRA.read_text().splitlines())}
    other = Counter(json.loads(OTHER.read_text())) if OTHER.exists() else Counter()
    total_new = 0
    for board in a.boards:
        cid = network_id(session, board)
        time.sleep(a.delay)
        if not cid:
            print(f"{board}: not a Getro board (skipped)", flush=True)
            continue
        jobs = sweep(session, board, cid, a.delay, a.max_pages)
        new = 0
        with EXTRA.open("a") as f:
            for j in jobs:
                hit = board_from_url(j.get("url"))
                company = (j.get("organization") or {}).get("name")
                if not hit:
                    host = (urlsplit(j.get("url") or "").hostname or "").lower()
                    if host:
                        other[host] += 1
                    continue
                k = (hit[0], hit[1].lower())
                if k in seen:
                    continue
                seen.add(k)
                new += 1
                f.write(json.dumps({"ats": hit[0], "id": hit[1], "company": company, "source": f"getro:{board}"}) + "\n")
        total_new += new
        print(f"{board}: collection {cid}, {len(jobs)} India jobs, {new} new board candidates", flush=True)
    OTHER.write_text(json.dumps(dict(other.most_common(500)), indent=1))
    print(f"total new candidates: {total_new}; top non-ATS hosts: {other.most_common(15)}", flush=True)


if __name__ == "__main__":
    main()
