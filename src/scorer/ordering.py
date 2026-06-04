"""Layer 4 — display ordering (match-then-recency + section order).

Selection (selector.py) ranks by score; this module decides the order items
appear on the resume. Pure functions, config-driven.

  * Experiences: if the top two selected experiences differ by more than
    ``selection.experience.match_then_recency_gap`` (0.20), the best match
    takes position 1 and the rest follow recency; otherwise all follow
    recency (most-recent end_date first). (CLAUDE.md EXPERIENCE rule.)
  * Project bullets are already descending by score from selection.
  * Section order is fixed except Skills vs Projects, which are ordered by
    aggregate JD match (CLAUDE.md SECTION rule).
"""

from __future__ import annotations

from src.config import settings
from src.scorer.selector import SelectedExperience, SelectedProject


def _recency_key(end_date: str) -> tuple[int, int]:
    """Sort key for an experience end_date; "present" sorts newest."""
    value = (end_date or "").strip().lower()
    if value == "present":
        return (9999, 12)
    parts = value.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        return (year, month)
    except (ValueError, IndexError):
        return (0, 0)


def order_experiences(
    selected: list[SelectedExperience],
) -> list[SelectedExperience]:
    """Order selected experiences by match-then-recency."""
    if len(selected) <= 1:
        return list(selected)
    by_score = sorted(selected, key=lambda x: x.score, reverse=True)
    by_recency = sorted(selected, key=lambda x: _recency_key(x.end_date), reverse=True)
    gap = by_score[0].score - by_score[1].score
    if gap > settings.selection.experience.match_then_recency_gap:
        best = by_score[0]
        rest = [x for x in by_recency if x.id != best.id]
        return [best, *rest]
    return by_recency


def skills_before_projects(
    skill_candidates: list[tuple[str, float]],
    projects: list[SelectedProject],
) -> bool:
    """True if the Skills section should precede Projects (higher aggregate
    JD match). Ties favour Skills."""
    skill_avg = (
        sum(s for _, s in skill_candidates) / len(skill_candidates)
        if skill_candidates
        else 0.0
    )
    project_avg = (
        sum(p.score for p in projects) / len(projects) if projects else 0.0
    )
    return skill_avg >= project_avg
