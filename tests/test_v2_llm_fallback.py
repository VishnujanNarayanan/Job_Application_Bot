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


def test_chain_is_ordered_cheapest_first():
    """Free providers must be exhausted before a metered one is touched.

    Groq is free; Gemini is metered and already hit a spend cap once.
    Reordering these would mean paying for an outage the free account could
    have absorbed.
    """
    providers = [str(cfg.provider) for _, cfg in llm_client.provider_chain()]

    assert providers[0] == "groq", "the free provider must lead"
    assert providers[-1] == "gemini", "the metered provider must be last resort"


def test_cerebras_stays_disabled():
    """Verified 2026-08-08: the key authenticates and models list, but a real
    call returns 402 payment_required. Enabling it would put a paid provider
    in a free-tier-only pipeline (hard rule #12) — and listing models is not
    enough to notice, which is exactly why this is pinned.
    """
    entries = {name: enabled for name, _cfg, enabled in llm_client.all_providers()}

    assert entries.get("cerebras") is False
    assert "cerebras" not in [
        str(cfg.provider) for _, cfg in llm_client.provider_chain()
    ]


def test_no_provider_in_the_chain_is_a_thinking_model():
    """Reasoning tokens bill as output, on every link of the chain.

    gemini-3.6-flash cost roughly 20x its apparent token count parsing 60 job
    ads, which is what triggered the spend cap in the first place.
    """
    for _, cfg in llm_client.provider_chain():
        model = str(cfg.model)
        assert not model.startswith("gemini-3."), (
            f"{model} may emit reasoning tokens billed as output"
        )

    metered = [
        cfg for _, cfg in llm_client.provider_chain()
        if str(cfg.provider) == "gemini"
    ]
    assert metered, "the chain should still end at the metered provider"
    assert "lite" in str(metered[0].model), (
        "the one provider that costs money must be the cheapest model"
    )


def test_every_provider_has_its_own_key_and_endpoint():
    """A chain that shares a key or a host is not a chain — one outage or one
    exhausted account would take every link down together."""
    chain = llm_client.provider_chain()

    assert len({str(cfg.base_url) for _, cfg in chain}) == len(chain)
    assert len({str(cfg.api_key_env) for _, cfg in chain}) == len(chain)


def test_clients_are_cached_per_provider():
    """Every provider must be able to coexist within one run."""
    built: list[str] = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            built.append(kwargs["base_url"])

    keys = {
        str(cfg.api_key_env): f"k{i}"
        for i, (_, cfg) in enumerate(llm_client.provider_chain())
    }

    with patch("openai.OpenAI", FakeOpenAI), \
         patch("instructor.from_openai", side_effect=lambda c, mode: c), \
         patch.dict("os.environ", keys):
        clients = [llm_client.get_client("primary"), llm_client.get_client("primary")]
        clients += [
            llm_client.get_client(which)
            for which, _ in llm_client.provider_chain()[1:]
        ]

    expected = len(llm_client.provider_chain())
    assert clients[0] is clients[1], "primary should be cached, not rebuilt"
    assert len({id(c) for c in clients}) == expected, "one client per provider"
    assert len(set(built)) == expected, "each provider needs its own base_url"


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


def test_every_failure_is_named_when_the_whole_chain_is_down():
    """Without every provider's own error, debugging is guesswork — the links
    fail for different reasons."""
    primary = _client(raises=RuntimeError("404 not found"))
    fallback = _client(raises=RuntimeError("503 unavailable"))

    with _split(primary, fallback), patch("time.sleep"):
        with pytest.raises(LLMError) as excinfo:
            llm_client.complete(Dummy, "p")

    message = str(excinfo.value)
    chain = llm_client.provider_chain()
    assert f"All {len(chain)} providers failed" in message
    for _, cfg in chain:
        assert str(cfg.provider) in message


def test_a_capped_provider_does_not_stop_a_later_one_serving():
    """A spend cap is one account's problem. Gemini being capped is exactly
    why the chain exists, so it must not abort while a link remains."""
    capped = _client(raises=RuntimeError(
        "429 Your project has exceeded its monthly spending cap."
    ))
    working = _client(returns=Dummy(value="served"))

    last = llm_client.provider_chain()[-1][0]

    def pick(which="primary"):
        return working if which == last else capped

    with patch.object(llm_client, "get_client", side_effect=pick), \
         patch("time.sleep"):
        assert llm_client.complete(Dummy, "p").value == "served"


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
