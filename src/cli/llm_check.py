"""CLI — verify the configured LLM provider actually works.

    python -m src.cli.llm_check --list    # model names the key can see
    python -m src.cli.llm_check           # real structured call, end to end

Exists because two assumptions failed on 2026-08-08 and both were only
detectable with a live request:

  * A model name that appears in the provider's own model list is not
    necessarily callable. Gemini listed ``gemini-2.5-flash`` right up to the
    moment every request returned "no longer available to new users".
  * A key that authenticates can still be unable to serve traffic — the same
    project hit its monthly spend cap hours later and returned 429 on
    everything.

So run this after changing ``llm.provider``, ``llm.model`` or
``llm.instructor_mode``, and whenever a run fails at the parse step. It uses
the project's real ``JDParsed`` schema and the real client, so a pass here
means the pipeline's only LLM call works.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

import structlog

from src.config import settings


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.WARNING)
    structlog.configure(
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


def _banner() -> None:
    from src.llm.client import fallback_enabled, provider_config

    for which in ("primary", "fallback"):
        if which == "fallback" and not fallback_enabled():
            print("fallback : (disabled)")
            continue
        cfg = provider_config(which)
        print(
            f"{which:8} : {cfg.provider} · {cfg.model} · "
            f"{cfg.instructor_mode} · {cfg.api_key_env}"
        )
    print()


def _list_one(which: str) -> int:
    """Print every model id one provider's key can see."""
    import os

    from openai import OpenAI

    from src.llm.client import provider_config

    cfg = provider_config(which)
    key = os.environ.get(str(cfg.api_key_env), "").strip()
    if not key:
        print(f"[{which}] {cfg.api_key_env} not set — skipping.", file=sys.stderr)
        return 1

    client = OpenAI(api_key=key, base_url=str(cfg.base_url))
    try:
        ids = sorted(m.id for m in client.models.list())
    except Exception as exc:
        print(f"[{which}] could not list models: {exc}", file=sys.stderr)
        return 1

    configured = str(cfg.model)
    print(f"[{which}] {cfg.provider} — {len(ids)} models visible:")
    for model_id in ids:
        mark = "   <- configured" if model_id == configured else ""
        print(f"    {model_id}{mark}")

    if configured not in ids:
        print(
            f"  WARNING: configured model {configured!r} is NOT listed. "
            f"Pick one of the above.",
            file=sys.stderr,
        )
        return 1
    print()
    return 0


def list_models() -> int:
    """List models for every configured provider."""
    from src.llm.client import fallback_enabled

    rc = _list_one("primary")
    if fallback_enabled():
        rc |= _list_one("fallback")
    print("Being listed is necessary but not sufficient — run without --list "
          "to prove a model is callable.")
    return rc


def test_call() -> int:
    """Make one real structured call using the pipeline's own schema."""
    from src.llm.client import LLMBudgetError, LLMError, complete
    from src.llm.prompts import jd_parse_prompt, jd_parse_system
    from src.llm.schemas import JDParsed
    from src.state.models import AllJobs

    job = AllJobs(
        job_id="llm-check",
        company="Example Fintech",
        role="Backend Engineer",
        site="check",
        location="Bangalore, India",
        jd_text=(
            "We are hiring a Backend Engineer with 2-4 years of experience. "
            "Required: Python, FastAPI, PostgreSQL, AWS. Nice to have: Kafka. "
            "You will build and operate payment APIs. Hybrid, 3 days in office. "
            "Compensation 12-18 LPA."
        ),
    )

    t0 = time.time()
    try:
        parsed = complete(
            JDParsed, jd_parse_prompt(job), system=jd_parse_system()
        )
    except LLMBudgetError as exc:
        print(f"FAILED — account out of budget/quota ({time.time() - t0:.1f}s)")
        print(f"  {str(exc)[:400]}", file=sys.stderr)
        return 2
    except LLMError as exc:
        print(f"FAILED ({time.time() - t0:.1f}s)")
        print(f"  {str(exc)[:400]}", file=sys.stderr)
        return 1

    print(f"OK — structured call succeeded in {time.time() - t0:.1f}s\n")
    for field in (
        "role_category", "role_level", "years_required", "required_skills",
        "nice_to_have", "location_type", "salary_min_lpa", "salary_max_lpa",
    ):
        print(f"  {field:16} {getattr(parsed, field)}")

    # The parse is only useful if the fields the scorer depends on came back.
    problems = []
    if parsed.years_required not in (2, 3, 4):
        problems.append(f"years_required={parsed.years_required}, expected 2-4")
    if not parsed.required_skills:
        problems.append("required_skills is empty")
    if parsed.role_level not in ("junior", "mid"):
        problems.append(f"role_level={parsed.role_level!r}, expected junior/mid")

    if problems:
        print("\nExtraction looks weak on this model:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "  These feed scoring directly (role_level is 60% of success_prob), "
            "so consider a stronger model.",
            file=sys.stderr,
        )
        return 1

    print("\nAll scorer-critical fields extracted correctly.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.cli.llm_check")
    parser.add_argument(
        "--list", action="store_true", help="list model ids instead of calling"
    )
    args = parser.parse_args(argv)

    _configure_logging()
    _banner()
    return list_models() if args.list else test_call()


if __name__ == "__main__":
    raise SystemExit(main())
