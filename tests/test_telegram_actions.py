"""#15 — Telegram "Mark applied" / "Dismiss" buttons.

Telegram and the database are faked: no messages are sent, nothing is written.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from src import telegram_actions as ta
from src.state import job_status

CHAT = "12345"


# ---------------------------------------------------------------------------
# callback_data
# ---------------------------------------------------------------------------

def test_callback_data_round_trips():
    for status in ("applied", "dismissed", "pending", "noop"):
        data = ta.callback_data(status, "linkedin-li-4474023752")
        assert ta.parse_callback(data) == (status, "linkedin-li-4474023752")


def test_job_ids_containing_colons_survive():
    assert ta.parse_callback(ta.callback_data("applied", "a:b:c")) == ("applied", "a:b:c")


def test_callback_data_respects_telegrams_64_byte_cap():
    assert ta.callback_data("applied", "x" * 59) is not None
    assert ta.callback_data("applied", "x" * 60) is None


@pytest.mark.parametrize("data", [None, "", "st", "st:a", "st:a:", "zz:a:job", "st:q:job"])
def test_foreign_or_malformed_callback_data_is_rejected(data):
    assert ta.parse_callback(data) is None


def test_an_oversized_id_drops_the_buttons_not_the_message():
    assert ta.decision_row("x" * 80) == []


def test_recorded_row_offers_undo_and_pending_restores_the_choice():
    when = datetime(2026, 10, 7, tzinfo=timezone.utc)
    marker, undo = ta.recorded_row("job1", "applied", when)
    assert marker.text == "✅ Applied 07 Oct"
    assert ta.parse_callback(undo.callback_data) == ("pending", "job1")
    assert [b.text for b in ta.recorded_row("job1", "pending", None)] == ["Mark applied", "Dismiss"]


def test_replacing_the_decision_row_keeps_the_link_buttons():
    links = [InlineKeyboardButton("Apply", url="https://example.com/a")]
    markup = InlineKeyboardMarkup([links, ta.decision_row("job1")])
    new = ta._replace_decision_row(markup, ta.recorded_row("job1", "applied", None))
    assert [b.text for b in new.inline_keyboard[0]] == ["Apply"]
    assert new.inline_keyboard[1][0].text == "✅ Applied"
    assert len(new.inline_keyboard) == 2


# ---------------------------------------------------------------------------
# set_job_status (shared with the dashboard)
# ---------------------------------------------------------------------------

class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.commits = 0

    def get(self, model, key):
        return self.rows.get((model.__name__, key))

    def add(self, row):
        self.rows[(type(row).__name__, row.key)] = row

    def commit(self):
        self.commits += 1


def test_set_job_status_records_and_dates_the_decision():
    row = NS(user_status="pending", user_status_at=None)
    session = _Session({("Applied", "job1"): row})
    previous, when = job_status.set_job_status(session, "job1", "applied")
    assert previous == "pending" and row.user_status == "applied"
    assert when is not None and row.user_status_at == when
    assert session.commits == 1


def test_returning_to_pending_clears_the_date():
    row = NS(user_status="applied", user_status_at=datetime.now(timezone.utc))
    job_status.set_job_status(_Session({("Applied", "job1"): row}), "job1", "pending")
    assert row.user_status == "pending" and row.user_status_at is None


def test_set_job_status_rejects_unknown_jobs_and_statuses():
    with pytest.raises(job_status.UnknownJobError):
        job_status.set_job_status(_Session({}), "nope", "applied")
    with pytest.raises(ValueError):
        job_status.set_job_status(_Session({}), "job1", "maybe")


# ---------------------------------------------------------------------------
# Processing taps
# ---------------------------------------------------------------------------

class _Bot:
    """Stands in for telegram.Bot: replays `updates`, records what it's asked."""

    def __init__(self, updates, edit_error=None):
        self.updates = updates
        self.edit_error = edit_error
        self.offsets, self.edits, self.answers = [], [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_updates(self, offset=None, **_kw):
        self.offsets.append(offset)
        return [u for u in self.updates if offset is None or u.update_id >= offset]

    async def edit_message_reply_markup(self, **kw):
        if self.edit_error:
            raise self.edit_error
        self.edits.append(kw)

    async def answer_callback_query(self, query_id, text=None):
        self.answers.append(text)


def _tap(update_id, data, chat=CHAT, job_markup=None):
    message = NS(chat=NS(id=int(chat)), message_id=99,
                 reply_markup=job_markup or InlineKeyboardMarkup([ta.decision_row("job1")]))
    return NS(update_id=update_id, callback_query=NS(id=f"q{update_id}", data=data, message=message))


@pytest.fixture
def world(monkeypatch):
    """A fake database with one matched job, wired into the module."""
    db = _Session({("Applied", "job1"): NS(user_status="pending", user_status_at=None)})

    @contextmanager
    def scope():
        yield db

    monkeypatch.setattr(ta, "_session_scope", scope)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    return db


def _run(bot):
    with patch.object(ta, "Bot", lambda token: bot):
        return ta.process_pending_taps()


def test_a_tap_records_the_status_then_edits_the_message(world):
    bot = _Bot([_tap(10, ta.callback_data("applied", "job1"))])
    assert _run(bot) == 1
    assert world.rows[("Applied", "job1")].user_status == "applied"
    row = bot.edits[0]["reply_markup"].inline_keyboard[-1]
    assert row[0].text.startswith("✅ Applied") and row[1].text == "↩ Undo"
    assert bot.answers == ["Marked applied"]


def test_processed_taps_are_never_reapplied(world):
    bot = _Bot([_tap(10, ta.callback_data("applied", "job1"))])
    _run(bot)
    assert world.rows[("SearchRotationState", ta._OFFSET_KEY)].value == "11"
    assert _run(bot) == 0
    assert bot.offsets == [None, 11]


def test_taps_from_another_chat_are_ignored(world):
    bot = _Bot([_tap(10, ta.callback_data("applied", "job1"), chat="999")])
    assert _run(bot) == 0
    assert world.rows[("Applied", "job1")].user_status == "pending"
    assert bot.edits == []
    # ...but consumed, so they don't block the queue.
    assert world.rows[("SearchRotationState", ta._OFFSET_KEY)].value == "11"


def test_repeat_taps_are_harmless(world):
    data = ta.callback_data("applied", "job1")
    bot = _Bot([_tap(10, data), _tap(11, data)],
               edit_error=BadRequest("Message is not modified"))
    assert _run(bot) == 2
    assert world.rows[("Applied", "job1")].user_status == "applied"


def test_the_marker_button_changes_nothing(world):
    bot = _Bot([_tap(10, ta.callback_data("noop", "job1"))])
    assert _run(bot) == 0
    assert world.rows[("Applied", "job1")].user_status == "pending"
    assert bot.answers == ["Already recorded. Tap Undo to change it."]


def test_undo_returns_the_job_to_pending(world):
    world.rows[("Applied", "job1")].user_status = "applied"
    bot = _Bot([_tap(10, ta.callback_data("pending", "job1"))])
    _run(bot)
    assert world.rows[("Applied", "job1")].user_status == "pending"
    assert [b.text for b in bot.edits[0]["reply_markup"].inline_keyboard[-1]] == ["Mark applied", "Dismiss"]


def test_a_failed_write_leaves_the_tap_queued(world, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(job_status, "set_job_status", boom)
    bot = _Bot([_tap(10, ta.callback_data("applied", "job1"))])
    assert _run(bot) == 0  # swallowed, never raised
    assert ("SearchRotationState", ta._OFFSET_KEY) not in world.rows
    assert bot.edits == []


def test_an_unknown_job_is_answered_and_consumed(world):
    bot = _Bot([_tap(10, ta.callback_data("applied", "gone"))])
    assert _run(bot) == 0
    assert bot.answers == ["That job is no longer in the database"]
    assert world.rows[("SearchRotationState", ta._OFFSET_KEY)].value == "11"


def test_nothing_happens_without_telegram_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with patch.object(ta, "asyncio") as fake_asyncio:
        assert ta.process_pending_taps() == 0
    fake_asyncio.run.assert_not_called()


# ---------------------------------------------------------------------------
# The match notification carries the buttons
# ---------------------------------------------------------------------------

def test_match_notification_has_a_decision_row():
    from tests.test_iteration_2_notifications import _make_job, _make_parsed, _make_result
    from src.notifications import send_match_notification

    sent = []

    async def capture(text, keyboard=None):
        sent.append(keyboard)

    with patch("src.notifications._send_match", new=capture):
        send_match_notification(
            job=_make_job(), parsed=_make_parsed(), result=_make_result(),
            gap_skills=[], endpoint_base_url="https://bot.example.ts.net",
        )

    rows = sent[0].inline_keyboard
    assert [b.text for b in rows[0]] == ["Apply", "Resume PDF", "Resume DOCX"]
    assert [b.text for b in rows[1]] == ["Mark applied", "Dismiss"]
    assert ta.parse_callback(rows[1][0].callback_data) == ("applied", "job123")
