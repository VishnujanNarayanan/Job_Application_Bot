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
import re
import time
from functools import lru_cache
from typing import TypeVar

import structlog
from pydantic import BaseModel

from src.config import Section, settings

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when a call fails after exhausting retries."""


class LLMConfigError(LLMError):
    """A provider is misconfigured — it cannot serve ANY call this run.

    Separate from a failed call because it is a property of the process, not
    of the request: an unset ``${OLLAMA_BASE_URL}`` is exactly as broken on job
    25 as on job 1. The chain remembers it (see ``_DEAD_PROVIDERS``) and skips
    the provider for the rest of the run instead of rediscovering it per job —
    the 2026-08-09 Actions run logged the same traceback 25 times.
    """


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
    "insufficient_quota",
    "free_tier_requests",
    # A hard paywall: the account cannot serve at all until someone pays.
    # Cerebras returns this for a key that authenticates and lists models
    # perfectly happily (verified 2026-08-08).
    "payment_required",
    "payment required",
)

# A quota measured PER DAY does not clear inside a run, so it counts as
# exhaustion even though it arrives worded as a rate limit.
_DAILY_QUOTA_MARKERS = (
    "requests per day",
    "tokens per day",
    "per_day",
)

# A quota measured PER MINUTE clears in seconds — the backoff exists for
# exactly this. These must never be mistaken for exhaustion.
#
# Groq appends "Upgrade to Dev Tier today at .../settings/billing" to EVERY
# per-minute rate-limit message. A bare "billing" marker therefore matched
# routine throttling and abandoned a live run after 5 of 21 jobs on
# 2026-08-08, sending it to a capped fallback for a limit that would have
# cleared in 3 seconds. That marker is gone; these take precedence instead.
_TRANSIENT_RATE_MARKERS = (
    "tokens per minute",
    "requests per minute",
    "rate limit reached",
    "rate_limit_exceeded",
    "please try again in",
)


# Providers that throttle usually say exactly how long to wait. Groq returns
# "Please try again in 11.344999999s"; others phrase it "retry after 5
# seconds". Guessing instead — 2s, 4s, 8s — retries before the window has
# reopened, so every guess is refused and the attempt budget is spent on
# nothing. That is what exhausted 5 attempts and ended a run on 2026-08-08.
_RETRY_AFTER = re.compile(
    r"(?:try again in|retry after|retry in)\s+([0-9]+(?:\.[0-9]+)?)\s*(m?s|seconds?|secs?)?",
    re.IGNORECASE,
)

# Never sleep longer than this on a provider's say-so. A limit measured in
# hours is a daily quota, which is classified as exhaustion and moves the
# chain on rather than blocking a run.
_MAX_HINTED_WAIT_SECONDS = 60.0


def retry_after_seconds(exc: Exception) -> float | None:
    """How long the provider asked us to wait, if it said.

    Returns None when there is no usable hint, leaving exponential backoff to
    decide.
    """
    match = _RETRY_AFTER.search(str(exc))
    if not match:
        return None

    value = float(match.group(1))
    if (match.group(2) or "").lower() == "ms":
        value /= 1000.0
    if value <= 0 or value > _MAX_HINTED_WAIT_SECONDS:
        return None
    # A shade past the stated window: retrying on the exact boundary is
    # routinely a hair too early.
    return value + 0.5


def _is_budget_exhausted(exc: Exception) -> bool:
    """True when the account itself is out of budget or daily quota.

    Order matters: a daily quota outranks the rate-limit wording it shares,
    and per-minute throttling outranks any billing chatter in the message.
    """
    message = str(exc).casefold()
    if any(marker in message for marker in _DAILY_QUOTA_MARKERS):
        return True
    if any(marker in message for marker in _TRANSIENT_RATE_MARKERS):
        return False
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


def _fallback_blocks() -> list:
    """The enabled fallback providers, in the order they should be tried.

    ``llm.fallbacks`` is a list so the chain can be as long as the operator
    wants; a single ``llm.fallback`` mapping is still accepted so an existing
    instance's config keeps working.

    Order is the operator's cost preference, not ours: free providers first,
    metered ones last, so a metered account is only ever reached when every
    free one has failed.
    """
    return [cfg for cfg in _configured_fallbacks() if bool(cfg.get("enabled", False))]


def _configured_fallbacks() -> list:
    """Every fallback block in config, enabled or not."""
    raw = settings.llm.get("fallbacks")
    if raw is None:
        single = settings.llm.get("fallback")
        raw = [single] if single else []
    return [Section(entry) if isinstance(entry, dict) else entry for entry in raw]


def all_providers() -> list[tuple[str, object, bool]]:
    """``[(provider_name, config, enabled)]`` for every configured provider.

    Includes disabled entries, which ``provider_chain`` deliberately omits —
    tooling needs to inspect a candidate provider before it is switched on.
    Keyed by provider name rather than chain position, because a disabled
    entry has no position.
    """
    providers = [(str(settings.llm.provider), settings.llm, True)]
    providers += [
        (str(cfg.provider), cfg, bool(cfg.get("enabled", False)))
        for cfg in _configured_fallbacks()
    ]
    return providers


def provider_chain() -> list[tuple[str, object]]:
    """``[(name, config)]`` for every provider, primary first."""
    chain: list[tuple[str, object]] = [("primary", settings.llm)]
    chain += [(f"fallback:{i}", cfg) for i, cfg in enumerate(_fallback_blocks())]
    return chain


def provider_config(which: str = "primary"):
    """The config block for ``primary``, ``fallback:N``, or ``fallback``.

    Bare ``fallback`` means the first enabled one — the next provider that
    would actually be tried. A provider NAME also resolves, including a
    disabled one, so tooling can address a candidate before switching it on.
    """
    if which == "primary":
        return settings.llm

    blocks = _fallback_blocks()
    index = 0
    if which.startswith("fallback:"):
        index = int(which.split(":", 1)[1])
    elif which != "fallback":
        for name, cfg, _enabled in all_providers():
            if name == which:
                return cfg
        raise ValueError(f"Unknown provider selector: {which!r}")

    try:
        return blocks[index]
    except IndexError as exc:
        raise LLMError(
            f"No fallback provider at position {index}; "
            f"{len(blocks)} are enabled in config.yaml under llm.fallbacks."
        ) from exc


def fallback_enabled() -> bool:
    """True when at least one secondary provider is configured and on."""
    return bool(_fallback_blocks())


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

# `${VAR}` left intact by src.config._expand_env — i.e. the variable is unset.
_UNEXPANDED_ENV = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# Providers found structurally unusable this run: {which: reason}. Populated by
# LLMConfigError and never retried, so a misconfigured provider costs one log
# line per run instead of one per job.
_DEAD_PROVIDERS: dict[str, str] = {}


def get_client(which: str = "primary"):
    """Build and cache the Instructor-wrapped chat client for one provider.

    Any OpenAI-compatible endpoint works, so a provider is entirely determined
    by its ``base_url`` and ``model`` in config.
    """
    if which in _CLIENTS:
        return _CLIENTS[which]

    cfg = provider_config(which)

    import instructor
    from openai import OpenAI

    base_url = str(cfg.base_url)
    # A `${VAR}` that survived expansion means the variable is unset. Catch it
    # here, where the provider and the missing name can both be named, instead
    # of letting httpx raise "Request URL is missing an 'http://' protocol"
    # once per job. On the GitHub Actions run of 2026-08-09 that produced 25
    # identical tracebacks and sent every parse to Groq, which then exhausted
    # its daily token budget mid-run.
    if _UNEXPANDED_ENV.search(base_url):
        missing = ", ".join(_UNEXPANDED_ENV.findall(base_url))
        raise LLMConfigError(
            f"{cfg.provider}: base_url still contains {base_url!r} — "
            f"environment variable(s) not set: {missing}"
        )

    mode = getattr(instructor.Mode, str(cfg.instructor_mode))
    client = OpenAI(
        api_key=_api_key(cfg, which),
        base_url=base_url,
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
    _DEAD_PROVIDERS.clear()


# The bespoke Ollama transport that used to live here is gone. It existed
# because /v1 was measured as ignoring `response_format`, so Instructor's
# JSON_SCHEMA mode "changed nothing" — that is no longer true (and may have
# been an artefact of an older Ollama). Re-measured 2026-08-09 against Ollama
# 0.32.6: /v1 honours `response_format: {"type": "json_schema", ...}`,
# respecting both `required` and `minItems`, so every provider now shares one
# OpenAI-compatible transport and Instructor owns validation.
#
# Dropping it also fixed the extraction bug. The native path sent the raw
# schema and nothing else, and a 7B model omitted required_skills,
# nice_to_have and responsibilities on 71% of real JDs because the schema made
# them optional. Instructor's JSON_SCHEMA mode also states the schema in the
# prompt, so the model is told the fields exist rather than left to infer it
# from a grammar: 5/5 populated on the same JDs.


def _call_once(which: str, cfg, response_model: type[T], messages) -> T:
    """One attempt, through the single client seam every test patches."""
    return get_client(which).chat.completions.create(
        model=str(cfg.model),
        messages=messages,
        response_model=response_model,
        max_retries=0,   # Instructor's own reask loop stays off
    )


def _complete_with(
    which: str, response_model: type[T], messages: list[dict[str, str]]
) -> T:
    """Run one provider's attempt sequence. Raises on give-up."""
    # Already proved unusable this run — don't rebuild the client, don't call,
    # don't log another traceback. Just hand the chain the reason so it moves
    # to the next provider immediately.
    if which in _DEAD_PROVIDERS:
        raise LLMConfigError(_DEAD_PROVIDERS[which])

    cfg = provider_config(which)
    backoff = settings.llm.backoff
    delay = float(backoff.initial_seconds)
    max_delay = float(backoff.max_seconds)
    # A provider may cap its own attempts. A local model is either there or it
    # is not: on a GitHub Actions runner the laptop is unreachable, and the
    # generous retry budget meant for a throttled hosted API would spend five
    # backoffs per job discovering that, before every job.
    attempts = int(cfg.get("max_attempts", backoff.max_attempts))

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return _call_once(which, cfg, response_model, messages)
        except LLMConfigError as exc:
            # Misconfiguration, not a failed request. Retrying cannot fix it
            # and neither can the next job, so record it once and retire the
            # provider for the run.
            _DEAD_PROVIDERS[which] = str(exc)
            log.error(
                "llm_provider_unusable",
                which=which,
                provider=str(cfg.provider),
                error=str(exc),
            )
            raise
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
                # The provider's own figure beats our guess: it knows when the
                # window reopens, and retrying early just spends an attempt.
                hinted = retry_after_seconds(exc)
                wait = hinted if hinted is not None else delay
                log.warning(
                    "llm_retry",
                    which=which,
                    attempt=attempt,
                    max_attempts=attempts,
                    wait_seconds=round(wait, 2),
                    hinted=hinted is not None,
                    error=str(exc),
                )
                time.sleep(wait)
                if hinted is None:
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
    exhausted = LLMError(
        f"Call failed after {attempts} attempts "
        f"(provider={cfg.provider}, model={cfg.model})"
    )
    # Retryable errors ran out of attempts rather than proving hopeless: the
    # window may well have reopened by the next job. Marked so the chain can
    # tell "this might work again" from "this cannot".
    exhausted.transient = True
    raise exhausted from last_error


