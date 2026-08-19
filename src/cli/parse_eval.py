"""CLI — run the JD parser over real listings and record what it did.

    python -m src.cli.parse_eval --variant=fixed --limit=8
    python -m src.cli.parse_eval --compare baseline fixed

Exists because Layer 3 changes were being judged from a handful of parses read
off the terminal, which is how a 25% failure rate stayed invisible for months:
the model extracts cleanly on most JDs, so a spot check looks fine while a
quarter of listings lose every technology they name.

Each run writes one ``parse_eval`` row per listing holding the prompt as sent
and the object as returned, so two configurations can be compared over the same
ads. Nothing in the pipeline reads the table back.

``--variant`` is a free-text label for the configuration under test; it does
NOT change behaviour. Set the configuration in ``config.yaml``, then name it
here so the rows can be told apart.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from collections import defaultdict

import structlog
from pydantic import Field
from sqlalchemy import select

from src.llm.client import provider_config
from src.llm.prompts import jd_parse_prompt, jd_parse_system
from src.llm.schemas import JDParsed, collect_skill_rejections
from src.parser import parse
from src.state.db import session_scope
from src.state.models import AllJobs, ParseEval

log = structlog.get_logger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.WARNING)
    structlog.configure(
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


def _max_skill_chars() -> int | None:
    """The schema's own bound, read off the compiled schema rather than a
    constant, so the recorded value is what the model was actually held to."""
    items = JDParsed.model_json_schema()["properties"]["required_skills"]["items"]
    return items.get("maxLength")


class _UnboundedJDParsed(JDParsed):
    """`JDParsed` before the skill-length bound, for measuring against it.

    Kept here rather than in `schemas.py` so nothing in the pipeline can reach
    for it by accident: its only purpose is to reproduce the old behaviour on
    the same listings, so the bound's effect is measured rather than asserted.
    """

    required_skills: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)


def _parse_unbounded(job: AllJobs, cfg) -> JDParsed:
    """One parse with the pre-fix schema and the provider's default sampling."""
    from src.llm.client import get_client

    return get_client("primary").chat.completions.create(
        model=str(cfg.model),
        messages=[
            {"role": "system", "content": jd_parse_system()},
            {"role": "user", "content": jd_parse_prompt(job, provider_cfg=cfg)},
        ],
        response_model=_UnboundedJDParsed,
        max_retries=0,
    )


def run_variant(variant: str, limit: int, *, baseline: bool = False) -> None:
    """Parse the newest ``limit`` listings and record each attempt."""
    cfg = provider_config("primary")
    temperature = None if baseline else cfg.get("temperature")
    bound = None if baseline else _max_skill_chars()

    with session_scope() as session:
        jobs = session.scalars(
            select(AllJobs)
            .where(AllJobs.jd_text.isnot(None))
            .order_by(AllJobs.scraped_at.desc())
            .limit(limit)
        ).all()

        print(f"variant={variant}  provider={cfg.provider}  model={cfg.model}  "
              f"temperature={temperature}  max_skill_chars={bound}")
        print(f"{'job':34} {'jd':>6} {'time':>7} {'skills':>7} {'dropped':>8} {'longest':>8}")
        print("-" * 80)

        for job in jobs:
            prompt = jd_parse_prompt(job, provider_cfg=cfg)
            started = time.time()
            parsed: JDParsed | None = None
            error: str | None = None
            with collect_skill_rejections() as rejections:
                try:
                    parsed = _parse_unbounded(job, cfg) if baseline else parse(job)
                except Exception as exc:  # noqa: BLE001 - recorded, not handled
                    error = f"{type(exc).__name__}: {exc}"
            elapsed_ms = int((time.time() - started) * 1000)

            skills = list(parsed.required_skills) + list(parsed.nice_to_have) if parsed else []
            longest = max((len(s) for s in skills), default=0)

            session.add(
                ParseEval(
                    job_id=job.job_id,
                    variant=variant,
                    provider=str(cfg.provider),
                    model=str(cfg.model),
                    temperature=None if temperature is None else float(temperature),
                    max_skill_chars=bound,
                    prompt_sent=prompt,
                    prompt_chars=len(prompt),
                    jd_chars=len(job.jd_text or ""),
                    output_json=parsed.model_dump() if parsed else None,
                    rejections=list(rejections) or None,
                    error=error,
                    latency_ms=elapsed_ms,
                )
            )
            session.flush()

            label = f"{job.company[:16]} {job.role[:16]}"
            print(f"{label:34} {len(job.jd_text or ''):>6} "
                  f"{elapsed_ms/1000:>6.1f}s {len(skills):>7} {len(rejections):>8} {longest:>8}"
                  + ("  ERROR" if error else ""))
            if error:
                print(f"    {error[:100]}")


