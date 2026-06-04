"""LLM — prompt builders for Gemini Calls 1a (parse) and 1b (build).

Call 1b (title + skills + cover letter) is assembled in Layer 5; this
module currently holds Call 1a (JD parse). Prompts are plain functions so
they unit-test without the API: feed a job, assert the rendered text.
"""

from __future__ import annotations

from collections.abc import Sequence

from src.state.models import AllJobs

_PARSE_SYSTEM = (
    "You extract structured data from a job description. Use ONLY information "
    "present in the description — never infer, embellish, or invent skills, "
    "salaries, or URLs. When a field is not stated, return null (or an empty "
    "list). Skills must be copied as they appear in the text."
)


def jd_parse_system() -> str:
    """System instruction for Call 1a (grounding / anti-fabrication)."""
    return _PARSE_SYSTEM


def jd_parse_prompt(job: AllJobs, role_categories: Sequence[str]) -> str:
    """Render the Call 1a user prompt for one scraped job.

    ``role_categories`` is the set of allowed ``role_category`` values
    (the config ``parser.role_clusters`` keys); the model must pick the
    closest one.
    """
    categories = ", ".join(role_categories)
    return (
        f"Job title: {job.role}\n"
        f"Company: {job.company}\n"
        f"Location (from listing): {job.location or 'unspecified'}\n\n"
        "Job description:\n"
        f"{job.jd_text or '(no description text was scraped)'}\n\n"
        "Extract the structured fields. For role_category choose the single "
        f"closest of: {categories}. For role_level pick junior, mid, senior, "
        "or lead. For years_required give the minimum years demanded (0 if "
        "none stated). Salary only if explicitly stated; convert annual "
        "figures to LPA (lakhs per annum). apply_url only if a literal URL "
        "appears in the description."
    )
