"""Every scraped job reaches a verdict, and the cheap verdicts come first.

Run 37333275047 (2026-10-05) scraped 40 jobs, reported 10 matched + 11
skipped, and never looked at 10 of the rest: the 20-job short-circuit stopped
it, and the 1-hour scrape window meant they were never scraped again. These
tests pin the replacements -- pre-parse checks on scraped fields, a backlog for
whatever a run does not reach, and a summary that accounts for every job.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src.config import settings
from src.main import _load_backlog
from src.notifications import run_summary_text
from src.scraper.filters import cannot_reach_threshold, company_blocked, title_disallowed
from src.state.models import AllJobs

NOW = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
PATTERNS = list(settings.filters.title_blocklist)


# --- Title -------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Software Developer Intern",          # the parser called this full-time
    "Artificial Intelligence Intern",
    "Data Science Internship",
    "Apprentice - Cloud",
    "Part-time Python Tutor",
    "Part Time Data Analyst",
    "Freelance ML Engineer",
    "Contract Data Engineer",
    "Contractor - SRE",
])
def test_a_rejected_employment_type_in_the_title_is_caught(title):
    assert title_disallowed(title, PATTERNS)


@pytest.mark.parametrize("title", [
    "Graduate Engineer Trainee",   # full-time entry role in India
    "Internal Tools Engineer",     # "intern" inside a word
    "International Payments Developer",
    "Smart Contract Developer",    # blockchain, not a contract
    "Senior Data Engineer",
])
def test_a_full_time_title_passes(title):
    assert not title_disallowed(title, PATTERNS)


def test_only_the_title_is_read():
    """A JD that mentions a trainee period or a contract clause is not the
    title -- the check takes the title string and nothing else."""
    assert not title_disallowed(None, PATTERNS)
    assert not title_disallowed("Backend Engineer", PATTERNS)


# --- Applicants --------------------------------------------------------------

def test_a_crowded_posting_that_cannot_reach_the_threshold_is_skipped():
    assert cannot_reach_threshold(201, best_fit=0.75, threshold=0.45)


def test_an_early_posting_is_never_skipped():
    assert not cannot_reach_threshold(24, best_fit=0.75, threshold=0.45)
    assert not cannot_reach_threshold(100, best_fit=0.75, threshold=0.45)


def test_an_unknown_count_is_never_skipped():
    """An absent measurement is not a crowd."""
    assert not cannot_reach_threshold(None, best_fit=0.75, threshold=0.45)


def test_the_cut_off_moves_with_the_threshold():
    assert not cannot_reach_threshold(150, best_fit=0.75, threshold=0.45)
    assert cannot_reach_threshold(150, best_fit=0.75, threshold=0.65)


# --- Company blocklist -------------------------------------------------------

def test_the_company_blocklist_is_enforced():
    assert company_blocked("ACME Corp", ["acme corp"])
    assert not company_blocked("ACME Corporation", ["acme corp"])
    assert not company_blocked(None, ["acme corp"])
    assert not company_blocked("ACME Corp", [])


# --- Nothing is dropped ------------------------------------------------------

def test_the_short_circuit_is_off():
    assert int(settings.scraper.short_circuit_count) == 0


def test_the_time_budget_ends_the_run_inside_the_workflow_timeout():
    """A run killed by the timeout writes no verdicts and sends no summary."""
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/pipeline.yml").read_text())
    timeout = int(workflow["jobs"]["run"]["timeout-minutes"])
    budget = float(settings.scraper.time_budget_minutes)
    assert 0 < budget <= timeout - 10, "leave room to write verdicts and notify"


def test_the_backlog_skips_jobs_already_in_this_batch():
    old = AllJobs(job_id="linkedin-1", company="A", role="DE", site="linkedin")
    both = AllJobs(job_id="linkedin-2", company="B", role="DE", site="linkedin")
    session = MagicMock()
    session.scalars.return_value.all.return_value = [old, both]

    assert _load_backlog(session, NOW, 3, {"linkedin-2"}) == [old]


def test_a_zero_day_backlog_is_off():
    session = MagicMock()
    assert _load_backlog(session, NOW, 0, set()) == []
    session.scalars.assert_not_called()


# --- Summary -----------------------------------------------------------------

def test_the_summary_accounts_for_every_job():
    outcomes = {"LOW_SCORE": 5, "HARD_FILTER_LAYER_3": 5, "JOB_TYPE_DISALLOWED": 1,
                "COMPANY_COOLDOWN": 4, "LOCATION_DISALLOWED": 4, "NOT_REACHED": 10,
                "EMPTY_JD": 1}
    text = run_summary_text(scraped=40, skipped=11, applied=10,
                            outcomes=outcomes, dry_run=False)

    counts = [int(line.rsplit(": ", 1)[1]) for line in text.splitlines()
              if ": " in line and not line.startswith("Scraped")]
    assert sum(counts) == 40
    assert "Not reached (carried to next run): 10" in text


def test_a_live_run_is_not_called_a_dry_run():
    assert "Dry Run" not in run_summary_text(scraped=1, skipped=0, applied=1, dry_run=False)
    assert "Dry Run" in run_summary_text(scraped=1, skipped=0, applied=1, dry_run=True)


def test_carried_over_jobs_are_shown():
    text = run_summary_text(scraped=25, skipped=0, applied=0, backlog=10, dry_run=False)
    assert "Scraped: 25 (+10 carried over)" in text
