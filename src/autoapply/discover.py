"""Find the bot's jobs inside the Job Bot Edge: LinkedIn, Easy Apply only, last hour.

LinkedIn throttles plain Python clients (429) after a burst, but not a real
browser on the same connection. So the Easy Apply bot reads its search in Edge:

1. LinkedIn's job-list address (``jobs-guest/jobs/api/seeMoreJobPostings/search``,
   the one its own pages load lists from) with ``f_AL=true`` (Easy Apply) and
   ``f_TPR=r<seconds>`` (posted within). The normal Jobs page now redirects to the
   "AI-powered job search", which drops the time filter and hides job ids.
2. Each job's ``jobs-guest/jobs/api/jobPosting/<id>`` page for its description,
   applicant count and criteria.

The rows have the shape JobSpy's DataFrame has, so the pipeline
(``src.main --jobs-file``) scores and tailors them exactly like scraped ones.
Only the bot uses this; normal pipeline runs keep JobSpy.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
PAGE_SIZE = 10  # LinkedIn's list pages


def search_url(term: str, location: str | None, seconds: int, start: int = 0, easy_apply: bool = True) -> str:
    params: dict[str, Any] = {"keywords": term}
    if location:
        params["location"] = location
    params["f_TPR"] = f"r{int(seconds)}"
    if easy_apply:
        params["f_AL"] = "true"
    params["sortBy"] = "DD"  # newest first
    params["start"] = start
    return f"{SEARCH}?{urlencode(params)}"


def parse_cards(html: str) -> list[dict[str, Any]]:
    """The job cards of one list page: id, title, company, location, date."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("li"):
        urn = card.select_one("[data-entity-urn]")
        m = re.search(r"jobPosting:(\d+)", urn["data-entity-urn"] if urn else str(card))
        if not m:
            continue

        def text(sel: str) -> str | None:
            el = card.select_one(sel)
            return " ".join(el.get_text(" ", strip=True).split()) if el and el.get_text(strip=True) else None

        t = card.select_one("time")
        out.append({
            "id": m.group(1),
            "title": text("h3.base-search-card__title"),
            "company": text("h4.base-search-card__subtitle"),
            "location": text("span.job-search-card__location"),
            "date_posted": t.get("datetime") if t is not None else None,
        })
    return out


def _criteria(html: str) -> dict[str, str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for item in soup.select("li.description__job-criteria-item"):
        h, v = item.select_one("h3"), item.select_one("span")
        if h and v:
            out[h.get_text(strip=True).lower()] = v.get_text(strip=True)
    return out


def to_row(card: dict[str, Any], posting_html: str | None) -> dict[str, Any]:
    """One JobSpy-shaped row (``site``/``id``/``title``/``company``/...)."""
    from src.scraper.jobspy_wrapper import LinkedInFetchError, parse_linkedin_job_html

    url = f"https://www.linkedin.com/jobs/view/{card['id']}"
    row: dict[str, Any] = {
        "site": "linkedin", "id": f"li-{card['id']}", "job_url": url,
        "title": card.get("title"), "company": card.get("company"),
        "location": card.get("location"), "date_posted": card.get("date_posted"),
        "description": None, "applicants_text": None, "job_type": None, "job_level": None,
    }
    if posting_html:
        try:
            d = parse_linkedin_job_html(posting_html, url)
        except LinkedInFetchError:
            d = {}
        row["description"] = d.get("description") or None
        row["applicants_text"] = d.get("applicants_text")
        row["title"] = row["title"] or d.get("title")
        row["company"] = row["company"] or d.get("company")
        row["closed"] = bool(d.get("closed"))
        crit = _criteria(posting_html)
        if crit.get("employment type"):
            row["job_type"] = crit["employment type"].lower().replace("-", "")
        if crit.get("seniority level"):
            row["job_level"] = crit["seniority level"].lower()
    return row


def discover(page, term: str, location: str | None, seconds: int, *, max_jobs: int = 40,
             log: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """The last ``seconds`` of Easy Apply postings for ``term``, read in the given Edge tab."""
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for start in range(0, max_jobs, PAGE_SIZE):
        resp = page.goto(search_url(term, location, seconds, start), wait_until="domcontentloaded", timeout=40000)
        status = resp.status if resp else None
        new = [c for c in parse_cards(page.content()) if c["id"] not in seen] if status == 200 else []
        if log:
            log("autoapply_discover_page", start=start, status=status, cards=len(new))
        if not new:
            break
        seen.update(c["id"] for c in new)
        cards.extend(new)
        if len(new) < PAGE_SIZE:
            break
        time.sleep(random.uniform(1.0, 2.0))
    rows = []
    for card in cards[:max_jobs]:
        html = None
        try:
            resp = page.goto(POSTING.format(card["id"]), wait_until="domcontentloaded", timeout=40000)
            if resp and resp.status == 200:
                html = page.content()
        except Exception:  # noqa: BLE001 - a card without its page is still a job
            html = None
        row = to_row(card, html)
        if not row.get("closed"):
            rows.append(row)
        time.sleep(random.uniform(0.6, 1.4))
    return rows


def write(rows: list[dict[str, Any]], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    return path
