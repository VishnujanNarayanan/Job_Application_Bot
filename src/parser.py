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

from collections.abc import Callable
from functools import lru_cache

from src.config import settings
from src.llm.client import complete as _default_complete
from src.llm.prompts import jd_parse_prompt, jd_parse_system
from src.llm.schemas import JDParsed
from src.state.models import AllJobs

# Type of the LLM transport (injectable for tests).
CompleteFn = Callable[..., JDParsed]


def parse(job: AllJobs, *, complete: CompleteFn | None = None) -> JDParsed:
    """Run Gemini Call 1a for ``job`` and return a grounded :class:`JDParsed`."""
    run = complete or _default_complete
    prompt = jd_parse_prompt(job)
    parsed: JDParsed = run(JDParsed, prompt, system=jd_parse_system())

    jd_text = job.jd_text or ""
    parsed.required_skills = grounded_skills(parsed.required_skills, jd_text)
    parsed.nice_to_have = grounded_skills(parsed.nice_to_have, jd_text)
    return parsed


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

