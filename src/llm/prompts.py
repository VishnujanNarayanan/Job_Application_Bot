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


def clip_jd_text(
    text: str,
    head: int | None = None,
    tail: int | None = None,
    provider_cfg=None,
) -> str:
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
    # A provider may override the global bound. The limit exists to survive a
    # hosted token budget; a local model has none, so it should read the whole
    # ad — pay is first mentioned a median 77% of the way in. The override has
    # to be per-provider because the chain can fall back from a local model to
    # a metered one, and that one still needs the text bounded.
    cfg = (provider_cfg.get("jd_text") if provider_cfg is not None else None) or (
        settings.llm.get("jd_text_default") or {}
    )
    head = int(cfg.get("head_chars", 3000)) if head is None else head
    tail = int(cfg.get("tail_chars", 1500)) if tail is None else tail

    if not text or len(text) <= head + tail:
        return text
    # `text[-0:]` is the WHOLE string, not the empty one — guard tail == 0.
    return text[:head] + _ELISION + (text[-tail:] if tail else "")

# "When a field is not stated, return null (or an empty list)" used to end this
# instruction. On a 7B model under constrained decoding that read as blanket
# permission: measured 2026-08-09, qwen2.5:7b OMITTED required_skills,
# nice_to_have and responsibilities from the JSON entirely on 71% of real JDs
# (17/24), independent of description length. The escape hatch is still here —
# a JD that names no skills must be able to say so — but it is now scoped to
# "genuinely names nothing" rather than offered as the easy default.
_PARSE_SYSTEM = (
    "You extract structured data from a job description. Use ONLY information "
    "present in the description — never infer, embellish, or invent skills, "
    "salaries, or URLs. Skills must be copied as they appear in the text. "
    "Return EVERY field of the schema: use null for a scalar the description "
    "does not state, and an empty list only when the description genuinely "
    "names nothing for that list."
)


def jd_parse_system() -> str:
    """System instruction for Call 1a (grounding / anti-fabrication)."""
    return _PARSE_SYSTEM


def jd_parse_prompt(job: AllJobs, provider_cfg=None) -> str:
    """Render the Call 1a user prompt for one scraped job.

    ``provider_cfg`` lets the description bound follow whichever provider is
    about to be called — a local model reads the whole ad, a metered one gets
    it clipped.
    """
    return (
        f"Job title: {job.role}\n"
        f"Company: {job.company}\n"
        f"Location (from listing): {job.location or 'unspecified'}\n\n"
        "Job description:\n"
        f"{clip_jd_text(job.jd_text, provider_cfg=provider_cfg) or '(no description text was scraped)'}\n\n"
        "Extract the structured fields. For role_category give a short slug "
        "classifying the role type (e.g. backend, data, ml, fullstack, devops, "
        "quant). For role_level pick junior, mid, senior, or lead. For "
        "years_required give the minimum years demanded (0 if none stated). "
        "Salary only if explicitly stated; convert annual figures to LPA "
        "(lakhs per annum). apply_url only if a literal URL appears in the "
        "description.\n\n"
        # These three had NO instruction here at all, while every scalar did —
        # and they are the ones that feed scoring (required_skills and
        # nice_to_have become the JD skill vectors; responsibilities is half of
        # the bullet-matching vector). A 7B model handed an optional schema slot
        # and no instruction simply skipped them. Naming them explicitly, and
        # saying what SHAPE they take, is half the fix; the wire-schema
        # constraint in the Ollama path is the other half.
        # Exhaustive on purpose. Each extracted skill becomes its own query
        # vector, and a pool skill is scored against the BEST individual JD
        # skill — so an extra skill can only ever add a chance to match, never
        # dilute an existing one. Under-extraction silently costs matches;
        # over-extraction costs nothing, and `grounded_skills` drops anything
        # not actually in the text.
        "For required_skills, list EVERY must-have technology, tool, language, "
        "framework, platform, database, cloud service, methodology or technique "
        "named anywhere in the description — be exhaustive rather than "
        "selective, and do not stop at the first few or summarise. Copy each "
        "verbatim as a SHORT term (e.g. \"Python\", \"AWS\", \"Kubernetes\") — "
        "never whole sentences. For nice_to_have do the same, just as "
        "exhaustively, for items the description marks as preferred, desirable, "
        "a bonus, or a plus. For responsibilities list every distinct duty as a "
        "short phrase. Do not omit these three lists."
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
