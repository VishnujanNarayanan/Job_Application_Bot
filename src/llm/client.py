"""LLM transport — one structured call, any OpenAI-compatible provider.

Every call returns a Pydantic model: Instructor enforces the schema at the API
boundary so we never hand-parse JSON out of a model string (CLAUDE.md
tech-stack rule). Only Call 1a (the JD parse) remains — Call 1b became
deterministic maths in ``src/builder/deterministic.py``.

**The provider is configuration, not code.** Two failures on 2026-08-08 forced
this: ``gemini-2.5-flash`` was retired mid-run (every call 404'd), and hours
later the project hit its monthly spend cap (every call 429'd). Both were
single-vendor outages for a task — extracting fields from a job ad — that any
competent instruct model handles. ``base_url`` + ``model`` + ``api_key_env`` in
config means switching provider, or escaping one, is an edit rather than a
rewrite.

Cost lesson from the same day: prefer a **non-thinking** model. Reasoning
tokens bill as output, and a reasoning model parsing 60 job ads cost roughly
20x what the token count suggested.

Providers are reached through their OpenAI-compatible endpoints (Groq,
Cerebras, OpenRouter natively; Gemini via
``/v1beta/openai/``), so one code path serves all of them. Heavy imports stay
inside ``get_client`` so importing this module is cheap and unit tests can
patch ``complete`` without a key or a network.
"""

from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import TypeVar

import structlog
from pydantic import BaseModel

from src.config import settings

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when a call fails after exhausting retries."""


class LLMBudgetError(LLMError):
    """The account cannot serve any more calls — stop the run.

    Distinct from ``LLMError`` because it is not a per-job problem: a spend cap
    or exhausted daily quota fails every remaining job in the run identically,
    so the orchestrator should abandon the run rather than march through the
    rest of the batch collecting PARSE_FAILUREs.
    """


# Substrings identifying failures that will NEVER succeed on retry: a retired
# or misspelled model, a malformed request, a bad or unauthorised key. Retrying
# these burns the whole backoff budget (minutes per job, every job) on an
# outcome that cannot change — exactly what a retired model did before this
# guard existed.
_PERMANENT_ERROR_MARKERS = (
    "is no longer available",
    "not found for api version",
    "is not found",
    "does not exist",
    "model_not_found",
    "404",
    "api key not valid",
    "api_key_invalid",
    "invalid_api_key",
    "incorrect api key",
    "permission denied",
    "permission_denied",
    "403",
    "401",
    "invalid argument",
    "invalid_argument",
)

# Account-level exhaustion. These arrive as 429/RESOURCE_EXHAUSTED, the same
# status as ordinary rate limiting, but they do NOT clear on their own within a
# run: a spend cap holds until the operator raises it or the month rolls over,
# and a daily quota until the quota window resets. Treating them as transient
# cost a full 5-attempt backoff on every job of a 25-job run.
_BUDGET_ERROR_MARKERS = (
    "spending cap",
    "spend cap",
    "exceeded your current quota",
    "billing",
    "insufficient_quota",
    "free_tier_requests",
    "requests per day",
    "tokens per day",
    "per_day",
)


def _is_budget_exhausted(exc: Exception) -> bool:
    """True when the account itself is out of budget or daily quota."""
    message = str(exc).casefold()
    return any(marker in message for marker in _BUDGET_ERROR_MARKERS)


def _is_permanent(exc: Exception) -> bool:
    """True when retrying ``exc`` cannot possibly help.

    Matched on message text rather than exception type because the SDK,
    Instructor and the transport each raise their own classes and wrap one
    another; the status text is the one thing that survives the layers.
    """
    message = str(exc).casefold()
    if _is_budget_exhausted(exc):
        return True    # a 429, but nothing about it changes on retry
    if "429" in message or "resource_exhausted" in message:
        return False   # ordinary rate limiting — transient, keep retrying
    return any(marker in message for marker in _PERMANENT_ERROR_MARKERS)


def provider_config(which: str = "primary"):
    """The config block for ``primary`` or ``fallback``."""
    if which == "primary":
        return settings.llm
    return settings.llm.fallback


def fallback_enabled() -> bool:
    """True when a secondary provider is configured and switched on."""
    fb = settings.llm.get("fallback")
    return bool(fb) and bool(fb.get("enabled", False))


def _api_key(cfg, which: str) -> str:
    """Read a provider's key from the env var named in its config block."""
    var = str(cfg.api_key_env)
    key = os.environ.get(var, "").strip()
    if not key:
        raise LLMError(
            f"{var} is not set in .env. The {which} LLM provider is "
            f"'{cfg.provider}'; see .env.example."
        )
    return key


# Cached per provider rather than with a single-slot lru_cache, so the primary
# and the fallback can both be live in one run.
_CLIENTS: dict[str, object] = {}


