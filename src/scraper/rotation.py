"""Layer 2 — serial search-term rotation (architecture §4).

One search term is used per run to stay well inside JobSpy's polite
request budget; the term advances each run. State is a single row in
``search_rotation_state`` (key ``search_term_index``) so rotation survives
restarts. Pure index arithmetic so the term list can change between runs
without breaking (modulo wraps).

The 20-job short-circuit (config ``scraper.short_circuit_count``) is a
*scrape-loop* concern enforced by the orchestrator/jobspy wrapper, not
here; rotation only answers "which term this run" and "advance".
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.state.models import SearchRotationState

_ROTATION_KEY = "search_term_index"


def current_index(session: Session) -> int:
    """The persisted rotation index (0 if never set)."""
    val = session.scalar(
        select(SearchRotationState.value).where(
            SearchRotationState.key == _ROTATION_KEY
        )
    )
    return int(val) if val is not None else 0


def current_term(session: Session, terms: Sequence[str]) -> str:
    """The search term for this run (index wrapped into ``terms``)."""
    if not terms:
        raise ValueError("rotation: search_terms is empty")
    return terms[current_index(session) % len(terms)]


def advance(session: Session, terms: Sequence[str]) -> int:
    """Move to the next term, persist, and return the new index.

    Flushes within the caller's transaction; the caller commits.
    """
    if not terms:
        raise ValueError("rotation: search_terms is empty")
    next_index = (current_index(session) + 1) % len(terms)
    row = session.get(SearchRotationState, _ROTATION_KEY)
    if row is None:
        session.add(SearchRotationState(key=_ROTATION_KEY, value=str(next_index)))
    else:
        row.value = str(next_index)
    session.flush()
    return next_index
