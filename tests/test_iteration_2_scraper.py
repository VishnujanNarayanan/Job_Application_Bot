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
    # The live ceiling: only an EXPLICIT requirement above 6 rejects; an
    # unstated one parses to 0 and passes.
    assert exceeds_years_ceiling(6, 6) is False
    assert exceeds_years_ceiling(7, 6) is True
    assert exceeds_years_ceiling(0, 6) is False


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


def test_scrape_remote_pass(monkeypatch) -> None:
    """remote_results_wanted adds one LinkedIn call with the Remote filter on,
    its own cap and window; rows it shares with the plain search are dropped."""
    monkeypatch.setattr("src.scraper.jobspy_wrapper.time.sleep", lambda *_: None)
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        ids = ["2", "3"] if kwargs.get("is_remote") else ["1", "2"]
        return _FakeDF([{"site": "linkedin", "id": i, "company": "A", "title": "DE"}
                        for i in ids])

    fake_module = types.ModuleType("jobspy")
    fake_module.scrape_jobs = fake_scrape_jobs
    monkeypatch.setitem(sys.modules, "jobspy", fake_module)

    from src.scraper.jobspy_wrapper import scrape

    jobs = scrape(
        "data engineer",
        sites=["linkedin"],
        country="india",
        results_wanted=50,
        hours_old=1,
        linkedin_results_wanted=25,
        remote_results_wanted=15,
        remote_hours_old=24,
    )
    assert [j.job_id for j in jobs] == ["linkedin-1", "linkedin-2", "linkedin-3"]
    plain, remote = calls
    assert "is_remote" not in plain and plain["hours_old"] == 1
    assert remote["is_remote"] is True
    assert remote["results_wanted"] == 15 and remote["hours_old"] == 24


def test_scrape_remote_pass_off_by_default(monkeypatch) -> None:
    """No remote_results_wanted, no extra call."""
    calls: list[dict] = []
    fake_module = types.ModuleType("jobspy")
    fake_module.scrape_jobs = lambda **kw: calls.append(kw) or _FakeDF([])
    monkeypatch.setitem(sys.modules, "jobspy", fake_module)

    from src.scraper.jobspy_wrapper import scrape

    scrape("data engineer", sites=["linkedin"], country="india",
           results_wanted=50, hours_old=1)
    assert len(calls) == 1 and "is_remote" not in calls[0]


# ---------------------------------------------------------------------------
# LinkedIn applicant count (issue #13)
# ---------------------------------------------------------------------------

#: Trimmed from the live public page of linkedin.com/jobs/view/4473167100
#: (2026-10-04): the top-card figure JobSpy fetches and then discards.
_TOPCARD = """
        <figure class="num-applicants__figure topcard__flavor--metadata topcard__flavor--bullet">
          <span class="num-applicants__icon num-applicants__icon--notify-pebble lazy-load"></span>
          <figcaption class="num-applicants__caption">
            Over 200 applicants
          </figcaption>
        </figure>
"""


@pytest.mark.parametrize("caption,expected", [
    ("Be among the first 25 applicants", 24),   # FEWER than 25
    ("139 applicants", 139),
    ("1,024 applicants", 1024),
    ("Over 200 applicants", 201),               # MORE than 200
    ("No longer accepting applications", None),
    (None, None),
])
def test_parse_applicants(caption, expected) -> None:
    from src.scraper.jobspy_wrapper import parse_applicants

    assert parse_applicants(caption) == expected


def test_applicant_caption_is_read_from_the_live_page_markup() -> None:
    from src.scraper.jobspy_wrapper import applicant_caption

    assert applicant_caption(_TOPCARD) == "Over 200 applicants"
    assert applicant_caption("<html>no top card</html>") is None


def test_captured_captions_land_on_their_own_rows(monkeypatch) -> None:
    """A caption captured during the scrape is merged by JobSpy row id."""
    from src.scraper import jobspy_wrapper as jw

    monkeypatch.setattr(jw.time, "sleep", lambda *_: None)
    monkeypatch.setattr(jw, "install_applicant_capture", lambda: None)
    jw._APPLICANTS.clear()
    jw._APPLICANTS["li-111"] = "139 applicants"

    fake_module = types.ModuleType("jobspy")
    fake_module.scrape_jobs = lambda **kw: _FakeDF([
        {"site": "linkedin", "id": "li-111", "company": "A", "title": "SDE"},
        {"site": "linkedin", "id": "li-222", "company": "B", "title": "SDE"},
    ])
    monkeypatch.setitem(sys.modules, "jobspy", fake_module)

    jobs = {j.job_id: j for j in jw.scrape(
        "sde", sites=["linkedin"], country="india", results_wanted=5, hours_old=1,
        linkedin_fetch_description=True,
    )}
    assert jobs["linkedin-li-111"].applicants_count == 139
    assert jobs["linkedin-li-111"].applicants_text == "139 applicants"
    assert jobs["linkedin-li-222"].applicants_count is None
    assert jw._APPLICANTS == {}, "captions must not leak into the next scrape"
