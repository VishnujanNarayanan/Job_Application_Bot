"""Board candidates from Hacker News "Ask HN: Who is hiring?" threads (#162).

Startups post a comment a month with a link to their job board; the links
that name an ATS board (src.scraper.ats.board_from_url) become candidates in
data/ats_probe/extra_ids.jsonl for tools.ats_measure. Uses HN's public
Algolia API; one request at a time, --delay seconds apart. Nothing touches
the database.

USAGE
-----
    python -m tools.ats_hn                 # the last 24 monthly threads
    python -m tools.ats_hn --months 6
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from pathlib import Path

import requests

OUT = Path("data/ats_probe")
EXTRA = OUT / "extra_ids.jsonl"
ALGOLIA = "https://hn.algolia.com/api/v1"
UA = "job-application-bot (personal research)"
_URL = re.compile(r'https?://[^\s"<>\')]+')


def threads(session, months: int) -> list[int]:
    r = session.get(f"{ALGOLIA}/search_by_date", timeout=60, params={
        "tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": months * 3})
    r.raise_for_status()
    hits = [h for h in r.json()["hits"] if (h.get("title") or "").lower().startswith("ask hn: who is hiring")]
    return [int(h["objectID"]) for h in hits[:months]]


def urls_in(item: dict) -> list[str]:
    out = []
    for child in item.get("children") or []:
        text = html.unescape(child.get("text") or "")
        out += _URL.findall(text.replace("<p>", " ").replace("&#x2F;", "/"))
    return out


def main() -> None:
    from src.scraper.ats import board_from_url

    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--delay", type=float, default=2.0)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = UA
    seen = set()
    if EXTRA.exists():
        seen = {(r["ats"], r["id"].lower()) for r in map(json.loads, EXTRA.read_text().splitlines())}
    total = 0
    for tid in threads(session, a.months):
        time.sleep(a.delay)
        try:
            r = session.get(f"{ALGOLIA}/items/{tid}", timeout=120)
            r.raise_for_status()
        except requests.RequestException as exc:
            print(f"thread {tid}: {type(exc).__name__}", flush=True)
            continue
        item = r.json()
        new = 0
        with EXTRA.open("a") as f:
            for url in urls_in(item):
                hit = board_from_url(url.rstrip(".,;"))
                if not hit or (hit[0], hit[1].lower()) in seen:
                    continue
                seen.add((hit[0], hit[1].lower()))
                new += 1
                f.write(json.dumps({"ats": hit[0], "id": hit[1], "company": None, "source": f"hn:{tid}"}) + "\n")
        total += new
        print(f"{item.get('title')}: {len(item.get('children') or [])} posts, {new} new board candidates", flush=True)
    print(f"total new candidates: {total}", flush=True)


if __name__ == "__main__":
    main()
