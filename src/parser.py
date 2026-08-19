"""Layer 3 — JD parser (Gemini Call 1a, always run).

Wraps the single always-on Gemini call: a scraped ``AllJobs`` row in, a
schema-validated :class:`JDParsed` out (Instructor guarantees the shape).

One safeguard sits around the call:

  * **Skill grounding** — the model is told to copy skills verbatim, but
    we still drop any returned skill that isn't actually present in the JD
    text (substring, or a spaCy-lemma subset match for inflections). This
    keeps fabricated skills out of the scoring inputs.

Contract: ``parse(job) -> JDParsed``; ``apply_to_row`` writes the
structured fields onto the row. ``parse`` accepts an injectable
``complete`` callable so tests run without Gemini.
"""

from __future__ import annotations

import re


from collections.abc import Callable
from functools import lru_cache

import structlog

from src.config import settings
from src.state.vocabulary import scan
from src.llm.client import complete as _default_complete
from src.llm.prompts import jd_parse_prompt, jd_parse_system
from src.llm.schemas import JDParsed
from src.state.models import AllJobs

# Type of the LLM transport (injectable for tests).
CompleteFn = Callable[..., JDParsed]


def parse(job: AllJobs, *, complete: CompleteFn | None = None) -> JDParsed:
    """Run Gemini Call 1a for ``job`` and return a grounded :class:`JDParsed`."""
    run = complete or _default_complete
    # Built per provider rather than once: how much of the description to send
    # depends on which provider answers. A local model has no token budget and
    # reads the whole ad; a metered one further down the chain still gets it
    # clipped. `prompt` stays for injected test doubles that take a string.
    parsed: JDParsed = run(
        JDParsed,
        jd_parse_prompt(job),
        system=jd_parse_system(),
        prompt_fn=lambda cfg: jd_parse_prompt(job, provider_cfg=cfg),
    )

    jd_text = job.jd_text or ""
    parsed.required_skills = grounded_skills(parsed.required_skills, jd_text)
    parsed.nice_to_have = grounded_skills(parsed.nice_to_have, jd_text)
    parsed.required_skills = with_pool_skills(parsed.required_skills, jd_text)
    parsed.required_skills = with_vocabulary_skills(parsed.required_skills, jd_text)
    return parsed


# Requirement boilerplate that is short enough to clear the schema's length
# bound but is not a skill: "Bachelor's degree" (17 chars), "Equal Opportunity
# Employer" (26), "3+ years experience" (19). Matching on the phrase is enough
# — a real skill never contains these words.
log = structlog.get_logger(__name__)


@lru_cache(maxsize=1)
def _pool_terms() -> tuple[str, ...]:
    """The operator's skills_pool, longest first.

    Read from master_profile.json — the parsed cache Layer 7 maintains — so
    this uses the same source the scorer does rather than re-reading the YAML.
    """
    import json

    from src.state.master_profile import _JSON_PATH as path

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("pool_skills_unavailable", path=str(path), error=str(exc))
        return ()
    pool = [str(s).strip() for s in (data.get("skills_pool") or []) if str(s).strip()]
    return tuple(sorted(pool, key=len, reverse=True))


def with_pool_skills(skills: list[str], jd_text: str) -> list[str]:
    """Add pool skills the JD names outright but the model failed to return.

    The model is the only thing extracting skills, and it under-reports. On one
    real ad it returned "experience working with LLMs" and silently dropped the
    "(e.g., GPT-3/4, Claude, Mistral)" that followed; on another it extracted
    Java and missed Python from a Python job. Measured 2026-08-19 across four
    ads: Python, MLOps, NLP, BERT, GPT, Mistral, TinyML, LangChain, LangGraph,
    RAG and Scikit were all present in the text and absent from the output.

    That asymmetry is expensive. `fit` is 55% of the final score and each pool
    skill is scored against its BEST matching JD skill, so a JD skill the model
    invented costs nothing (it simply never matches) while one it missed drags
    the corresponding pool skill down — the operator looks less qualified than
    the ad says they need.

    The operator's pool is a known, closed list, so its members do not need to
    be inferred: if the JD names one literally, it is a required skill. Only
    exact substring matches on a word boundary are added — no lemmatising, no
    fuzzy matching — so this can only ever add something the ad actually says.
    """
    if not jd_text:
        return skills
    haystack = jd_text.casefold()
    present = {s.casefold() for s in skills}
    added: list[str] = []
    for term in _pool_terms():
        key = term.casefold()
        if key in present:
            continue
        # Word-boundary match so "R" does not fire on every word containing r
        # and "Go" does not fire inside "Google". A dot only blocks the match
        # when a word follows it, so "Node.js" is not matched by "Node" while
        # a term ending a sentence ("...and FastAPI.") still matches.
        if re.search(rf"(?<![\w+#.]){re.escape(key)}(?![\w+#]|\.\w)", haystack):
            added.append(term)
            present.add(key)
    if added:
        log.info("pool_skills_recovered", count=len(added), skills=added[:12])
    return skills + added


