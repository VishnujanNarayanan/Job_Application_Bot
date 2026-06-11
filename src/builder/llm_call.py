"""Layer 5 — Gemini Call 1b driver: title alias + skills + cover letter.

Accepts the Layer-4 ``SelectionResult`` and ``Profile``, calls the LLM,
post-validates the output, and returns a ``StoredSelection`` ready to be
written to ``applied.selection_json``. Returns ``None`` on irrecoverable
failure (caller records BUILD_FAILURE in ``not_applied``).

Budget: this is Call 1b — the 2nd and final call per matched job (Call 1a
is the JD parse). Hard rule #13 is never violated here.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.builder.skills_validator import validate
from src.config import settings
from src.llm.client import LLMError
from src.llm.client import complete as _default_complete
from src.llm.prompts import build_prompt, build_system
from src.llm.schemas import (
    ResumeBuildLLMOutput,
    SelectedExpEntry,
    SelectedProjEntry,
    StoredSelection,
)
from src.reasons import BUILD_FAILURE as _BUILD_FAILURE_REASON
from src.scorer.apply_decision import SelectionResult
from src.scorer.selector import Profile

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]


def _template_version() -> str:
    """MD5[:8] of the current template file (cache-bust key)."""
    path = _ROOT / settings.endpoint.template_path
    try:
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        return digest[:8]
    except OSError:
        return "00000000"


def _gap_skills_for_jd(
    required_skills: list[str], skills_pool: list[str]
) -> list[str]:
    """JD required skills not present in the operator's skills pool."""
    pool_lower = {s.casefold() for s in skills_pool}
    return [
        s for s in required_skills
        if s.casefold() not in pool_lower
        and not any(s.casefold() in p for p in pool_lower)
    ]


def _cover_letter_clean(text: str) -> list[str]:
    """Return list of banned-word violations in the cover letter text."""
    violations = []
    lower = text.casefold()
    for word in settings.voice.banned_words:
        if word.casefold() in lower:
            violations.append(f"banned word in cover letter: {word!r}")
    return violations


def build(
    result: SelectionResult,
    profile: Profile,
    jd_role_summary: str,
    jd_required_skills: list[str],
    jd_team_or_product: str | None = None,
    *,
    complete_fn=None,
) -> StoredSelection | None:
    """Run Call 1b, validate, and return a ``StoredSelection`` or ``None``.

    ``complete_fn`` is injectable for tests (avoids real Gemini calls).
    """
    run = complete_fn or _default_complete
    max_attempts = int(settings.builder.llm_regenerate_attempts)
    banned_names: list[str] = settings.selection.skills.banned_category_names

    # Derive inputs from profile + selection
    skills_pool = [sc.skill for sc in profile.skills]
    gap_skills = _gap_skills_for_jd(jd_required_skills, skills_pool)

    title_candidates: dict[str, list[str]] = {
        exp.id: list(exp.safe_title_aliases)
        for exp in profile.experiences
        if any(se.id == exp.id for se in result.experiences)
    }

    skill_candidates: list[tuple[str, float]] = result.skill_candidates
    cand_names = [s for s, _ in skill_candidates]

    prompt = build_prompt(
        result=result,
        title_candidates=title_candidates,
        skill_candidates=skill_candidates,
        gap_skills=gap_skills,
        jd_role_summary=jd_role_summary,
        jd_team_or_product=jd_team_or_product,
        banned_category_names=banned_names,
    )
    system = build_system()

    last_violations: list[str] = []
    for attempt in range(1, max_attempts + 2):  # up to regenerate_attempts+1 total
        try:
            output: ResumeBuildLLMOutput = run(ResumeBuildLLMOutput, prompt, system=system)
        except LLMError as exc:
            log.error(
                BUILD_FAILURE_EVENT,
                caller="builder",
                reason="gemini_error",
                attempt=attempt,
                error=str(exc),
            )
            return None

        violations = validate(output, cand_names, gap_skills)
        violations += _cover_letter_clean(output.cover_letter_text)

        if not violations:
            break

        last_violations = violations
        if attempt <= max_attempts:
            log.warning(
                "build_validation_retry",
                attempt=attempt,
                violations=violations,
            )
    else:
        log.error(
            BUILD_FAILURE_EVENT,
            caller="builder",
            reason="validation_failed",
            violations=last_violations,
        )
        return None

    # Assemble StoredSelection from result + LLM output
    exp_entries: list[SelectedExpEntry] = []
    for se in result.experiences:
        alias = output.title_choices.get(se.id, se.actual_title)
        exp_entries.append(
            SelectedExpEntry(
                exp_id=se.id,
                title_alias=alias,
                bullet_ids=[b.id for b in se.bullets],
            )
        )

    # Look up project links from profile
    proj_link_map = {p.id: p.link for p in profile.projects}
    proj_entries: list[SelectedProjEntry] = []
    for sp in result.projects:
        proj_entries.append(
            SelectedProjEntry(
                proj_id=sp.id,
                link=proj_link_map.get(sp.id, ""),
                bullet_ids=[b.id for b in sp.bullets],
            )
        )

    section_order = (
        ["Work", "Skills", "Projects"]
        if result.skills_before_projects
        else ["Work", "Projects", "Skills"]
    )

    # Find summary_id: use the selected summary's id or a fallback
    summary_id = result.summary.id if result.summary else ""

    return StoredSelection(
        job_id="",  # caller fills in job_id before writing to DB
        summary_id=summary_id,
        experiences=exp_entries,
        projects=proj_entries,
        skills=output.skills_selection,
        section_order=section_order,
        cover_letter_text=output.cover_letter_text,
        template_version=_template_version(),
        built_at=datetime.now(timezone.utc).isoformat(),
    )


# Event name referenced by CloudWatch metric filters (CLAUDE.md logging section).
BUILD_FAILURE_EVENT = _BUILD_FAILURE_REASON
