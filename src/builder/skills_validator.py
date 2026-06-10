"""Layer 5 — Post-generation validation of Gemini Call 1b skills output.

Checks that the LLM respected the source-set constraints (hard rules #1, #7):
  - Exactly 3 categories, each with 3-5 skills.
  - Every category skill is in the top-14 pool candidates.
  - Every Familiar With skill is in the gap-skills set.
  - No banned category names.
  - No duplicate skills across categories.

Returns a list of violation strings — empty means valid. The caller
regenerates up to ``config.builder.llm_regenerate_attempts`` times before
raising BUILD_FAILURE.
"""

from __future__ import annotations

from src.config import settings
from src.llm.schemas import ResumeBuildLLMOutput


def validate(
    output: ResumeBuildLLMOutput,
    candidates: list[str],
    gap_skills: list[str],
) -> list[str]:
    """Validate Call 1b output against source sets. Returns violation list."""
    violations: list[str] = []
    cfg = settings.selection.skills
    banned: list[str] = cfg.banned_category_names

    cats = output.skills_selection.categories
    if len(cats) != cfg.categories_count:
        violations.append(
            f"expected {cfg.categories_count} categories, got {len(cats)}"
        )

    cand_set = {s.casefold() for s in candidates}
    gap_set = {s.casefold() for s in gap_skills}
    banned_set = {n.casefold() for n in banned}
    seen_skills: set[str] = set()

    for cat in cats:
        if cat.name.casefold() in banned_set:
            violations.append(f"banned category name: {cat.name!r}")

        n = len(cat.skills)
        if not (cfg.skills_per_category_min <= n <= cfg.skills_per_category_max):
            violations.append(
                f"category {cat.name!r}: {n} skills, "
                f"need {cfg.skills_per_category_min}-{cfg.skills_per_category_max}"
            )

        for skill in cat.skills:
            key = skill.casefold()
            if key not in cand_set:
                violations.append(
                    f"category {cat.name!r}: {skill!r} not in pool candidates"
                )
            if key in seen_skills:
                violations.append(f"duplicate skill across categories: {skill!r}")
            seen_skills.add(key)

    fw = output.skills_selection.familiar_with
    if len(fw) > cfg.familiar_with_max:
        violations.append(
            f"familiar_with has {len(fw)} skills, max {cfg.familiar_with_max}"
        )
    for skill in fw:
        if skill.casefold() not in gap_set:
            violations.append(f"familiar_with: {skill!r} not in gap skills")

    return violations
