"""Layer 2 — near-duplicate JD detection (architecture §4.1 / FR-2).

Cross-portal reposts (the same job on Indeed *and* LinkedIn) would
otherwise notify the operator twice. Before a job moves downstream we
compare its JD embedding against recent canonical jobs; if cosine
exceeds the configured threshold (>0.95) the job is marked
NEAR_DUPLICATE and linked to the original via ``all_jobs.near_duplicate_of``.

``near_duplicate_of`` is a real self-referential FK, so the target row
**must exist** before a duplicate references it. ``resolve_batch`` enforces
this insert order: originals are added and flushed first, then duplicates
are linked and flushed. This also covers the same-run case where two
reposts (X then Y) both arrive new — X is classified original and flushed
before Y links to it. The caller owns the final ``commit``.

``find_near_duplicate`` and ``cosine`` are pure: unit-testable with
hand-built vectors and no DB.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import chain

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.reasons import NEAR_DUPLICATE
from src.scorer.embeddings import Vector, cosine
from src.state.models import AllJobs


def find_near_duplicate(
    embedding: Vector,
    candidates: Iterable[tuple[str, Vector]],
    threshold: float,
) -> tuple[str, float] | None:
    """Return ``(job_id, score)`` of the closest candidate above ``threshold``.

    Comparison is strictly greater-than (hard rule #15: ">0.95"). Returns
    None when no candidate clears the bar.
    """
    best: tuple[str, float] | None = None
    for cid, cvec in candidates:
        score = cosine(embedding, cvec)
        if score > threshold and (best is None or score > best[1]):
            best = (cid, score)
    return best


def canonical_embeddings(session: Session, limit: int) -> list[tuple[str, Vector]]:
    """Recent canonical jobs (non-null embedding, not themselves duplicates).

    Only canonical originals are compared against, so duplicate links stay
    one level deep (a duplicate never points at another duplicate).
    """
    rows = session.execute(
        select(AllJobs.job_id, AllJobs.jd_embedding)
        .where(AllJobs.jd_embedding.is_not(None))
        .where(AllJobs.near_duplicate_of.is_(None))
        .order_by(AllJobs.scraped_at.desc())
        .limit(limit)
    ).all()
    return [(jid, list(vec)) for jid, vec in rows]


@dataclass
class DedupOutcome:
    """Per-job classification result from :func:`resolve_batch`."""

    job: AllJobs
    is_duplicate: bool
    original_id: str | None = None
    score: float | None = None


def resolve_batch(
    session: Session,
    new_jobs: list[AllJobs],
    *,
    threshold: float,
    lookback: int,
) -> list[DedupOutcome]:
    """Classify ``new_jobs`` as originals or near-duplicates and persist them.

    Jobs must already carry ``jd_embedding`` (set by the orchestrator after
    scrape); a job with no embedding cannot be deduped and is treated as an
    original. Persistence respects the self-FK: originals are added +
    flushed first, then duplicates are linked and flushed. The caller
    commits.
    """
    db_candidates = canonical_embeddings(session, lookback)
    batch_originals: list[tuple[str, Vector]] = []
    outcomes: list[DedupOutcome] = []

    for job in new_jobs:
        emb = job.jd_embedding
        if emb is None:
            outcomes.append(DedupOutcome(job, is_duplicate=False))
            continue
        match = find_near_duplicate(
            emb, chain(db_candidates, batch_originals), threshold
        )
        if match is None:
            outcomes.append(DedupOutcome(job, is_duplicate=False))
            batch_originals.append((job.job_id, list(emb)))
        else:
            outcomes.append(
                DedupOutcome(
                    job, is_duplicate=True, original_id=match[0], score=match[1]
                )
            )

    # Insert order: originals first so their job_id rows exist, then link
    # duplicates (FK target must be present before the reference is written).
    originals = [o.job for o in outcomes if not o.is_duplicate]
    if originals:
        session.add_all(originals)
        session.flush()

    duplicates = [o for o in outcomes if o.is_duplicate]
    for o in duplicates:
        o.job.near_duplicate_of = o.original_id
        o.job.outcome = NEAR_DUPLICATE
        session.add(o.job)
    if duplicates:
        session.flush()

    return outcomes
