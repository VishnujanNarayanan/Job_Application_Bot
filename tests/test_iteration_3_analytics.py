"""Layer 9 — local CSV index + monthly text report.

The index is DERIVED from Postgres rather than appended during a run, so
these tests feed fake DB rows through the row builders and assert on the
files written. No Google APIs remain; nothing external is contacted.

Key properties under test:
  * correct columns and values per file
  * **idempotence** — exporting twice yields byte-identical files
  * an unwritable directory logs rather than raising (a failed export must
    never crash a pipeline run)
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import analytics
from src.reasons import DUPLICATE, LOW_SCORE
from src.state.models import AllJobs, Applied, NotApplied

_NOW = datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc)


def _make_job(**overrides) -> AllJobs:
    defaults = dict(
        job_id="linkedin-123",
        company="Fintech Inc",
        role="Python Developer",
        site="linkedin",
        location="Bangalore, India",
        job_url="https://linkedin.com/jobs/123",
        jd_text="We need a Python backend engineer with FastAPI experience.",
        salary_max_lpa=10.0,
    )
    defaults.update(overrides)
    return AllJobs(**defaults)


def _make_applied(**overrides) -> Applied:
    defaults = dict(
        job_id="linkedin-123",
        selection_json={"experiences": [{"title_alias": "Backend Engineer"}]},
        final_score=0.78,
        gap_skills=["Kafka", "Redis"],
        user_status="pending",
        built_at=_NOW,
    )
    defaults.update(overrides)
    return Applied(**defaults)


def _make_not_applied(reason: str, **overrides) -> NotApplied:
    defaults = dict(
        job_id="linkedin-123",
        reason_category=reason,
        reason_detail="0.42",
        not_applied_at=_NOW,
    )
    defaults.update(overrides)
    return NotApplied(**defaults)


def _session_returning(pairs: list[tuple]) -> MagicMock:
    """A session whose single `execute()` yields the given row tuples."""
    session = MagicMock()
    session.execute.return_value = pairs
    return session


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def test_match_rows_uses_title_alias_and_builds_resume_links():
    session = _session_returning([(_make_applied(), _make_job())])

    rows = analytics._match_rows(session, "https://box.tail.ts.net")

    assert len(rows) == 1
    row = dict(zip(analytics._MATCH_HEADER, rows[0]))
    assert row["Date"] == "2026-08-08 10:30"
    assert row["Company"] == "Fintech Inc"
    # Title comes from the stored selection, not the raw scraped role.
    assert row["Role"] == "Backend Engineer"
    assert row["Match"] == "0.78"
    assert row["Salary"] == "10 LPA"
    assert row["Source"] == "linkedin"
    assert row["Apply Link"] == "https://linkedin.com/jobs/123"
    assert row["PDF Link"] == "https://box.tail.ts.net/resume/linkedin-123.pdf"
    assert row["DOCX Link"] == "https://box.tail.ts.net/resume/linkedin-123.docx"
    assert row["Status"] == "pending"
    assert row["Gap Skills"] == "Kafka, Redis"


def test_match_rows_falls_back_to_scraped_role_without_selection():
    """A selection with no experiences must not blank the Role column."""
    session = _session_returning([
        (_make_applied(selection_json={"experiences": []}), _make_job()),
    ])

    rows = analytics._match_rows(session, "http://localhost:8000")

    assert rows[0][analytics._MATCH_HEADER.index("Role")] == "Python Developer"


def test_match_rows_handles_missing_salary():
    session = _session_returning([(_make_applied(), _make_job(salary_max_lpa=None))])

    rows = analytics._match_rows(session, "http://localhost:8000")

    assert rows[0][analytics._MATCH_HEADER.index("Salary")] == ""


def test_skipped_rows_truncates_jd_and_recovers_score_from_detail():
    """final_score is often unset on not_applied; the detail carries it."""
    job = _make_job(jd_text="x" * 500)
    session = _session_returning([(_make_not_applied(LOW_SCORE), job)])

    rows = analytics._skipped_rows(session)

    row = dict(zip(analytics._SKIPPED_HEADER, rows[0]))
    assert row["Reason"] == LOW_SCORE
    assert row["Score"] == "0.42"
    assert len(row["JD Snippet"]) == 300


def test_skipped_rows_tolerates_unparseable_detail():
    session = _session_returning([
        (_make_not_applied(LOW_SCORE, reason_detail="not-a-number"), _make_job()),
    ])

    rows = analytics._skipped_rows(session)

    assert rows[0][analytics._SKIPPED_HEADER.index("Score")] == ""


def test_near_duplicate_rows_link_to_the_original():
    job = _make_job(near_duplicate_of="indeed-999")
    session = _session_returning([(_make_not_applied(DUPLICATE), job)])

    rows = analytics._near_duplicate_rows(session)

    row = dict(zip(analytics._NEAR_DUP_HEADER, rows[0]))
    assert row["Original Job ID"] == "indeed-999"
    assert row["Source"] == "linkedin"


# ---------------------------------------------------------------------------
# export_index
# ---------------------------------------------------------------------------

@pytest.fixture
def index_dir(tmp_path: Path):
    """Redirect the export to a temp directory."""
    target = tmp_path / "index"
    with patch.object(analytics, "_index_dir", return_value=target):
        yield target


def test_export_index_writes_all_three_files(index_dir: Path):
    session = MagicMock()
    with patch.object(analytics, "_match_rows", return_value=[["a"] * 11]), \
         patch.object(analytics, "_skipped_rows", return_value=[["b"] * 8]), \
         patch.object(analytics, "_near_duplicate_rows", return_value=[["c"] * 5]):
        counts = analytics.export_index(session, endpoint_base_url="http://x")

    assert counts == {"matches": 1, "skipped": 1, "near_duplicates": 1}

    matches = _read_csv(index_dir / "matches.csv")
    assert matches[0] == analytics._MATCH_HEADER
    assert matches[1] == ["a"] * 11
    assert _read_csv(index_dir / "skipped.csv")[0] == analytics._SKIPPED_HEADER
    assert _read_csv(index_dir / "near_duplicates.csv")[0] == analytics._NEAR_DUP_HEADER


def test_export_index_writes_header_only_when_empty(index_dir: Path):
    session = MagicMock()
    with patch.object(analytics, "_match_rows", return_value=[]), \
         patch.object(analytics, "_skipped_rows", return_value=[]), \
         patch.object(analytics, "_near_duplicate_rows", return_value=[]):
        counts = analytics.export_index(session, endpoint_base_url="http://x")

    assert counts == {"matches": 0, "skipped": 0, "near_duplicates": 0}
    assert _read_csv(index_dir / "matches.csv") == [analytics._MATCH_HEADER]


def test_export_index_is_idempotent(index_dir: Path):
    """Exporting twice must not duplicate rows — it's a rewrite, not an append.

    This is the property that lets the dashboard call it on every page load
    and lets a phone-triggered run be picked up later without bookkeeping.
    """
    session = _session_returning([(_make_applied(), _make_job())])

    with patch.object(analytics, "_skipped_rows", return_value=[]), \
         patch.object(analytics, "_near_duplicate_rows", return_value=[]):
        analytics.export_index(session, endpoint_base_url="http://x")
        first = (index_dir / "matches.csv").read_bytes()

        analytics.export_index(session, endpoint_base_url="http://x")
        second = (index_dir / "matches.csv").read_bytes()

    assert first == second
    assert len(_read_csv(index_dir / "matches.csv")) == 2  # header + one row


def test_export_index_swallows_write_errors(index_dir: Path, caplog):
    """An unwritable target logs and returns; it must never raise."""
    session = MagicMock()
    with patch.object(analytics, "_match_rows", return_value=[]), \
         patch.object(analytics, "_skipped_rows", return_value=[]), \
         patch.object(analytics, "_near_duplicate_rows", return_value=[]), \
         patch.object(analytics, "_write_csv", side_effect=OSError("read-only fs")):
        counts = analytics.export_index(session, endpoint_base_url="http://x")

    assert counts == {}


def test_export_index_swallows_query_errors(index_dir: Path):
    """A DB error in one builder must not stop the other two files."""
    session = MagicMock()
    with patch.object(analytics, "_match_rows", side_effect=RuntimeError("boom")), \
         patch.object(analytics, "_skipped_rows", return_value=[]), \
         patch.object(analytics, "_near_duplicate_rows", return_value=[]):
        counts = analytics.export_index(session, endpoint_base_url="http://x")

    assert "matches" not in counts
    assert counts == {"skipped": 0, "near_duplicates": 0}
    assert (index_dir / "skipped.csv").exists()


def test_export_index_strips_trailing_slash_from_base_url(index_dir: Path):
    session = _session_returning([(_make_applied(), _make_job())])

    with patch.object(analytics, "_skipped_rows", return_value=[]), \
         patch.object(analytics, "_near_duplicate_rows", return_value=[]):
        analytics.export_index(session, endpoint_base_url="http://localhost:8000/")

    pdf = _read_csv(index_dir / "matches.csv")[1][analytics._MATCH_HEADER.index("PDF Link")]
    assert pdf == "http://localhost:8000/resume/linkedin-123.pdf"


def test_write_csv_leaves_no_temp_files(index_dir: Path):
    analytics._write_csv(index_dir / "matches.csv", ["A"], [["1"]])

    assert not list(index_dir.glob("*.tmp"))


# ---------------------------------------------------------------------------
# Monthly report
# ---------------------------------------------------------------------------

def test_monthly_report_skipped_when_disabled():
    fake_settings = MagicMock()
    fake_settings.analytics.report.monthly_enabled = False
    with patch.object(analytics, "settings", fake_settings), \
         patch.object(analytics, "_synthesize_report") as synth:
        assert analytics.write_monthly_report() is None
        synth.assert_not_called()


def test_render_report_contains_every_section():
    report = MagicMock(
        headline="Steady demand.",
        skill_demand="Python everywhere.",
        recurring_gaps="Kafka.",
        hiring_companies="Fintech Inc.",
        salary_observations="8-14 LPA.",
    )
    stats = {"total_scraped": 120, "total_matched": 9}

    text = analytics._render_report(report, stats)

    for fragment in (
        "Steady demand.", "Python everywhere.", "Kafka.",
        "Fintech Inc.", "8-14 LPA.", "120", "9",
    ):
        assert fragment in text


def test_monthly_report_failure_is_swallowed():
    """A Gemini or DB failure must not surface as a pipeline failure."""
    fake_settings = MagicMock()
    fake_settings.analytics.report.monthly_enabled = True
    with patch.object(analytics, "settings", fake_settings), \
         patch("src.state.db.session_scope", side_effect=RuntimeError("no db")):
        assert analytics.write_monthly_report() is None
