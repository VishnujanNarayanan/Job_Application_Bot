"""Layer 2 — JobSpy scraper (listings only).

Reads PUBLIC listings from Indeed, Glassdoor and LinkedIn. JobSpy never
logs into or acts on the operator's account (CLAUDE.md hard rule #4 /
FR-15) — LinkedIn is a read-only listings source, so the only risk is
recoverable scraper-IP throttling.

The contract is ``scrape(search_term, ...) -> list[AllJobs]``: rows are
returned, not persisted — the caller embeds them and persists them. JobSpy
is imported lazily and the DataFrame→AllJobs mapping (:func:`_row_to_job`)
is pure, so unit tests run without the dependency or network by feeding
plain dicts.
"""

from __future__ import annotations

import hashlib
import random
import re
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


# Leading junk on scraped titles: whitespace, punctuation, separators, bullets
# (e.g. Indeed's ``": Data Engineer | Azure"``). Stripped from the front only.
# Opening brackets ``(`` / ``[`` are preserved — they pair with content, so a
# title like ``"(Remote) Backend"`` keeps its bracket instead of leaving a
# dangling ``)``.
_LEADING_JUNK = re.compile(r"^[^\w([]+", re.UNICODE)


def _clean_title(value: Any) -> str | None:
    """Trim a scraped title, then strip any leading punctuation/separators.

    Whitespace and leading non-alphanumeric characters (``:``, ``-``, ``|``,
    bullets, etc.) are removed so the title starts at its first real word. A
    title that is entirely punctuation collapses to None (unusable, like a
    blank title)."""
    text = _as_str(value)
    if text is None:
        return None
    cleaned = _LEADING_JUNK.sub("", text)
    return cleaned or None


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


# ---------------------------------------------------------------------------
# LinkedIn applicant count
# ---------------------------------------------------------------------------
#
# JobSpy returns no applicant count, but the public job page it already fetches
# for the description (`linkedin_fetch_description`) shows one in its top card:
# "Be among the first 25 applicants", "139 applicants", "Over 200 applicants".
# JobSpy parses that page internally and discards the caption, so
# :func:`install_applicant_capture` wraps its detail fetch to read the caption
# from the SAME response -- no extra request, so no added throttle risk (hard
# rule #4). Captions land in ``_APPLICANTS`` keyed by JobSpy's row id
# (``li-<id>``) and are merged onto the rows in :func:`scrape`.

_CAPTION = re.compile(
    r'num-applicants__caption[^>]*>\s*([^<]+?)\s*<', re.IGNORECASE
)
_FIRST_N = re.compile(r"first\s+([\d,]+)", re.IGNORECASE)
_OVER_N = re.compile(r"over\s+([\d,]+)", re.IGNORECASE)
_N = re.compile(r"([\d,]+)\s+applicant", re.IGNORECASE)

_APPLICANTS: dict[str, str] = {}
_capture_installed = False


def parse_applicants(caption: str | None) -> int | None:
    """LinkedIn's applicant caption -> a count, or None when it carries none.

    The two capped forms are stored as the nearest bound, so band edges stay
    honest: "Be among the first 25" means FEWER than 25 and becomes 24; "Over
    200" means MORE than 200 and becomes 201. The raw caption is kept alongside
    (``applicants_text``) so the reading can always be audited.
    """
    if not caption:
        return None
    for pattern, shift in ((_FIRST_N, -1), (_OVER_N, 1), (_N, 0)):
        m = pattern.search(caption)
        if m:
            return int(m.group(1).replace(",", "")) + shift
    return None


def applicant_caption(html: str) -> str | None:
    """The applicant caption from a LinkedIn job page's HTML, if present."""
    m = _CAPTION.search(html or "")
    return " ".join(m.group(1).split()) if m else None


def install_applicant_capture() -> None:
    """Wrap JobSpy's LinkedIn detail fetch so it records the applicant caption.

    Idempotent. The wrapper swaps the scraper's session ``get`` for the duration
    of ONE original call, keeps the response it returns, and restores it -- the
    original parsing runs untouched. Any failure here is swallowed: a missing
    applicant count must never cost the description.
    """
    global _capture_installed
    if _capture_installed:
        return
    try:
        from jobspy.linkedin import LinkedIn
    except ImportError as exc:  # a JobSpy layout change: lose the count, not the run
        log.warning("applicants_capture_unavailable", error=str(exc))
        return

    original = LinkedIn._get_job_details

    def _get_job_details(self, job_id: str) -> dict:
        seen: list[Any] = []
        real_get = self.session.get

        def _get(*args, **kwargs):
            resp = real_get(*args, **kwargs)
            seen.append(resp)
            return resp

        self.session.get = _get
        try:
            details = original(self, job_id)
        finally:
            self.session.get = real_get
        try:
            caption = applicant_caption(seen[-1].text) if seen else None
            if caption:
                _APPLICANTS[f"li-{job_id}"] = caption
        except Exception as exc:  # never let the count cost the description
            log.warning("applicants_capture_failed", job_id=job_id, error=str(exc))
        return details

    LinkedIn._get_job_details = _get_job_details
    _capture_installed = True