def complete(
    response_model: type[T],
    prompt: str | None = None,
    *,
    system: str | None = None,
    prompt_fn=None,
) -> T:
    """Run one completion and return ``response_model``.

    Walks the configured provider chain in order, each with its own backoff
    budget, and returns the first success.

    The chain exists because both of the day's failures were provider-wide,
    not per-job: a retired model 404'd every request, and a spend cap 429'd
    every request. Either would have taken the whole run down; another
    provider turns that into a slower run instead. Ordinary rate limiting is
    NOT a trigger to move on — the current provider's own backoff handles
    that, and switching on it would send steady traffic down the chain for no
    reason.

    Budget exhaustion moves to the next provider rather than aborting: a spend
    cap is one account's problem, and the whole point of a chain is that
    another account can still serve. :class:`LLMBudgetError` is raised only
    when EVERY provider failed and at least one was out of budget, since the
    next job would hit the same wall — src/main.py abandons the run on it.
    """
    def build(cfg) -> list[dict[str, str]]:
        """The messages for one provider.

        ``prompt_fn`` lets the prompt depend on WHICH provider is about to be
        called, because how much job description to send is a property of the
        provider: a local model has no token budget and should read the whole
        ad, while a metered one further down the chain still needs it bounded.
        Building once up front would send whichever size to both.
        """
        body = prompt_fn(cfg) if prompt_fn is not None else prompt
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": body})
        return messages

    chain = provider_chain() if fallback_enabled() else [("primary", settings.llm)]

    # Nothing to fall back to: let the provider's own error through untouched,
    # rather than wrapping a single failure in chain language.
    if len(chain) == 1:
        return _complete_with("primary", response_model, build(settings.llm))

    failures: list[tuple[object, Exception]] = []
    budget_failures = 0

    for position, (which, cfg) in enumerate(chain):
        if position:
            previous_cfg, previous_error = failures[-1]
            log.warning(
                "llm_fallback_engaged",
                from_provider=str(previous_cfg.provider),
                to_provider=str(cfg.provider),
                to_model=str(cfg.model),
                reason=str(previous_error)[:200],
            )
        try:
            result = _complete_with(which, response_model, build(cfg))
        except LLMBudgetError as exc:
            budget_failures += 1
            failures.append((cfg, exc))
        except LLMError as exc:
            failures.append((cfg, exc))
        else:
            if position:
                log.info(
                    "llm_fallback_succeeded",
                    provider=str(cfg.provider),
                    position=position,
                )
            return result

    detail = " | ".join(
        f"{cfg.provider}: {error}" for cfg, error in failures
    )
    summary = f"All {len(chain)} providers failed. {detail}"
    last_error = failures[-1][1]

    # Abandon the run only when nothing here can succeed for the NEXT job
    # either — that is, some account is out of budget and no provider merely
    # ran out of patience. A retired model plus a capped fallback qualifies:
    # both fail every job identically.
    #
    # A transient failure anywhere disqualifies it. A run on 2026-08-08 was
    # abandoned after 4 of 13 jobs because Groq was briefly throttled while
    # Gemini sat behind a spend cap; Groq's window reopens in seconds, so the
    # remaining 9 jobs were perfectly servable.
    any_transient = any(
        getattr(error, "transient", False) for _cfg, error in failures
    )
    if budget_failures and not any_transient:
        raise LLMBudgetError(summary) from last_error
    raise LLMError(summary) from last_error
