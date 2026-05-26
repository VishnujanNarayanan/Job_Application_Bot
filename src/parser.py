"""Layer 3 — JD parser. Iteration 1: stub returning a fixed JDParsed.

The real implementation in Iteration 2 wraps Gemini Call 1a with
Instructor + a spaCy hallucination check. For Iter 1 we return the same
plausible-looking parse for every job so Layer 4 has something to score.

Contract: ``parse(job: AllJobs) -> JDParsed``. Caller is responsible for
writing the structured fields back to the ``all_jobs`` row.
"""

from __future__ import annotations

from src.llm.schemas import JDParsed
from src.state.models import AllJobs


def parse(job: AllJobs) -> JDParsed:
    """Return a fixed JDParsed instance. Same shape every call."""
    return JDParsed(
        role_summary=f"Stub parse of {job.role} at {job.company}.",
        role_category="data_engineering",
        role_level="junior",
        years_required=2,
        required_skills=["Python", "SQL"],
        nice_to_have=["Airflow", "AWS"],
        responsibilities=["Build pipelines", "Maintain dashboards"],
    )


def apply_to_row(job: AllJobs, parsed: JDParsed) -> None:
    """Copy parsed fields onto the AllJobs row in-place.

    Layer 1 (orchestrator) calls this inside the session so SQLAlchemy
    flushes the changes on commit. No I/O here.
    """
    job.role_summary = parsed.role_summary
    job.role_category = parsed.role_category
    job.role_level = parsed.role_level
    job.years_required = parsed.years_required
    job.required_skills = parsed.required_skills
    job.nice_to_have = parsed.nice_to_have
    job.responsibilities = parsed.responsibilities
