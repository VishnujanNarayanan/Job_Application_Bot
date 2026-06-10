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
import random
import time
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any

import structlog

from src.state.models import AllJobs

log = structlog.get_logger(__name__)


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


def _scrape_with_retry(
    scrape_jobs,
    *,
    site_group: list[str],
    search_term: str,
    country: str,
    results_wanted: int,
    hours_old: int,
    linkedin_fetch_description: bool,
    proxies: Sequence[str] | None,
    max_retries: int,
    backoff_base_seconds: float,
) -> Any | None:
    """Call JobSpy for one site group; retry with exponential backoff + jitter.

    Returns the DataFrame on success or ``None`` if every attempt failed —
    so a single throttled site never aborts the whole run (anti-rate-limit,
    hard rule #4). Failures are logged with the ``scrape_*`` event names that
    CloudWatch metric filters watch.
    """
    kwargs: dict[str, Any] = dict(
        site_name=site_group,
        search_term=search_term,
        country_indeed=country,
        results_wanted=results_wanted,
        hours_old=hours_old,
        linkedin_fetch_description=linkedin_fetch_description,
    )
    if proxies:
        kwargs["proxies"] = list(proxies)

    last_exc: Exception | None = None
    for attempt in range(1, max(1, max_retries) + 1):
        try:
            return scrape_jobs(**kwargs)
        except Exception as exc:  # JobSpy raises bare/library errors on throttle
            last_exc = exc
            log.warning(
                "scrape_retry",
                site=site_group,
                attempt=attempt,
                max_attempts=max_retries,
                error=str(exc),
            )
            if attempt < max_retries:
                delay = backoff_base_seconds * (2 ** (attempt - 1))
                time.sleep(delay + random.uniform(0, backoff_base_seconds))
    log.error("scrape_site_failed", site=site_group, error=str(last_exc))
    return None


def scrape(
    search_term: str,
    *,
    sites: Sequence[str],
    country: str,
    results_wanted: int,
    hours_old: int,
    linkedin_fetch_description: bool = False,
    linkedin_results_wanted: int | None = None,
    per_site: bool = True,
    max_retries: int = 1,
    backoff_base_seconds: float = 5.0,
    inter_site_delay_seconds: float = 0.0,
    proxies: Sequence[str] | None = None,
) -> list[AllJobs]:
    """Scrape one search term across ``sites`` and return ``AllJobs`` rows.

    Rows are de-duplicated by ``job_id`` within this call (cross-portal
    near-duplicates are handled later by embedding cosine in
    :mod:`src.scraper.dedup`). Persistence is the caller's job.

    Anti-rate-limit (hard rule #4): with ``per_site`` each site is scraped in
    its own JobSpy call wrapped in retry/backoff, so one throttled portal does
    not lose the others' results. ``linkedin_fetch_description`` pulls the full
    JD body per LinkedIn listing (without it ``jd_text`` is empty); LinkedIn's
    request volume is capped separately by ``linkedin_results_wanted`` and runs
    are spaced by ``inter_site_delay_seconds``.
    """
    from jobspy import scrape_jobs  # lazy: heavy import, network-bound

    site_list = list(sites)
    site_groups: list[list[str]] = [[s] for s in site_list] if per_site else [site_list]

    jobs: list[AllJobs] = []
    seen: set[str] = set()
    for idx, group in enumerate(site_groups):
        # LinkedIn description-fetch multiplies requests — cap it lower.
        rw = results_wanted
        if linkedin_results_wanted and group == ["linkedin"]:
            rw = linkedin_results_wanted

        df = _scrape_with_retry(
            scrape_jobs,
            site_group=group,
            search_term=search_term,
            country=country,
            results_wanted=rw,
            hours_old=hours_old,
            linkedin_fetch_description=linkedin_fetch_description,
            proxies=proxies,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
        )
        if df is None:
            continue

        # DataFrame → list[dict] keeps _row_to_job pure and pandas-agnostic.
        for record in df.to_dict(orient="records"):
            job = _row_to_job(record)
            if job is None or job.job_id in seen:
                continue
            seen.add(job.job_id)
            jobs.append(job)

        if inter_site_delay_seconds and idx < len(site_groups) - 1:
            time.sleep(inter_site_delay_seconds + random.uniform(0, inter_site_delay_seconds))

    return jobs
