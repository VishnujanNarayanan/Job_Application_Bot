"""A stalled LLM call must not hold up the run.

Regression cover for 2026-10-06/07: OpenRouter padded queued free-model
requests with blank bytes, so request_timeout_seconds (a gap-between-bytes
limit) never fired and single calls ran 5-10 minutes before ending in an
empty body.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from src.llm import client as llm_client
from src.llm.client import LLMDeadlineError, _with_deadline


class Dummy(BaseModel):
    value: str


@pytest.fixture
def release():
    """Lets stalled fake calls finish once the test is over."""
    event = threading.Event()
    yield event
    event.set()


def test_a_fast_call_returns_normally():
    assert _with_deadline(lambda: 42, 5) == 42


def test_errors_raised_inside_the_call_reach_the_caller():
    def boom():
        raise ValueError("bad reply")

    with pytest.raises(ValueError, match="bad reply"):
        _with_deadline(boom, 5)


def test_a_stalled_call_is_abandoned_at_the_deadline(release):
    started = time.monotonic()
    with pytest.raises(LLMDeadlineError):
        _with_deadline(lambda: release.wait(30), 0.2)
    assert time.monotonic() - started < 2


def test_zero_means_no_deadline():
    calls = []
    assert _with_deadline(lambda: calls.append(threading.current_thread()) or "ok", 0) == "ok"
    assert calls == [threading.main_thread()]  # ran inline, no thread


def test_a_stalled_provider_hands_the_job_on_without_retrying(monkeypatch, release):
    monkeypatch.setitem(llm_client.settings.llm._data, "call_deadline_seconds", 0.2)
    stalled = MagicMock()
    stalled.chat.completions.create.side_effect = lambda **_kw: release.wait(30)
    good = MagicMock()
    good.chat.completions.create.return_value = Dummy(value="from-next")

    def pick(which="primary"):
        return stalled if which == "primary" else good

    started = time.monotonic()
    with patch.object(llm_client, "get_client", side_effect=pick), \
         patch.object(llm_client, "rotate", side_effect=lambda chain, _i: chain), \
         patch("time.sleep"):
        result = llm_client.complete(Dummy, "prompt")

    assert result.value == "from-next"
    assert stalled.chat.completions.create.call_count == 1  # no retries
    assert time.monotonic() - started < 3


def test_a_deadline_never_abandons_the_run(monkeypatch, release):
    """Stalls are per call. Even with every other provider out of budget, a
    stall must surface as an ordinary job failure, not LLMBudgetError."""
    monkeypatch.setitem(llm_client.settings.llm._data, "call_deadline_seconds", 0.2)
    stalled = MagicMock()
    stalled.chat.completions.create.side_effect = lambda **_kw: release.wait(30)
    capped = MagicMock()
    capped.chat.completions.create.side_effect = RuntimeError(
        "429 Your project has exceeded its monthly spending cap."
    )

    def pick(which="primary"):
        return stalled if which == "primary" else capped

    with patch.object(llm_client, "get_client", side_effect=pick), \
         patch.object(llm_client, "rotate", side_effect=lambda chain, _i: chain), \
         patch("time.sleep"):
        with pytest.raises(llm_client.LLMError) as caught:
            llm_client.complete(Dummy, "prompt")

    assert not isinstance(caught.value, llm_client.LLMBudgetError)


def test_the_deadline_is_configured():
    assert float(llm_client.settings.llm.call_deadline_seconds) == 90
