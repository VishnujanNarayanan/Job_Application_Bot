"""Layer 9 — local CSV index + monthly text report.

Two outputs (architecture §9, local-file variant):

  * **CSV index** — the live "which-resume-for-which-job" index, three files
    under ``analytics.local.dir``:
      - matches.csv          (every job >= threshold: company, role, score, links)
      - skipped.csv          (in-field LOW_SCORE jobs, with the rejection reason)
      - near_duplicates.csv  (cross-portal reposts deduped against an original)
  * **Report** — a monthly Gemini-written text file (skill demand, recurring
    gaps, hiring companies, salary ranges).

Design rules:

  * **The CSVs are DERIVED from Postgres, never appended during a run.** A
    pipeline run triggered from the phone executes on an ephemeral GitHub
    Actions runner whose filesystem is destroyed when the job ends, so inline
    appends would be lost. They don't need to survive: the run's real output
    already landed in Neon, and every column here is derivable from
    ``all_jobs`` / ``applied`` / ``not_applied`` — ``parser.apply_to_row``
    copies the parsed fields (``location_type``, ``salary_*``, ...) onto the
    ``AllJobs`` row, so nothing lives only on a transient ``JDParsed``.
    Deriving instead of appending means no sync queue, no merge conflicts,
    idempotent regeneration, and phone-triggered runs showing up the moment
    the laptop next opens.
  * **Never crash a pipeline run.** ``export_index`` is best-effort: on any
    OS error it logs and returns. A failed export must not roll back the DB
    writes that already happened (same contract as the Telegram send in
    ``src/main.py``).
  * Writes go to a temp file then ``os.replace``, so an interrupted export
    cannot truncate a good file.
"""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from src.config import settings

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# Column headers per file. Unchanged from the Google Sheets tabs they replace,
# so an existing exported sheet and a new CSV line up column-for-column.
_MATCH_HEADER = [
    "Date", "Company", "Role", "Match", "Salary", "Source",
    "Apply Link", "PDF Link", "DOCX Link", "Status", "Gap Skills",
]
_SKIPPED_HEADER = [
    "Date", "Company", "Role", "Reason", "Detail", "Score", "Source", "JD Snippet",
]
_NEAR_DUP_HEADER = [
    "Date", "Company", "Role", "Source", "Original Job ID",
]

_TS_FMT = "%Y-%m-%d %H:%M"


def _index_dir() -> Path:
    """Absolute path to the configured index directory."""
    return _ROOT / str(settings.analytics.local.dir)


def _report_dir() -> Path:
    return _ROOT / str(settings.analytics.report.dir)


def _fmt_ts(value: datetime | None) -> str:
    return value.strftime(_TS_FMT) if value else ""


