"""CLI — find the temperature that extracts the most real technologies.

    python -m src.cli.temp_sweep

Temperature is continuous, so it is tunable rather than a binary. It was set to
0 on 2026-08-19 because that looked tidier, and the tidiness turned out to be
the model summarising: it wrote "inference optimization" and dropped the
"(quantization, ONNX, efficient serving)" the ad had spelled out. This sweep
exists so the setting is chosen from recall over known listings instead of from
how clean the output looks.

The scoring asymmetry matters when reading the results: Layer 4 scores each
pool skill against the BEST single JD skill, so a spurious JD skill cannot pull
a real match down — it simply never matches. Under-extraction costs matches;
over-extraction costs almost nothing. Read `found` first and `skills` second.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import instructor
import structlog
from openai import OpenAI
from sqlalchemy import select

from src.llm.client import provider_config
from src.llm.prompts import jd_parse_prompt, jd_parse_system
from src.llm.schemas import JDParsed
from src.state.db import session_scope
from src.state.models import AllJobs

log = structlog.get_logger(__name__)

# Listings whose technologies were read off the ad by hand. Small on purpose:
# a hand-checked answer key over a few ads beats a guess over many.
GROUND_TRUTH: dict[str, list[str]] = {
    "linkedin-li-4455989022": [  # Experity — AI & ML Engineer
        "Python", "SQL", "PyTorch", "TensorFlow", "FastAPI", "Snowflake", "AWS",
        "SageMaker", "S3", "Lambda", "RDS", "DynamoDB", "Docker", "Kubernetes",
        "CI/CD", "MLOps", "RAG", "LLMs",
    ],
    "linkedin-li-4455286239": [  # Vidpro — Senior ML Engineer, Generative AI
        "Python", "RAG", "LangChain", "LangGraph", "LoRA", "QLoRA", "SFT", "DPO",
        "quantization", "ONNX", "DeepSpeed", "FSDP",
    ],
}


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.WARNING)
    structlog.configure(
        processors=[structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
    )


def _parse_at(job: AllJobs, cfg, temperature: float | None) -> tuple[float, JDParsed | None]:
    client = instructor.from_openai(
        OpenAI(api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
               base_url=os.environ["OLLAMA_BASE_URL"], timeout=300),
        mode=instructor.Mode.JSON_SCHEMA,
    )
    kwargs = {} if temperature is None else {"temperature": temperature}
    started = time.time()
    try:
        parsed = client.chat.completions.create(
            model=str(cfg.model), response_model=JDParsed, max_retries=1,
            messages=[{"role": "system", "content": jd_parse_system()},
                      {"role": "user", "content": jd_parse_prompt(job, provider_cfg=cfg)}],
            **kwargs,
        )
        return time.time() - started, parsed
    except Exception as exc:  # noqa: BLE001 - reported, not handled
        log.warning("sweep_call_failed", temperature=temperature, error=str(exc)[:120])
        return time.time() - started, None


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    ap = argparse.ArgumentParser(description="Sweep temperature against known listings.")
    ap.add_argument("--temps", default="0,0.2,0.4,0.6,0.8",
                    help="comma-separated; 'default' leaves the parameter unset")
    ap.add_argument("--repeats", type=int, default=1,
                    help="runs per temperature (sampling is stochastic above 0)")
    args = ap.parse_args(argv)

    temps: list[float | None] = [
        None if t.strip() == "default" else float(t) for t in args.temps.split(",")
    ]
    cfg = provider_config("primary")

    with session_scope() as session:
        jobs = {
            j.job_id: AllJobs(job_id=j.job_id, company=j.company, role=j.role,
                              site=j.site, location=j.location, jd_text=j.jd_text)
            for j in session.scalars(
                select(AllJobs).where(AllJobs.job_id.in_(GROUND_TRUTH))
            ).all()
        }

    missing = set(GROUND_TRUTH) - set(jobs)
    if missing:
        print(f"not in the database, skipping: {sorted(missing)}")

    print(f"model={cfg.model}  repeats={args.repeats}\n")
    print(f"{'temp':>8} {'found':>13} {'recall':>8} {'skills':>8} {'sec':>7}   missed")
    print("-" * 78)

    for temperature in temps:
        hits = total = skills = 0
        seconds = 0.0
        missed: set[str] = set()
        for _ in range(args.repeats):
            for job_id, wanted in GROUND_TRUTH.items():
                job = jobs.get(job_id)
                if job is None:
                    continue
                elapsed, parsed = _parse_at(job, cfg, temperature)
                seconds += elapsed
                total += len(wanted)
                if parsed is None:
                    missed.update(wanted)
                    continue
                got = {s.casefold() for s in parsed.required_skills + parsed.nice_to_have}
                skills += len(parsed.required_skills) + len(parsed.nice_to_have)
                for want in wanted:
                    if want.casefold() in got:
                        hits += 1
                    else:
                        missed.add(want)
        label = "default" if temperature is None else f"{temperature:g}"
        recall = 100 * hits / total if total else 0
        runs = max(args.repeats * len(GROUND_TRUTH), 1)
        print(f"{label:>8} {f'{hits}/{total}':>13} {recall:>7.0f}% "
              f"{skills / runs:>8.0f} {seconds / runs:>6.1f}s   "
              f"{', '.join(sorted(missed)[:6])}{' ...' if len(missed) > 6 else ''}")

    print("\nRead `found` before `skills`: Layer 4 scores each pool skill against the")
    print("best single JD skill, so a spurious skill never matches and costs nothing,")
    print("while a missing one silently costs a match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
