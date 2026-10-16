"""Dictionary-only parse: a :class:`JDParsed` with no LLM call.

Built as separate steps so each can be tuned, tested or swapped alone::

    text      = clean_ad_text(job.jd_text)
    sections  = split_sections(text)              # src.llm.sections
    skills    = scan_skills(text, dictionary)
    req, nice = split_required_nice(skills, text, sections, rules)
    years     = extract_years(sections, rules)
    job_type  = extract_job_type(job.role, text)
    duties    = extract_responsibilities(text, sections, rules)

The tunable values live on :class:`DictionaryRules`, so a test or a replay
can pass a modified copy.

Fields the scorer does not read (#71: ``role_level``, ``role_category``,
``team_or_product``) are left empty; ``role_level`` reads "mid" like an
unstated level does on the LLM path. Salary and location type are not
extracted.

The years and job-type rules were tuned on every disagreement with the LLM
in the 2026-10-16 replay of 1,323 parsed jobs. Years: the ceiling verdict
matches the LLM's on 95% of jobs (19 of the misses are the LLM reading "0"
where the ad says "7+ years"). Job type: 45 of the LLM's 50 non-full-time
ads caught, and 12 of the 19 extra flags have the type in the title, which
the LLM missed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from src.parsing.modes import ParseMode

if TYPE_CHECKING:
    from src.llm.schemas import JDParsed
    from src.state.models import AllJobs


# ---------------------------------------------------------------------------
# The dictionary: the three term lists #71 scans with
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _family_terms() -> tuple[str, ...]:
    """Every head and variant in config/keyword_families.yaml."""
    path = Path(__file__).resolve().parents[2] / "config" / "keyword_families.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return tuple(
        t for fam in (raw or {}).get("families") or []
        for t in (fam["head"], *(fam.get("variants") or []))
    )


@dataclass(frozen=True)
class Dictionary:
    """The skill terms a JD is scanned for.

    * ``vocabulary`` -- learned terms, ``(term, key)`` longest first, as
      ``src.state.vocabulary.load_terms`` returns them.
    * ``pool``       -- the operator's skills_pool, as ``parser._pool_terms``.
    * ``families``   -- every head and variant in config/keyword_families.yaml.
    """

    vocabulary: tuple[tuple[str, str], ...] = ()
    pool: tuple[str, ...] = ()
    families: tuple[str, ...] = ()

    @classmethod
    def load(cls) -> Dictionary:
        """Load all three sources. A source that fails to load is empty and
        logged by its loader, never fatal."""
        from src.parser import _pool_terms, _vocabulary

        return cls(vocabulary=_vocabulary(), pool=_pool_terms(), families=_family_terms())

    def terms(self) -> tuple[tuple[str, str], ...]:
        """All three merged and de-duplicated by key, longest first, in the
        ``(term, key)`` shape ``vocabulary.scan`` takes. Pool spelling wins a
        tie so the operator's own wording reaches the scorer."""
        merged: dict[str, str] = {}
        for term in (*self.pool, *(t for t, _ in self.vocabulary), *self.families):
            term = term.strip()
            if term:
                merged.setdefault(term.casefold(), term)
        return tuple(sorted(((t, k) for k, t in merged.items()),
                            key=lambda p: len(p[1]), reverse=True))


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DictionaryRules:
    """Tunable values for the dictionary parser."""

    #: Section categories (``sections.heading_category``) whose skills are
    #: nice-to-have rather than required.
    nice_categories: frozenset[str] = frozenset({"nice"})
    #: A line matching this is nice-to-have even inside a requirements
    #: section ("Kafka is a plus").
    nice_inline: re.Pattern[str] = re.compile(
        r"\b(a plus|plus point|nice to have|good to have|bonus|preferred|desirable"
        r"|added advantage)\b", re.IGNORECASE)
    #: Section categories read for years: not nice-to-have, company
    #: boilerplate or pay.
    years_skip_categories: frozenset[str] = frozenset({"drop", "nice", "pay"})
    years_cap: int = 30
    #: Section categories read as responsibilities.
    duty_categories: frozenset[str] = frozenset({"duty"})
    #: Character cap on ``role_summary`` (schema max is 1500).
    summary_chars: int = 1500
    extra: dict = field(default_factory=dict)


