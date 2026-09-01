"""Layer 4 — display ordering (match-then-recency + section order).

Selection (selector.py) ranks by score; this module decides the order items
appear on the resume. Pure functions, config-driven.

  * Work entries: if the top two differ by more than
    ``selection.work.match_then_recency_gap`` (0.20), the best match takes
    position 1 and the rest follow recency; otherwise all follow recency
    (most-recent end_date first).
  * Projects are ordered by score; they carry no dates to be recent about.

Section order itself is no longer computed. The Headless template fixes it —
Education & Certificates (static, hand-written into the template), then Work
History, then Projects — so the old ``skills_before_projects`` comparison had
nothing left to order and was removed with the Skills section.
"""

from __future__ import annotations

from src.config import settings
from src.scorer.selector import SelectedEntry


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


def order_entries(
    selected: list[SelectedEntry],
) -> list[SelectedEntry]:
    """Order selected work entries by match-then-recency."""
    if len(selected) <= 1:
        return list(selected)
    by_score = sorted(selected, key=lambda x: x.score, reverse=True)
    by_recency = sorted(selected, key=lambda x: _recency_key(x.end_date), reverse=True)
    gap = by_score[0].score - by_score[1].score
    if gap > settings.selection.work.match_then_recency_gap:
        best = by_score[0]
        rest = [x for x in by_recency if x.id != best.id]
        return [best, *rest]
    return by_recency
