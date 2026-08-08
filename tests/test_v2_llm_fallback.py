"""Layer 3 — provider fallback.

Both LLM failures on 2026-08-08 were provider-wide rather than per-job: a
retired model 404'd every request, and hours later a spend cap 429'd every
request. Either would have taken a whole run down. These tests pin the
behaviour that turns those into a slower run instead.

The distinction that matters: a *provider-wide* failure switches providers, a
*transient* one does not. Switching on ordinary rate limiting would send steady
traffic to the secondary for no reason.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from src.llm import client as llm_client
from src.llm.client import LLMBudgetError, LLMError


class Dummy(BaseModel):
    value: str


@pytest.fixture(autouse=True)
def _clean_clients():
    """Clients are cached per provider; don't leak between tests."""
    llm_client.reset_clients()
    yield
    llm_client.reset_clients()


def _split(primary, fallback):
    """Patch get_client so each provider gets its own stub."""
    return patch.object(
        llm_client,
        "get_client",
        side_effect=lambda which="primary": primary if which == "primary" else fallback,
    )


def _client(*, returns=None, raises=None):
    stub = MagicMock()
    if raises is not None:
        stub.chat.completions.create.side_effect = raises
    else:
        stub.chat.completions.create.return_value = returns
    return stub


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_fallback_is_enabled_and_points_elsewhere():
    assert llm_client.fallback_enabled() is True

    primary = llm_client.provider_config("primary")
    fallback = llm_client.provider_config("fallback")
    assert str(fallback.base_url) != str(primary.base_url)
    assert str(fallback.api_key_env) != str(primary.api_key_env)


def test_fallback_model_is_not_a_thinking_model():
    """Reasoning tokens bill as output; the fallback exists to be cheap.

    gemini-3.6-flash cost roughly 20x its apparent token count parsing 60 job
    ads, which is what triggered the spend cap in the first place.
    """
    model = str(llm_client.provider_config("fallback").model)

    assert "lite" in model, f"{model} should be a lite/non-thinking model"
    assert not model.startswith("gemini-3."), (
        f"{model} may emit reasoning tokens billed as output"
    )


def test_clients_are_cached_per_provider():
    """Primary and fallback must be able to coexist within one run."""
    built: list[str] = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            built.append(kwargs["base_url"])

    with patch("openai.OpenAI", FakeOpenAI), \
         patch("instructor.from_openai", side_effect=lambda c, mode: c), \
         patch.dict("os.environ", {"GROQ_API_KEY": "k1", "GEMINI_API_KEY": "k2"}):
        first = llm_client.get_client("primary")
        again = llm_client.get_client("primary")
        other = llm_client.get_client("fallback")

    assert first is again, "primary should be cached, not rebuilt"
    assert first is not other, "fallback must be a distinct client"
    assert len(set(built)) == 2, "each provider needs its own base_url"


def test_missing_key_names_the_variable_and_the_provider():
    """The commonest setup mistake should say exactly what to add."""
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(LLMError) as excinfo:
            llm_client.get_client("primary")

    message = str(excinfo.value)
    assert str(llm_client.provider_config("primary").api_key_env) in message
    assert "primary" in message


# ---------------------------------------------------------------------------
# Failover behaviour
# ---------------------------------------------------------------------------

def test_retired_model_on_primary_falls_back():
    """The first failure of 2026-08-08: 404 'no longer available'."""
    primary = _client(raises=RuntimeError("404 model is no longer available"))
    fallback = _client(returns=Dummy(value="from-fallback"))

    with _split(primary, fallback), patch("time.sleep"):
        assert llm_client.complete(Dummy, "p").value == "from-fallback"

    primary.chat.completions.create.assert_called_once()


def test_spend_cap_on_primary_falls_back():
    """The second failure of 2026-08-08: a 429 that never clears."""
    primary = _client(raises=RuntimeError(
        "429 Your project has exceeded its monthly spending cap."
    ))
    fallback = _client(returns=Dummy(value="ok"))

    with _split(primary, fallback), patch("time.sleep"):
        assert llm_client.complete(Dummy, "p").value == "ok"


