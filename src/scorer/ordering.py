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
    """Order every entry — work, freelance and project — by match, best first.

    v3.2 merged the two sections into one, so this now orders the whole page
    rather than just the work half, and recency no longer decides anything: the
    entry that matches this JD best leads, whatever kind it is. A project CAN open
    the resume if it fits better than any job.

    One guard. A job or freelance engagement must hold one of the first two slots,
    so the page never opens with two unpaid projects — the top of a resume is where
    a recruiter looks for employment, and a reader who finds none there stops
    reading. If the top two are both projects, the best-matching non-project is
    lifted into slot 2; everything else keeps its order.
    """
    if len(selected) <= 1:
        return list(selected)

    ordered = sorted(selected, key=lambda x: x.score, reverse=True)

    top_n = int(getattr(settings.selection.entry, "job_within_top", 2) or 0)
    if not top_n:
        return ordered
    head = ordered[:top_n]
    if any(e.kind != "project" for e in head):
        return ordered
    promoted = next((e for e in ordered if e.kind != "project"), None)
    if promoted is None:  # no job selected at all — nothing to guarantee
        return ordered
    rest = [e for e in ordered if e.id != promoted.id]
    return [rest[0], promoted, *rest[1:]]