DEFAULT_RULES = DictionaryRules()

Sections = tuple[str, list[tuple[str, str]]]


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def scan_skills(jd_text: str, dictionary: Dictionary) -> list[str]:
    """Every dictionary term the JD names, in order of first appearance.

    ``vocabulary.scan``: word-bounded and markdown-cleaned. Family variants
    are in ``dictionary.terms()``, so "retrieval-augmented generation" is
    found as itself.
    """
    from src.state.vocabulary import scan

    return scan(jd_text, dictionary.terms())


def split_required_nice(
    skills: list[str],
    jd_text: str,
    sections: Sections,
    dictionary: Dictionary,
    rules: DictionaryRules = DEFAULT_RULES,
) -> tuple[list[str], list[str]]:
    """``(required, nice_to_have)``.

    A skill is required if it is named anywhere outside a nice-to-have
    section and off a "nice to have" line; otherwise it is nice-to-have.
    """
    from src.scorer.keywords import canonical_key
    from src.state.vocabulary import scan

    preamble, secs = sections
    rest = [preamble] + [body for cat, body in secs if cat not in rules.nice_categories]
    rest_lines = [ln for ln in "\n".join(rest).splitlines() if not rules.nice_inline.search(ln)]
    required_keys = {canonical_key(s) for s in scan("\n".join(rest_lines), dictionary.terms())}
    required = [s for s in skills if canonical_key(s) in required_keys]
    nice = [s for s in skills if canonical_key(s) not in required_keys]
    return required, nice


# ---------------------------------------------------------------------------
# Years
# ---------------------------------------------------------------------------

_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen"
    " fourteen fifteen".split())}
_NUM = r"(\d{1,2}(?:\.\d)?|" + "|".join(_WORDS) + r")"
#: "7+ years", "seven (7) years", "15 or more years", "3-5 years",
#: "4.5-8 years", "Minimum 3 Year(s)".
_YEARS = re.compile(
    rf"(?<![\d.$₹]){_NUM}(?:\s*\(\s*\d{{1,2}}\s*\))?\s*(?:\+|plus|or more|or above)?\s*"
    rf"(?:(?:-|–|to)\s*{_NUM}\s*\+?\s*)?(?:years?|yrs?|year\(s\))(?=\W|$)", re.IGNORECASE)
#: "8+ experience": the ad dropped the word "years".
_YEARS_BARE = re.compile(rf"(?<![\d.$₹]){_NUM}\s*\+\s*(?:of\s+)?(?:\w+\s+)?experience",
                         re.IGNORECASE)
#: A number preceded by one of these is a cap, not a requirement.
_CAP = re.compile(r"(maximum|max\.?|up to|not more than)\s*(of\s*)?$", re.IGNORECASE)
#: Company history and other years that are not experience.
_NOT_EXPERIENCE = re.compile(
    r"founded|over the (past|last)|(has|have) been (operating|in business|around|serving"
    r"|helping|delivering|innovating|revolutionizing|a leader)|we.?ve been|our (company|firm"
    r"|history)|in business|legacy of|since \d{4}|years? (old|ago|history)|anniversary"
    r"|warranty|extensions? up to|consecutive years|(full.time|formal) education"
    r"|years? of (expertise|excellence|experience (enabling|helping|serving|delivering))"
    r"|(is|as) an? (global|leading|industry)|for (more than|over|nearly|almost) \d+ years",
    re.IGNORECASE)
#: A line offering alternatives ("Bachelor's + 5, or 7 without a degree").
_ALTERNATIVES = re.compile(
    r"\bor\b(?! (more|above|equivalent|related|similar))|in lieu|equivalent"
    r"|with a (phd|master)|master.?s degree (and|with)|phd (and|with)", re.IGNORECASE)


def _number(token: str) -> int:
    return int(float(token)) if token[0].isdigit() else _WORDS[token.casefold()]


