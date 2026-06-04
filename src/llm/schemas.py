"""Pydantic schemas for all Gemini call inputs and outputs.

Every LLM call returns a Pydantic model — Instructor enforces the schema
at the API boundary so malformed responses are physically impossible
(CLAUDE.md: "Why Instructor + Pydantic everywhere").

Iteration 1 ships only the minimum: ``JDParsed`` with the fields Layer 4
needs to make a scoring decision. Cover-letter / skills / form-question
schemas land in Iterations 2 and 3 alongside their respective Gemini
calls.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class JDParsed(BaseModel):
    """Structured output of Gemini Call 1a (JD parser).

    The contract Layer 4 (scoring) and Layer 8 (notification) consume.
    Every field here is used downstream: role_* drive scoring, salary_* +
    apply_url + location_type feed the notification, team_or_product seeds
    the cover letter. Fields with no downstream consumer are NOT added
    (CLAUDE.md: "Don't add JDParsed fields unused by Layer 4 or 5").
    """

    role_summary: str = Field(
        ..., max_length=500, description="2-3 sentence summary of the role"
    )
    role_category: str = Field(
        ..., description="One of the role_clusters keys in config.yaml"
    )
    role_level: Literal["junior", "mid", "senior", "lead"] = Field(
        ..., description="Seniority tier driving success_prob in Layer 4"
    )
    years_required: int = Field(
        ..., ge=0, le=30, description="Years of experience demanded by the JD"
    )
    required_skills: list[str] = Field(
        default_factory=list, description="Must-have skills extracted from JD"
    )
    nice_to_have: list[str] = Field(
        default_factory=list, description="Nice-to-have skills extracted from JD"
    )
    responsibilities: list[str] = Field(
        default_factory=list,
        description="Key responsibilities — informs bullet scoring in Layer 4",
    )

    # --- Iteration 2 additions (notification + informational salary) -------
    team_or_product: str | None = Field(
        default=None,
        max_length=200,
        description="Team / product the role sits on, if the JD names one",
    )
    job_type: Literal["fulltime", "contract", "internship", "parttime"] | None = (
        Field(default=None, description="Employment type if stated in the JD")
    )
    location_type: Literal["onsite", "remote", "hybrid"] | None = Field(
        default=None, description="Work arrangement if stated in the JD"
    )
    apply_url: str | None = Field(
        default=None,
        description="Direct application URL if present in the JD body (else null)",
    )
    salary_min_lpa: float | None = Field(
        default=None, ge=0, description="Lower salary bound in LPA, if stated"
    )
    salary_max_lpa: float | None = Field(
        default=None, ge=0, description="Upper salary bound in LPA, if stated"
    )
    salary_currency: str | None = Field(
        default=None, max_length=8, description="Salary currency code, if stated"
    )
