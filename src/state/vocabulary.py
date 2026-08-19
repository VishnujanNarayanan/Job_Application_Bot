"""Layer 3 — the technology vocabulary learned from previously parsed JDs.

The model under-reports. Measured 2026-08-19 across four real ads, every one of
LangChain, LangGraph, MLOps, NLP, BERT, GPT, Mistral, TinyML, RAG and Scikit
appeared in the JD text and not in the parse. `with_pool_skills` covers the
operator's own skills, because their pool is a known list; a technology outside
the pool has nothing to check against, and those are the gap skills Familiar
With is built from.

The corpus supplies the missing list. Terms the parser has emitted across many
unrelated ads are real — a hallucination does not recur in thirty different
listings — so recurrence is the filter, and the resulting vocabulary is scanned
against each JD deterministically with no model involved.

Nothing here infers or expands: a term enters the vocabulary only by having
been extracted from real ads, and matches only where the JD contains it
literally on a word boundary.
"""

from __future__ import annotations

import re
from collections import defaultdict

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.config import settings
from src.state.models import AllJobs, SkillVocabulary

log = structlog.get_logger(__name__)

# Terms that recur across ads without naming a technology. They pass the
# recurrence filter honestly — plenty of ads do say "communication skills" —
# but they match nothing in any skills pool and would be added to every JD.
_NOT_A_TECHNOLOGY = frozenset({
    "communication skills", "communication", "problem solving", "problem-solving",
    "teamwork", "team work", "collaboration", "leadership", "analytical",
    "analytical skills", "attention to detail", "time management", "adaptability",
    "creativity", "critical thinking", "interpersonal skills", "presentation skills",
    "written communication", "verbal communication", "stakeholder management",
    "documentation", "mentoring", "ownership", "experience", "skills", "knowledge",
    "data", "backend", "frontend", "development", "engineering", "design",
    "testing", "deployment", "monitoring", "automation", "optimization",
    "evaluation", "research", "production", "infrastructure", "architecture",
    "apis", "api", "cloud", "database", "databases", "software", "systems",
    "tools", "frameworks", "platforms", "services", "models", "modeling",
})


def _is_candidate(term: str) -> bool:
    """Cheap shape test before a term is considered for the vocabulary."""
    t = term.strip()
    if not (1 < len(t) <= 30):
        return False
    if t.casefold() in _NOT_A_TECHNOLOGY:
        return False
    # A technology name is at most a few words. Longer strings are prose that
    # the splitter did not manage to break up.
    if len(t.split()) > 3:
        return False
    # Must contain a letter — "3", "2026" and "10+" are not skills.
    return bool(re.search(r"[A-Za-z]", t))


def rebuild(session: Session, *, min_jobs: int | None = None) -> int:
    """Recompute the vocabulary from every parsed job. Returns the term count.

    Casing comes from the most common spelling across the corpus, so the
    vocabulary emits "PyTorch" rather than whichever ad happened to write
    "pytorch". Terms deactivated by hand stay deactivated: a rebuild updates
    counts and adds new terms, and never resurrects a retired one.
    """
    floor = int(min_jobs if min_jobs is not None else settings.scoring.get(
        "vocabulary_min_jobs", 5))

    # job_count is DISTINCT jobs, not occurrences: a term repeated four times
    # within one ad is one ad's worth of evidence, not four.
    jobs = session.scalars(
        select(AllJobs).where(AllJobs.required_skills.isnot(None))
    ).all()

    spellings: dict[str, defaultdict[str, int]] = {}
    job_counts: defaultdict[str, int] = defaultdict(int)
    for job in jobs:
        seen: set[str] = set()
        for raw in (job.required_skills or []) + (job.nice_to_have or []):
            term = str(raw).strip()
            if not _is_candidate(term):
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            job_counts[key] += 1
            spellings.setdefault(key, defaultdict(int))[term] += 1

    kept = {k: v for k, v in job_counts.items() if v >= floor}
    if not kept:
        log.warning("vocabulary_empty", jobs=len(jobs), min_jobs=floor)
        return 0

    for key, count in kept.items():
        term = max(spellings[key].items(), key=lambda kv: (kv[1], kv[0]))[0]
        session.execute(
            pg_insert(SkillVocabulary)
            .values(term_key=key, term=term, job_count=count)
            # is_active is deliberately absent from the update: a term retired
            # by hand must not come back on the next rebuild.
            .on_conflict_do_update(
                index_elements=["term_key"],
                set_={"term": term, "job_count": count},
            )
        )

    log.info("vocabulary_rebuilt", terms=len(kept), jobs=len(jobs), min_jobs=floor)
    return len(kept)


def load_terms(session: Session) -> tuple[tuple[str, str], ...]:
    """Active vocabulary as (term, lower-cased key), longest term first."""
    rows = session.execute(
        select(SkillVocabulary.term, SkillVocabulary.term_key)
        .where(SkillVocabulary.is_active.is_(True))
    ).all()
    return tuple(sorted(((r[0], r[1]) for r in rows), key=lambda p: len(p[1]), reverse=True))


def scan(jd_text: str, terms: tuple[tuple[str, str], ...]) -> list[str]:
    """Vocabulary terms the JD names literally, in the order they appear.

    Word-boundary matching only. A dot blocks the match only when a word
    follows it, so "Node" does not match inside "Node.js" while a term ending a
    sentence ("...and FastAPI.") still does.
    """
    if not jd_text or not terms:
        return []
    haystack = jd_text.casefold()
    found: list[tuple[int, str]] = []
    for term, key in terms:
        match = re.search(rf"(?<![\w+#.]){re.escape(key)}(?![\w+#]|\.\w)", haystack)
        if match:
            found.append((match.start(), term))
    found.sort()
    return [term for _, term in found]