def extract_years(sections: Sections, rules: DictionaryRules = DEFAULT_RULES) -> int | None:
    """The overall years of experience the ad demands; None if it states none.

    Read from the preamble and the sections not in ``rules.years_skip_categories``,
    skipping company-history lines. On a line offering alternatives the
    smallest number counts, as the LLM reads it; otherwise the line's
    largest. Across lines the largest wins: the overall requirement beats
    "2+ in Spark". A range counts by its lower bound.
    """
    preamble, secs = sections
    text = "\n".join([preamble] + [b for c, b in secs if c not in rules.years_skip_categories])
    per_line: list[int] = []
    for line in text.splitlines():
        if _NOT_EXPERIENCE.search(line):
            continue
        found = [_number(m.group(1)) for m in _YEARS.finditer(line)
                 if not _CAP.search(line[:m.start()])]
        found += [_number(m.group(1)) for m in _YEARS_BARE.finditer(line)]
        found = [v for v in found if 0 < v <= rules.years_cap]
        if found:
            per_line.append(min(found) if len(found) > 1 and _ALTERNATIVES.search(line)
                            else max(found))
    return max(per_line) if per_line else None


# ---------------------------------------------------------------------------
# Job type
# ---------------------------------------------------------------------------

_T_INTERN = re.compile(r"\bintern(ship)?s?\b", re.IGNORECASE)
_T_CONTRACT = re.compile(r"\bcontract(ual|or|ors)?\b|\bc2h\b|\bfreelance\b|\btemporary\b",
                         re.IGNORECASE)
_T_PART = re.compile(r"\bpart.time\b", re.IGNORECASE)
_TYPES_LIST = re.compile(r"(job|employment|position|work)\s*types?\s*[:\-]\s*(.+)", re.IGNORECASE)
_FULLTIME = re.compile(r"full.time|permanent", re.IGNORECASE)
_BARE_CONTRACT = frozenset({"contract", "contractual", "contract role", "contract position",
                            "contract opportunity", "temporary", "freelance", "contract to hire"})
_BARE_INTERN = frozenset({"internship", "internship details", "internship program",
                          "internship overview"})
_LINE_CONTRACT = re.compile(
    r"\|\s*\**contract (opportunity|role|position)\**|duration\s*:\s*short.term"
    r"|-\s*contract\s*$|duration\W{0,6}.{0,20}\bcontract\b", re.IGNORECASE)
_ROLE_INTERN = re.compile(
    r"\binterns will\b|(seeking|looking for|hiring) an? ([\w&/()-]+ ){0,5}intern\b"
    r"|\binternship (program|opportunity|position|role)\b|this internship"
    r"|(this|the) (is an? )?(\w+ ){0,2}intern(ship)? (position|role)|\*\*[^*]{0,60}\bintern\*\*",
    re.IGNORECASE)
_ROLE_PART = re.compile(
    r"(is|this) an? (\w+ ){0,2}part.time (\w+ ){0,2}(role|position|job|opportunity)"
    r"|part.time (hours|role|position|basis)", re.IGNORECASE)
_ROLE_CONTRACT = re.compile(
    r"(this|the) (is an? )?(\w+ ){0,3}(contract(.to.hire)?|contractor|freelance|temporary)"
    r"( |-)(position|role|opportunity|engagement|basis|assignment)"
    r"|on an? (\w+ )?contract(ual|or)? basis|contract.to.hire|\bc2h\b"
    r"|hourly(.paid)? contract(or)?|duration\s*[:\-]?\s*\d+.{0,12}(months?|weeks?)"
    r"|\d+.?months? (contract|engagement|assignment)"
    r"|(seeking|looking for|hiring) an? ([\w&/-]+ ){0,5}contractor\b"
    r"|employment type\s*[:\-]\s*contract", re.IGNORECASE)
#: How far into the ad bare headings are looked for.
_HEADING_LINES = 400


