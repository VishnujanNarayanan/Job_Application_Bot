"""Iteration 2 — Phase B Layer 2: filters, embedding math, rotation, and the
JobSpy DataFrame→AllJobs mapping.

Offline. Pure predicates and the cosine math need no DB. Exact same-id dedup
within one scrape call is covered here; near-duplicate cosine dedup was
removed (only exact job_id matches are deduped now). Rotation uses a
one-table in-memory SQLite (``search_rotation_state`` is plain Text,
SQLite-safe). JobSpy is faked via ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Filters — pure predicates (src/scraper/filters.py)
# ---------------------------------------------------------------------------


def test_location_disallowed_case_insensitive_substring() -> None:
    from src.scraper.filters import location_disallowed

    regions = ["Delhi", "Noida", "Gurugram"]
    assert location_disallowed("Sector 62, NOIDA, UP", regions) is True
    assert location_disallowed("Bengaluru, KA", regions) is False
    assert location_disallowed(None, regions) is False
    assert location_disallowed("", regions) is False


def test_exceeds_years_ceiling() -> None:
    from src.scraper.filters import exceeds_years_ceiling

    assert exceeds_years_ceiling(7, 5) is True
    assert exceeds_years_ceiling(5, 5) is False
    assert exceeds_years_ceiling(None, 5) is False  # unknown never rejects here


def test_company_in_cooldown() -> None:
    from src.scraper.filters import company_in_cooldown

    now = datetime(2026, 6, 4, tzinfo=timezone.utc)
    recent = datetime(2026, 5, 30, tzinfo=timezone.utc)   # 5 days ago
    old = datetime(2026, 5, 20, tzinfo=timezone.utc)      # 15 days ago
    assert company_in_cooldown(recent, now, 10) is True
    assert company_in_cooldown(old, now, 10) is False
    assert company_in_cooldown(None, now, 10) is False


# ---------------------------------------------------------------------------
# Embedding math — cosine is pure (src/scorer/embeddings.py)
# ---------------------------------------------------------------------------


def test_cosine_pure_math() -> None:
    from src.scorer.embeddings import cosine

    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # Zero/empty vectors degrade to 0.0 rather than dividing by zero.
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0


# ---------------------------------------------------------------------------
# Rotation — real one-table SQLite (search_rotation_state is Text-only).
# ---------------------------------------------------------------------------


@pytest.fixture()
def rotation_session():
    from src.state.models import SearchRotationState

    engine = create_engine("sqlite://")
    SearchRotationState.__table__.create(engine)
    with Session(engine) as s:
        yield s


def test_rotation_starts_at_zero_and_advances(rotation_session) -> None:
    from src.scraper import rotation

    terms = ["alpha", "beta", "gamma"]
    assert rotation.current_term(rotation_session, terms) == "alpha"
    assert rotation.advance(rotation_session, terms) == 1
    assert rotation.current_term(rotation_session, terms) == "beta"


def test_rotation_wraps_modulo(rotation_session) -> None:
    from src.scraper import rotation

    terms = ["alpha", "beta"]
    rotation.advance(rotation_session, terms)   # -> 1
    assert rotation.advance(rotation_session, terms) == 0  # wraps
    assert rotation.current_term(rotation_session, terms) == "alpha"


def test_rotation_empty_terms_raises(rotation_session) -> None:
    from src.scraper import rotation

    with pytest.raises(ValueError):
        rotation.current_term(rotation_session, [])


# ---------------------------------------------------------------------------
# JobSpy mapping — pure DataFrame-row → AllJobs (src/scraper/jobspy_wrapper.py)
# ---------------------------------------------------------------------------


def test_row_to_job_maps_fields() -> None:
    from src.scraper.jobspy_wrapper import _row_to_job

    job = _row_to_job(
        {
            "site": "linkedin",
            "id": "abc123",
            "company": "  Acme  ",
            "title": "Data Engineer",
            "location": "Bengaluru",
            "job_url": "https://x.invalid/j",
            "description": "Build pipelines.",
            "date_posted": date(2026, 6, 1),
            "job_type": "fulltime",
        }
    )
    assert job is not None
    assert job.job_id == "linkedin-abc123"
    assert job.company == "Acme"   # trimmed
    assert job.role == "Data Engineer"
    assert job.posted_at == datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_row_to_job_rejects_rows_without_company_or_title() -> None:
    from src.scraper.jobspy_wrapper import _row_to_job

    assert _row_to_job({"site": "indeed", "title": "DE"}) is None
    assert _row_to_job({"site": "indeed", "company": "Acme"}) is None


def test_row_to_job_strips_leading_punctuation_from_title() -> None:
    from src.scraper.jobspy_wrapper import _row_to_job

    job = _row_to_job(
        {"site": "indeed", "id": "x", "company": "Acme",
         "title": ": Data Engineer – Snowflake | Azure"}
    )
    assert job is not None
    assert job.role == "Data Engineer – Snowflake | Azure"  # only leading junk removed


def test_clean_title_edge_cases() -> None:
    from src.scraper.jobspy_wrapper import _clean_title

    assert _clean_title("- Senior SWE") == "Senior SWE"
    assert _clean_title("  •  Backend Engineer") == "Backend Engineer"
    assert _clean_title("Data Analyst") == "Data Analyst"   # already clean
    assert _clean_title("(Remote) Backend") == "(Remote) Backend"  # bracket kept
    assert _clean_title("  (Senior) SWE") == "(Senior) SWE"        # ws before bracket
    assert _clean_title(":::") is None                      # all punctuation → None
    assert _clean_title(None) is None


def test_row_to_job_handles_nan_and_url_fallback() -> None:
    from src.scraper.jobspy_wrapper import _row_to_job

    nan = float("nan")
    job = _row_to_job(
        {
            "site": "indeed",
            "id": nan,                       # missing id → url hash fallback
            "company": "Acme",
            "title": "DE",
            "location": nan,                 # NaN → None
            "job_url": "https://x.invalid/j",
            "date_posted": nan,
        }
    )
    assert job is not None
    assert job.location is None
    assert job.posted_at is None
    assert job.job_id.startswith("indeed-") and job.job_id != "indeed-nan"


def test_scrape_dedups_within_call(monkeypatch) -> None:
    """scrape() drops duplicate job_ids from one JobSpy call."""

    class _FakeDF:
        def __init__(self, records):
            self._records = records

        def to_dict(self, orient):  # noqa: ARG002 - mimic pandas signature
            return self._records

    def fake_scrape_jobs(**kwargs):
        return _FakeDF(
            [
                {"site": "indeed", "id": "1", "company": "A", "title": "DE"},
                {"site": "indeed", "id": "1", "company": "A", "title": "DE"},  # dup
                {"site": "indeed", "id": "2", "company": "B", "title": "ML"},
            ]
        )

    fake_module = types.ModuleType("jobspy")
    fake_module.scrape_jobs = fake_scrape_jobs
    monkeypatch.setitem(sys.modules, "jobspy", fake_module)

    from src.scraper.jobspy_wrapper import scrape

    jobs = scrape(
        "data engineer",
        sites=["indeed"],
        country="india",
        results_wanted=50,
        hours_old=2,
    )
    assert [j.job_id for j in jobs] == ["indeed-1", "indeed-2"]


class _FakeDF:
    def __init__(self, records):
        self._records = records

    def to_dict(self, orient):  # noqa: ARG002 - mimic pandas signature
        return self._records


def test_scrape_per_site_isolates_failures(monkeypatch) -> None:
    """A throttled site is retried then skipped; other sites still return."""
    monkeypatch.setattr("src.scraper.jobspy_wrapper.time.sleep", lambda *_: None)
    calls: list[list[str]] = []

    def fake_scrape_jobs(**kwargs):
        site = kwargs["site_name"]
        calls.append(site)
        if site == ["linkedin"]:
            raise RuntimeError("429 throttled")
        return _FakeDF([{"site": "indeed", "id": "1", "company": "A", "title": "DE"}])

    fake_module = types.ModuleType("jobspy")
    fake_module.scrape_jobs = fake_scrape_jobs
    monkeypatch.setitem(sys.modules, "jobspy", fake_module)

    from src.scraper.jobspy_wrapper import scrape

    jobs = scrape(
        "data engineer",
        sites=["indeed", "linkedin"],
        country="india",
        results_wanted=50,
        hours_old=2,
        per_site=True,
        max_retries=2,
    )
    # indeed succeeded once, linkedin retried twice then gave up — no crash.
    assert [j.job_id for j in jobs] == ["indeed-1"]
    assert calls.count(["linkedin"]) == 2


def test_scrape_linkedin_cap_and_fetch_flag(monkeypatch) -> None:
    """LinkedIn uses its own results cap + description fetch; others don't."""
    monkeypatch.setattr("src.scraper.jobspy_wrapper.time.sleep", lambda *_: None)
    seen: dict[str, dict] = {}

    def fake_scrape_jobs(**kwargs):
        seen[kwargs["site_name"][0]] = kwargs
        return _FakeDF([])

    fake_module = types.ModuleType("jobspy")
    fake_module.scrape_jobs = fake_scrape_jobs
    monkeypatch.setitem(sys.modules, "jobspy", fake_module)

    from src.scraper.jobspy_wrapper import scrape

    scrape(
        "data engineer",
        sites=["indeed", "linkedin"],
        country="india",
        results_wanted=50,
        hours_old=2,
        per_site=True,
        linkedin_fetch_description=True,
        linkedin_results_wanted=25,
    )
    assert seen["linkedin"]["results_wanted"] == 25
    assert seen["linkedin"]["linkedin_fetch_description"] is True
    assert seen["indeed"]["results_wanted"] == 50
