"""Layer 2 — JobSpy scraper (listings only).

Reads PUBLIC listings from Indeed, Glassdoor and LinkedIn. JobSpy never
logs into or acts on the operator's account (CLAUDE.md hard rule #4 /
FR-15) — LinkedIn is a read-only listings source, so the only risk is
recoverable scraper-IP throttling.

The contract is ``scrape(search_term, ...) -> list[AllJobs]``: rows are
returned, not persisted — the caller embeds them and hands them to
``dedup.resolve_batch`` (which owns add/flush so the self-FK insert order
holds). JobSpy is imported lazily and the DataFrame→AllJobs mapping
(:func:`_row_to_job`) is pure, so unit tests run without the dependency or
network by feeding plain dicts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any

from src.state.models import AllJobs


def _as_str(value: Any) -> str | None:
    """Normalise a DataFrame cell to a trimmed str, or None when blank/NaN."""
    if value is None:
        return None
    # pandas NaN is a float that is not equal to itself.
    if isinstance(value, float) and value != value:
        return None
    text = str(value).strip()
    return text or None


def _as_posted_at(value: Any) -> datetime | None:
    """Coerce JobSpy ``date_posted`` (date | datetime | str | NaN) to UTC dt."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _job_id(site: str, raw_id: Any, job_url: str | None) -> str:
    """Stable per-listing id: ``{site}-{jobspy_id}`` or a url hash fallback."""
    rid = _as_str(raw_id)
    if rid:
        return f"{site}-{rid}"
    if job_url:
        digest = hashlib.sha1(job_url.encode("utf-8")).hexdigest()[:16]
        return f"{site}-{digest}"
    # Last resort: hash whatever we have so PK stays non-null.
    digest = hashlib.sha1(repr((site, raw_id)).encode("utf-8")).hexdigest()[:16]
    return f"{site}-{digest}"


def _row_to_job(row: dict[str, Any]) -> AllJobs | None:
    """Map one JobSpy row (as a dict) to an ``AllJobs``; None if unusable.

    A row with no company or no title is unusable (can't score or notify).
    Embedding/parse fields are left null — populated downstream.
    """
    site = _as_str(row.get("site")) or "unknown"
    company = _as_str(row.get("company"))
    role = _as_str(row.get("title"))
    if not company or not role:
        return None
    job_url = _as_str(row.get("job_url"))
    return AllJobs(
        job_id=_job_id(site, row.get("id"), job_url),
        company=company,
        role=role,
        site=site,
        location=_as_str(row.get("location")),
        job_url=job_url,
        posted_at=_as_posted_at(row.get("date_posted")),
        jd_text=_as_str(row.get("description")),
        job_type=_as_str(row.get("job_type")),
    )


def scrape(
    search_term: str,
    *,
    sites: Sequence[str],
    country: str,
    results_wanted: int,
    hours_old: int,
) -> list[AllJobs]:
    """Scrape one search term across ``sites`` and return ``AllJobs`` rows.

    Rows are de-duplicated by ``job_id`` within this call (cross-portal
    near-duplicates are handled later by embedding cosine in
    :mod:`src.scraper.dedup`). Persistence is the caller's job.
    """
    from jobspy import scrape_jobs  # lazy: heavy import, network-bound

    df = scrape_jobs(
        site_name=list(sites),
        search_term=search_term,
        country_indeed=country,
        results_wanted=results_wanted,
        hours_old=hours_old,
    )

    jobs: list[AllJobs] = []
    seen: set[str] = set()
    # DataFrame → list[dict] keeps _row_to_job pure and pandas-agnostic.
    for record in df.to_dict(orient="records"):
        job = _row_to_job(record)
        if job is None or job.job_id in seen:
            continue
        seen.add(job.job_id)
        jobs.append(job)
    return jobs