def extract_job_type(title: str | None, jd_text: str) -> str | None:
    """"internship" | "contract" | "parttime" | "fulltime", or None if unstated.

    In order: the title; an explicit "Job Types:" list (full-time offered
    means it passes); a bare heading line ("**Contract**", "Internship
    Details", a short "... INTERN" line); then sentences about this role
    ("this is a contract role", "on a contractor basis", "a part-time remote
    role"). Legal boilerplate ("Service Contract Act", "union contract") is
    not matched because only those role-shaped phrases are.
    """
    t = title or ""
    if _T_INTERN.search(t):
        return "internship"
    if _T_PART.search(t):
        return "parttime"
    if _T_CONTRACT.search(t):
        return "contract"
    for m in _TYPES_LIST.finditer(jd_text):
        listed = m.group(2)[:120]
        if _FULLTIME.search(listed):
            return "fulltime"
        if _T_INTERN.search(listed):
            return "internship"
        if _T_PART.search(listed):
            return "parttime"
        if _T_CONTRACT.search(listed):
            return "contract"
    for line in jd_text.splitlines()[:_HEADING_LINES]:
        bare = re.sub(r"[*#_|:\-\s]+", " ", line).strip().casefold()
        if bare in _BARE_CONTRACT or _LINE_CONTRACT.search(line):
            return "contract"
        if bare in ("part time", "part-time"):
            return "parttime"
        if bare in _BARE_INTERN or (0 < len(bare.split()) <= 6 and _T_INTERN.search(bare)):
            return "internship"
    if _ROLE_INTERN.search(jd_text):
        return "internship"
    if _ROLE_PART.search(jd_text):
        return "parttime"
    if _ROLE_CONTRACT.search(jd_text):
        return "contract"
    return None


# ---------------------------------------------------------------------------
# Responsibilities
# ---------------------------------------------------------------------------


def extract_responsibilities(
    jd_text: str, sections: Sections, rules: DictionaryRules = DEFAULT_RULES
) -> tuple[list[str], str]:
    """``(responsibilities, role_summary)``.

    Lines of the ``rules.duty_categories`` sections, headings dropped; with
    none, the ad text itself. ``role_summary`` is the same text clipped to
    ``rules.summary_chars``: it feeds ``vec_match``, so it needs the role's
    content, not prose.
    """
    _, secs = sections
    duties = [ln.strip(" -*•") for cat, body in secs if cat in rules.duty_categories
              for ln in body.splitlines()[1:] if ln.strip(" -*•")]
    summary = ("\n".join(duties) if duties else jd_text)[:rules.summary_chars]
    return (duties or [jd_text[:3000]]), summary


# ---------------------------------------------------------------------------
# Assembly + strategy
# ---------------------------------------------------------------------------


def build_parsed(job: AllJobs, dictionary: Dictionary,
                 rules: DictionaryRules = DEFAULT_RULES) -> JDParsed:
    """Run the steps above and assemble a :class:`JDParsed`."""
    from src.llm.schemas import JDParsed
    from src.llm.sections import split_sections
    from src.scorer.keywords import clean_ad_text

    text = clean_ad_text(job.jd_text or "")
    sections = split_sections(text)
    skills = scan_skills(text, dictionary)
    required, nice = split_required_nice(skills, text, sections, dictionary, rules)
    responsibilities, summary = extract_responsibilities(text, sections, rules)
    job_type = extract_job_type(job.role, text)
    parsed = JDParsed.model_validate(dict(
        role_summary=summary or job.role or "role",
        role_category="",
        role_level=None,
        years_required=extract_years(sections, rules) or 0,
        required_skills=required,
        nice_to_have=nice,
        responsibilities=responsibilities,
        job_type=job_type,
    ))
    # Validation re-splits and de-duplicates skill lists; the scan's output is
    # already clean terms, so keep it verbatim, as `tailor.parsed_from_row`
    # does for stored parses.
    parsed.required_skills = required
    parsed.nice_to_have = nice
    return parsed


class DictionaryParser:
    """The ``dictionary`` mode. Holds its dictionary and rules so a replay can
    build one with a test dictionary or tweaked rules and call ``parse``."""

    mode = ParseMode.DICTIONARY

    def __init__(self, dictionary: Dictionary | None = None,
                 rules: DictionaryRules = DEFAULT_RULES) -> None:
        self._dictionary = dictionary
        self.rules = rules

    @property
    def dictionary(self) -> Dictionary:
        """Loaded on first use, once per instance."""
        if self._dictionary is None:
            self._dictionary = Dictionary.load()
        return self._dictionary

    def parse(self, job: AllJobs) -> JDParsed:
        return build_parsed(job, self.dictionary, self.rules)
