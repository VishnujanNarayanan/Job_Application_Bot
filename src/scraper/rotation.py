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


def current_terms(session: Session, terms: Sequence[str], count: int) -> list[str]:
    """The next ``count`` terms for this run, wrapping around the list.

    Runs are manually triggered rather than every 40 minutes, so one term per
    run would need as many clicks as there are terms to cover the list once.
    Taking several per run trades a longer run for far fewer clicks; the
    ceiling is the Gemini free-tier daily quota, not politeness to JobSpy,
    since each term is a separate throttled call either way.

    Duplicates are dropped (a ``count`` above ``len(terms)`` wraps onto
    itself), preserving order.
    """
    if not terms:
        raise ValueError("rotation: search_terms is empty")
    if count < 1:
        raise ValueError(f"rotation: terms_per_run must be >= 1, got {count}")

    start = current_index(session)
    picked = [terms[(start + offset) % len(terms)] for offset in range(count)]
    return list(dict.fromkeys(picked))


def advance(session: Session, terms: Sequence[str], step: int = 1) -> int:
    """Move ``step`` terms forward, persist, and return the new index.

    Flushes within the caller's transaction; the caller commits.
    """
    if not terms:
        raise ValueError("rotation: search_terms is empty")
    next_index = (current_index(session) + step) % len(terms)
    row = session.get(SearchRotationState, _ROTATION_KEY)
    if row is None:
        session.add(SearchRotationState(key=_ROTATION_KEY, value=str(next_index)))
    else:
        row.value = str(next_index)
    session.flush()
    return next_index
