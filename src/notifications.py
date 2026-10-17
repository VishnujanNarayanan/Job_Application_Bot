"""Layer 8 — Telegram notifications.

Iteration 1: ``send_dry_run_summary`` — run summary at end of dry-run.
Iteration 2: ``send_match_notification`` — per-match message bundling the
    apply link and on-demand PDF/DOCX resume links (architecture §8).

``python-telegram-bot`` is async-only. We keep the orchestrator sync by
wrapping each send in ``asyncio.run``. Each call opens and closes its
own Bot instance — fine for low call volume (a few per day).
"""

from __future__ import annotations

import asyncio
import os

import structlog
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from src.config import settings
from src.llm.schemas import JDParsed
from src.scorer.apply_decision import SelectionResult, threshold_for
from src.state.models import AllJobs

log = structlog.get_logger(__name__)

load_dotenv()


# Hosts Telegram will not accept in an inline button, and that would not be
# reachable from a phone anyway.
_PRIVATE_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _is_public_url(url: str) -> bool:
    """True when Telegram will accept this as an inline button target.

    Telegram rejects the ENTIRE message if any button url is unreachable —
    "Inline keyboard button url 'http://localhost:8000/...' is invalid: wrong
    http url" — so an unset endpoint.base_url silently cost a real match its
    notification. Checking here turns that into a missing button.
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    host = url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    return host not in _PRIVATE_HOSTS and not host.endswith(".local")


def _env_or_die(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"{key} is not set in .env. Telegram notifications require "
            f"both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
        )
    return value


async def _send(text: str) -> None:
    token = _env_or_die("TELEGRAM_BOT_TOKEN")
    chat_id = _env_or_die("TELEGRAM_CHAT_ID")
    bot = Bot(token=token)
    async with bot:
        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode="Markdown"
        )


def match_display_fields(
    job: AllJobs,
    parsed: JDParsed,
    *,
    title_alias: str | None = None,
) -> dict[str, str]:
    """Derive the human-facing fields shared by the Telegram message and
    the Layer 9 Sheets row, so the two views never disagree.

    Returns ``display_title``, ``salary_str``, ``loc_str``, ``apply_url``.
    """
    salary_str = ""
    if parsed.salary_max_lpa:
        salary_str = f"{parsed.salary_max_lpa:.0f} LPA"
    # Combine work mode (remote/onsite/hybrid) with the city, de-duped and
    # order-preserving, e.g. "hybrid · Bangalore, India" or just "Remote".
    loc_bits = [parsed.location_type, job.location]
    loc_str = " · ".join(dict.fromkeys([b for b in loc_bits if b]))
    return {
        "display_title": title_alias or job.role,
        "salary_str": salary_str,
        "loc_str": loc_str,
        "apply_url": (_usable_apply_url(parsed.apply_url) or job.job_url_direct
                      or job.job_url or ""),
    }


def _usable_apply_url(candidate: str | None) -> str:
    """The model's apply_url, but only when it is really a link.

    `parsed.apply_url` WINS over `job.job_url_direct` (the portal's link to
    the employer's site) and `job.job_url` — the JD's own application link
    beats a portal listing page — so a bad value does not merely add noise, it
    replaces the one URL known to work. Measured 2026-08-19: a JD ending in
    "send it to hiring@example.com" yielded `" hiring@example.com"`, which would
    have become the Apply button's target.

    Anything that is not http(s) is discarded rather than repaired. A mailto:
    would be defensible, but the Apply button promises a page to open, and the
    listing URL beside it always is one.
    """
    url = (candidate or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""


def send_match_notification(
    job: AllJobs,
    parsed: JDParsed,
    result: SelectionResult,
    gap_skills: list[str],
    endpoint_base_url: str,
    *,
    title_alias: str | None = None,
    resume_urls: dict[str, str] | None = None,
    resume_pdf: bytes | None = None,
) -> None:
    """Send a per-match Telegram message with apply + resume links.

    Architecture §8 format:
        🎯 {score}
        {role} at {company}
        {location_type} · {salary} · via {site}
        [Apply] [PDF] [DOCX]
        Gap skills: ...

    ``title_alias`` is the LLM-chosen title from the StoredSelection;
    falls back to ``job.role`` when not yet available at call time.

    ``resume_urls`` optionally supplies ``{"pdf": url, "docx": url}`` from a
    build-time render (see ``endpoint.cache.prerender``). When present those
    win, because they point at S3 and keep working with the operator's laptop
    switched off. Any format missing from the dict falls back to the endpoint
    URL, which only resolves while the laptop is on.

    ``resume_pdf`` (``prerender.delivery: telegram``, #99) is sent as a file
    right after the message, as a reply to it, so it never expires and needs
    no AWS. The link buttons then give way to a "Resume DOCX" button that
    sends the DOCX on request (:mod:`src.telegram_actions`).
    """
    score = f"{result.final_score:.2f}"
    fields = match_display_fields(job, parsed, title_alias=title_alias)
    display_title = fields["display_title"]
    salary_str = fields["salary_str"]
    loc_str = fields["loc_str"]

    salary_display = salary_str if salary_str else "CTC not listed"
    threshold = threshold_for(job.site)

    text_lines = [
        f"*Match Score: {score}* (threshold: {threshold})",
        "",
        f"*{display_title}* at *{job.company}*",
        f"Location: {loc_str}" if loc_str else "",
        f"Salary: {salary_display}",
        f"Source: {job.site}",
    ]
    if gap_skills:
        text_lines += ["", f"Gap skills: {', '.join(gap_skills)}"]

    text = "\n".join(line for line in text_lines if line is not None)

    apply_url = fields["apply_url"]
    resume_urls = resume_urls or {}
    base = endpoint_base_url.rstrip("/")
    pdf_url = resume_urls.get("pdf") or f"{base}/resume/{job.job_id}.pdf"
    docx_url = resume_urls.get("docx") or f"{base}/resume/{job.job_id}.docx"

    try:
        # Build the button row first, then construct the markup once —
        # InlineKeyboardMarkup.inline_keyboard is read-only after init.
        #
        # Unreachable links are dropped rather than sent: Telegram rejects the
        # WHOLE message over one bad button url, so a single localhost link
        # cost a real match its notification on 2026-08-08. A message with two
        # buttons beats no message at all.
        row = []
        links = [("Apply", apply_url)]
        if resume_pdf is None:
            links += [("Resume PDF", pdf_url), ("Resume DOCX", docx_url)]
        for label, url in links:
            if _is_public_url(url):
                row.append(InlineKeyboardButton(label, url=url))
            elif url:
                log.warning(
                    "notification_button_dropped",
                    job_id=job.job_id, label=label, url=url,
                )
        # Second row: record the decision from the phone (#15). Taps are
        # picked up by src.telegram_actions, not by anything listening here.
        from src.telegram_actions import decision_row, docx_button

        if resume_pdf is not None:
            button = docx_button(job.job_id)
            if button:
                row.append(button)

        rows = [r for r in (row, decision_row(job.job_id)) if r]
        keyboard = InlineKeyboardMarkup(rows) if rows else None
    except Exception as exc:
        log.warning("notification_keyboard_failed", job_id=job.job_id, error=str(exc))
        keyboard = None

    try:
        message = asyncio.run(_send_match(text, keyboard))
    except Exception as exc:
        # Last resort: the buttons are a convenience, the match is the point.
        # Anything Telegram dislikes about the markup must not swallow the
        # only signal this whole pipeline exists to produce.
        if keyboard is None:
            raise
        log.warning(
            "notification_buttons_rejected",
            job_id=job.job_id, error=str(exc),
        )
        message = asyncio.run(_send_match(text, None))

    if resume_pdf is not None:
        # After the message, so a failed upload never costs the match itself.
        try:
            asyncio.run(send_resume_file(
                resume_pdf, "pdf", caption=f"{display_title} at {job.company}",
                reply_to=getattr(message, "message_id", None)))
        except Exception as exc:
            log.error("resume_file_send_failed", job_id=job.job_id, fmt="pdf", error=str(exc))

    log.info("notification_sent", job_id=job.job_id, score=result.final_score,
             resume="file" if resume_pdf is not None else "links")


async def _send_match(text: str, keyboard=None):
    """Send the match message; returns it (its ``message_id`` is what the
    resume file replies to)."""
    token = _env_or_die("TELEGRAM_BOT_TOKEN")
    chat_id = _env_or_die("TELEGRAM_CHAT_ID")
    bot = Bot(token=token)
    async with bot:
        kwargs = dict(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
        )
        if keyboard:
            kwargs["reply_markup"] = keyboard
        return await bot.send_message(**kwargs)


# Telegram's limit on a document caption.
_CAPTION_LIMIT = 1024


async def send_resume_file(data: bytes, ext: str, *, caption: str = "", reply_to: int | None = None,
                           bot: Bot | None = None) -> None:
    """Upload a resume into the chat as ``Vishnujan_Narayanan_Resume.<ext>``
    (``resume_download.download_stem``). ``bot`` is an open Bot to reuse."""
    from telegram import InputFile

    from src.resume_download import download_stem

    chat_id = _env_or_die("TELEGRAM_CHAT_ID")
    kwargs = dict(chat_id=chat_id, document=InputFile(data, filename=f"{download_stem()}.{ext}"),
                  caption=caption[:_CAPTION_LIMIT] or None)
    if reply_to:
        kwargs["reply_to_message_id"] = reply_to
    if bot is not None:
        await bot.send_document(**kwargs)
        return
    async with Bot(token=_env_or_die("TELEGRAM_BOT_TOKEN")) as own:
        await own.send_document(**kwargs)


# Run-summary wording for each outcome, in the order a job meets them.
_OUTCOME_LABELS = (
    ("DUPLICATE", "Already notified"),
    ("JOB_CLOSED", "No longer accepting applications"),
    ("EMPTY_JD", "No description scraped"),
    ("LOCATION_DISALLOWED", "Blocked location"),
    ("COMPANY_BLOCKED", "Blocked company"),
    ("TITLE_DISALLOWED", "Intern/contract/part-time title"),
    ("JOB_LEVEL_DISALLOWED", "Too senior (portal level)"),
    ("EXPERIENCE_TOO_HIGH", "Too many years (portal band)"),
    ("TOO_MANY_APPLICANTS", "Too many applicants to match"),
    ("PARSE_FAILURE", "Parse failed"),
    ("PARSE_FAILED", "Parse failed (carried to next run)"),
    ("JOB_TYPE_DISALLOWED", "Not full-time"),
    ("HARD_FILTER_LAYER_3", "Too many years required"),
    ("LOW_SCORE", "Score below threshold"),
    ("BUILD_FAILURE", "Resume build failed"),
    ("NOT_REACHED", "Not reached (carried to next run)"),
)


def _outcome_lines(outcomes: dict[str, int]) -> list[str]:
    """``Label: n`` for each non-zero outcome, in pipeline order."""
    outcomes = dict(outcomes)
    known = {key for key, _label in _OUTCOME_LABELS}
    rows = [(label, outcomes.pop(key)) for key, label in _OUTCOME_LABELS if outcomes.get(key)]
    rows += sorted((key, n) for key, n in outcomes.items() if key not in known and n)
    return [f"{label}: {n}" for label, n in rows]


# --- Progress messages: what a run is doing, while it does it --------------

def run_started_text(terms: list[str], *, dry_run: bool) -> str:
    return (f"*Job Bot — {'Dry run' if dry_run else 'Run'} started*\n"
            f"Searching: {', '.join(terms)}")


def scrape_done_text(term: str, *, found: int, remote: int, new: int) -> str:
    """One per search term. ``remote``: found only by the remote search."""
    parts = [f"{found} jobs"]
    if remote:
        parts.append(f"{remote} of them from the remote search")
    if new != found:
        parts.append(f"{found - new} already found by an earlier term")
    return f"Found for \"{term}\": " + ", ".join(parts)


def ats_poll_done_text(stats) -> str:
    """One line per ATS poll (#97), sent only when it found something or a
    board failed."""
    parts = [f"{stats.new} new jobs from {stats.boards} boards"]
    if stats.duplicates:
        parts.append(f"{stats.duplicates} already found on LinkedIn/Naukri")
    if stats.closed:
        parts.append(f"{stats.closed} closed")
    if stats.failed:
        parts.append(f"{stats.failed} boards failed")
    if getattr(stats, "gone_stopped", 0):
        parts.append(f"{stats.gone_stopped} boards gone (polling stopped, check ats_boards.poll_error)")
    if getattr(stats, "atses_skipped", None):
        parts.append(f"skipped {', '.join(stats.atses_skipped)} (not answering normally)")
    return "ATS poll: " + ", ".join(parts)


def prechecks_done_text(
    *, checked: int, to_parse: int, backlog: int, outcomes: dict[str, int],
    refreshed: int = 0,
) -> str:
    """After the checks that need no LLM: what goes on to be parsed and scored."""
    lines = [f"*Pre-checks done* — {to_parse} of {checked} going on to scoring"]
    if backlog:
        lines.append(f"({checked - backlog} scraped + {backlog} carried over from earlier runs)")
    if refreshed:
        lines.append(f"Applicant counts re-checked on carried-over jobs: {refreshed} changed")
    skipped = _outcome_lines(outcomes)
    if skipped:
        lines += ["", "Skipped before parsing:"] + skipped
    return "\n".join(lines)


def send_status(text: str) -> None:
    """Best-effort progress message. Never raises: a progress line that fails
    to send must not cost the run, which matters far more than the line."""
    if not settings.notifications.get("progress", True):
        return
    try:
        asyncio.run(_send(text))
    except Exception as exc:  # noqa: BLE001 - any send failure is non-fatal
        log.warning("telegram_status_error", error=str(exc))


def run_summary_text(
    *,
    scraped: int,
    skipped: int,
    applied: int,
    backlog: int = 0,
    outcomes: dict[str, int] | None = None,
    dry_run: bool = True,
) -> str:
    """The end-of-run Telegram message: every job accounted for.

    It used to say "Dry Run Complete" on every run and list Scraped / Matched /
    Skipped, where Skipped left out everything filtered before the parse -- a
    40-job run reported 10 + 11 and the other 19 were nowhere.
    """
    outcomes = dict(outcomes or {})
    lines = [
        f"*Job Bot — {'Dry Run' if dry_run else 'Run'} Complete*",
        "",
        f"Scraped: {scraped}" + (f" (+{backlog} carried over)" if backlog else ""),
        f"Matched: {applied}",
    ]
    rows = _outcome_lines(outcomes)
    if rows:
        lines += [""] + rows
    return "\n".join(lines)


def send_dry_run_summary(
    *,
    scraped: int,
    skipped: int,
    applied: int,
    backlog: int = 0,
    outcomes: dict[str, int] | None = None,
    dry_run: bool = True,
) -> None:
    """Send the run summary (see ``run_summary_text``) to Telegram.

    Raises ``RuntimeError`` if env vars are missing, or whatever the
    Telegram library raises on network/API failure. The orchestrator
    catches and logs — a failed Telegram send must not roll back the
    DB writes that already happened this run.
    """
    text = run_summary_text(
        scraped=scraped, skipped=skipped, applied=applied,
        backlog=backlog, outcomes=outcomes, dry_run=dry_run,
    )
    asyncio.run(_send(text))
