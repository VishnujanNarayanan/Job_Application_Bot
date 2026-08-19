"""Pydantic schemas for all Gemini call inputs and outputs.

Every LLM call returns a Pydantic model — Instructor enforces the schema
at the API boundary so malformed responses are physically impossible.

Call 1a: ``JDParsed`` (Layer 3 — always run).
Call 1b removed: Layer 5 selection is deterministic.
``StoredSelection`` is the ~2 KB blob written to ``applied.selection_json``
and consumed by the endpoint assembler at render time.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

# Bound on one extracted skill, enforced by the JSON schema rather than asked
# for in the prompt. Ollama compiles the schema into a decoding grammar, so a
# sentence is *unrepresentable* in a skill slot — the model cannot disobey it
# the way it ignored "never whole sentences".
#
# Measured 2026-08-19 on a JD where the unbounded schema returned 11 whole
# bullet lines and none of the 18 technologies the ad names: with the bound,
# 26 entries, none over four words, 17 of the 18 found.
#
# 30, not 20: real skills reach into the twenties ("AI orchestration
# frameworks", "distributed data processing"), and a tighter bound truncates
# legitimate names rather than only catching prose.
_MAX_SKILL_CHARS = 30

SkillTerm = Annotated[str, StringConstraints(max_length=_MAX_SKILL_CHARS)]

# Lead-ins the model wraps around a technology name. Stripped before the length
# test so "Experience with Docker" is recognised as "Docker" rather than
# discarded for length.
_SKILL_LEAD_IN = re.compile(
    r"^(?:(?:strong|solid|good|proven|deep|basic|prior|hands[- ]?on|extensive)\s+)*"
    r"(?:experience\s+(?:with|in|building|using)|familiarity\s+with|knowledge\s+of|"
    r"exposure\s+to|proficiency\s+(?:with|in)|working\s+knowledge\s+of|"
    r"understanding\s+of|expertise\s+in)\s+",
    re.IGNORECASE,
)
_SKILL_TAIL = re.compile(r"\s+(?:experience|proficiency|skills?|expertise|practices?)$",
                         re.IGNORECASE)
# Split an over-long entry into the terms it names. NOT on "/" — that would
# break CI/CD, ECS/EKS and TCP/IP, which are single technologies.
_SKILL_SPLIT = re.compile(r",|\band\b|\bor\b|\bsuch as\b|\bincluding\b", re.IGNORECASE)


def _clean(text: str) -> str:
    """Strip the prose the model wraps around a technology name."""
    return _SKILL_TAIL.sub("", _SKILL_LEAD_IN.sub("", text.strip()).strip(" .;:")).strip()


def _as_terms(raw: str) -> list[str]:
    """Reduce one returned string to the short terms it actually names.

    Splitting is unconditional rather than only for over-long entries: "CI/CD
    and ECS/EKS" fits the length bound comfortably but is still two skills, and
    each surviving term becomes its own embedding query vector, so a conjunction
    left intact matches nothing well.
    """
    text = _clean(raw)
    if not text:
        return []
    terms = [t for t in (_clean(p) for p in _SKILL_SPLIT.split(text))
             if t and len(t) <= _MAX_SKILL_CHARS]
    # A single term that survives splitting intact is the common case; falling
    # back to the whole string keeps a hyphenated or comma-free name that the
    # splitter had nothing to do with.
    return terms or ([text] if len(text) <= _MAX_SKILL_CHARS else [])


class JDParsed(BaseModel):
    """Structured output of Gemini Call 1a (JD parser).

    The contract Layer 4 (scoring) and Layer 8 (notification) consume.
    Every field here is used downstream: role_* drive scoring, salary_* +
    apply_url + location_type feed the notification, team_or_product seeds
    the cover letter. Fields with no downstream consumer are NOT added
    (CLAUDE.md: "Don't add JDParsed fields unused by Layer 4 or 5").
    """

    # 1500, not 500: providers that validate tool calls server-side (Groq)
    # REJECT the whole call when a bound is exceeded, and the retry costs a
    # full call's tokens. A 632-character summary did that twice on
    # 2026-08-08 and the wasted tokens are what breached the per-minute
    # limit. The cap is a sanity bound, not a budget — nothing downstream
    # cares about the exact length, so it should be loose enough never to be
    # the reason a parse fails.
    role_summary: str = Field(
        ..., max_length=1500, description="2-3 sentence summary of the role"
    )
    role_category: str = Field(
        ..., description="Short slug classifying the role type (e.g. backend, data, ml, fullstack, devops, quant)"
    )
    role_level: Literal["junior", "mid", "senior", "lead"] = Field(
        ..., description="Seniority tier driving success_prob in Layer 4"
    )
    years_required: int = Field(
        ..., ge=0, le=30, description="Years of experience demanded by the JD"
    )
    required_skills: list[SkillTerm] = Field(
        default_factory=list, description="Must-have skills extracted from JD"
    )
    nice_to_have: list[SkillTerm] = Field(
        default_factory=list, description="Nice-to-have skills extracted from JD"
    )
    responsibilities: list[str] = Field(
        default_factory=list,
        description="Key responsibilities — informs bullet scoring in Layer 4",
    )

    @field_validator("role_level", mode="before")
    @classmethod
    def _null_level_means_unknown(cls, value):
        """Read an explicit null role_level as the unknown-seniority default.

        A JD that never states a level is ordinary, and the model says so with
        null — but ``Literal`` rejects it, and a provider that validates tool
        calls server-side throws the WHOLE call away for it. Observed on the
        live run of 2026-08-09: Groq returned
        ``[`/role_level`: expected string, but got null]`` and the retry cost a
        full call against a token-per-day cap that the same run went on to
        exhaust.

        "mid" is not a guess dressed as data — it is the same value
        ``scoring.success_prob.seniority_scores`` already assigns to the
        ``null`` key (0.80), so the score is identical to treating it as
        unknown. This only makes that agreement survive the type system.
        """
        return "mid" if value is None else value

    @field_validator("required_skills", "nice_to_have", mode="before")
    @classmethod
    def _terms_only(cls, value):
        """Make the length bound true instead of fatal.

        `SkillTerm` caps a skill at 30 characters, and Instructor states that
        cap in the prompt — but measured 2026-08-19, Ollama does NOT enforce
        `maxLength` in its decoding grammar: on the full schema the model
        returned over-long strings and the whole parse died with a validation
        error. The bound is therefore a hint the model may ignore, and this
        runs first (`mode="before"`) to make it hold.

        Over-long entries are SPLIT, never dropped. A blob like "Experience
        with Docker, Kubernetes, CI/CD pipelines, and MLOps practices" names
        four real technologies; discarding it for length throws all four away,
        which is exactly the failure an earlier length filter caused.
        """
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            for term in _as_terms(item):
                key = term.casefold()
                if key not in seen:
                    seen.add(key)
                    out.append(term)
        return out

    @field_validator("responsibilities", mode="before")
    @classmethod
    def _null_list_means_empty(cls, value):
        """Accept an explicit null for a list field and read it as "none".

        A default only applies when a key is ABSENT; models routinely emit the
        key with null instead, which is the same statement. Groq validates
        tool calls server-side and rejected the whole call for it — one wasted
        call per occurrence, against a per-minute token limit.
        """
        return [] if value is None else value

    # --- Iteration 2 additions (notification + informational salary) -------
    team_or_product: str | None = Field(
        default=None,
        max_length=200,
        description="Team / product the role sits on, if the JD names one",
    )
    # These two are worded to hold each other apart. A 7B local model kept
    # answering "hybrid" for job_type — a location answer to an employment
    # question — and each rejection cost a full retry (9s+ locally), turning a
    # 13s parse into 83s. Naming the other field in each description is what
    # stopped it.
    job_type: Literal["fulltime", "contract", "internship", "parttime"] | None = (
        Field(
            default=None,
            description=(
                "CONTRACT type — how the person is employed. Exactly one of "
                "fulltime, contract, internship, parttime. This is NOT about "
                "where the work happens: remote/hybrid/onsite belong in "
                "location_type. Null if the JD does not say."
            ),
        )
    )
    location_type: Literal["onsite", "remote", "hybrid"] | None = Field(
        default=None,
        description=(
            "WHERE the work happens. Exactly one of onsite, remote, hybrid. "
            "This is NOT the employment type: fulltime/contract/internship/"
            "parttime belong in job_type. Null if the JD does not say."
        ),
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


# ---------------------------------------------------------------------------
# Gemini Call 1b — title alias + skills selection + cover letter (Layer 5)
# ---------------------------------------------------------------------------


class SkillCategory(BaseModel):
    """One named skills grouping, filled from Layer 4's pool candidates."""

    name: str = Field(
        ...,
        max_length=60,
        description="Category label, from selection.skills.category_names.",
    )
    skills: list[str] = Field(
        ...,
        min_length=1,
        max_length=5,
        description="Skills drawn from Layer 4's top-N pool candidates.",
    )