@lru_cache(maxsize=1)
def _vocabulary() -> tuple[tuple[str, str], ...]:
    """The learned technology vocabulary, loaded once per process.

    Opens its own session rather than taking one as an argument: `parse()` is
    called from inside the orchestrator's transaction, and threading a session
    through it only to read a small static table would couple Layer 3 to the
    caller's transaction for no benefit.
    """
    from src.state.db import session_scope
    from src.state.vocabulary import load_terms

    try:
        with session_scope() as session:
            return load_terms(session)
    except Exception as exc:  # noqa: BLE001 - a missing vocabulary must not fail a parse
        log.warning("vocabulary_unavailable", error=str(exc)[:160])
        return ()


def with_vocabulary_skills(skills: list[str], jd_text: str) -> list[str]:
    """Add technologies the JD names that the model failed to return.

    `with_pool_skills` covers the operator's own skills — the ones that decide
    the match score. This covers the rest: technologies outside the pool, which
    become the gap skills Familiar With is built from, and which the model drops
    just as readily. Measured 2026-08-19, LangChain, LangGraph, MLOps, NLP, RAG
    and Scikit were all in the JD text and missing from the parse.

    The vocabulary is learned from previously parsed ads (see
    `src.state.vocabulary`), so it needs no curated list and grows as the corpus
    does. Matching is literal and word-bounded, so this can only add something
    the ad actually says.
    """
    if not jd_text:
        return skills
    terms = _vocabulary()
    if not terms:
        return skills
    present = {s.casefold() for s in skills}
    added = [t for t in scan(jd_text, terms) if t.casefold() not in present]
    if added:
        log.info("vocabulary_skills_recovered", count=len(added), skills=added[:12])
    return skills + added


def apply_to_row(job: AllJobs, parsed: JDParsed) -> None:
    """Copy parsed fields onto the AllJobs row in-place (no I/O).

    ``apply_url`` has no ``all_jobs`` column — it's a transient
    notification hint the orchestrator reads off ``parsed`` in the same
    run, falling back to ``job.job_url``.
    """
    job.role_summary = parsed.role_summary
    job.role_category = parsed.role_category
    job.role_level = parsed.role_level
    job.years_required = parsed.years_required
    job.required_skills = parsed.required_skills
    job.nice_to_have = parsed.nice_to_have
    job.responsibilities = parsed.responsibilities
    job.team_or_product = parsed.team_or_product
    job.job_type = parsed.job_type
    job.location_type = parsed.location_type
    job.salary_min_lpa = parsed.salary_min_lpa
    job.salary_max_lpa = parsed.salary_max_lpa
    job.salary_currency = parsed.salary_currency


# ---------------------------------------------------------------------------
# Skill grounding (anti-fabrication on parser output)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _nlp():
    """Lazy-load + cache the spaCy model (used only for lemma fallback)."""
    import spacy

    return spacy.load(settings.spacy.model)


@lru_cache(maxsize=256)
def _jd_lemmas(jd_text: str) -> frozenset[str]:
    """Set of alphabetic lemmas in the JD (case-folded), via spaCy."""
    doc = _nlp()(jd_text)
    return frozenset(tok.lemma_.casefold() for tok in doc if tok.is_alpha)


def grounded_skills(skills: list[str], jd_text: str) -> list[str]:
    """Keep only skills actually present in ``jd_text``.

    Fast path: case-insensitive substring match (covers multi-word skills
    and acronyms as they appear). Fallback: every alphabetic token of the
    skill lemmatises to a lemma present in the JD (catches inflections like
    "pipelines"→"pipeline"). Order and de-duplication are preserved.
    """
    if not jd_text:
        return []
    haystack = jd_text.casefold()
    kept: list[str] = []
    seen: set[str] = set()
    jd_lemmas: frozenset[str] | None = None
    for skill in skills:
        s = skill.strip()
        key = s.casefold()
        if not s or key in seen:
            continue
        if key in haystack:
            kept.append(s)
            seen.add(key)
            continue
        # Lemma fallback only when the literal string isn't present.
        if jd_lemmas is None:
            jd_lemmas = _jd_lemmas(jd_text)
        tokens = [t.lemma_.casefold() for t in _nlp()(s) if t.is_alpha]
        if tokens and all(t in jd_lemmas for t in tokens):
            kept.append(s)
            seen.add(key)
    return kept

