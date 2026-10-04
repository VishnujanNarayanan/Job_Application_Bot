"""A re-scraped job that already has a row must refresh it, not re-insert it.

Regression for 2026-10-04: two runs 17 minutes apart under the 1-hour window both
returned the same Haystack listing. The first rejected it (HARD_FILTER_LAYER_3),
so ``existing_job_ids`` -- which skips only NOTIFIED jobs -- let it through again,
and ``session.add_all`` hit the all_jobs primary key and crashed the whole run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.main import _upsert_scraped
from src.state.models import AllJobs


def _job(job_id, **kw):
    return AllJobs(job_id=job_id, company="Acme", role="Data Engineer", site="linkedin", **kw)


def test_a_new_job_is_added():
    session = MagicMock()
    session.scalars.return_value = []
    new = _job("li-1", jd_text="fresh")

    out = _upsert_scraped(session, [new])

    session.add.assert_called_once_with(new)
    assert out == [new]


def test_an_existing_row_is_refreshed_in_place_not_reinserted():
    stored = _job("li-1", jd_text="old", required_skills=["Python"], role_level="mid",
                  applicants_count=None)
    session = MagicMock()
    session.scalars.return_value = [stored]
    rescraped = _job("li-1", jd_text="new", applicants_text="139 applicants",
                     applicants_count=139)

    out = _upsert_scraped(session, [rescraped])

    session.add.assert_not_called()
    assert out == [stored], "the run must work on the persistent row"
    assert stored.jd_text == "new"
    assert stored.applicants_count == 139
    # Parsed fields survive until this run's re-parse overwrites them.
    assert stored.required_skills == ["Python"]
    assert stored.role_level == "mid"


def test_a_blank_scraped_field_does_not_wipe_the_stored_one():
    stored = _job("li-1", location="Pune")
    session = MagicMock()
    session.scalars.return_value = [stored]

    _upsert_scraped(session, [_job("li-1", location=None)])

    assert stored.location == "Pune"
