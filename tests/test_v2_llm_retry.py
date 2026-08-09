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
# Attempt sequencing for a SINGLE provider
#
# These exercise _complete_with rather than complete(): they are about how one
# provider's retry budget is spent, and complete() would run the sequence twice
# (primary, then fallback) and report a combined error. Fallback orchestration
# is covered separately at the bottom of this file.
# ---------------------------------------------------------------------------

# The attempt budget is a HOSTED provider's property. The primary is now a
# local model configured with max_attempts=1 — it is either reachable or it is
# not, and retrying a laptop that is asleep helps nobody. So these exercise
# fallback:0 (Groq), which carries the full backoff budget.
_HOSTED = "fallback:0"


def _primary(response_model):
    """Run a hosted provider's attempt sequence for one prompt."""
    return llm_client._complete_with(
        _HOSTED, response_model, [{"role": "user", "content": "prompt"}]
    )


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
            _primary(Dummy)

    assert calls["n"] == 1, "a permanent error must not be retried"
    sleep.assert_not_called(), "and must not sleep"


def test_transient_error_still_retries_to_exhaustion():
    exc = RuntimeError("503 service unavailable")
    fake, calls = _client_raising(exc)

    with patch.object(llm_client, "get_client", return_value=fake), \
         patch("time.sleep"):
        with pytest.raises(LLMError):
            _primary(Dummy)

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
            _primary(Dummy)

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


# ---------------------------------------------------------------------------
# Budget exhaustion — a 429 that does NOT clear on retry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "429 Your project has exceeded its monthly spending cap. Please go to AI Studio",
    "429 RESOURCE_EXHAUSTED: spend cap reached",
    "429 You exceeded your current quota, please check your plan and billing details",
    "429 Quota exceeded for quota metric 'Generate requests per day'",
])
def test_budget_exhaustion_is_recognised(message):
    """Spend caps and daily quotas arrive as 429 but never clear mid-run."""
    from src.llm.client import _is_budget_exhausted

    exc = RuntimeError(message)
    assert _is_budget_exhausted(exc) is True
    assert _is_permanent(exc) is True, "must not be retried"


@pytest.mark.parametrize("message", [
    "429 Resource exhausted. Please retry shortly.",
    "429 Too many requests per minute",
])
def test_ordinary_rate_limiting_still_retries(message):
    """A per-minute limit clears on its own; it must not abort the run."""
    from src.llm.client import _is_budget_exhausted

    exc = RuntimeError(message)
    assert _is_budget_exhausted(exc) is False
    assert _is_permanent(exc) is False


def test_budget_error_raises_the_dedicated_type_on_first_attempt():
    """The orchestrator distinguishes this from a per-job failure by type."""
    from src.llm.client import LLMBudgetError

    exc = RuntimeError(
        "429 Your project has exceeded its monthly spending cap."
    )
    fake, calls = _client_raising(exc)

    with patch.object(llm_client, "get_client", return_value=fake), \
         patch("time.sleep") as sleep:
        with pytest.raises(LLMBudgetError, match="out of budget"):
            _primary(Dummy)

    assert calls["n"] == 1, "a spend cap must not be retried"
    sleep.assert_not_called()


def test_budget_error_is_an_llm_error_subclass():
    """Callers catching LLMError must still catch this."""
    from src.llm.client import LLMBudgetError

    assert issubclass(LLMBudgetError, LLMError)


# ---------------------------------------------------------------------------
# Fallback provider
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_clients():
    """Clients are cached per provider; don't leak between tests."""
    llm_client.reset_clients()
    yield
    llm_client.reset_clients()


def test_fallback_is_configured_and_enabled():
    """The escape hatch must actually be on, and point somewhere different."""
    assert llm_client.fallback_enabled() is True

    primary = llm_client.provider_config("primary")
    fallback = llm_client.provider_config("fallback")
    assert str(fallback.base_url) != str(primary.base_url)
    assert str(fallback.api_key_env) != str(primary.api_key_env)


def test_no_fallback_model_is_a_thinking_model():
    """Reasoning tokens bill as output, on every link of the chain.

    gemini-3.6-flash cost ~20x its apparent token count on 2026-08-08.
    """
    for which, cfg in llm_client.provider_chain()[1:]:
        model = str(cfg.model)
        assert "3.6" not in model and "3.5" not in model, (
            f"{which} ({model}) may emit reasoning tokens; prefer a "
            f"lite/non-thinking model"
        )


