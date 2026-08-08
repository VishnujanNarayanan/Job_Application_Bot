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
from src.scorer.apply_decision import SelectionResult
from src.state.models import AllJobs

log = structlog.get_logger(__name__)

load_dotenv()


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
        "apply_url": parsed.apply_url or job.job_url or "",
    }


def send_match_notification(
    job: AllJobs,
    parsed: JDParsed,
    result: SelectionResult,
    gap_skills: list[str],
    endpoint_base_url: str,
    *,
    title_alias: str | None = None,
    resume_urls: dict[str, str] | None = None,
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
    """
    score = f"{result.final_score:.2f}"
    fields = match_display_fields(job, parsed, title_alias=title_alias)
    display_title = fields["display_title"]
    salary_str = fields["salary_str"]
    loc_str = fields["loc_str"]

    salary_display = salary_str if salary_str else "CTC not listed"
    threshold = settings.scoring.apply_threshold

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
        row = []
        if apply_url:
            row.append(InlineKeyboardButton("Apply", url=apply_url))
        row.append(InlineKeyboardButton("Resume PDF", url=pdf_url))
        row.append(InlineKeyboardButton("Resume DOCX", url=docx_url))
        keyboard = InlineKeyboardMarkup([row])
    except Exception as exc:
        log.warning("notification_keyboard_failed", job_id=job.job_id, error=str(exc))
        keyboard = None

    asyncio.run(_send_match(text, keyboard))
    log.info("notification_sent", job_id=job.job_id, score=result.final_score)


async def _send_match(text: str, keyboard=None) -> None:
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
        await bot.send_message(**kwargs)


def send_dry_run_summary(
    *, scraped: int, skipped: int, applied: int
) -> None:
    """Send a one-line dry-run summary to Telegram.

    Raises ``RuntimeError`` if env vars are missing, or whatever the
    Telegram library raises on network/API failure. The orchestrator
    catches and logs — a failed Telegram send must not roll back the
    DB writes that already happened this run.
    """
    text = (
        "*Job Bot — Dry Run Complete*\n"
        "\n"
        f"Scraped: {scraped}\n"
        f"Matched: {applied}\n"
        f"Skipped: {skipped}"
    )
    asyncio.run(_send(text))
