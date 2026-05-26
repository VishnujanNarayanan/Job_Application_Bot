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

    Iteration 1 stub returns a hardcoded instance. Iteration 2 replaces
    the stub with a real Gemini call; this schema is the contract both
    sides honour.
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
