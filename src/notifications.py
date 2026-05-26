"""Layer 8 — Telegram notifications.

Iteration 1 ships one function: ``send_dry_run_summary`` which delivers
a real message at the end of each dry-run. Iteration 2 adds the morning
digest with presigned S3 URLs; Iteration 3 adds session-expired alerts
and the Cat-3 inline review flow.

``python-telegram-bot`` is async-only. We keep the orchestrator sync by
wrapping each send in ``asyncio.run``. Each call opens and closes its
own Bot instance — fine for low call volume (a few per day).
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from telegram import Bot

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
