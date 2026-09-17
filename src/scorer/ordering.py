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


def _is_salaried(entry: SelectedEntry) -> bool:
    """Is this the operator's actual job — the one an employer paid a salary for?

    ``kind`` cannot answer this. A freelance engagement loads as ``kind="work"``
    (``master_profile.load_profile`` gives every ``work_experience`` row that kind,
    because they all render under Work History), so keying the guard off ``kind``
    counted a two-month gig as employment and let it satisfy the rule below — the
    exact page the guard exists to prevent, with a freelance line standing in for
    the job. ``employment_type`` is the field that distinguishes them, and projects
    carry its default, so both halves of the test are needed.
    """
    return entry.kind == "work" and entry.employment_type == "employment"


def order_entries(
    selected: list[SelectedEntry],
) -> list[SelectedEntry]:
    """Order every entry — work, freelance and project — by match, best first.

    v3.2 merged the two sections into one, so this now orders the whole page
    rather than just the work half, and recency no longer decides anything: the
    entry that matches this JD best leads, whatever kind it is. A project — or a
    freelance engagement — CAN open the resume if it fits better than the job.

    One guard. The salaried employment entry must hold one of the first
    ``selection.entry.job_within_top`` slots. The top of a resume is where a
    recruiter looks for employment, and a reader who finds none there stops
    reading — so position 1 goes to whatever matches best, and if that is not the
    job, the job takes position 2 and everything else keeps descending match
    order. Freelance does NOT satisfy this: to a recruiter scanning for
    employment, a gig reads as a project with an invoice, which is also why it is
    selected on merit like one.

    If no salaried entry was selected at all, there is nothing to guarantee and
    the order is pure match.
    """
    if len(selected) <= 1:
        return list(selected)

    ordered = sorted(selected, key=lambda x: x.score, reverse=True)

    top_n = int(getattr(settings.selection.entry, "job_within_top", 2) or 0)
    if not top_n:
        return ordered
    if any(_is_salaried(e) for e in ordered[:top_n]):
        return ordered
    promoted = next((e for e in ordered if _is_salaried(e)), None)
    if promoted is None:  # no salaried entry selected — nothing to guarantee
        return ordered
    rest = [e for e in ordered if e.id != promoted.id]
    return [rest[0], promoted, *rest[1:]]
