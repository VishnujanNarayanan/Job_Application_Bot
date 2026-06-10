"""Iteration 2 — Phase B Layer 2: filters, embedding math, dedup, rotation,
and the JobSpy DataFrame→AllJobs mapping.

Offline. Pure predicates and the cosine math need no DB. ``resolve_batch``
is exercised with a fake session that records add/flush order (proving the
self-FK insert discipline) so no Postgres/pgvector is required. Rotation
uses a one-table in-memory SQLite (``search_rotation_state`` is plain
Text, SQLite-safe). JobSpy is faked via ``sys.modules``.
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.reasons import NEAR_DUPLICATE


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
# Dedup — find_near_duplicate is pure; resolve_batch's insert order is the
# point of the self-FK fix.
# ---------------------------------------------------------------------------


def test_find_near_duplicate_strict_threshold() -> None:
    from src.scraper.dedup import find_near_duplicate

    cand = [("a", [1.0, 0.0]), ("b", [0.0, 1.0])]
    # Identical → cosine 1.0 > 0.95.
    assert find_near_duplicate([1.0, 0.0], cand, 0.95) == ("a", pytest.approx(1.0))
    # 45° from both → cosine ~0.707, below threshold → no match.
    assert find_near_duplicate([1.0, 1.0], cand, 0.95) is None


def test_find_near_duplicate_picks_closest() -> None:
    from src.scraper.dedup import find_near_duplicate

    cand = [("a", [1.0, 0.0]), ("b", [0.99, 0.14])]
    match = find_near_duplicate([1.0, 0.01], cand, 0.95)
    assert match is not None and match[0] == "a"  # closest, not merely first


class _FakeSession:
    """Records add/flush ordering so we can assert FK target was flushed."""

    def __init__(self) -> None:
        self.flushed: set[str] = set()
        self._pending: list[object] = []
        # job_id -> bool: at the moment a duplicate was added, was its
        # near_duplicate_of target already flushed?
        self.dup_saw_original_flushed: dict[str, bool] = {}

    def add_all(self, objs) -> None:
        self._pending.extend(objs)

    def add(self, obj) -> None:
        self._pending.append(obj)
        ndo = getattr(obj, "near_duplicate_of", None)
        if ndo is not None:
            self.dup_saw_original_flushed[obj.job_id] = ndo in self.flushed

    def flush(self) -> None:
        for o in self._pending:
            self.flushed.add(o.job_id)
        self._pending.clear()


def test_resolve_batch_same_run_repost_links_after_flush(monkeypatch) -> None:
    """X then Y (a cross-portal repost of X) in one run: Y links to X, and
    X is flushed before Y's FK is written (the self-FK insert-order fix)."""
    from src.scraper import dedup
    from src.state.models import AllJobs

    # No prior DB candidates — both jobs are new this run.
    monkeypatch.setattr(dedup, "canonical_embeddings", lambda *a, **k: [])

    x = AllJobs(job_id="indeed-1", company="Acme", role="DE", site="indeed")
    x.jd_embedding = [1.0, 0.0]
    y = AllJobs(job_id="linkedin-9", company="Acme", role="DE", site="linkedin")
    y.jd_embedding = [1.0, 0.0]  # identical → cosine 1.0

    session = _FakeSession()
    outcomes = dedup.resolve_batch(session, [x, y], threshold=0.95, lookback=500)

    by_id = {o.job.job_id: o for o in outcomes}
    assert by_id["indeed-1"].is_duplicate is False
    assert by_id["linkedin-9"].is_duplicate is True
    assert y.near_duplicate_of == "indeed-1"
    assert y.outcome == NEAR_DUPLICATE
    # The crux: the FK target existed (was flushed) before Y was linked.
    assert session.dup_saw_original_flushed["linkedin-9"] is True


def test_resolve_batch_distinct_jobs_all_originals(monkeypatch) -> None:
    from src.scraper import dedup
    from src.state.models import AllJobs

    monkeypatch.setattr(dedup, "canonical_embeddings", lambda *a, **k: [])
    a = AllJobs(job_id="indeed-1", company="A", role="DE", site="indeed")
    a.jd_embedding = [1.0, 0.0]
    b = AllJobs(job_id="indeed-2", company="B", role="ML", site="indeed")
    b.jd_embedding = [0.0, 1.0]  # orthogonal → not a duplicate

    outcomes = dedup.resolve_batch(_FakeSession(), [a, b], threshold=0.95, lookback=500)
    assert all(o.is_duplicate is False for o in outcomes)
    assert a.near_duplicate_of is None and b.near_duplicate_of is None


def test_resolve_batch_no_embedding_treated_as_original(monkeypatch) -> None:
    from src.scraper import dedup
    from src.state.models import AllJobs

    monkeypatch.setattr(dedup, "canonical_embeddings", lambda *a, **k: [])
    j = AllJobs(job_id="indeed-1", company="A", role="DE", site="indeed")
    # jd_embedding stays None → can't dedup.
    (outcome,) = dedup.resolve_batch(_FakeSession(), [j], threshold=0.95, lookback=500)
    assert outcome.is_duplicate is False


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
