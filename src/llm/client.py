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
            if attempt < attempts:
                log.warning(
                    "gemini_retry",
                    attempt=attempt,
                    max_attempts=attempts,
                    error=str(exc),
                )
                time.sleep(delay)
                delay = min(delay * 2, max_delay)

    log.error("gemini_failure", error=str(last_error), attempts=attempts)
    raise LLMError(f"Gemini call failed after {attempts} attempts") from last_error
