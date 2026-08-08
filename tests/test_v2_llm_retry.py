"""Layer 3/5 — the LLM client must not retry errors that cannot succeed.

Regression cover for 2026-08-08: `gemini-2.5-flash` was retired, every call
404'd, and the client retried each one five times with exponential backoff.
That cost roughly four minutes *per job* — on every job in the run — waiting
for an outcome that could never change, and it burned billable Actions
minutes doing it.

Rate limiting (429) is the important counter-case: that IS transient and must
still retry, or the pipeline gives up the moment it gets busy.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from src.llm import client as llm_client
from src.llm.client import LLMError, _is_permanent


class Dummy(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "404 This model models/gemini-2.5-flash is no longer available to new users.",
    "404 models/gemini-9-flash is not found for API version v1beta",
    "400 Invalid argument: malformed request",
    "403 Permission denied on resource",
    "API key not valid. Please pass a valid API key.",
])
def test_permanent_errors_are_recognised(message):
    assert _is_permanent(RuntimeError(message)) is True


@pytest.mark.parametrize("message", [
    "429 Resource exhausted. Please retry shortly.",
    "429 Quota exceeded for requests per minute",
    "500 Internal error",
    "503 The service is currently unavailable.",
    "Connection reset by peer",
    "Deadline exceeded",
])
def test_transient_errors_are_not_permanent(message):
    assert _is_permanent(RuntimeError(message)) is False


def test_rate_limit_wins_over_a_404_substring():
    """A 429 body that happens to mention a 404-ish phrase must still retry.

    Classification is text-based, so the transient check runs first and wins.
    """
    exc = RuntimeError("429 quota exceeded; model is not found in free tier")
    assert _is_permanent(exc) is False


# ---------------------------------------------------------------------------
# complete() behaviour
# ---------------------------------------------------------------------------

def _client_raising(exc, *, succeed_after=None):
    fake = MagicMock()
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if succeed_after is not None and calls["n"] > succeed_after:
            return Dummy(value="ok")
        raise exc

    fake.chat.completions.create.side_effect = create
    return fake, calls


def test_permanent_error_fails_after_a_single_attempt():
    exc = RuntimeError("404 This model is no longer available to new users.")
    fake, calls = _client_raising(exc)

    with patch.object(llm_client, "get_client", return_value=fake), \
         patch("time.sleep") as sleep:
        with pytest.raises(LLMError, match="permanently"):
            llm_client.complete(Dummy, "prompt")

    assert calls["n"] == 1, "a permanent error must not be retried"
    sleep.assert_not_called(), "and must not sleep"


def test_transient_error_still_retries_to_exhaustion():
    exc = RuntimeError("503 service unavailable")
    fake, calls = _client_raising(exc)

    with patch.object(llm_client, "get_client", return_value=fake), \
         patch("time.sleep"):
        with pytest.raises(LLMError):
            llm_client.complete(Dummy, "prompt")

    assert calls["n"] == llm_client.settings.llm.backoff.max_attempts


def test_transient_error_recovers_when_the_service_comes_back():
    exc = RuntimeError("429 resource exhausted")
    fake, calls = _client_raising(exc, succeed_after=2)

    with patch.object(llm_client, "get_client", return_value=fake), \
         patch("time.sleep"):
        result = llm_client.complete(Dummy, "prompt")

    assert result.value == "ok"
    assert calls["n"] == 3


def test_backoff_is_bounded_by_max_seconds():
    """Delays must double but never exceed the configured ceiling."""
    exc = RuntimeError("500 internal error")
    fake, _ = _client_raising(exc)
    delays = []

    with patch.object(llm_client, "get_client", return_value=fake), \
         patch("time.sleep", side_effect=delays.append):
        with pytest.raises(LLMError):
            llm_client.complete(Dummy, "prompt")

    ceiling = float(llm_client.settings.llm.backoff.max_seconds)
    assert delays == sorted(delays), "delays must be non-decreasing"
    assert max(delays) <= ceiling


def test_configured_model_is_not_a_retired_or_moving_target():
    """Guards the two failure modes that have actually bitten this project."""
    model = str(llm_client.settings.llm.model)

    assert model != "gemini-2.5-flash", "retired 2026-08-08; every call 404s"
    assert not model.endswith("-latest"), (
        "a floating alias can change structured-output behaviour mid-run"
    )
    assert "-preview" not in model, "preview models are withdrawn without notice"