def get_client(which: str = "primary"):
    """Build and cache the Instructor-wrapped chat client for one provider.

    Any OpenAI-compatible endpoint works, so a provider is entirely determined
    by its ``base_url`` and ``model`` in config.
    """
    if which in _CLIENTS:
        return _CLIENTS[which]

    import instructor
    from openai import OpenAI

    cfg = provider_config(which)
    mode = getattr(instructor.Mode, str(cfg.instructor_mode))
    client = OpenAI(
        api_key=_api_key(cfg, which),
        base_url=str(cfg.base_url),
        timeout=float(cfg.get("request_timeout_seconds", 60)),
        max_retries=0,   # retries and backoff are handled below, not by the SDK
    )
    wrapped = instructor.from_openai(client, mode=mode)
    _CLIENTS[which] = wrapped
    log.info(
        "llm_client_built",
        which=which,
        provider=str(cfg.provider),
        model=str(cfg.model),
        base_url=str(cfg.base_url),
    )
    return wrapped


def reset_clients() -> None:
    """Drop cached clients (tests, and after a config change)."""
    _CLIENTS.clear()


def _complete_with(
    which: str, response_model: type[T], messages: list[dict[str, str]]
) -> T:
    """Run one provider's attempt sequence. Raises on give-up."""
    cfg = provider_config(which)
    backoff = settings.llm.backoff
    delay = float(backoff.initial_seconds)
    max_delay = float(backoff.max_seconds)
    attempts = int(backoff.max_attempts)

    client = get_client(which)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return client.chat.completions.create(
                model=str(cfg.model),
                messages=messages,
                response_model=response_model,
                max_retries=0,   # Instructor's own reask loop stays off
            )
        except Exception as exc:  # noqa: BLE001 - many wrapped types; classify by text
            last_error = exc

            # Account out of budget — no amount of retrying or waiting helps.
            if _is_budget_exhausted(exc):
                log.error(
                    "llm_budget_exhausted",
                    which=which,
                    provider=str(cfg.provider),
                    model=str(cfg.model),
                    attempt=attempt,
                    error=str(exc),
                )
                raise LLMBudgetError(
                    f"{cfg.provider} account is out of budget or quota: {exc}"
                ) from exc

            if _is_permanent(exc):
                log.error(
                    "llm_failure",
                    which=which,
                    provider=str(cfg.provider),
                    model=str(cfg.model),
                    attempt=attempt,
                    permanent=True,
                    error=str(exc),
                    exc_info=exc,
                )
                raise LLMError(
                    f"Call failed permanently "
                    f"(provider={cfg.provider}, model={cfg.model}): {exc}"
                ) from exc

            if attempt < attempts:
                log.warning(
                    "llm_retry",
                    which=which,
                    attempt=attempt,
                    max_attempts=attempts,
                    error=str(exc),
                )
                time.sleep(delay)
                delay = min(delay * 2, max_delay)

    # exc_info=last_error (the captured instance), NOT True — we are outside
    # the except block here, so sys.exc_info() is already cleared.
    log.error(
        "llm_failure",
        which=which,
        provider=str(cfg.provider),
        model=str(cfg.model),
        attempts=attempts,
        error=str(last_error),
        exc_info=last_error,
    )
    raise LLMError(
        f"Call failed after {attempts} attempts "
        f"(provider={cfg.provider}, model={cfg.model})"
    ) from last_error


def complete(response_model: type[T], prompt: str, *, system: str | None = None) -> T:
    """Run one completion and return ``response_model``.

    Tries the primary provider with exponential backoff. If it gives up, and a
    fallback is configured, tries that once with its own attempt budget.

    The fallback exists because both of the day's failures were provider-wide,
    not per-job: a retired model 404'd every request, and a spend cap 429'd
    every request. Either would have taken the whole run down; a second
    provider turns them into a slower run instead. Ordinary rate limiting is
    NOT a fallback trigger — the primary's own backoff handles that, and
    switching on it would send steady traffic to the secondary for no reason.

    :class:`LLMBudgetError` still propagates when BOTH providers are out of
    budget, so the orchestrator abandons the run rather than looping.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        return _complete_with("primary", response_model, messages)
    except LLMError as primary_error:
        if not fallback_enabled():
            raise

        fb = provider_config("fallback")
        log.warning(
            "llm_fallback_engaged",
            from_provider=str(settings.llm.provider),
            to_provider=str(fb.provider),
            to_model=str(fb.model),
            reason=str(primary_error)[:200],
        )
        try:
            result = _complete_with("fallback", response_model, messages)
        except LLMBudgetError:
            # Both exhausted: the run cannot continue.
            raise
        except LLMError as fallback_error:
            raise LLMError(
                f"Both providers failed. "
                f"primary({settings.llm.provider}): {primary_error} | "
                f"fallback({fb.provider}): {fallback_error}"
            ) from fallback_error

        log.info("llm_fallback_succeeded", provider=str(fb.provider))
        return result
