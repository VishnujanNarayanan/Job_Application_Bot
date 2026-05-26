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
    Determine cycle: "peak" if 8am <= now_IST < 11am, else "regular".
    N = 3 (peak) or 1 (regular) — applications to send this run.
    Acquire single-run lock to prevent overlapping cron executions.

LAYER 2 — Scraper (Indeed; Glassdoor in Iteration 4)
    Iteration 1: stub returning 3 fake jobs.
    Iteration 2+: JobSpy on Indeed with serial term rotation,
        hard filters (years, region, cooldown, dup), short-circuit at 20.

LAYER 3 — JD Parser (Gemini Call 1a — runs for EVERY passing job)
    Iteration 1: stub returning a fixed JDParsed.
    Iteration 2+: Instructor-enforced JDParsed + spaCy hallucination
        check + role-acceptance gate against role_clusters.

LAYER 4 — Scoring & Selection
    Iteration 1: rejects every job (master_bullets empty).
    Iteration 2+: full Section 4 algorithm — experience selection,
        project selection, summary pick, skills hybrid, final_score,
        cycle-aware top-N picking with queue management.

LAYER 5 — Resume Builder (Gemini Call 1b — ONLY for picked jobs)
    Iteration 1: no-op.
    Iteration 2+: skills hybrid, title alias pick, DOCX assembly with
        structural detection, LibreOffice PDF conversion, S3 upload.

LAYER 6 — Application Sender (Gemini Call 2 — only for unknown questions)
    Iteration 1: no-op (dry-run only).
    Iteration 3+: Playwright Indeed Easy Apply, manual queue, retry≤1.

LAYER 7 — State Management (always running)
    Iteration 1: SQLAlchemy 2.0 sync session against Neon, all 13 tables
        created via Alembic migration 0001_initial.

LAYER 8 — Notifications (Telegram)
    Iteration 1: dry-run summary delivered at end of run.
    Iteration 2+: morning digest with presigned S3 URLs.
    Iteration 3+: critical-alert triggers, CloudWatch-bridged Lambda alerts.

LAYER 9 — Analytics (Google Sheets + Docs)
    Iteration 1: no-op.
    Iteration 2+: Sheets tabs (Applied / Skipped / ManualRequired) with
        presigned S3 URLs; Sunday Gemini-synthesised Docs report.
"""

from __future__ import annotations

import argparse
import logging
import sys

import structlog

from src.notifications import send_dry_run_summary
from src.parser import apply_to_row as apply_parsed_to_row
from src.parser import parse as parse_jd
from src.scorer.apply_decision import decide
from src.scraper.jobspy_wrapper import scrape
from src.state.db import session_scope
from src.state.models import NotApplied


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
    log.info("run_started", dry_run=args.dry_run, iteration=1)

    # Layer 2 — scraper
    jobs = scrape()
    log.info("layer2_scrape_done", scraped=len(jobs))

    skipped = 0
    applied = 0

    with session_scope() as session:
        # Persist scraped rows so subsequent inserts (NotApplied) can FK them.
        session.add_all(jobs)
        session.flush()  # assign defaults / surface FK errors before child writes

        for job in jobs:
            # Layer 3 — parse
            parsed = parse_jd(job)
            apply_parsed_to_row(job, parsed)
            log.info(
                "layer3_parse_done",
                job_id=job.job_id,
                role_category=parsed.role_category,
            )

            # Layer 4 — decide
            decision = decide(session)
            log.info(
                "layer4_decision",
                job_id=job.job_id,
                apply=decision.apply,
                reason=decision.reason_category,
            )

            if decision.apply:
                # Layer 5 + 6 land in later iterations.
                applied += 1
                log.info("layer5_layer6_noop", job_id=job.job_id)
            else:
                # Layer 7 — record skip
                session.add(
                    NotApplied(
                        job_id=job.job_id,
                        reason_category=decision.reason_category,
                        reason_detail=decision.reason_detail,
                        final_score=decision.final_score,
                    )
                )
                skipped += 1

    # Layer 8 — Telegram. Outside session_scope: DB writes are already
    # committed; a failed Telegram send must not roll them back.
    try:
        send_dry_run_summary(scraped=len(jobs), skipped=skipped, applied=applied)
        log.info("layer8_telegram_sent")
    except Exception as exc:  # noqa: BLE001 — log and continue
        log.error("layer8_telegram_failed", error=str(exc))

    # Layer 9 — analytics no-op in Iter 1.
    log.info(
        "run_complete",
        scraped=len(jobs),
        skipped=skipped,
        applied=applied,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