def test_missing_primary_key_falls_back():
    """A provider you haven't signed up for yet must not block the run."""
    fallback = _client(returns=Dummy(value="ok"))

    def pick(which="primary"):
        if which == "primary":
            raise LLMError("GROQ_API_KEY is not set in .env")
        return fallback

    with patch.object(llm_client, "get_client", side_effect=pick), \
         patch("time.sleep"):
        assert llm_client.complete(Dummy, "p").value == "ok"


def test_transient_error_does_not_engage_the_fallback():
    """Rate limiting is the primary's own backoff to absorb."""
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 Too many requests per minute")
        return Dummy(value="primary-recovered")

    primary = MagicMock()
    primary.chat.completions.create.side_effect = flaky
    fallback = MagicMock()

    with _split(primary, fallback), patch("time.sleep"):
        assert llm_client.complete(Dummy, "p").value == "primary-recovered"

    fallback.chat.completions.create.assert_not_called()


def test_primary_exhausting_its_retries_falls_back():
    """Give-up after the full backoff should still try the other provider."""
    primary = _client(raises=RuntimeError("503 service unavailable"))
    fallback = _client(returns=Dummy(value="rescued"))

    with _split(primary, fallback), patch("time.sleep"):
        assert llm_client.complete(Dummy, "p").value == "rescued"

    attempts = int(llm_client.settings.llm.backoff.max_attempts)
    assert primary.chat.completions.create.call_count == attempts


# ---------------------------------------------------------------------------
# When both are down
# ---------------------------------------------------------------------------

def test_both_out_of_budget_raises_budget_error_to_abort_the_run():
    """src/main.py aborts on this type rather than grinding through the batch."""
    dead = _client(raises=RuntimeError("429 exceeded your current quota"))

    with _split(dead, dead), patch("time.sleep"):
        with pytest.raises(LLMBudgetError):
            llm_client.complete(Dummy, "p")


def test_fallback_budget_exhaustion_surfaces_as_budget_error():
    """Primary merely broken, fallback out of budget -> still abort the run."""
    primary = _client(raises=RuntimeError("404 not found"))
    fallback = _client(raises=RuntimeError(
        "429 Your project has exceeded its monthly spending cap."
    ))

    with _split(primary, fallback), patch("time.sleep"):
        with pytest.raises(LLMBudgetError):
            llm_client.complete(Dummy, "p")


def test_both_failing_reports_both_providers():
    """Without both names in the message, debugging is guesswork."""
    primary = _client(raises=RuntimeError("404 not found"))
    fallback = _client(raises=RuntimeError("503 unavailable"))

    with _split(primary, fallback), patch("time.sleep"):
        with pytest.raises(LLMError) as excinfo:
            llm_client.complete(Dummy, "p")

    message = str(excinfo.value)
    assert "Both providers failed" in message
    assert str(llm_client.provider_config("primary").provider) in message
    assert str(llm_client.provider_config("fallback").provider) in message


def test_disabled_fallback_propagates_the_primary_error():
    primary = _client(raises=RuntimeError("404 not found"))

    with _split(primary, MagicMock()), \
         patch.object(llm_client, "fallback_enabled", return_value=False), \
         patch("time.sleep"):
        with pytest.raises(LLMError, match="permanently"):
            llm_client.complete(Dummy, "p")


def test_system_prompt_reaches_both_providers():
    """The fallback must get the same messages, not a truncated retry."""
    primary = _client(raises=RuntimeError("404 not found"))
    fallback = _client(returns=Dummy(value="ok"))

    with _split(primary, fallback), patch("time.sleep"):
        llm_client.complete(Dummy, "user text", system="system text")

    sent = fallback.chat.completions.create.call_args.kwargs["messages"]
    assert sent[0] == {"role": "system", "content": "system text"}
    assert sent[1] == {"role": "user", "content": "user text"}
