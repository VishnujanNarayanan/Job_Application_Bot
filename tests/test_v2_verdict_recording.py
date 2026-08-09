"""v2 — every rejection gets a verdict, and the verdict carries its scores.

Two defects found by the 2026-08-09 data-flow audit, neither of which had any
test:

  * `not_applied` rows were written with NULL fit/success_prob/recency/final
    across all 693 of them, because `_write_not_applied` took only
    (job, reason, detail) and dropped the SelectionResult. The rejected
    population — the one you read to answer "why did nothing match?" — was
    unauditable.
  * Rejections decided BEFORE the batch insert (duplicate, disallowed
    location, company cooldown) were skipped entirely to avoid an FK
    violation, so two whole reason categories never reached the database:
    zero COMPANY_COOLDOWN and zero LOCATION_DISALLOWED rows existed despite
    51 companies sitting in cooldown.

The session is a MagicMock (house pattern) — nothing here touches Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.main import _write_not_applied
from src.reasons import COMPANY_COOLDOWN, LOW_SCORE
from src.state.models import AllJobs

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _job(job_id: str) -> AllJobs:
    return AllJobs(job_id=job_id, company="ACME", role="Data Engineer", site="linkedin")


def _session(present: set[str]) -> MagicMock:
    """A session whose all_jobs already contains ``present``."""
    session = MagicMock()
    session.execute.return_value.scalars.return_value = list(present)
    return session


def _scores(final: float) -> SimpleNamespace:
    return SimpleNamespace(
        fit=0.42, success_prob=0.36, recency=0.60, final_score=final
    )


def test_a_scored_rejection_records_the_numbers_behind_it() -> None:
    job = _job("linkedin-1")
    session = _session({"linkedin-1"})

    _write_not_applied(session, [(job, LOW_SCORE, "0.47", _scores(0.47))], NOW)

    (row,), _ = session.merge.call_args
    assert row.reason_category == LOW_SCORE
    assert row.fit_score == 0.42
    assert row.success_prob == 0.36
    assert row.recency_score == 0.60
    assert row.final_score == 0.47


def test_an_unscored_rejection_records_nulls_rather_than_inventing_zeros() -> None:
    """A cooldown skip never reached the scorer, so it has no scores.

    Zeros would be a lie that reads as "scored terribly" in any later
    analysis; NULL correctly says "never scored".
    """
    job = _job("linkedin-2")
    session = _session({"linkedin-2"})

    _write_not_applied(session, [(job, COMPANY_COOLDOWN, "ACME", None)], NOW)

    (row,), _ = session.merge.call_args
    assert row.final_score is None
    assert row.fit_score is None


def test_a_prefilter_rejection_is_persisted_instead_of_dropped() -> None:
    """The job is not yet in all_jobs, so insert it — don't discard the verdict.

    This is the COMPANY_COOLDOWN / LOCATION_DISALLOWED hole: the FK guard used
    to skip these, which is why neither reason ever appeared in 693 rows.
    """
    job = _job("linkedin-new")
    session = _session(set())          # all_jobs has never seen it

    _write_not_applied(session, [(job, COMPANY_COOLDOWN, "ACME", None)], NOW)

    assert session.add.call_args_list, "the missing all_jobs row must be inserted"
    assert session.add.call_args_list[0].args[0] is job
    assert session.merge.called, "and the verdict must still be recorded"


def test_a_job_already_in_all_jobs_is_not_re_added() -> None:
    """Re-adding would blank what the pipeline already learned.

    These instances come off the scraper with nulls for every parsed column,
    so writing one over an existing row would wipe its jd_embedding,
    required_skills and role_* fields.
    """
    job = _job("linkedin-3")
    session = _session({"linkedin-3"})

    _write_not_applied(session, [(job, LOW_SCORE, "0.30", _scores(0.30))], NOW)

    assert not session.add.called


def test_the_same_job_twice_in_one_batch_is_inserted_once() -> None:
    """Two search terms can surface one listing; the PK must not collide."""
    job = _job("linkedin-dup")
    session = _session(set())

    _write_not_applied(
        session,
        [(job, COMPANY_COOLDOWN, "ACME", None), (job, COMPANY_COOLDOWN, "ACME", None)],
        NOW,
    )

    assert session.add.call_count == 1
