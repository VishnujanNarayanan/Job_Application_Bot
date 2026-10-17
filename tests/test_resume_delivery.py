"""#99: the resume PDF attached to the match message, the DOCX on request.

Telegram and S3 are faked: nothing is sent or uploaded.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest
from telegram import InlineKeyboardMarkup

from src import notifications
from src import telegram_actions as ta
from src.endpoint import cache
from tests.test_iteration_2_notifications import _make_job, _make_parsed, _make_result

CHAT = "12345"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setattr("src.resume_download._candidate_name", lambda: "Vishnujan Narayanan")


class _Bot:
    """Stands in for telegram.Bot; records every call on the class."""

    calls: list = []
    fail_document = False

    def __init__(self, token=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_message(self, **kw):
        _Bot.calls.append(("message", kw))
        return NS(message_id=42)

    async def send_document(self, **kw):
        if _Bot.fail_document:
            raise RuntimeError("upload failed")
        _Bot.calls.append(("document", kw))


@pytest.fixture
def bot(monkeypatch):
    _Bot.calls, _Bot.fail_document = [], False
    monkeypatch.setattr(notifications, "Bot", _Bot)
    return _Bot


def _notify(**kw):
    notifications.send_match_notification(
        job=_make_job(), parsed=_make_parsed(), result=_make_result(), gap_skills=[],
        endpoint_base_url="https://laptop.example.ts.net", **kw)


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


# --- sending ---------------------------------------------------------------------------

def test_the_pdf_is_sent_as_a_file_replying_to_the_match(bot):
    _notify(resume_pdf=b"%PDF-1.7")
    (kind, message), (kind2, document) = bot.calls
    assert (kind, kind2) == ("message", "document")
    assert document["reply_to_message_id"] == 42
    assert document["document"].filename == "Vishnujan_Narayanan_Resume.pdf"
    assert document["caption"] == "Python Developer at Fintech Inc"


def test_with_the_pdf_attached_the_links_give_way_to_a_docx_button(bot):
    _notify(resume_pdf=b"%PDF")
    buttons = _buttons(bot.calls[0][1]["reply_markup"])
    labels = [b.text for b in buttons]
    assert "Resume PDF" not in labels and "Apply" in labels
    docx = next(b for b in buttons if b.text == "Resume DOCX")
    assert docx.url is None and ta.parse_docx_callback(docx.callback_data) == "job123"


def test_without_the_file_the_link_buttons_stay(bot):
    _notify(resume_urls={"pdf": "https://s3.example.com/a.pdf", "docx": "https://s3.example.com/a.docx"})
    assert [k for k, _ in bot.calls] == ["message"]
    urls = {b.text: b.url for b in _buttons(bot.calls[0][1]["reply_markup"])}
    assert urls["Resume PDF"] == "https://s3.example.com/a.pdf"


def test_a_failed_upload_does_not_cost_the_match(bot):
    bot.fail_document = True
    _notify(resume_pdf=b"%PDF")  # does not raise
    assert [k for k, _ in bot.calls] == ["message"]


def test_a_long_caption_is_cut_to_telegrams_limit(bot):
    import asyncio

    asyncio.run(notifications.send_resume_file(b"x", "docx", caption="y" * 2000))
    document = bot.calls[0][1]
    assert len(document["caption"]) == 1024 and "reply_to_message_id" not in document
    assert document["document"].filename == "Vishnujan_Narayanan_Resume.docx"


# --- what a run attaches ------------------------------------------------------------------

def _settings(monkeypatch, **prerender):
    base = {"delivery": "telegram", "enabled": True, "link_expiry_days": 7, **prerender}
    monkeypatch.setattr(cache, "settings", NS(prerender=NS(get=base.get, **base)))


def test_telegram_delivery_renders_the_pdf_without_s3(monkeypatch):
    _settings(monkeypatch)
    asked = []
    monkeypatch.setattr(cache, "get_or_build",
                        lambda job_id, ext, session, use_cache=True: asked.append((ext, use_cache)) or (b"%PDF", "x"))
    monkeypatch.setattr(cache, "prerender", lambda *a, **k: pytest.fail("no S3 links"))
    assert cache.for_notification("j1", None) == {"resume_pdf": b"%PDF"}
    assert asked == [("pdf", False)]


def test_s3_delivery_sends_links(monkeypatch):
    _settings(monkeypatch, delivery="s3")
    monkeypatch.setattr(cache, "prerender", lambda job_id, s, expires_seconds: {"pdf": f"u{expires_seconds}"})
    assert cache.for_notification("j1", None) == {"resume_urls": {"pdf": "u604800"}}
    _settings(monkeypatch, delivery="s3", enabled=False)
    assert cache.for_notification("j1", None) == {}


def test_a_render_failure_falls_back_to_links(monkeypatch):
    _settings(monkeypatch)

    def broken(*_a, **_k):
        raise RuntimeError("LibreOffice missing")

    monkeypatch.setattr(cache, "get_or_build", broken)
    assert cache.for_notification("j1", None) == {}


def test_an_unknown_delivery_is_refused(monkeypatch):
    _settings(monkeypatch, delivery="email")
    with pytest.raises(ValueError):
        cache.delivery()


def test_an_uncached_render_never_touches_s3(monkeypatch, tmp_path):
    monkeypatch.setattr(cache, "_load_selection", lambda _json, job_id: NS(job_id=job_id, template_version="v3"))
    monkeypatch.setattr(cache, "_check_render_cache", lambda *a: pytest.fail("render_cache read"))
    monkeypatch.setattr(cache, "cache_get_bytes", lambda *a: pytest.fail("S3 read"))
    monkeypatch.setattr(cache, "cache_put", lambda *a: pytest.fail("S3 upload"))
    monkeypatch.setattr(cache, "_record_render_cache", lambda *a: pytest.fail("render_cache write"))
    monkeypatch.setattr(cache, "assemble_docx", lambda output_path, **_k: output_path.write_bytes(b"DOCX"))
    session = NS(get=lambda _model, _id: NS(selection_json={"v": 3}))
    assert cache.get_or_build("j1", "docx", session, use_cache=False) == (b"DOCX", cache._CONTENT_TYPES["docx"])


# --- the "Resume DOCX" tap -------------------------------------------------------------------

class _TapBot(_Bot):
    def __init__(self, updates):
        self.updates, self.answers = updates, []

    async def get_updates(self, offset=None, **_kw):
        return [u for u in self.updates if offset is None or u.update_id >= offset]

    async def answer_callback_query(self, query_id, text=None):
        self.answers.append(text)


@pytest.fixture
def db(monkeypatch):
    rows = {}

    class _Session:
        def get(self, model, key):
            return rows.get((model.__name__, key))

        def add(self, row):
            rows[(type(row).__name__, row.key)] = row

    @contextmanager
    def scope():
        yield _Session()

    monkeypatch.setattr(ta, "_session_scope", scope)
    return rows


def _docx_tap(job_id="job1"):
    data = ta.docx_button(job_id).callback_data
    message = NS(chat=NS(id=int(CHAT)), message_id=99, reply_markup=InlineKeyboardMarkup([ta.decision_row(job_id)]))
    return NS(update_id=5, callback_query=NS(id="q5", data=data, message=message))


def _tap(bot):
    _Bot.calls = []
    with patch.object(ta, "Bot", lambda token: bot):
        return ta.process_pending_taps()


def test_a_docx_tap_sends_the_docx_as_a_reply_and_changes_no_status(db, monkeypatch):
    asked = []
    monkeypatch.setattr("src.endpoint.cache.get_or_build",
                        lambda job_id, ext, session, use_cache=True: asked.append((job_id, ext, use_cache)) or (b"DOCX", "x"))
    bot = _TapBot([_docx_tap()])
    assert _tap(bot) == 0  # not a status change
    assert asked == [("job1", "docx", False)]
    (kind, document), = _Bot.calls
    assert kind == "document" and document["reply_to_message_id"] == 99
    assert document["document"].filename == "Vishnujan_Narayanan_Resume.docx"
    assert bot.answers == ["DOCX sent"]


def test_a_docx_that_cannot_be_built_says_so(db, monkeypatch):
    def broken(*_a, **_k):
        raise KeyError("no selection")

    monkeypatch.setattr("src.endpoint.cache.get_or_build", broken)
    bot = _TapBot([_docx_tap()])
    _tap(bot)
    assert _Bot.calls == [] and bot.answers == ["Could not build the DOCX"]


def test_the_docx_button_fits_telegrams_callback_limit():
    assert ta.docx_button("linkedin-li-4474023752").callback_data == "rd:linkedin-li-4474023752"
    assert ta.docx_button("x" * 70) is None
    assert ta.parse_docx_callback("st:a:job1") is None
