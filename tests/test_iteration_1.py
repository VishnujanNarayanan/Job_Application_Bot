"""Iteration 1 — end-to-end skeleton tests.

Each iteration's behaviour gets its own test file. Iter 1 tests are
offline (mocks for DB and Telegram). The real end-to-end run against
Neon happens as the acceptance check, not in pytest.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.llm.schemas import JDParsed
from src.parser import apply_to_row, parse
from src.scorer.apply_decision import Decision, decide
from src.scraper.jobspy_wrapper import scrape
from src.state.models import AllJobs


# ---------------------------------------------------------------------------
# Layer 2 — scraper stub
# ---------------------------------------------------------------------------


def test_scrape_returns_three_jobs() -> None:
    jobs = scrape()
    assert len(jobs) == 3
    assert all(isinstance(j, AllJobs) for j in jobs)


def test_scraped_job_ids_are_unique() -> None:
    jobs = scrape()
    ids = [j.job_id for j in jobs]
    assert len(set(ids)) == len(ids)


def test_scraped_jobs_have_required_fields() -> None:
    jobs = scrape()
    for job in jobs:
        assert job.company
        assert job.role
        assert job.site == "indeed"
        assert job.jd_text


# ---------------------------------------------------------------------------
# Layer 3 — parser stub
# ---------------------------------------------------------------------------


def test_parse_returns_jdparsed_with_valid_role_level() -> None:
    jobs = scrape()
    parsed = parse(jobs[0])
    assert isinstance(parsed, JDParsed)
    # Literal-enforced — Pydantic would have raised on construction otherwise.
    assert parsed.role_level in {"junior", "mid", "senior", "lead"}


def test_apply_to_row_mutates_job_fields() -> None:
    job = scrape()[0]
    parsed = parse(job)
    apply_to_row(job, parsed)
    assert job.role_category == parsed.role_category
    assert job.years_required == parsed.years_required
    assert job.required_skills == parsed.required_skills


# ---------------------------------------------------------------------------
# Layer 4 — scorer stub
# ---------------------------------------------------------------------------


def test_decide_rejects_when_no_bullets() -> None:
    """Empty master_bullets → LOW_SCORE rejection."""
    session = MagicMock()
    session.scalar.return_value = None  # no bullet rows

    decision = decide(session)

    assert isinstance(decision, Decision)
    assert decision.apply is False
    assert decision.reason_category == "LOW_SCORE"
    assert decision.final_score == 0.0


def test_decide_still_rejects_when_bullets_exist() -> None:
    """Iter 1 is a stub: even with bullets present, decision is LOW_SCORE."""
    session = MagicMock()
    session.scalar.return_value = "some-bullet-id"

    decision = decide(session)

    assert decision.apply is False
    assert decision.reason_category == "LOW_SCORE"


# ---------------------------------------------------------------------------
# Layer 7 — models import
# ---------------------------------------------------------------------------


def test_all_models_importable() -> None:
    from src.state.models import (  # noqa: F401
        AllJobs,
        AnswerBank,
        Applied,
        ApplicationQueue,
        CompanyCooldown,
        MasterBullets,
        MasterMeta,
        MasterSummaries,
        MasterTitleAliases,
        NotApplied,
        PendingReview,
        PortalHealth,
        ProcessingQueue,
        SearchRotationState,
    )
