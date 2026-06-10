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


def send_match_notification(
    job: AllJobs,
    parsed: JDParsed,
    result: SelectionResult,
    gap_skills: list[str],
    endpoint_base_url: str,
    *,
    title_alias: str | None = None,
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
    """
    score = f"{result.final_score:.2f}"
    display_title = title_alias or job.role

    # Salary string (informational only)
    sal_parts = []
    if parsed.salary_max_lpa:
        currency = parsed.salary_currency or "INR"
        sal_parts.append(f"{parsed.salary_max_lpa:.0f} LPA")
    salary_str = " · ".join(sal_parts) if sal_parts else ""

    # Location display
    loc_str = parsed.location_type or job.location or ""

    meta_parts = [p for p in [loc_str, salary_str, f"via {job.site}"] if p]
    meta_line = " · ".join(meta_parts)

    # Gap skills line
    gap_line = f"Gap skills: {', '.join(gap_skills)}" if gap_skills else ""

    text_lines = [
        f"🎯 *{score}*",
        f"*{display_title}* at {job.company}",
    ]
    if meta_line:
        text_lines.append(meta_line)
    if gap_line:
        text_lines.append(gap_line)

    text = "\n".join(text_lines)

    apply_url = parsed.apply_url or job.job_url or ""
    pdf_url = f"{endpoint_base_url}/resume/{job.job_id}.pdf"
    docx_url = f"{endpoint_base_url}/resume/{job.job_id}.docx"

    try:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 Apply", url=apply_url) if apply_url else None,
                InlineKeyboardButton("📄 PDF", url=pdf_url),
                InlineKeyboardButton("📝 DOCX", url=docx_url),
            ],
        ])
        # Remove None buttons
        keyboard.inline_keyboard = [
            [btn for btn in row if btn is not None]
            for row in keyboard.inline_keyboard
        ]
    except Exception:
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
        "*Job bot — dry run complete*\n"
        f"Scraped: `{scraped}`\n"
        f"Skipped: `{skipped}`\n"
        f"Applied: `{applied}`"
    )
    asyncio.run(_send(text))