class StoredSkills(BaseModel):
    """Skills section of a resume selection."""

    # The bounds were "exactly 3" when this was the LLM's output contract and
    # the prompt asked for three. Grouping is now computed, so the count is a
    # RESULT: with a full candidate list all configured categories fill, but a
    # sparse pool legitimately yields fewer, and an empty category is dropped
    # rather than padded with filler.
    categories: list[SkillCategory] = Field(
        ...,
        max_length=6,
        description="Non-empty skill categories, ordered by JD relevance.",
    )
    familiar_with: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="0-4 gap skills the JD wants but that are not in the pool.",
    )


# ResumeBuildLLMOutput was removed with Call 1b. Title alias and skill
# grouping are now deterministic (src/builder/deterministic.py), so no model
# returns them and there is nothing to validate at the API boundary.

class SelectedExpEntry(BaseModel):
    """One selected work-experience entry."""

    exp_id: str
    title_alias: str
    bullet_ids: list[str] = Field(..., description="3 ordered bullet IDs.")


class SelectedProjEntry(BaseModel):
    """One selected project entry."""

    proj_id: str
    link: str = Field(..., description="Project URL used for the 'Code →' hyperlink.")
    bullet_ids: list[str] = Field(
        ..., description="2-3 ordered bullet IDs, descending by score."
    )


