"""CLI — score jobs that were scraped but never judged.

    python -m src.cli.backfill --dry-run    # count and report only
    python -m src.cli.backfill              # parse, score, record a verdict
    python -m src.cli.backfill --limit 50   # work through it in bites

Every scraped job is written to ``all_jobs`` before any of them is parsed, so
a run that stops partway — as several did on 2026-08-08, abandoned by a spend
cap — leaves its remainder there with no verdict. The duplicate check used to
treat mere presence in ``all_jobs`` as "already seen", so those jobs were
skipped on every later run: scraped, never scored, and never going to be. 176
of 745 had gone quiet that way.

That check is fixed, but the fix only reaches jobs the scraper encounters
again, and the scraper only returns postings inside ``hours_old`` (24). 139 of
the 176 were scraped in June and July — no future scrape will ever return
them. Recovering those needs a pass driven by the database rather than by a
search, which is this.

It deliberately does NOT notify. A stranded job from six weeks ago is very
likely closed, and a burst of notifications for dead postings costs attention
while returning nothing. Verdicts are recorded, so a match becomes visible in
the dashboard and the CSV index without interrupting anyone.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def unjudged(session, limit: int | None = None) -> list:
    """Jobs in ``all_jobs`` with no verdict in either verdict table."""
    from sqlalchemy import exists, select

    from src.state.models import AllJobs, Applied, NotApplied

    stmt = (
        select(AllJobs)
        .where(
            AllJobs.jd_text.isnot(None),
            ~exists().where(Applied.job_id == AllJobs.job_id),
            ~exists().where(NotApplied.job_id == AllJobs.job_id),
        )
        .order_by(AllJobs.scraped_at.desc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list(session.execute(stmt).scalars())


def backfill(limit: int | None = None, dry_run: bool = False) -> dict[str, int]:
    """Parse and score every unjudged job, recording a verdict for each."""
    from src.config import settings as cfg
    from src.llm.client import LLMBudgetError, LLMError
    from src.parser import apply_to_row, parse
    from src.builder.llm_call import build as build_selection
    from src.main import _compute_gap_skills
    from src.reasons import (
        BUILD_FAILURE, HARD_FILTER_LAYER_3, LOW_SCORE, PARSE_FAILURE,
    )
    from src.scraper import filters as job_filters
    from src.scorer.apply_decision import evaluate
    from src.scorer.selector import build_jd_context
    from src.state.db import session_scope
    from src.state.master_profile import load_profile
    from src.state.models import Applied, NotApplied

    def _gap_skills(parsed, profile):
        pool = [sc.skill for sc in profile.skills]
        return _compute_gap_skills(list(parsed.required_skills or []), pool)

    counts = {"found": 0, "scored": 0, "matched": 0, "failed": 0, "aborted": 0}
    now = datetime.now(timezone.utc)

    with session_scope() as session:
        jobs = unjudged(session, limit)
        counts["found"] = len(jobs)
        if dry_run or not jobs:
            return counts

        profile = load_profile(session)

        for job in jobs:
            try:
                parsed = parse(job)
            except LLMBudgetError as exc:
                # Same reasoning as a live run: every remaining job would fail
                # identically, so stop and keep what is already committed.
                log.error("backfill_aborted", reason="llm_budget_exhausted",
                          done=counts["scored"], error=str(exc))
                counts["aborted"] = 1
                break
            except LLMError as exc:
                log.warning("backfill_parse_failed", job_id=job.job_id, error=str(exc))
                session.add(NotApplied(
                    job_id=job.job_id, reason_category=PARSE_FAILURE,
                    reason_detail=str(exc)[:500], not_applied_at=now,
                ))
                counts["failed"] += 1
                continue

            apply_to_row(job, parsed)

            if job_filters.exceeds_years_ceiling(
                parsed.years_required, int(cfg.filters.years_ceiling)
            ):
                session.add(NotApplied(
                    job_id=job.job_id, reason_category=HARD_FILTER_LAYER_3,
                    reason_detail=str(parsed.years_required), not_applied_at=now,
                ))
                counts["scored"] += 1
                continue

            # No scrape_window_hours: the window this row was actually caught
            # in was not recorded, so recency_score falls back to the
            # configured peak. It barely matters here — these rows were
            # scraped weeks ago, so the elapsed term dominates the half-window
            # correction and they still score as the old postings they are.
            result = evaluate(
                profile,
                build_jd_context(
                    parsed,
                    posted_at=job.posted_at,
                    scraped_at=job.scraped_at,
                ),
            )
            counts["scored"] += 1

            log.info(
                "job_scored", job_id=job.job_id, company=job.company,
                role=job.role, score=round(result.final_score, 3),
                threshold=float(cfg.scoring.apply_threshold),
                matched=result.apply, source="backfill",
            )

            if not result.apply:
                # Scores recorded, not just the reason: a backfill exists to
                # answer "what did we miss and why", and a verdict with no
                # numbers cannot answer the second half.
                session.add(NotApplied(
                    job_id=job.job_id, reason_category=LOW_SCORE,
                    reason_detail=str(result.final_score),
                    fit_score=result.fit, success_prob=result.success_prob,
                    recency_score=result.recency, final_score=result.final_score,
                    not_applied_at=now,
                ))
                session.flush()
                continue

            # A match earns a real Applied row, so it appears in the dashboard
            # like any other. Recording it as LOW_SCORE to avoid the work would
            # put a false verdict in the record, which is worse than the work.
            selection = build_selection(
                result=result,
                profile=profile,
                jd_role_summary=parsed.role_summary,
                jd_required_skills=list(parsed.required_skills or []),
                jd_team_or_product=parsed.team_or_product,
            )
            if selection is None:
                log.error(BUILD_FAILURE, job_id=job.job_id, source="backfill")
                session.add(NotApplied(
                    job_id=job.job_id, reason_category=BUILD_FAILURE,
                    not_applied_at=now,
                ))
                counts["failed"] += 1
                session.flush()
                continue

            selection.job_id = job.job_id
            counts["matched"] += 1
            session.add(Applied(
                job_id=job.job_id,
                selection_json=selection.model_dump(),
                template_version=selection.template_version,
                cover_letter_text=selection.cover_letter_text,
                expected_salary_lpa=(
                    parsed.salary_max_lpa or float(cfg.salary.default_expected_lpa)
                ),
                fit_score=result.fit,
                success_prob=result.success_prob,
                recency_score=result.recency,
                final_score=result.final_score,
                gap_skills=_gap_skills(parsed, profile),
                built_at=now,
                user_status="pending",
            ))

            session.flush()

        session.commit()

    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.cli.backfill")
    parser.add_argument("--dry-run", action="store_true",
                        help="report how many are unjudged, change nothing")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    _configure_logging()
    counts = backfill(limit=args.limit, dry_run=args.dry_run)

    if args.dry_run:
        print(f"{counts['found']} jobs have never been judged")
        return 0

    print(
        f"scored {counts['scored']} of {counts['found']} "
        f"({counts['matched']} would have matched, {counts['failed']} failed to parse)"
        + (" — stopped early, provider out of budget" if counts["aborted"] else "")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