def test_permanent_primary_failure_falls_back():
    """A retired model on the primary must not fail the job."""
    from src.llm.client import LLMError

    def primary(*a, **k):
        raise RuntimeError("404 model is no longer available")

    good = MagicMock()
    good.chat.completions.create.return_value = Dummy(value="from-fallback")
    bad = MagicMock()
    bad.chat.completions.create.side_effect = primary

    def pick(which="primary"):
        return bad if which == "primary" else good

    with patch.object(llm_client, "get_client", side_effect=pick), \
         patch("time.sleep"):
        result = llm_client.complete(Dummy, "prompt")

    assert result.value == "from-fallback"


def test_primary_spend_cap_falls_back():
    """The exact failure of 2026-08-08 must now be survivable."""
    bad = MagicMock()
    bad.chat.completions.create.side_effect = RuntimeError(
        "429 Your project has exceeded its monthly spending cap."
    )
    good = MagicMock()
    good.chat.completions.create.return_value = Dummy(value="ok")

    with patch.object(
        llm_client, "get_client",
        side_effect=lambda which="primary": bad if which == "primary" else good,
    ), patch("time.sleep"):
        assert llm_client.complete(Dummy, "prompt").value == "ok"


def test_both_providers_out_of_budget_raises_budget_error():
    """When neither can serve, the run must abort rather than grind on."""
    from src.llm.client import LLMBudgetError

    dead = MagicMock()
    dead.chat.completions.create.side_effect = RuntimeError(
        "429 exceeded your current quota"
    )

    with patch.object(llm_client, "get_client", return_value=dead), \
         patch("time.sleep"):
        with pytest.raises(LLMBudgetError):
            llm_client.complete(Dummy, "prompt")


def test_whole_chain_failing_reports_every_provider():
    """The error must name each provider, or debugging is guesswork."""
    from src.llm.client import LLMError

    dead = MagicMock()
    dead.chat.completions.create.side_effect = RuntimeError("503 unavailable")

    with patch.object(llm_client, "get_client", return_value=dead), \
         patch("time.sleep"):
        with pytest.raises(LLMError, match="providers failed"):
            llm_client.complete(Dummy, "prompt")


def test_a_hosted_provider_absorbs_throttling_itself():
    """Rate limiting is a hosted provider's own backoff to handle.

    Switching providers on a per-minute limit would send steady traffic down
    the chain for no reason. (The local primary is the deliberate exception:
    max_attempts=1, because an absent laptop will not become present.)
    """
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 Too many requests per minute")
        return Dummy(value="primary-recovered")

    hosted = MagicMock()
    hosted.chat.completions.create.side_effect = flaky
    downstream = MagicMock()

    with patch.object(
        llm_client, "get_client",
        side_effect=lambda which="primary": hosted if which == _HOSTED else downstream,
    ), patch("time.sleep"):
        result = llm_client._complete_with(
            _HOSTED, Dummy, [{"role": "user", "content": "prompt"}]
        )

    assert result.value == "primary-recovered"
    downstream.chat.completions.create.assert_not_called()


def test_no_fallback_configured_propagates_the_primary_error():
    from src.llm.client import LLMError

    dead = MagicMock()
    dead.chat.completions.create.side_effect = RuntimeError("404 not found")

    with patch.object(llm_client, "get_client", return_value=dead), \
         patch.object(llm_client, "fallback_enabled", return_value=False), \
         patch("time.sleep"):
        with pytest.raises(LLMError, match="permanently"):
            llm_client.complete(Dummy, "prompt")


def test_clients_are_cached_per_provider():
    """Primary and fallback must be able to coexist in one run."""
    built = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            built.append(kwargs["base_url"])

    # Only providers reached over the OpenAI transport build an OpenAI client.
    hosted = [
        (which, cfg) for which, cfg in llm_client.provider_chain()
        if str(cfg.get("api", "openai")) != "ollama"
    ]
    keys = {str(cfg.api_key_env): f"k{i}" for i, (_, cfg) in enumerate(hosted)}

    with patch("openai.OpenAI", FakeOpenAI), \
         patch("instructor.from_openai", side_effect=lambda c, mode: c), \
         patch.dict("os.environ", keys):
        a = llm_client.get_client(hosted[0][0])
        b = llm_client.get_client(hosted[0][0])
        c = llm_client.get_client(hosted[1][0])

    assert a is b, "a provider's client should be cached, not rebuilt"
    assert a is not c, "each provider needs its own client"
    assert len(set(built)) == 2
