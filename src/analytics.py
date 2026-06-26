"""Layer 9 — Google Sheets index + monthly Gemini Docs report.

Two outputs (architecture §9):

  * **Sheets** — the live "which-resume-for-which-job" index. Three tabs:
      - Matches   (every job >= 0.50: company, role, score, links, status)
      - Skipped   (in-field LOW_SCORE jobs, with the rejection reason)
      - Near-dupes (cross-portal reposts deduped against an original)
  * **Docs** — a monthly Gemini-written report (skill demand, recurring
    gaps, hiring companies, salary ranges).

Design rules:

  * **Never crash a pipeline run.** Every public function is best-effort:
    if Google is unconfigured or the API errors, it logs and returns. A
    failed Sheets write must not roll back the DB writes that already
    happened (same contract as the Telegram send in ``src/main.py``).
  * **Lazy + cached clients** so importing this module is cheap and the
    pipeline runs fine with no Google credentials at all.
  * Row fields reuse ``notifications.match_display_fields`` so the Sheet
    and the Telegram message never disagree.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import structlog

from src.config import settings

log = structlog.get_logger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
]

# Column headers per tab (written once when a tab is first created).
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
_NEAR_DUP_TAB = "Near-duplicates"


def _enabled() -> bool:
    """True only when the Sheets ID + credentials file are both present."""
    sheet_id = os.environ.get("GOOGLE_SHEETS_ID", "").strip()
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not sheet_id or not cred_path:
        return False
    return Path(cred_path).is_file()


@lru_cache(maxsize=1)
def _credentials():
    from google.oauth2.service_account import Credentials

    cred_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    return Credentials.from_service_account_file(cred_path, scopes=_SCOPES)


@lru_cache(maxsize=1)
def _spreadsheet():
    """Open and cache the operator's index spreadsheet."""
    import gspread

    gc = gspread.authorize(_credentials())
    return gc.open_by_key(os.environ["GOOGLE_SHEETS_ID"])


def _worksheet(title: str, header: list[str]):
    """Return the named worksheet, creating it (with header) if absent."""
    import gspread

    ss = _spreadsheet()
    try:
        return ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=max(len(header), 12))
        ws.append_row(header, value_input_option="USER_ENTERED")
        return ws


# ---------------------------------------------------------------------------
# Per-run row writers (called from the pipeline)
# ---------------------------------------------------------------------------

def append_match_row(
    job,
    parsed,
    result,
    gap_skills: list[str],
    endpoint_base_url: str,
    *,
    title_alias: str | None = None,
) -> None:
    """Append one matched job to the Matches tab. Best-effort."""
    if not _enabled():
        return
    from src.notifications import match_display_fields

    try:
        fields = match_display_fields(job, parsed, title_alias=title_alias)
        pdf_url = f"{endpoint_base_url}/resume/{job.job_id}.pdf"
        docx_url = f"{endpoint_base_url}/resume/{job.job_id}.docx"
        row = [
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            job.company or "",
            fields["display_title"],
            f"{result.final_score:.2f}",
            fields["salary_str"],
            job.site or "",
            fields["apply_url"],
            pdf_url,
            docx_url,
            "pending",
            ", ".join(gap_skills),
        ]
        ws = _worksheet(settings.analytics.sheets.applied_tab, _MATCH_HEADER)
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info("sheet_row_written", tab="matches", job_id=job.job_id)
    except Exception as exc:
        log.error("sheet_write_failed", tab="matches", job_id=job.job_id, error=str(exc))


def append_skipped_row(
    job,
    reason_category: str,
    reason_detail: str | None,
    final_score: float | None,
) -> None:
    """Append one skipped (in-field) job to the Skipped tab. Best-effort."""
    if not _enabled():
        return
    try:
        jd_snippet = (job.jd_text or "")[:300]
        row = [
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            job.company or "",
            job.role or "",
            reason_category,
            reason_detail or "",
            f"{final_score:.2f}" if final_score is not None else "",
            job.site or "",
            jd_snippet,
        ]
        ws = _worksheet(settings.analytics.sheets.skipped_tab, _SKIPPED_HEADER)
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info("sheet_row_written", tab="skipped", job_id=job.job_id)
    except Exception as exc:
        log.error("sheet_write_failed", tab="skipped", job_id=job.job_id, error=str(exc))


def append_near_duplicate_row(job, original_job_id: str | None) -> None:
    """Append one deduped job to the Near-duplicates tab. Best-effort."""
    if not _enabled():
        return
    try:
        row = [
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            job.company or "",
            job.role or "",
            job.site or "",
            original_job_id or "",
        ]
        ws = _worksheet(_NEAR_DUP_TAB, _NEAR_DUP_HEADER)
        ws.append_row(row, value_input_option="USER_ENTERED")
        log.info("sheet_row_written", tab="near_duplicates", job_id=job.job_id)
    except Exception as exc:
        log.error(
            "sheet_write_failed", tab="near_duplicates", job_id=job.job_id, error=str(exc)
        )


# ---------------------------------------------------------------------------
# Monthly report (separate CLI / cron, not per-run)
# ---------------------------------------------------------------------------

def _doc_enabled() -> bool:
    doc_id = os.environ.get("GOOGLE_DOC_ID", "").strip()
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    return bool(doc_id and cred_path and Path(cred_path).is_file())


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
    threshold = float(settings.analytics.docs.skill_demand_alert_threshold)
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


def _append_to_doc(report) -> None:
    """Append the report as a new dated section to the Google Doc."""
    from googleapiclient.discovery import build

    docs = build("docs", "v1", credentials=_credentials(), cache_discovery=False)
    doc_id = os.environ["GOOGLE_DOC_ID"]

    # Find the current end index so we append rather than overwrite.
    doc = docs.documents().get(documentId=doc_id).execute()
    end_index = doc["body"]["content"][-1]["endIndex"] - 1

    heading = f"\n\nMonthly Report — {datetime.now(timezone.utc):%Y-%m-%d}\n"
    body = (
        f"{report.headline}\n\n"
        f"Skill demand: {report.skill_demand}\n\n"
        f"Recurring gaps: {report.recurring_gaps}\n\n"
        f"Hiring companies: {report.hiring_companies}\n\n"
        f"Salary: {report.salary_observations}\n"
    )
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {"insertText": {"location": {"index": end_index}, "text": heading + body}}
            ]
        },
    ).execute()


def write_monthly_report() -> None:
    """Build and append the monthly Gemini report to the Google Doc.

    Best-effort: logs and returns if Docs is unconfigured or any step
    fails. Called from ``python -m src.cli.report`` (monthly cron), never
    per pipeline run.
    """
    if not settings.analytics.docs.monthly_report_enabled:
        log.info("monthly_report_skipped", reason="disabled_in_config")
        return
    if not _doc_enabled():
        log.info("monthly_report_skipped", reason="docs_not_configured")
        return

    from src.state.db import session_scope

    try:
        with session_scope() as session:
            stats = _gather_month_stats(session)
        report = _synthesize_report(stats)
        _append_to_doc(report)
        log.info(
            "monthly_report_written",
            scraped=stats["total_scraped"],
            matched=stats["total_matched"],
            alert_skills=len(stats["alert_skills"]),
        )
    except Exception as exc:
        log.error("monthly_report_failed", error=str(exc), exc_info=True)
