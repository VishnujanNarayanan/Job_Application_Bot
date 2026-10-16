"""Collect ATS board ids from the Common Crawl URL index (#97).

Common Crawl publishes an index of every URL each of its crawls fetched. Asking
it for every URL under ``jobs.ashbyhq.com/`` (etc.) and keeping the first path
segment gives board ids for companies we have never heard of. Each crawl sees
only part of the web, so several crawls are combined.

Writes data/ats_probe/commoncrawl_ids.jsonl (local only, gitignored); probe
the ids afterwards with ``python -m tools.ats_discover --commoncrawl``.
Nothing here touches the database.

The index server is shared and often overloaded (504s). One request at a time,
``--delay`` seconds apart (default 10), and a timed-out request backs off
30s, 60s, 120s, 240s, 480s before that page is skipped. Finished pages are
recorded in commoncrawl_done.json, so a rerun resumes where it stopped.

USAGE
-----
    python -m tools.ats_commoncrawl                 # 12 most recent crawls
    python -m tools.ats_commoncrawl --crawls 3
    python -m tools.ats_commoncrawl --summary       # ids per ATS so far
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests

OUT = Path("data/ats_probe")
IDS = OUT / "commoncrawl_ids.jsonl"
DONE = OUT / "commoncrawl_done.json"
INDEX = "https://index.commoncrawl.org"
UA = "job-application-bot (personal research)"
BACKOFF = (30, 60, 120, 240, 480)

HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.smartrecruiters.com": "smartrecruiters",
}
# First path segments that are pages of the ATS itself, not boards.
NOT_BOARDS = {
    "robots.txt", "sitemap.xml", "favicon.ico", "embed", "api", "static", "assets",
    "jobs", "job_app", "oauth", "login", "search", "v1", "_next", "cdn-cgi", "",
    "privacy", "terms", "apple-touch-icon.png", "manifest.json",
}


def board_id(url: str) -> str | None:
    u = urlparse(url)
    seg = unquote(u.path.lstrip("/").split("/", 1)[0]).strip().lower()
    if seg == "embed":  # boards.greenhouse.io/embed/job_board?for=<id>
        seg = (parse_qs(u.query).get("for") or [""])[0].strip().lower()
    if seg in NOT_BOARDS or len(seg) < 2 or len(seg) > 80:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9 ._&'-]*", seg):
        return None
    return seg


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with (OUT / "commoncrawl.log").open("a") as f:
        f.write(line + "\n")


def get(session, url, params, delay):
    """GET with the backoff schedule; None once it is exhausted."""
    for wait in (0, *BACKOFF):
        if wait:
            log(f"  busy, retrying in {wait}s")
            time.sleep(wait)
        try:
            r = session.get(url, params=params, timeout=180)
        except requests.RequestException as exc:
            log(f"  error: {type(exc).__name__}")
            continue
        finally:
            time.sleep(delay)
        if r.status_code == 200:
            return r
        if r.status_code == 404:  # no captures for this host in this crawl
            return r
        if r.status_code not in (429, 500, 502, 503, 504):
            log(f"  HTTP {r.status_code}, giving up on this request")
            return None
    return None


def collect(n_crawls: int, delay: float) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = UA
    done = set(json.loads(DONE.read_text())) if DONE.exists() else set()
    seen = set()
    if IDS.exists():
        seen = {(r["ats"], r["id"]) for r in map(json.loads, IDS.read_text().splitlines())}
    r = get(session, f"{INDEX}/collinfo.json", None, delay)
    if r is None:
        raise SystemExit("index server unavailable")
    crawls = [c["id"] for c in r.json()[:n_crawls]]
    log(f"crawls: {crawls}")
    for crawl in crawls:
        api = f"{INDEX}/{crawl}-index"
        for host, ats in HOSTS.items():
            if f"{crawl}|{host}|all" in done:
                continue
            r = get(session, api, {"url": f"{host}/*", "output": "json", "showNumPages": "true"}, delay)
            if r is None:
                log(f"{crawl} {host}: page count unavailable, skipped")
                continue
            if r.status_code == 404:
                pages = 0
            else:
                try:
                    pages = int(json.loads(r.text.splitlines()[0])["pages"])
                except (ValueError, KeyError, IndexError):
                    log(f"{crawl} {host}: unreadable page count, skipped")
                    continue
            new = 0
            complete = True
            for page in range(pages):
                tag = f"{crawl}|{host}|{page}"
                if tag in done:
                    continue
                r = get(session, api, {"url": f"{host}/*", "output": "json", "fl": "url",
                                       "page": str(page)}, delay)
                if r is None:
                    complete = False
                    log(f"{crawl} {host}: page {page} failed, will retry on rerun")
                    continue
                with IDS.open("a") as f:
                    for line in r.text.splitlines():
                        try:
                            ident = board_id(json.loads(line)["url"])
                        except (ValueError, KeyError):
                            continue
                        if ident and (ats, ident) not in seen:
                            seen.add((ats, ident))
                            new += 1
                            f.write(json.dumps({"ats": ats, "id": ident, "crawl": crawl, "host": host}) + "\n")
                done.add(tag)
                DONE.write_text(json.dumps(sorted(done)))
            if complete:
                done.add(f"{crawl}|{host}|all")
                DONE.write_text(json.dumps(sorted(done)))
            log(f"{crawl} {host}: {pages} pages, {new} new ids")
    summary()


def summary() -> None:
    counts: dict[str, int] = defaultdict(int)
    if IDS.exists():
        for r in map(json.loads, IDS.read_text().splitlines()):
            counts[r["ats"]] += 1
    log("ids so far: " + json.dumps(dict(counts)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawls", type=int, default=12)
    ap.add_argument("--delay", type=float, default=10.0)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.summary:
        summary()
        return
    if a.delay < 5:
        raise SystemExit("--delay below 5s is not allowed for the shared index server")
    collect(a.crawls, a.delay)


if __name__ == "__main__":
    main()
