"""Layer 5 — selection builder: title alias + skills, no LLM.

Accepts the Layer-4 ``SelectionResult`` and ``Profile`` and returns a
``StoredSelection`` ready to be written to ``applied.selection_json``.
Returns ``None`` on irrecoverable failure (caller records BUILD_FAILURE in
``not_applied``).

**Call 1b was removed.** It asked Gemini for a title alias, skill-category
names, and cover-letter text; none needed a language model. Title choice is an
argmax over an allow-list, the skills were already chosen deterministically by
Layer 4 (only their grouping came from the LLM), and the cover letter was
written to the DB and never read by anything. See
``src/builder/deterministic.py`` for the reasoning.

Budget: a matched job now costs ONE Gemini call (Call 1a, the JD parse) rather
than two. Hard rule #13 caps it at two; this is comfortably under.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.builder.deterministic import (
    assign_skill_categories,
    choose_title_alias,
    jd_role_vector,
)
from src.config import settings
from src.llm.schemas import (
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


def build(
    result: SelectionResult,
    profile: Profile,
    jd_role_summary: str,
    jd_required_skills: list[str],
    jd_team_or_product: str | None = None,
    *,
    complete_fn=None,
) -> StoredSelection | None:
    """Build the ``StoredSelection`` for a matched job. No LLM call.

    ``complete_fn`` is accepted and ignored: it existed so tests could stub
    Gemini, and callers (including ``src/main.py``) still pass nothing. Kept in
    the signature so removing Call 1b is not a breaking change for anything
    that already calls this.
    """
    if complete_fn is not None:
        log.debug("complete_fn_ignored", reason="call_1b_removed")

    skills_pool = [sc.skill for sc in profile.skills]
    gap_skills = _gap_skills_for_jd(jd_required_skills, skills_pool)

    # One embedding of the JD role text, reused for every experience's aliases.
    jd_role_vec = jd_role_vector(jd_role_summary, fallback_text=jd_team_or_product or "")

    exp_entries: list[SelectedExpEntry] = []
    for se in result.experiences:
        alias = choose_title_alias(
            list(se.safe_title_aliases),
            jd_role_vec,
            fallback=se.actual_title,
        )
        exp_entries.append(
            SelectedExpEntry(
                exp_id=se.id,
                title_alias=alias,
                bullet_ids=[b.id for b in se.bullets],
            )
        )

    skills_selection = assign_skill_categories(
        result.skill_candidates, gap_skills, jd_role_vec=jd_role_vec
    )

    # A selection with no experiences cannot produce a resume; Layer 4 should
    # force-include two, so this means the profile or the selection is broken.
    if not exp_entries:
        log.error(
            BUILD_FAILURE_EVENT,
            caller="builder",
            reason="no_experiences_selected",
        )
        return None

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
        skills=skills_selection,
        section_order=section_order,
        cover_letter_text="",
        template_version=_template_version(),
        built_at=datetime.now(timezone.utc).isoformat(),
    )


# Event name referenced by CloudWatch metric filters (CLAUDE.md logging section).
BUILD_FAILURE_EVENT = _BUILD_FAILURE_REASON
