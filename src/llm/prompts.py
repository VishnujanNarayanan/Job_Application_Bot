"""LLM — prompt builders for Gemini Calls 1a (parse) and 1b (build).

Both are plain functions so they unit-test without the API: feed inputs,
assert rendered text. Call 1a runs for every passing job; Call 1b only for
matched jobs (final_score >= 0.50).
"""

from __future__ import annotations

from src.config import settings
from src.scorer.apply_decision import SelectionResult
from src.state.models import AllJobs

# Marker standing in for the removed middle, so the model is told text is
# missing rather than being handed a sentence that stops mid-clause.
_ELISION = "\n[... middle of the description omitted ...]\n"


def clip_jd_text(text: str, head: int | None = None, tail: int | None = None) -> str:
    """Bound one JD's length, keeping BOTH ends.

    Head-only truncation was measured against 667 real listings and rejected:
    pay is stated in only a third of them, and when it is, the first mention
    sits a median 77% of the way in — a head-only cut at 1500 chars would have
    destroyed the salary signal in 72% of the listings that carry one. The end
    of a JD holds compensation and application details; the start holds the
    role and its requirements. Both matter, so both are kept.

    The point is to bound the OUTLIERS. JD length runs from a 4,172-char
    median to 11,815, and it is the long tail that spikes per-minute token
    use. Clipping aggressively enough to shave every job measurably degraded
    years_required and role_level, which together drive success_prob — a
    worse trade than the tokens are worth.
    """
    cfg = settings.llm.get("jd_text") or {}
    head = int(cfg.get("head_chars", 3000)) if head is None else head
    tail = int(cfg.get("tail_chars", 1500)) if tail is None else tail

    if not text or len(text) <= head + tail:
        return text
    # `text[-0:]` is the WHOLE string, not the empty one — guard tail == 0.
    return text[:head] + _ELISION + (text[-tail:] if tail else "")

_PARSE_SYSTEM = (
    "You extract structured data from a job description. Use ONLY information "
    "present in the description — never infer, embellish, or invent skills, "
    "salaries, or URLs. When a field is not stated, return null (or an empty "
    "list). Skills must be copied as they appear in the text."
)


def jd_parse_system() -> str:
    """System instruction for Call 1a (grounding / anti-fabrication)."""
    return _PARSE_SYSTEM


def jd_parse_prompt(job: AllJobs) -> str:
    """Render the Call 1a user prompt for one scraped job."""
    return (
        f"Job title: {job.role}\n"
        f"Company: {job.company}\n"
        f"Location (from listing): {job.location or 'unspecified'}\n\n"
        "Job description:\n"
        f"{clip_jd_text(job.jd_text) or '(no description text was scraped)'}\n\n"
        "Extract the structured fields. For role_category give a short slug "
        "classifying the role type (e.g. backend, data, ml, fullstack, devops, "
        "quant). For role_level pick junior, mid, senior, or lead. For "
        "years_required give the minimum years demanded (0 if none stated). "
        "Salary only if explicitly stated; convert annual figures to LPA "
        "(lakhs per annum). apply_url only if a literal URL appears in the "
        "description."
    )


# ---------------------------------------------------------------------------
# Call 1b — title alias + skills + cover letter (Layer 5, matched jobs only)
# ---------------------------------------------------------------------------

_BUILD_SYSTEM = (
    "You are helping tailor a resume for a specific job. "
    "You MUST only use information and options explicitly given to you — "
    "never invent, infer, or add skills, titles, or facts not provided. "
    "Return valid JSON matching the requested schema exactly."
)

# build_system() / build_prompt() were removed with Call 1b: title alias and
# skill grouping are now deterministic (src/builder/deterministic.py), so there
# is no second prompt. Only the JD parse (Call 1a) still prompts a model.