def _fmt_score(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else ""


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    """Atomically write ``header`` + ``rows`` to ``path``.

    Writes to a temp file in the same directory then ``os.replace``s, so a
    crash mid-write leaves the previous good file intact rather than a
    truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(tmp_name, path)
    except BaseException:
        # Don't leave the temp file behind on any failure path.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Row builders — one per file, each a pure function of DB rows
# ---------------------------------------------------------------------------

def _match_rows(session, endpoint_base_url: str) -> list[list[str]]:
    """Every matched job, oldest first."""
    from sqlalchemy import select

    from src.state.models import AllJobs, Applied

    stmt = (
        select(Applied, AllJobs)
        .join(AllJobs, Applied.job_id == AllJobs.job_id)
        .order_by(Applied.built_at)
    )
    rows: list[list[str]] = []
    for applied, job in session.execute(stmt):
        # Prefer the LLM-chosen title alias from the stored selection so the
        # CSV shows the same role text the resume and Telegram message use.
        rows.append([
            _fmt_ts(applied.built_at),
            job.company or "",
            _title_alias(applied.selection_json) or job.role or "",
            _fmt_score(applied.final_score),
            f"{job.salary_max_lpa:.0f} LPA" if job.salary_max_lpa else "",
            job.site or "",
            job.job_url or "",
            f"{endpoint_base_url}/resume/{job.job_id}.pdf",
            f"{endpoint_base_url}/resume/{job.job_id}.docx",
            applied.user_status or "pending",
            ", ".join(str(s) for s in (applied.gap_skills or [])),
        ])
    return rows


def _title_alias(selection_json) -> str | None:
    """First experience's title alias from a stored selection, if present."""
    if not isinstance(selection_json, dict):
        return None
    experiences = selection_json.get("experiences") or []
    if experiences and isinstance(experiences[0], dict):
        return experiences[0].get("title_alias")
    return None


def _skipped_rows(session) -> list[list[str]]:
    """In-field jobs rejected on score — the "relevant skipped" index."""
    from sqlalchemy import select

    from src.reasons import LOW_SCORE
    from src.state.models import AllJobs, NotApplied

    stmt = (
        select(NotApplied, AllJobs)
        .join(AllJobs, NotApplied.job_id == AllJobs.job_id)
        .where(NotApplied.reason_category == LOW_SCORE)
        .order_by(NotApplied.not_applied_at)
    )
    rows: list[list[str]] = []
    for na, job in session.execute(stmt):
        # `final_score` is populated on the row when available; the orchestrator
        # also stashes the score string in reason_detail, so fall back to that.
        score = na.final_score
        if score is None and na.reason_detail:
            try:
                score = float(na.reason_detail)
            except ValueError:
                score = None
        rows.append([
            _fmt_ts(na.not_applied_at),
            job.company or "",
            job.role or "",
            na.reason_category or "",
            na.reason_detail or "",
            _fmt_score(score),
            job.site or "",
            (job.jd_text or "")[:300],
        ])
    return rows


def _near_duplicate_rows(session) -> list[list[str]]:
    """Cross-portal reposts deduped against an already-seen listing."""
    from sqlalchemy import select

    from src.reasons import DUPLICATE
    from src.state.models import AllJobs, NotApplied

    stmt = (
        select(NotApplied, AllJobs)
        .join(AllJobs, NotApplied.job_id == AllJobs.job_id)
        .where(NotApplied.reason_category == DUPLICATE)
        .order_by(NotApplied.not_applied_at)
    )
    rows: list[list[str]] = []
    for na, job in session.execute(stmt):
        rows.append([
            _fmt_ts(na.not_applied_at),
            job.company or "",
            job.role or "",
            job.site or "",
            job.near_duplicate_of or "",
        ])
    return rows


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def export_index(session, *, endpoint_base_url: str | None = None) -> dict[str, int]:
    """Regenerate all three index CSVs from the database.

    Full deterministic rewrite — safe to call any number of times; exporting
    twice produces byte-identical files. Best-effort: on an OS error it logs
    ``index_export_failed`` and returns the counts written so far rather than
    raising, so a read-only disk can never fail a pipeline run.

    Returns a ``{filename_stem: row_count}`` mapping.
    """
    if endpoint_base_url is None:
        endpoint_base_url = str(settings.endpoint.base_url).rstrip("/")
    else:
        endpoint_base_url = endpoint_base_url.rstrip("/")

    cfg = settings.analytics.local
    directory = _index_dir()
    counts: dict[str, int] = {}

    targets = [
        (str(cfg.matches_file), _MATCH_HEADER,
         lambda: _match_rows(session, endpoint_base_url)),
        (str(cfg.skipped_file), _SKIPPED_HEADER,
         lambda: _skipped_rows(session)),
        (str(cfg.near_duplicates_file), _NEAR_DUP_HEADER,
         lambda: _near_duplicate_rows(session)),
    ]

    for filename, header, build_rows in targets:
        try:
            rows = build_rows()
            _write_csv(directory / filename, header, rows)
            counts[Path(filename).stem] = len(rows)
        except OSError as exc:
            log.error("index_export_failed", file=filename, error=str(exc))
        except Exception as exc:
            log.error(
                "index_export_failed", file=filename, error=str(exc), exc_info=True
            )

    log.info("index_exported", dir=str(directory), **counts)
    return counts


# ---------------------------------------------------------------------------
# Monthly report (separate CLI / cron, not per-run)
# ---------------------------------------------------------------------------

def _gather_month_stats(session) -> dict:
    """Aggregate the last 30 days of jobs for the monthly report."""
    from collections import Counter

    from sqlalchemy import select

    from src.state.models import AllJobs, Applied

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    jobs = list(
        session.execute(
            select(AllJobs).where(AllJobs.scraped_at >= cutoff)
        ).scalars()
    )
    matched = list(
        session.execute(
            select(Applied).where(Applied.built_at >= cutoff)
        ).scalars()
    )

    skill_counter: Counter[str] = Counter()
    company_counter: Counter[str] = Counter()
    gap_counter: Counter[str] = Counter()
    salaries: list[float] = []

    for j in jobs:
        for sk in (j.required_skills or []):
            skill_counter[str(sk).strip()] += 1
        if j.company:
            company_counter[j.company] += 1
        if j.salary_max_lpa:
            salaries.append(float(j.salary_max_lpa))

    for a in matched:
        for sk in (a.gap_skills or []):
            gap_counter[str(sk).strip()] += 1

    total_jobs = len(jobs) or 1
    threshold = float(settings.analytics.report.skill_demand_alert_threshold)
    alert_skills = [
        s for s, c in skill_counter.most_common()
        if c / total_jobs >= threshold
    ]

    return {
        "total_scraped": len(jobs),
        "total_matched": len(matched),
        "top_skills": skill_counter.most_common(15),
        "alert_skills": alert_skills,
        "top_gaps": gap_counter.most_common(10),
        "top_companies": company_counter.most_common(10),
        "salary_min": min(salaries) if salaries else None,
        "salary_max": max(salaries) if salaries else None,
        "salary_avg": (sum(salaries) / len(salaries)) if salaries else None,
    }


def _synthesize_report(stats: dict):
    """Single Gemini call → MonthlyReportLLM prose from the aggregated stats."""
    from src.llm.client import complete
    from src.llm.schemas import MonthlyReportLLM

    prompt = (
        "Write a concise monthly job-market report for a software engineer's "
        "personal job-search assistant. Narrate ONLY the figures below; do not "
        "invent companies, skills, or numbers not present here.\n\n"
        f"Jobs scraped (30d): {stats['total_scraped']}\n"
        f"Jobs matched (30d): {stats['total_matched']}\n"
        f"Most-demanded skills (skill, count): {stats['top_skills']}\n"
        f"High-demand alert skills (>= threshold of JDs): {stats['alert_skills']}\n"
        f"Recurring skill gaps (skill, count): {stats['top_gaps']}\n"
        f"Most-active companies (company, count): {stats['top_companies']}\n"
        f"Salary LPA — min/avg/max: "
        f"{stats['salary_min']}/{stats['salary_avg']}/{stats['salary_max']}\n"
    )
    return complete(MonthlyReportLLM, prompt)


def _render_report(report, stats: dict) -> str:
    """Format the report as the plain-text body written to disk."""
    now = datetime.now(timezone.utc)
    return (
        f"Monthly Report — {now:%Y-%m-%d}\n"
        f"{'=' * 40}\n\n"
        f"{report.headline}\n\n"
        f"Jobs scraped (30d): {stats['total_scraped']}\n"
        f"Jobs matched (30d): {stats['total_matched']}\n\n"
        f"Skill demand: {report.skill_demand}\n\n"
        f"Recurring gaps: {report.recurring_gaps}\n\n"
        f"Hiring companies: {report.hiring_companies}\n\n"
        f"Salary: {report.salary_observations}\n"
    )


def write_monthly_report() -> Path | None:
    """Build the monthly Gemini report and write it to a local text file.

    Best-effort: logs and returns ``None`` if disabled or any step fails.
    Called from ``python -m src.cli.report`` (monthly cron), never per
    pipeline run. Runs on the laptop — it reads Neon, so the report is
    complete regardless of where the pipeline runs happened.

    Returns the path written, or ``None``.
    """
    if not settings.analytics.report.monthly_enabled:
        log.info("monthly_report_skipped", reason="disabled_in_config")
        return None

    from src.state.db import session_scope

    try:
        with session_scope() as session:
            stats = _gather_month_stats(session)
        report = _synthesize_report(stats)

        directory = _report_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"report-{datetime.now(timezone.utc):%Y-%m}.txt"
        path.write_text(_render_report(report, stats), encoding="utf-8")

        log.info(
            "monthly_report_written",
            path=str(path),
            scraped=stats["total_scraped"],
            matched=stats["total_matched"],
            alert_skills=len(stats["alert_skills"]),
        )
        return path
    except Exception as exc:
        log.error("monthly_report_failed", error=str(exc), exc_info=True)
        return None