# LinkedIn's guest job page for a posting that has closed.
_CLOSED = re.compile(r"no longer accepting applications", re.IGNORECASE)


def _linkedin_id(job: AllJobs) -> str | None:
    """LinkedIn's numeric job id from our ``linkedin-li-<id>`` key."""
    m = re.fullmatch(r"linkedin-li-(\d+)", job.job_id or "")
    return m.group(1) if m else None


def refresh_applicants(jobs: Sequence[AllJobs], *, delay_seconds: float = 1.5) -> dict[str, int]:
    """Re-read the applicant count of LinkedIn ``jobs`` from their job pages.

    For the backlog: a job carried over from an earlier run still holds the
    count it was scraped with, which can be hours stale -- and the applicant
    check, and the score, should judge the posting as it is NOW. One GET per
    job, the same public page and headers JobSpy's own description fetch uses,
    paced by ``delay_seconds`` (hard rule #4).

    Updates ``applicants_text``/``applicants_count`` in place when the page
    shows a caption; marks ``job.closed = True`` (a plain attribute, not a
    column) when LinkedIn says the posting no longer accepts applications.
    Any failure leaves the job as it was: a stale count beats no job.
    Returns ``{"refreshed": n, "changed": n, "closed": n, "failed": n}``.
    """
    import requests

    from jobspy.linkedin.constant import headers

    stats = {"refreshed": 0, "changed": 0, "closed": 0, "failed": 0}
    linkedin = [(job, _linkedin_id(job)) for job in jobs]
    linkedin = [(job, jid) for job, jid in linkedin if jid]
    for i, (job, jid) in enumerate(linkedin):
        if i and delay_seconds:
            time.sleep(delay_seconds + random.uniform(0, delay_seconds))
        try:
            resp = requests.get(f"https://www.linkedin.com/jobs/view/{jid}",
                                headers=headers, timeout=10)
            resp.raise_for_status()
            if "linkedin.com/signup" in resp.url:
                raise RuntimeError("redirected to signup")
        except Exception as exc:  # noqa: BLE001 - keep the stale count
            stats["failed"] += 1
            log.warning("applicants_refresh_failed", job_id=job.job_id, error=str(exc))
            continue
        stats["refreshed"] += 1
        if _CLOSED.search(resp.text):
            job.closed = True
            stats["closed"] += 1
            continue
        caption = applicant_caption(resp.text)
        count = parse_applicants(caption)
        if count is not None and count != job.applicants_count:
            log.info("applicants_refreshed", job_id=job.job_id,
                     was=job.applicants_count, now=count)
            job.applicants_text, job.applicants_count = caption, count
            stats["changed"] += 1
    return stats


def _row_to_job(row: dict[str, Any]) -> AllJobs | None:
    """Map one JobSpy row (as a dict) to an ``AllJobs``; None if unusable.

    A row with no company or no title is unusable (can't score or notify).
    Embedding/parse fields are left null — populated downstream.
    """
    site = _as_str(row.get("site")) or "unknown"
    company = _as_str(row.get("company"))
    role = _clean_title(row.get("title"))
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
        applicants_text=_as_str(row.get("applicants_text")),
        applicants_count=parse_applicants(_as_str(row.get("applicants_text"))),
    )


