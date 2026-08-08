"""LLM — Gemini 2.0 Flash via Instructor (structured output + retry).

Every Gemini call returns a Pydantic model — Instructor enforces the
schema at the API boundary, so we never hand-parse JSON from a model
string (CLAUDE.md tech-stack rule). The 2-call-per-job budget lives at the
call sites (Layer 3 = Call 1a, Layer 5 = Call 1b); this module is the thin
transport: build the client once, run a completion, retry with exponential
backoff on transient failures.

The Gemini SDK and Instructor are imported lazily inside ``get_client`` so
importing this module is cheap and unit tests can monkeypatch
``complete`` without the API key or network.
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

_API_KEY_ENV = "GEMINI_API_KEY"


class LLMError(RuntimeError):
    """Raised when a Gemini call fails after exhausting retries."""


# Substrings identifying failures that will NEVER succeed on retry: a retired
# or misspelled model, a malformed request, a bad or unauthorised key. Retrying
# these burns the whole backoff budget (minutes per job, every job) on an
# outcome that cannot change — which is exactly what a retired `gemini-2.5-flash`
# did on 2026-08-08 before this guard existed.
#
# 429 is deliberately absent: rate limiting IS transient and must still retry.
_PERMANENT_ERROR_MARKERS = (
    "is no longer available",
    "not found for api version",
    "is not found",
    "404",
    "api key not valid",
    "api_key_invalid",
    "permission denied",
    "permission_denied",
    "403",
    "invalid argument",
    "invalid_argument",
)


def _is_permanent(exc: Exception) -> bool:
    """True when retrying ``exc`` cannot possibly help.

    Matched on message text rather than exception type because the SDK,
    Instructor and the transport each raise their own classes and wrap one
    another; the status text is the one thing that survives the layers.
    """
    message = str(exc).casefold()
    if "429" in message or "resource_exhausted" in message or "quota" in message:
        return False   # rate limited — transient, keep retrying
    return any(marker in message for marker in _PERMANENT_ERROR_MARKERS)


@lru_cache(maxsize=1)
def get_client():
    """Build and cache the Instructor-wrapped Gemini client.

    Reads the API key from ``GEMINI_API_KEY`` (``.env``); the model name
    comes from ``config.llm.model``. Heavy imports happen here, not at
    module import.
    """
    import google.generativeai as genai
    import instructor

    api_key = os.environ.get(_API_KEY_ENV)
    if not api_key:
        raise LLMError(f"{_API_KEY_ENV} is not set (.env)")
    genai.configure(api_key=api_key)
    return instructor.from_gemini(
        genai.GenerativeModel(model_name=settings.llm.model),
        mode=instructor.Mode.GEMINI_JSON,
    )


def complete(response_model: type[T], prompt: str, *, system: str | None = None) -> T:
    """Run one Gemini completion and return ``response_model``.

    Retries transient failures with exponential backoff per
    ``config.llm.backoff``. On final failure logs ``gemini_failure`` and
    raises :class:`LLMError`; the caller decides whether to count a
    PARSE_FAILURE / BUILD_FAILURE.
    """
    backoff = settings.llm.backoff
    delay = float(backoff.initial_seconds)
    max_delay = float(backoff.max_seconds)
    attempts = int(backoff.max_attempts)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    client = get_client()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.chat.completions.create(
                messages=messages,
                response_model=response_model,
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises many types; we retry then re-wrap
            last_error = exc

            # Fail fast on errors retrying cannot fix. Without this a retired
            # model costs the full backoff budget on every job in the run.
            if _is_permanent(exc):
                log.error(
                    "gemini_failure",
                    error=str(exc),
                    attempt=attempt,
                    permanent=True,
                    model=settings.llm.model,
                    exc_info=exc,
                )
                raise LLMError(
                    f"Gemini call failed permanently (model={settings.llm.model}): {exc}"
                ) from exc

            if attempt < attempts:
                log.warning(
                    "gemini_retry",
                    attempt=attempt,
                    max_attempts=attempts,
                    error=str(exc),
                )
                time.sleep(delay)
                delay = min(delay * 2, max_delay)

    # exc_info=last_error (the captured instance), NOT True — we are outside
    # the except block here, so sys.exc_info() is already cleared.
    log.error("gemini_failure", error=str(last_error), attempts=attempts, exc_info=last_error)
    raise LLMError(f"Gemini call failed after {attempts} attempts") from last_error
