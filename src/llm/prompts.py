"""LLM — prompt builders for Gemini Calls 1a (parse) and 1b (build).

Both are plain functions so they unit-test without the API: feed inputs,
assert rendered text. Call 1a runs for every passing job; Call 1b only for
matched jobs (final_score >= 0.50).
"""

from __future__ import annotations

from src.scorer.apply_decision import SelectionResult
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


def jd_parse_prompt(job: AllJobs) -> str:
    """Render the Call 1a user prompt for one scraped job."""
    return (
        f"Job title: {job.role}\n"
        f"Company: {job.company}\n"
        f"Location (from listing): {job.location or 'unspecified'}\n\n"
        "Job description:\n"
        f"{job.jd_text or '(no description text was scraped)'}\n\n"
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


def build_system() -> str:
    """System instruction for Call 1b (anti-fabrication)."""
    return _BUILD_SYSTEM


def build_prompt(
    result: SelectionResult,
    title_candidates: dict[str, list[str]],
    skill_candidates: list[tuple[str, float]],
    gap_skills: list[str],
    jd_role_summary: str,
    jd_team_or_product: str | None,
    banned_category_names: Sequence[str],
) -> str:
    """Render the Call 1b user prompt.

    Args:
        result:               Layer 4 SelectionResult (selected experiences, projects).
        title_candidates:     {exp_id: [safe_title_alias, ...]} for the selected exps.
        skill_candidates:     Top-14 (skill, score) tuples from the pool.
        gap_skills:           JD-required skills not in pool (Familiar With candidates).
        jd_role_summary:      Parsed JD role summary sentence(s).
        jd_team_or_product:   Optional team/product context from the JD.
        banned_category_names: Category names the LLM must not use.
    """
    # --- experience section ---
    exp_lines: list[str] = []
    for exp in result.experiences:
        aliases = title_candidates.get(exp.id, [exp.actual_title])
        exp_lines.append(
            f"  {exp.id}: company={exp.company!r}, "
            f"allowed_aliases={aliases}"
        )
    exp_block = "\n".join(exp_lines) if exp_lines else "  (none selected)"

    # --- project section ---
    proj_lines = [
        f"  {p.id}: name={p.name!r}"
        for p in result.projects
    ]
    proj_block = "\n".join(proj_lines) if proj_lines else "  (none selected)"

    # --- skills section ---
    cand_lines = [f"  {skill} (score={score:.3f})" for skill, score in skill_candidates]
    cand_block = "\n".join(cand_lines) if cand_lines else "  (no candidates)"
    gap_block = ", ".join(gap_skills) if gap_skills else "(none)"

    banned = ", ".join(f'"{n}"' for n in banned_category_names)

    return (
        "You are tailoring a resume for the following job:\n"
        f"Role summary: {jd_role_summary}\n"
        + (f"Team/product: {jd_team_or_product}\n" if jd_team_or_product else "")
        + "\n"
        "SELECTED WORK EXPERIENCE (choose exactly one title alias per entry):\n"
        f"{exp_block}\n\n"
        "SELECTED PROJECTS:\n"
        f"{proj_block}\n\n"
        "SKILL POOL CANDIDATES (top-14; use ONLY these for skill categories):\n"
        f"{cand_block}\n\n"
        f"GAP SKILLS (JD wants but not in pool; use ONLY these for Familiar With):\n"
        f"  {gap_block}\n\n"
        "INSTRUCTIONS:\n"
        "1. title_choices: for each experience id above, pick exactly one alias "
        "   from its allowed_aliases list. Do not invent titles.\n"
        "2. skills_selection: name exactly 3 categories (3-5 pool skills each). "
        "   Category skills must come from the SKILL POOL CANDIDATES list only. "
        f"   Do NOT use these banned category names: {banned}. "
        "   familiar_with: 0-4 skills from GAP SKILLS only.\n"
        "3. cover_letter_text: one short paragraph (max 900 chars) about why this "
        "   candidate fits the role. Reference only the experience and projects "
        "   listed above. No clichés, no buzzwords like 'leverage', 'synergy', "
        "   'robust', 'scalable', 'passionate about', 'I am excited to', "
        "   'I am writing to', 'as a [role]'.\n"
        "Return the result as a JSON object matching the ResumeBuildLLMOutput schema."
    )
