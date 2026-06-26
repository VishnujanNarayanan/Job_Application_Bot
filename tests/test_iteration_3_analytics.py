"""Iteration 3 — Layer 9: Google Sheets index + monthly Docs report.

Google APIs are mocked — no real Sheets/Docs calls. Verifies row content,
tab/header creation, and that Google failures are swallowed (a failed write
must never crash a pipeline run).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src import analytics
from src.llm.schemas import JDParsed
from src.scorer.apply_decision import SelectionResult
from src.scorer.selector import (
    SelectedBullet, SelectedExperience, SummaryCand,
)
from src.state.models import AllJobs


def _make_job() -> AllJobs:
    return AllJobs(
        job_id="job123",
        company="Fintech Inc",
        role="Python Developer",
        site="linkedin",
        location="Bangalore, India",
        job_url="https://linkedin.com/jobs/123",
        jd_text="We need a Python backend engineer with FastAPI experience.",
    )


def _make_parsed() -> JDParsed:
    return JDParsed(
        role_summary="Backend Python role at fintech.",
        role_category="backend",
        role_level="mid",
        years_required=2,
        required_skills=["Python", "FastAPI"],
        nice_to_have=["Kafka"],
        responsibilities=["Build APIs"],
        salary_max_lpa=10.0,
        salary_currency="INR",
        location_type="remote",
        apply_url="https://apply.fintech.com/123",
    )


def _make_result() -> SelectionResult:
    exp = SelectedExperience(
        id="exp1", company="ACME", actual_title="Backend Dev",
        safe_title_aliases=["Backend Dev"], score=0.80,
        alias_score=0.75, end_date="present",
        bullets=[SelectedBullet("b1", "Did X", 0.9)],
    )
    summary = SummaryCand(
        id="sum1", text="Engineer.", role_categories=["backend"], embedding=[0.0]
    )
    return SelectionResult(
        apply=True, final_score=0.78, fit=0.75, success_prob=0.82,
        recency=0.60, project_score=0.70,
        summary=summary, summary_score=0.65,
        experiences=[exp], projects=[],
        skill_candidates=[("Python", 0.9)],
        skills_before_projects=True,
    )


def test_append_match_row_writes_expected_values():
    """append_match_row builds the correct row and appends it to the tab."""
    fake_ws = MagicMock()
    job, parsed, result = _make_job(), _make_parsed(), _make_result()

    with patch("src.analytics._enabled", return_value=True), \
         patch("src.analytics._worksheet", return_value=fake_ws):
        analytics.append_match_row(
            job=job, parsed=parsed, result=result,
            gap_skills=["Kafka"],
            endpoint_base_url="https://abc.ngrok-free.app",
            title_alias="Backend Engineer",
        )

    fake_ws.append_row.assert_called_once()
    row = fake_ws.append_row.call_args[0][0]
    assert "Fintech Inc" in row
    assert "Backend Engineer" in row
    assert "0.78" in row
    assert "https://apply.fintech.com/123" in row
    assert "https://abc.ngrok-free.app/resume/job123.pdf" in row
    assert "https://abc.ngrok-free.app/resume/job123.docx" in row
    assert "pending" in row
    assert "Kafka" in row


def test_append_skipped_row_writes_reason_and_snippet():
    fake_ws = MagicMock()
    job = _make_job()

    with patch("src.analytics._enabled", return_value=True), \
         patch("src.analytics._worksheet", return_value=fake_ws):
        analytics.append_skipped_row(job, "LOW_SCORE", "0.42", 0.42)

    row = fake_ws.append_row.call_args[0][0]
    assert "LOW_SCORE" in row
    assert "0.42" in row
    assert "Python backend engineer" in row[-1]  # JD snippet column


def test_append_near_duplicate_row_links_original():
    fake_ws = MagicMock()
    job = _make_job()

    with patch("src.analytics._enabled", return_value=True), \
         patch("src.analytics._worksheet", return_value=fake_ws):
        analytics.append_near_duplicate_row(job, "original-456")

    row = fake_ws.append_row.call_args[0][0]
    assert "original-456" in row


def test_disabled_is_a_noop():
    """With Google unconfigured, writers do nothing (no client built)."""
    job, parsed, result = _make_job(), _make_parsed(), _make_result()
    with patch("src.analytics._enabled", return_value=False), \
         patch("src.analytics._worksheet") as ws:
        analytics.append_match_row(
            job=job, parsed=parsed, result=result, gap_skills=[],
            endpoint_base_url="http://x", title_alias="T",
        )
        analytics.append_skipped_row(job, "LOW_SCORE", "0.4", 0.4)
        analytics.append_near_duplicate_row(job, None)
    ws.assert_not_called()


def test_google_failure_is_swallowed():
    """A raising Sheets client must not propagate (run continues)."""
    job, parsed, result = _make_job(), _make_parsed(), _make_result()
    with patch("src.analytics._enabled", return_value=True), \
         patch("src.analytics._worksheet", side_effect=RuntimeError("403 boom")):
        # Should not raise.
        analytics.append_match_row(
            job=job, parsed=parsed, result=result, gap_skills=[],
            endpoint_base_url="http://x", title_alias="T",
        )
        analytics.append_skipped_row(job, "LOW_SCORE", "0.4", 0.4)


def test_write_monthly_report_disabled_in_config_is_noop():
    """Respects the config flag; no Gemini call when disabled."""
    fake_settings = MagicMock()
    fake_settings.analytics.docs.monthly_report_enabled = False
    with patch("src.analytics.settings", fake_settings), \
         patch("src.analytics._synthesize_report") as synth:
        analytics.write_monthly_report()
    synth.assert_not_called()
