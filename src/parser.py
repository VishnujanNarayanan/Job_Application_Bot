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
from src.llm.schemas import JDParsed, _reject
from src.scorer.keywords import clean_ad_text, family_hit, literal_hit, norm
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

    # Stored adverts are markdown ("C\+\+", "end\-to\-end") with typographic
    # hyphens; every literal test below must see the plain text (#73).
    jd_text = clean_ad_text(job.jd_text or "")
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


def with_pool_skills(
    skills: list[str], jd_text: str, pool: tuple[str, ...] | None = None
) -> list[str]:
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
    terms = _pool_terms() if pool is None else tuple(
        sorted(pool, key=len, reverse=True)
    )
    if not jd_text or not terms:
        return skills
    haystack = clean_ad_text(jd_text).casefold()
    present = {s.casefold() for s in skills}
    added: list[str] = []
    for term in terms:
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


#: How close the words of a multi-word skill must sit to count as named together:
#: "AWS S3" is grounded by "S3 on AWS", not by "AWS" in one paragraph and "S3"
#: in another.
_NEAR_WINDOW = 6
_WORD = re.compile(r"[a-z0-9+#.]+")
_FILLER = frozenset(("and", "or", "of", "the", "a", "an", "in", "on", "for", "with", "to"))


def _stem(w: str) -> str:
    """Crude plural fold for the proximity check: "pipelines" ~ "pipeline"."""
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _words_near(skill_norm: str, jd_words: list[str]) -> bool:
    """Every content word of a 2-4 word skill appears within ``_NEAR_WINDOW``
    words of the others in the advert, in any order."""
    words = [_stem(w.strip(".")) for w in _WORD.findall(skill_norm)]
    need = {w for w in words if w and w not in _FILLER}
    if not 2 <= len(need) <= 4:
        return False
    stems = [_stem(w) for w in jd_words]
    for i, w in enumerate(stems):
        if w in need and need <= set(stems[i:i + _NEAR_WINDOW]):
            return True
    return False


#: Vendor prefixes and the names an advert may use for the same vendor.
_VENDORS = {
    "aws": ("aws", "amazon"), "amazon": ("aws", "amazon"),
    "azure": ("azure", "microsoft"), "microsoft": ("azure", "microsoft"),
    "gcp": ("gcp", "google"), "google": ("gcp", "google"),
    "apache": ("apache",),
}


def _vendor_split(skill: str) -> tuple[tuple[str, ...], str] | None:
    """("AWS Glue") -> (("aws", "amazon"), "Glue"); None without a vendor prefix."""
    head, _, rest = skill.partition(" ")
    names = _VENDORS.get(head.casefold())
    return (names, rest.strip()) if names and rest.strip() else None


def _substring_safe(skill: str) -> bool:
    """Whether a raw substring test can ground ``skill`` without matching inside
    an unrelated word: a multi-word phrase, a word of six or more characters, or
    a non-Latin term (which ``norm`` would erase entirely, e.g. 数据结构)."""
    if not re.search(r"[A-Za-z0-9]", skill):
        return True
    return len(skill.split()) >= 2 or len(skill) >= 6


def grounded_skills(skills: list[str], jd_text: str) -> list[str]:
    """Keep only skills actually present in ``jd_text``.

    The model is told to copy skills verbatim, but this is what keeps an
    invented skill out of the scoring inputs. A skill is kept when the advert,
    with markdown escapes and typographic hyphens undone (``clean_ad_text``):

      1. names it through ``keywords.family_hit``: a word-boundary match plus
         the spelling and keyword-family rules the scorer uses, so "RAG"
         grounds "retrieval-augmented generation" -- and "Java" is NOT
         grounded by "JavaScript", which the old raw-substring test allowed;
      2. for a single all-letter word, holds it as a spaCy lemma (inflections:
         "pipelines" -> "pipeline");
      3. names every word of a 2-4 word skill close together, in any order
         ("S3 on AWS" grounds "AWS S3");
      4. names a vendor-prefixed skill's service and, anywhere, its vendor
         ("Glue" plus "AWS" grounds "AWS Glue"; AWS and Amazon are one vendor);
      5. contains it as a raw substring, for phrases, long words and non-Latin
         terms only -- scraped lists sometimes lose their separators
         ("MinitabCAD"), but for short tokens a substring is how "AWS" matched
         "laws" (``_substring_safe``).

    Anything else is dropped and recorded as an ``ungrounded`` rejection, which
    ``parse_eval`` keeps, so a new pattern of real skills being dropped shows
    up in the audit rather than in a lower fit score.

    Before #73 grounding was a raw substring test plus a lemma test over the
    whole advert, run on the raw markdown, so a
    skill written with a hyphen, plus or dot ("scikit-learn", "C++",
    "end-to-end testing") failed against "scikit\\-learn" on 99% of adverts.
    Order and de-duplication are preserved.
    """
    if not jd_text:
        return []
    clean = clean_ad_text(jd_text)
    haystack = clean.casefold()
    jd_norm = norm(clean)
    kept: list[str] = []
    seen: set[str] = set()
    jd_lemmas: frozenset[str] | None = None
    jd_words: list[str] | None = None
    for skill in skills:
        s = clean_ad_text(skill).strip()
        key = s.casefold()
        if not s or key in seen:
            continue
        ok = family_hit(s, jd_norm)
        if not ok and _substring_safe(s):
            # Scraped list items sometimes lose their separators
            # ("ApacheAirflowCommunication Skills", "MinitabCAD"), which no
            # word-boundary match can see. A raw substring is safe for a phrase
            # or a long word; for a short token it is how "AWS" matched "laws"
            # and "excel" matched "excellence", so those stay boundary-only.
            ok = s.casefold() in haystack
        if not ok and (split := _vendor_split(s)):
            # Adverts list a vendor's services under one mention of the vendor
            # ("AWS cloud services including S3, EC2, Glue, Lambda"), so "AWS
            # Glue" is named even though the two words never touch.
            names, service = split
            ok = (family_hit(service, jd_norm)
                  or (_substring_safe(service) and service.casefold() in haystack)
                  ) and any(literal_hit(n, jd_norm) for n in names)
        if not ok and re.fullmatch(r"[A-Za-z]+", s):
            # Lemma fallback for one plain word only: across several words it
            # accepted a phrase whose words sat paragraphs apart, and it skipped
            # non-letter tokens, so "AWS S3" passed on "AWS" alone.
            if jd_lemmas is None:
                jd_lemmas = _jd_lemmas(clean)
            tokens = [t.lemma_.casefold() for t in _nlp()(s) if t.is_alpha]
            ok = bool(tokens) and all(t in jd_lemmas for t in tokens)
        if not ok:
            if jd_words is None:
                jd_words = [w.strip(".") for w in _WORD.findall(jd_norm)]
            ok = _words_near(norm(s), jd_words)
        if ok:
            kept.append(s)
            seen.add(key)
        else:
            _reject(skill, "ungrounded")  # logged and kept for parse_eval
    return kept