class StoredSelection(BaseModel):
    """The durable per-job artifact written to applied.selection_json.

    The endpoint assembler reads this to render the resume on demand.
    Bullet texts are looked up from master_bullets by id; profile
    structure (company, dates, project names) from master_profile.json.
    """

    job_id: str
    summary_id: str
    experiences: list[SelectedExpEntry]
    projects: list[SelectedProjEntry]
    skills: StoredSkills
    section_order: list[str] = Field(
        ...,
        description="Variable section names in display order. "
        "E.g. ['Work', 'Skills', 'Projects'] or ['Work', 'Projects', 'Skills'].",
    )
    cover_letter_text: str
    template_version: str = Field(
        ..., description="MD5[:8] of the template file at build time."
    )
    built_at: str = Field(..., description="ISO-8601 timestamp.")


class MonthlyReportLLM(BaseModel):
    """Layer 9 — Gemini synthesis of the monthly analytics report.

    A SINGLE monthly Gemini call (not per-job; the 2-call budget is per
    job). Input is aggregated stats over the last 30 days; output is prose
    written verbatim into the Google Doc. No fabrication: the model only
    narrates the numbers it is given.
    """

    headline: str = Field(
        ..., description="One-sentence summary of the month's job market for the operator."
    )
    skill_demand: str = Field(
        ..., description="Prose on the most in-demand skills this month and trends."
    )
    recurring_gaps: str = Field(
        ...,
        description="Prose on skills frequently required but missing from the "
        "operator's pool (the 'consider learning' signal).",
    )
    hiring_companies: str = Field(
        ..., description="Prose on which companies are hiring and for what."
    )
    salary_observations: str = Field(
        ..., description="Prose on observed salary ranges across matched/scraped jobs."
    )
