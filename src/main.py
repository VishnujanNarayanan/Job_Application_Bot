"""Orchestrator entry point — invoked by the Layer 1 scheduler (cron).

Usage:
    python -m src.main             # live run
    python -m src.main --dry-run   # scrape/score/build, notify to test chat

Pipeline (architecture doc §4):

  LAYER 1   Scheduler — single-run lock (data/run.lock), cron cadence
  LAYER 2   Scraper — JobSpy, serial rotation, hard filters, dedup
  LAYER 3   JD Parser — Gemini Call 1a (always), role-acceptance gate
  LAYER 4   Scoring — deterministic selection + final score formula
  LAYER 5   Resume Builder — Gemini Call 1b (matched jobs), selection_json
  LAYER 6   Notification — apply link + resume links via Telegram
  LAYER 7   State — Neon Postgres, master_profile rebuild on mtime change
  LAYER 8   Notifications — per-match Telegram message
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

_ROOT = Path(__file__).resolve().parents[1]


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,  # render exc_info → "exception" field
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    from src.aws.cloudwatch import build_handler as _cw_handler
    _handler = _cw_handler()
    if _handler:
        logging.getLogger().addHandler(_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.main")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging()
    log = structlog.get_logger()
    log.info("run_started", dry_run=args.dry_run, iteration=3)

    # Single-run lock — prevents overlapping cron executions.
    lock_path = _ROOT / "data" / "run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        lock_file = open(lock_path, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        # Lock already held by another run (or Windows where fcntl is absent).
        log.info("run_skipped", reason="lock_held")
        return 0

    try:
        return _run(args.dry_run, log)
    finally:
        import fcntl as _fcntl
        _fcntl.flock(lock_file, _fcntl.LOCK_UN)
        lock_file.close()


def _run(dry_run: bool, log) -> int:
    from src.builder.llm_call import build as build_selection
    from src.config import settings
    from src.llm.client import LLMError
    from src.notifications import send_dry_run_summary, send_match_notification
    from src.parser import apply_to_row, grounded_skills, parse
    from src.reasons import (
        BUILD_FAILURE,
        COMPANY_COOLDOWN,
        DUPLICATE,
        HARD_FILTER_LAYER_3,
        LOCATION_DISALLOWED,
        LOW_SCORE,
        PARSE_FAILURE,
    )
    from src.scorer.apply_decision import evaluate
    from src.scorer.embeddings import embed_batch
    from src.scorer.selector import build_jd_context
    from src.scraper import filters, jobspy_wrapper, rotation
    from src.state import master_profile
    from src.state.db import session_scope
    from src.state.models import AllJobs, Applied, CompanyCooldown, NotApplied

    cfg = settings
    now = datetime.now(timezone.utc)

    with session_scope() as session:
        # --- Layer 7: master profile rebuild (mtime short-circuit) ---
        try:
            rebuild_report = master_profile.rebuild(session)
            if not rebuild_report.skipped:
                log.info("master_profile_rebuilt", **{
                    k: v for k, v in vars(rebuild_report).items()
                    if not k.startswith("_")
                })
        except Exception as exc:
            log.error(
                "master_profile_validation_failure",
                error=str(exc),
                exc_info=True,
            )
            return 1

        profile = master_profile.load_profile(session)

        # --- Layer 2: scraper ---
        terms = list(cfg.scraper.search_terms)
        term = rotation.current_term(session, terms)
        is_peak = True  # TODO: detect from time-of-day in Layer 1
        hours_old = int(cfg.scraper.hours_old.peak if is_peak else cfg.scraper.hours_old.off_peak)

        log.info("scrape_start", term=term, hours_old=hours_old, dry_run=dry_run)
        rl = cfg.scraper.rate_limit
        try:
            raw_jobs = jobspy_wrapper.scrape(
                term,
                sites=list(cfg.scraper.sites),
                country=str(cfg.scraper.country),
                results_wanted=int(cfg.scraper.results_wanted_per_term),
                hours_old=hours_old,
                linkedin_fetch_description=bool(cfg.scraper.linkedin_fetch_description),
                linkedin_results_wanted=int(rl.linkedin_results_wanted),
                per_site=bool(rl.per_site),
                max_retries=int(rl.max_retries),
                backoff_base_seconds=float(rl.backoff_base_seconds),
                inter_site_delay_seconds=float(rl.inter_site_delay_seconds),
                proxies=list(rl.proxies),
            )
        except Exception as exc:
            log.error("scraper_error", term=term, error=str(exc))
            raw_jobs = []

        log.info("scrape_done", term=term, raw_count=len(raw_jobs))

        # --- Layer 2: hard filters (raw fields) ---
        existing_ids = filters.existing_job_ids(session, [j.job_id for j in raw_jobs])
        disallowed = list(cfg.filters.disallowed_regions)
        cooldown_days = int(cfg.scraper.cooldown_days)

        passing: list[AllJobs] = []
        not_applied_queue: list[tuple[AllJobs, str, str | None]] = []

        for job in raw_jobs:
            if job.job_id in existing_ids:
                not_applied_queue.append((job, DUPLICATE, None))
                continue
            if not (job.jd_text or "").strip():
                # No description body (e.g. throttled LinkedIn fetch). Drop
                # before embedding so a degenerate empty-text embedding can't
                # collapse near-duplicate detection (all empties are cosine
                # 1.0). Not persisted — the empty is usually transient, so it
                # gets re-scraped (and hopefully fetched) on the next run.
                log.info("empty_jd_skipped", job_id=job.job_id, site=job.site)
                continue
            if filters.location_disallowed(job.location, disallowed):
                not_applied_queue.append((job, LOCATION_DISALLOWED, job.location))
                continue
            last_notified = filters.company_last_notified(session, job.company)
            if filters.company_in_cooldown(last_notified, now, cooldown_days):
                not_applied_queue.append((job, COMPANY_COOLDOWN, job.company))
                continue
            passing.append(job)

        # --- Layer 2: embed JD texts in a single batch ---
        if passing:
            jd_texts = [j.jd_text or "" for j in passing]
            embeddings = embed_batch(jd_texts)
            for job, emb in zip(passing, embeddings):
                job.jd_embedding = emb
            session.add_all(passing)
            session.flush()

        matched_count = 0
        skipped_count = 0
        short_circuit = int(cfg.scraper.short_circuit_count)

        for job in passing:
            # --- Layer 3: parse ---
            try:
                parsed = parse(job)
            except LLMError as exc:
                log.error(
                    "gemini_failure",
                    reason=PARSE_FAILURE,
                    job_id=job.job_id,
                    error=str(exc),
                )
                not_applied_queue.append((job, PARSE_FAILURE, str(exc)))
                skipped_count += 1
                continue

            apply_to_row(job, parsed)

            # Layer 3 hard filter: years ceiling on structured field
            if filters.exceeds_years_ceiling(parsed.years_required, int(cfg.filters.years_ceiling)):
                not_applied_queue.append((job, HARD_FILTER_LAYER_3, str(parsed.years_required)))
                skipped_count += 1
                continue

            # --- Layer 4: scoring ---
            jd_context = build_jd_context(parsed, posted_at=job.posted_at)
            result = evaluate(profile, jd_context)

            if not result.apply:
                not_applied_queue.append((job, LOW_SCORE, str(result.final_score)))
                skipped_count += 1
                continue

            # --- Layer 5: build selection_json ---
            # Gap skills: JD required skills not in operator's pool
            skills_pool = [sc.skill for sc in profile.skills]
            gap_skills = _compute_gap_skills(
                list(parsed.required_skills or []), skills_pool
            )

            selection = build_selection(
                result=result,
                profile=profile,
                jd_role_summary=parsed.role_summary,
                jd_required_skills=list(parsed.required_skills or []),
                jd_team_or_product=parsed.team_or_product,
            )

            if selection is None:
                log.error(BUILD_FAILURE, job_id=job.job_id, reason="selection_returned_none")
                not_applied_queue.append((job, BUILD_FAILURE, None))
                skipped_count += 1
                continue

            # Fill job_id into selection before persisting
            selection.job_id = job.job_id

            # --- Layer 7: persist applied row ---
            title_alias = (
                selection.experiences[0].title_alias
                if selection.experiences
                else job.role
            )
            expected_salary = (
                parsed.salary_max_lpa
                or float(cfg.salary.default_expected_lpa)
            )
            session.add(Applied(
                job_id=job.job_id,
                selection_json=selection.model_dump(),
                template_version=selection.template_version,
                cover_letter_text=selection.cover_letter_text,
                expected_salary_lpa=expected_salary,
                fit_score=result.fit,
                success_prob=result.success_prob,
                recency_score=result.recency,
                final_score=result.final_score,
                gap_skills=gap_skills,
                user_status="pending",
            ))
            job.outcome = "matched"
            job.outcome_at = now

            # Update company cooldown
            cooldown_row = session.get(CompanyCooldown, job.company)
            if cooldown_row is None:
                session.add(CompanyCooldown(company=job.company, last_applied_at=now))
            else:
                cooldown_row.last_applied_at = now

            session.flush()

            # --- Layer 8: notify ---
            if not dry_run:
                try:
                    send_match_notification(
                        job=job,
                        parsed=parsed,
                        result=result,
                        gap_skills=gap_skills,
                        endpoint_base_url=str(cfg.endpoint.base_url),
                        title_alias=title_alias,
                    )
                except Exception as exc:
                    log.error("notification_error", job_id=job.job_id, error=str(exc))
            else:
                log.info(
                    "dry_run_match",
                    job_id=job.job_id,
                    company=job.company,
                    role=job.role,
                    score=result.final_score,
                    title_alias=title_alias,
                )

            matched_count += 1
            log.info(
                "selection_built",
                job_id=job.job_id,
                score=result.final_score,
                experiences=len(selection.experiences),
                projects=len(selection.projects),
            )

            if matched_count + skipped_count >= short_circuit:
                log.info("short_circuit", threshold=short_circuit)
                break

        # --- Persist not-applied records ---
        _write_not_applied(session, not_applied_queue, now)

        # --- Advance rotation ---
        rotation.advance(session, terms)

        session.commit()

    # --- Layer 8: dry-run summary ---
    total = len(raw_jobs)
    try:
        send_dry_run_summary(
            scraped=total,
            skipped=skipped_count,
            applied=matched_count,
        )
    except Exception as exc:
        log.error("telegram_summary_error", error=str(exc))

    log.info(
        "run_complete",
        dry_run=dry_run,
        scraped=total,
        matched=matched_count,
        skipped=skipped_count,
    )
    return 0


def _compute_gap_skills(required: list[str], pool: list[str]) -> list[str]:
    """Required skills not present in the skills pool (substring match)."""
    pool_lower = {s.casefold() for s in pool}
    return [
        s for s in required
        if s.casefold() not in pool_lower
        and not any(s.casefold() in p for p in pool_lower)
    ]


def _write_not_applied(session, queue: list, now: datetime) -> None:
    """Persist not_applied rows, but only for jobs already in all_jobs.

    `not_applied.job_id` is a NOT NULL FK to all_jobs.job_id. Post-parse
    rejections went through `dedup.resolve_batch` so they exist; pre-filter
    rejections (DUPLICATE/LOCATION/COOLDOWN) may not — writing those would
    violate the FK and abort the whole commit, so we skip absent rows.
    """
    from sqlalchemy import select

    from src.state.models import AllJobs, NotApplied

    job_ids = [job.job_id for job, _reason, _detail in queue]
    if not job_ids:
        return
    present = set(
        session.execute(
            select(AllJobs.job_id).where(AllJobs.job_id.in_(job_ids))
        ).scalars()
    )
    for job, reason, detail in queue:
        if job.job_id not in present:
            continue
        session.merge(NotApplied(
            job_id=job.job_id,
            reason_category=reason,
            reason_detail=detail,
            not_applied_at=now,
        ))


if __name__ == "__main__":
    raise SystemExit(main())
