"""Telegram "Mark applied" / "Dismiss" buttons on match notifications (#15).

A callback button only works if something receives the tap, and nothing here
is always listening: pipeline runs are ephemeral, and the laptop endpoint is
reachable only on the tailnet, so Telegram can't deliver a webhook to it.
Taps are therefore PULLED with ``getUpdates`` (Telegram keeps them for 24 h):

- at the start and end of every pipeline run, and
- every few seconds by a poller in the laptop endpoint process, so a tap
  lands at once while the laptop is on.

Each tap writes ``applied.user_status`` through ``set_job_status`` -- the same
path as the dashboard -- and only THEN edits the message, swapping the two
buttons for a "✅ Applied" marker and an Undo. So the message only claims what
the database actually holds. Taps are idempotent: two processes handling the
same update both write the same status, and the second edit is a no-op.

The last processed ``update_id`` lives in ``search_rotation_state`` so taps
are never re-applied. Taps from any chat but ``TELEGRAM_CHAT_ID`` are ignored.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

log = structlog.get_logger(__name__)

# callback_data is "st:<code>:<job_id>". Telegram caps it at 64 bytes; LinkedIn
# ids ("linkedin-li-4474023752") fit with room to spare.
_PREFIX = "st"
_CODES = {"a": "applied", "d": "dismissed", "p": "pending", "n": "noop"}
_CODE_OF = {status: code for code, status in _CODES.items()}
_MAX_CALLBACK_BYTES = 64

_OFFSET_KEY = "telegram_update_offset"

_LABELS = {"applied": "✅ Applied", "dismissed": "🚫 Dismissed"}


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------

def callback_data(status: str, job_id: str) -> str | None:
    """``st:<code>:<job_id>``, or None when it would break Telegram's limit.

    None rather than an exception: Telegram rejects the WHOLE message over one
    invalid button, so an oversized id must cost the buttons, not the match.
    """
    data = f"{_PREFIX}:{_CODE_OF[status]}:{job_id}"
    return data if len(data.encode()) <= _MAX_CALLBACK_BYTES else None


def parse_callback(data: str | None) -> tuple[str, str] | None:
    """``(status, job_id)`` for one of our buttons, else None."""
    parts = (data or "").split(":", 2)
    if len(parts) != 3 or parts[0] != _PREFIX or parts[1] not in _CODES or not parts[2]:
        return None
    return _CODES[parts[1]], parts[2]


def decision_row(job_id: str) -> list[InlineKeyboardButton]:
    """The "Mark applied" / "Dismiss" row for a fresh match, or [] if unusable."""
    applied, dismissed = callback_data("applied", job_id), callback_data("dismissed", job_id)
    if not (applied and dismissed):
        log.warning("decision_buttons_skipped", job_id=job_id, reason="callback_data_too_long")
        return []
    return [
        InlineKeyboardButton("Mark applied", callback_data=applied),
        InlineKeyboardButton("Dismiss", callback_data=dismissed),
    ]


def recorded_row(job_id: str, status: str, when: datetime | None) -> list[InlineKeyboardButton]:
    """What replaces the decision row once a decision is recorded."""
    if status == "pending":
        return decision_row(job_id)
    stamp = f" {when:%d %b}" if when else ""
    return [
        InlineKeyboardButton(f"{_LABELS[status]}{stamp}", callback_data=callback_data("noop", job_id)),
        InlineKeyboardButton("↩ Undo", callback_data=callback_data("pending", job_id)),
    ]


def _replace_decision_row(markup, new_row: list) -> InlineKeyboardMarkup | None:
    """Keep the message's link buttons; swap only the row of our own buttons."""
    rows = []
    for row in (markup.inline_keyboard if markup else ()):
        if any(parse_callback(getattr(b, "callback_data", None)) for b in row):
            continue
        rows.append(list(row))
    if new_row:
        rows.append(new_row)
    return InlineKeyboardMarkup(rows) if rows else None


# ---------------------------------------------------------------------------
# Processing taps
# ---------------------------------------------------------------------------

def process_pending_taps() -> int:
    """Apply every unprocessed button tap. Returns how many were recorded.

    Never raises: a Telegram or database hiccup here must not cost a run or
    kill the endpoint's poller. Unprocessed taps stay queued at Telegram and
    are picked up next time (within its 24 h retention).
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return 0
    try:
        return asyncio.run(_process(token, chat_id))
    except Exception as exc:
        log.warning("telegram_taps_failed", error=str(exc))
        return 0


def _session_scope():
    """The database session, imported late so this module loads without a DB."""
    from src.state.db import session_scope

    return session_scope()


async def _process(token: str, chat_id: str) -> int:
    with _session_scope() as session:
        offset = _load_offset(session)

    recorded = 0
    async with Bot(token=token) as bot:
        updates = await bot.get_updates(
            offset=offset, timeout=0, allowed_updates=["callback_query"]
        )
        for update in updates:
            # A tap that fails to record stops the batch WITHOUT advancing the
            # offset, so it is retried on the next pass rather than lost.
            if await _handle(bot, update, chat_id):
                recorded += 1
            with _session_scope() as session:
                _save_offset(session, update.update_id + 1)
    return recorded


async def _handle(bot: Bot, update, chat_id: str) -> bool:
    """Record one tap. True when it changed (or re-confirmed) a job's status."""
    from src.state.job_status import UnknownJobError, set_job_status

    query = update.callback_query
    if query is None:
        return False
    message = query.message
    if message is None or str(message.chat.id) != str(chat_id):
        log.warning("telegram_tap_ignored", reason="foreign_chat",
                    chat=getattr(getattr(message, "chat", None), "id", None))
        return False

    parsed = parse_callback(query.data)
    if parsed is None:
        await _answer(bot, query.id, "Unknown button")
        return False
    status, job_id = parsed
    if status == "noop":
        await _answer(bot, query.id, "Already recorded. Tap Undo to change it.")
        return False

    try:
        with _session_scope() as session:
            previous, when = set_job_status(session, job_id, status)
    except UnknownJobError:
        await _answer(bot, query.id, "That job is no longer in the database")
        return False

    log.info("job_status_changed", job_id=job_id, was=previous, now=status, via="telegram")
    markup = _replace_decision_row(message.reply_markup, recorded_row(job_id, status, when))
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id, message_id=message.message_id, reply_markup=markup
        )
    except BadRequest as exc:
        # "message is not modified": another process already edited it.
        if "not modified" not in str(exc).lower():
            log.warning("telegram_edit_failed", job_id=job_id, error=str(exc))
    await _answer(bot, query.id, "Back to pending" if status == "pending"
                  else f"Marked {status}")
    return True


async def _answer(bot: Bot, query_id: str, text: str) -> None:
    """Stop the button's spinner. A query older than ~15 min can't be answered
    any more ("query is too old"), which is expected for taps picked up late."""
    try:
        await bot.answer_callback_query(query_id, text=text)
    except BadRequest:
        pass


def _load_offset(session) -> int | None:
    from src.state.models import SearchRotationState

    row = session.get(SearchRotationState, _OFFSET_KEY)
    return int(row.value) if row and row.value else None


def _save_offset(session, offset: int) -> None:
    from src.state.models import SearchRotationState

    row = session.get(SearchRotationState, _OFFSET_KEY)
    if row is None:
        session.add(SearchRotationState(key=_OFFSET_KEY, value=str(offset)))
    elif int(row.value or 0) < offset:
        row.value = str(offset)
