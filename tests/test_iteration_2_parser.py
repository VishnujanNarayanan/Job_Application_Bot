"""Iteration 2 — Phase B Layer 3: JD parser (Gemini Call 1a).

Offline. The Gemini transport is injected as a stub ``complete`` callable,
so no API key or network is needed. Skill grounding's fast path (substring)
is pure; one test exercises the spaCy lemma fallback (the model is present
in the venv).
"""

from __future__ import annotations

from src.llm.schemas import JDParsed
from src.parser import apply_to_row, grounded_skills, parse
from src.state.models import AllJobs


def _job(jd_text: str = "Build data pipelines with Python and SQL on AWS.") -> AllJobs:
    return AllJobs(
        job_id="j1",
        company="Acme",
        role="Data Engineer",
        site="indeed",
        location="Bengaluru",
        jd_text=jd_text,
    )


def _stub_complete(parsed: JDParsed):
    def _complete(response_model, prompt, *, system=None):  # noqa: ARG001
        assert response_model is JDParsed
        return parsed
    return _complete


# ---------------------------------------------------------------------------
# parse() — injected transport + grounding
# ---------------------------------------------------------------------------


def test_parse_returns_schema_and_grounds_skills() -> None:
    raw = JDParsed(
        role_summary="Build pipelines.",
        role_category="data",
        role_level="junior",
        years_required=2,
        required_skills=["Python", "SQL", "Rust"],   # Rust is not in the JD
        nice_to_have=["AWS", "Kubernetes"],          # Kubernetes is not in the JD
    )
    parsed = parse(_job(), complete=_stub_complete(raw))
    assert isinstance(parsed, JDParsed)
    assert parsed.required_skills == ["Python", "SQL"]   # Rust dropped
    assert parsed.nice_to_have == ["AWS"]                # Kubernetes dropped


def test_parse_passes_role_categories_into_prompt() -> None:
    seen = {}

    def _complete(response_model, prompt, *, system=None):
        seen["prompt"] = prompt
        seen["system"] = system
        return JDParsed(
            role_summary="x", role_category="data", role_level="mid",
            years_required=1,
        )

    parse(_job(), complete=_complete)
    # Config cluster keys are offered to the model.
    assert "data" in seen["prompt"] and "backend" in seen["prompt"]
    assert "never infer" in seen["system"]


# ---------------------------------------------------------------------------
# apply_to_row() — new Iteration-2 fields copied through
# ---------------------------------------------------------------------------


def test_apply_to_row_copies_new_fields() -> None:
    job = _job()
    parsed = JDParsed(
        role_summary="s",
        role_category="data",
        role_level="mid",
        years_required=3,
        required_skills=["Python"],
        nice_to_have=["AWS"],
        responsibilities=["Build pipelines"],
        team_or_product="Data Platform",
        job_type="fulltime",
        location_type="hybrid",
        salary_min_lpa=8.0,
        salary_max_lpa=12.0,
        salary_currency="INR",
    )
    apply_to_row(job, parsed)
    assert job.role_category == "data"
    assert job.team_or_product == "Data Platform"
    assert job.job_type == "fulltime"
    assert job.location_type == "hybrid"
    assert job.salary_min_lpa == 8.0
    assert job.salary_max_lpa == 12.0
    assert job.salary_currency == "INR"


# ---------------------------------------------------------------------------
# grounded_skills() — substring fast path, dedup, lemma fallback
# ---------------------------------------------------------------------------


def test_grounded_skills_substring_and_dedup() -> None:
    jd = "We use Python, SQL and Apache Airflow daily."
    out = grounded_skills(["Python", "python", "SQL", "Go"], jd)
    assert out == ["Python", "SQL"]   # case-insensitive dedup; Go not present


def test_grounded_skills_empty_jd() -> None:
    assert grounded_skills(["Python"], "") == []


def test_grounded_skills_lemma_fallback() -> None:
    # "pipelines" appears inflected; the skill "pipeline" grounds via lemma.
    jd = "You will own data pipelines end to end."
    out = grounded_skills(["pipeline"], jd)
    assert out == ["pipeline"]
