"""Layer 4 — apply/skip decision. Iteration 1: rejects every job.

In Iteration 1 the master_profile rebuild hasn't run yet (no YAML),
so master_bullets is empty. Without bullets we cannot score experiences
or build a resume, so every job is correctly classified ``LOW_SCORE``.

Iteration 2 replaces this with the full Section 4 scoring algorithm
(experience selection, project selection, summary pick, skills hybrid,
final_score formula, cycle-aware top-N picking with queue management).

The ``Decision`` shape is the stable contract between Layers 4 and 7.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.state.models import MasterBullets


@dataclass(frozen=True)
class Decision:
    """Outcome of scoring one job."""

    apply: bool
    reason_category: str
    reason_detail: str | None = None
    final_score: float | None = None


def decide(session: Session) -> Decision:
    """Return the apply/skip decision for the current job.

    Iter 1 ignores the job entirely and returns LOW_SCORE if no bullets
    exist. The signature accepts ``session`` so Iter 2 can swap in real
    scoring against ``master_bullets`` without a contract change.
    """
    bullet_count = session.scalar(
        select(MasterBullets.bullet_id).limit(1)
    )
    if bullet_count is None:
        return Decision(
            apply=False,
            reason_category="LOW_SCORE",
            reason_detail=(
                "Iter 1: master_bullets is empty (master_profile.yaml "
                "rebuild not yet run). Cannot score; rejecting."
            ),
            final_score=0.0,
        )

    # If bullets exist somehow, still reject — Iter 1 has no real scorer.
    return Decision(
        apply=False,
        reason_category="LOW_SCORE",
        reason_detail="Iter 1 stub: real scoring lands in Iteration 2.",
        final_score=0.0,
    )