def _scrape_with_retry(
    scrape_jobs,
    *,
    site_group: list[str],
    search_term: str,
    country: str,
    location: str | None,
    results_wanted: int,
    hours_old: int,
    linkedin_fetch_description: bool,
    proxies: Sequence[str] | None,
    max_retries: int,
    backoff_base_seconds: float,
    is_remote: bool = False,
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
    # location is LinkedIn's only geo-filter (country_indeed is ignored there)
    # and refines Indeed/Glassdoor within the country.
    if location:
        kwargs["location"] = location
    if proxies:
        kwargs["proxies"] = list(proxies)
    # LinkedIn's "Remote" workplace filter (f_WT=2). Combined with `location`
    # it means remote roles open to that country.
    if is_remote:
        kwargs["is_remote"] = True

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
    # exc_info=last_exc (the captured instance), NOT True — we are outside the
    # except block here, so sys.exc_info() is already cleared.
    log.error("scrape_site_failed", site=site_group, error=str(last_exc), exc_info=last_exc)
    return None


def scrape(
    search_term: str,
    *,
    sites: Sequence[str],
    country: str,
    location: str | None = None,
    results_wanted: int,
    hours_old: int,
    linkedin_fetch_description: bool = False,
    linkedin_results_wanted: int | None = None,
    per_site: bool = True,
    max_retries: int = 1,
    backoff_base_seconds: float = 5.0,
    inter_site_delay_seconds: float = 0.0,
    proxies: Sequence[str] | None = None,
    remote_results_wanted: int = 0,
    remote_hours_old: int | None = None,
    stats: dict | None = None,
) -> list[AllJobs]:
    """Scrape one search term across ``sites`` and return ``AllJobs`` rows.

    Rows are de-duplicated by exact ``job_id`` within this call; the
    orchestrator additionally skips ``job_id``s already in ``all_jobs``.
    There is no cosine near-duplicate detection. Persistence is the caller's
    job.

    Anti-rate-limit (hard rule #4): with ``per_site`` each site is scraped in
    its own JobSpy call wrapped in retry/backoff, so one throttled portal does
    not lose the others' results. ``linkedin_fetch_description`` pulls the full
    JD body per LinkedIn listing (without it ``jd_text`` is empty); LinkedIn's
    request volume is capped separately by ``linkedin_results_wanted`` and runs
    are spaced by ``inter_site_delay_seconds``.

    ``remote_results_wanted`` > 0 adds one more LinkedIn call with the Remote
    filter on, over ``remote_hours_old`` (default: ``hours_old``). The plain
    search returns LinkedIn's top listings for the location, which are almost
    all on-site or hybrid -- ~10% of parsed jobs were remote -- so remote roles
    only arrive in volume when asked for. Its rows are de-duplicated against
    the plain search's like any other.

    ``stats``, when given, is filled with ``remote``: how many of the returned
    jobs only the remote search found (for the run's progress message).
    """
    from jobspy import scrape_jobs  # lazy: heavy import, network-bound

    if linkedin_fetch_description and "linkedin" in sites:
        install_applicant_capture()

    site_list = list(sites)
    site_groups: list[list[str]] = [[s] for s in site_list] if per_site else [site_list]

    # (site group, results wanted, hours_old, remote-only)
    passes: list[tuple[list[str], int, int, bool]] = []
    for group in site_groups:
        # LinkedIn description-fetch multiplies requests — cap it lower.
        rw = results_wanted
        if linkedin_results_wanted and group == ["linkedin"]:
            rw = linkedin_results_wanted
        passes.append((group, rw, hours_old, False))
    if remote_results_wanted > 0 and "linkedin" in site_list:
        passes.append((["linkedin"], remote_results_wanted,
                       remote_hours_old or hours_old, True))

    jobs: list[AllJobs] = []
    seen: set[str] = set()
    for idx, (group, rw, hours, remote) in enumerate(passes):

        df = _scrape_with_retry(
            scrape_jobs,
            site_group=group,
            search_term=search_term,
            country=country,
            location=location,
            results_wanted=rw,
            hours_old=hours,
            linkedin_fetch_description=linkedin_fetch_description,
            proxies=proxies,
            max_retries=max_retries,
            backoff_base_seconds=backoff_base_seconds,
            is_remote=remote,
        )
        if df is None:
            continue

        # DataFrame → list[dict] keeps _row_to_job pure and pandas-agnostic.
        for record in df.to_dict(orient="records"):
            caption = _APPLICANTS.pop(_as_str(record.get("id")) or "", None)
            if caption:
                record["applicants_text"] = caption
            job = _row_to_job(record)
            if job is None or job.job_id in seen:
                continue
            seen.add(job.job_id)
            jobs.append(job)
            if remote and stats is not None:
                stats["remote"] = stats.get("remote", 0) + 1

        if inter_site_delay_seconds and idx < len(passes) - 1:
            time.sleep(inter_site_delay_seconds + random.uniform(0, inter_site_delay_seconds))

    return jobs
