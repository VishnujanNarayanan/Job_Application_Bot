"""Orchestrator entry point — invoked by the Layer 1 scheduler.

Usage:
    python -m src.main             # live run (Iteration 3+)
    python -m src.main --dry-run   # scrape/score/build but never submit

Iteration 1 implements the end-to-end skeleton: Layers 2-4 + 7 + 8 run
real code, Layers 5-6 + 9 are explicit no-ops. Every job is rejected
with reason LOW_SCORE (master_bullets empty until Iteration 2's
master-profile rebuild lands).

Pipeline sequence (architecture doc Section 4) — preserved for reference
and to be filled in iteration by iteration:

LAYER 1 — Scheduler
    Iteration 1: single linear run.
    Iteration 2+: cron cadence (peak/off-peak scrape frequency). No
        application quotas — every match >= 0.50 notifies.
    Acquire single-run lock to prevent overlapping cron executions.

LAYER 2 — Scraper (Indeed, Glassdoor, LinkedIn — listings only)
    Iteration 1: stub returning 3 fake jobs.
    Iteration 2+: JobSpy across all three sources with serial term
        rotation, hard filters (years, region, cooldown, dup),
        near-duplicate detection (>0.95 cosine), short-circuit at 20.

LAYER 3 — JD Parser (Gemini Call 1a — runs for EVERY passing job)
    Iteration 1: stub returning a fixed JDParsed.
    Iteration 2+: Instructor-enforced JDParsed + spaCy hallucination
        check + role-acceptance gate against role_clusters.

LAYER 4 — Scoring & Selection
    Iteration 1: rejects every job (master_bullets empty).
    Iteration 2+: full Section 4 algorithm — experience selection,
        project selection, summary pick, skills hybrid, final_score.
        Every match >= 0.50 builds + notifies; no quotas, no top-N.

LAYER 5 — Resume Builder (Gemini Call 1b — ONLY for matched jobs)
    Iteration 1: no-op.
    Iteration 2+: skills hybrid, title alias pick, cover letter →
        selection_json (~2 KB) written to `applied`. NO PDF rendered here.

LAYER 6 — Application Assist (notify + on-demand resume endpoint)
    Iteration 1: no-op (dry-run only).
    Iteration 2+: FastAPI endpoint renders PDF/DOCX on demand from
        selection_json + current template, cached in S3. No auto-apply,
        no Playwright, no form filling (removed in the pivot).

LAYER 7 — State Management (always running)
    Iteration 1: SQLAlchemy 2.0 sync session against Neon, base schema
        created via Alembic migration 0001_initial (auto-apply tables
        dropped in 0002).

LAYER 8 — Notifications (Telegram)
    Iteration 1: dry-run summary delivered at end of run.
    Iteration 2+: per-match message bundling apply link + resume links.
    Iteration 3+: CloudWatch-bridged alarm alerts.

LAYER 9 — Analytics (Google Sheets + Docs)
    Iteration 1: no-op.
    Iteration 2+: Sheets index mapping every match to apply / PDF / DOCX
        links + status; Sunday Gemini-synthesised Docs report.
"""

from __future__ import annotations

import argparse
import logging
import sys

import structlog


def _configure_logging() -> None:
    """One-time structlog setup. JSON output to stderr."""
    logging.basicConfig(
        format="%(message)s", stream=sys.stderr, level=logging.INFO
    )
    # httpx/httpcore log full request URLs at INFO, which leaks the bot
    # token embedded in Telegram API calls. Raise to WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.main")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape/score/build but never submit. Default for Iteration 1.",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    log = structlog.get_logger()
    log.info("run_started", dry_run=args.dry_run, iteration=2)

    # The Iteration-1 linear skeleton (fake scrape → fixed parse → reject-all
    # decide) has been dismantled now that Layers 2/3/4 are real modules:
    #   Layer 2  src.scraper.{jobspy_wrapper,filters,dedup,rotation}
    #   Layer 3  src.parser  (Gemini Call 1a)
    #   Layer 4  src.scorer.{selector,ordering,apply_decision.evaluate}
    # The end-to-end pipeline — rotation-driven scrape, embed, dedup, hard
    # filters, parse + role-acceptance, candidate loading from the rebuilt
    # master_profile, Layer 4 evaluate, Layer 5 build, notify — is assembled
    # in the dedicated orchestrator step that lands right before the test-chat
    # dry-run (the candidate loader depends on the master_profile rebuild).
    raise NotImplementedError(
        "Iteration 2 orchestrator rewiring in progress. Layers 2/3/4 exist as "
        "real, tested modules; the end-to-end wiring + master_profile candidate "
        "loader land before the dry-run. Drive the layers directly meanwhile."
    )


if __name__ == "__main__":
    raise SystemExit(main())