def compare(variants: list[str]) -> None:
    """Summarise recorded variants over the listings they share."""
    with session_scope() as session:
        rows = session.scalars(
            select(ParseEval).where(ParseEval.variant.in_(variants))
        ).all()

    by_variant: dict[str, list[ParseEval]] = defaultdict(list)
    for row in rows:
        by_variant[row.variant].append(row)

    if not by_variant:
        print("no recorded runs for those variants")
        return

    # Only listings every variant covers, so the comparison is like-for-like.
    shared = set.intersection(
        *({r.job_id for r in rs} for rs in by_variant.values())
    )
    print(f"comparing over {len(shared)} shared listings\n")
    print(f"{'variant':14} {'skills/job':>11} {'>4 words':>9} {'longest':>8} "
          f"{'sec/job':>8} {'errors':>7}")
    print("-" * 66)

    for variant in variants:
        rs = [r for r in by_variant.get(variant, []) if r.job_id in shared]
        if not rs:
            continue
        counts, longs, longest, secs, errs = [], 0, 0, [], 0
        total_skills = 0
        for r in rs:
            secs.append(r.latency_ms / 1000)
            if r.error or not r.output_json:
                errs += 1
                continue
            sk = (r.output_json.get("required_skills") or []) + (
                r.output_json.get("nice_to_have") or []
            )
            counts.append(len(sk))
            total_skills += len(sk)
            longs += sum(1 for s in sk if len(s.split()) > 4)
            longest = max(longest, max((len(s) for s in sk), default=0))
        pct = 100 * longs / total_skills if total_skills else 0
        print(f"{variant:14} {statistics.mean(counts) if counts else 0:>11.1f} "
              f"{pct:>8.0f}% {longest:>8} {statistics.mean(secs):>7.1f}s {errs:>7}")

    # What each variant threw away, so a rejection rule can be judged rather
    # than assumed safe.
    for variant in variants:
        rs = [r for r in by_variant.get(variant, []) if r.job_id in shared]
        dropped: dict[str, list[str]] = defaultdict(list)
        for r in rs:
            for item in (r.rejections or []):
                dropped[item.get("rule", "?")].append(item.get("text", ""))
        if not dropped:
            continue
        total = sum(len(v) for v in dropped.values())
        print(f"\n{variant}: {total} skills discarded")
        for rule, texts in sorted(dropped.items(), key=lambda kv: -len(kv[1])):
            print(f"  {rule:20} {len(texts):>4}")
            for t in texts[:4]:
                print(f"      {t[:88]}")
            if len(texts) > 4:
                print(f"      ... and {len(texts) - 4} more")


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    ap = argparse.ArgumentParser(description="Record and compare Layer 3 parses.")
    ap.add_argument("--variant", help="label for the configuration under test")
    ap.add_argument("--limit", type=int, default=8, help="listings to parse")
    ap.add_argument("--baseline", action="store_true",
                    help="run the pre-fix schema and default sampling, to measure against")
    ap.add_argument("--compare", nargs="+", metavar="VARIANT",
                    help="summarise recorded variants instead of running")
    args = ap.parse_args(argv)

    if args.compare:
        compare(args.compare)
        return 0
    if not args.variant:
        ap.error("--variant is required unless --compare is used")
    run_variant(args.variant, args.limit, baseline=args.baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
