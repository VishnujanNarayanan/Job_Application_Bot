"""Dictionary-only parse: a :class:`JDParsed` with no LLM call.

Built as separate steps so each can be tuned, tested or swapped alone::

    text      = clean_ad_text(job.jd_text)
    sections  = split_sections(text)              # src.llm.sections
    skills    = scan_skills(text, dictionary)
    req, nice = split_required_nice(skills, sections, rules)
    years     = extract_years(text, rules)
    duties    = extract_responsibilities(text, sections, rules)

Every pattern lives on :class:`DictionaryRules` rather than in a module
constant, so a test or a replay can try different headings or regexes by
passing a modified copy.

Fields the scorer does not read (#71: ``role_level``, ``role_category``,
``team_or_product``) are left at their empty values. ``role_level`` has a
schema default of "mid" via its validator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.parsing.modes import ParseMode

if TYPE_CHECKING:
    from src.llm.schemas import JDParsed
    from src.state.models import AllJobs


# ---------------------------------------------------------------------------
# The dictionary: the three term lists #71 scans with
# ---------------------------------------------------------------------------


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
        logged, never fatal -- the same rule ``parser._vocabulary`` follows."""
        raise NotImplementedError("#71: load dictionary sources")

    def terms(self) -> tuple[tuple[str, str], ...]:
        """All three merged and de-duplicated by key, longest first, in the
        ``(term, key)`` shape ``vocabulary.scan`` takes. Pool spelling wins a
        tie so the operator's own wording reaches the scorer."""
        raise NotImplementedError("#71: merge sources")


# ---------------------------------------------------------------------------
# Rules: every tunable pattern in one place
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DictionaryRules:
    """Patterns the dictionary parser uses. Defaults are #71's starting set."""

    #: Section categories (from ``sections.heading_category``) whose skills
    #: are nice-to-have rather than required.
    nice_categories: frozenset[str] = frozenset({"nice"})
    #: Inline markers that make a single line nice-to-have even inside a
    #: requirements section ("Kafka is a plus").
    nice_inline: re.Pattern[str] = re.compile(
        r"\b(a plus|nice to have|good to have|bonus|preferred|desirable|added advantage)\b",
        re.IGNORECASE,
    )
    #: Years of experience. Group 1 is the number; the smallest match wins
    #: when a ranged ad says "3-5 years".
    years: tuple[re.Pattern[str], ...] = (
        re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)", re.IGNORECASE),
        re.compile(r"(?:minimum|at least|min\.?)\s+(?:of\s+)?(\d{1,2})\s*(?:years?|yrs?)",
                   re.IGNORECASE),
    )
    years_cap: int = 30
    #: Section categories read as responsibilities.
    duty_categories: frozenset[str] = frozenset({"duty"})
    #: Character cap on ``role_summary`` (schema max is 1500).
    summary_chars: int = 1500
    extra: dict = field(default_factory=dict)


DEFAULT_RULES = DictionaryRules()


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def scan_skills(jd_text: str, dictionary: Dictionary) -> list[str]:
    """Every dictionary term the JD names, in order of first appearance.

    Delegates to ``vocabulary.scan`` (word-bounded, markdown-cleaned) over
    ``dictionary.terms()``; should also accept keyword-family variants so
    "RAG" is found as "retrieval-augmented generation" (``family_hit``).
    """
    raise NotImplementedError("#71: skill scan")


def split_required_nice(
    skills: list[str],
    jd_text: str,
    sections: tuple[str, list[tuple[str, str]]],
    rules: DictionaryRules = DEFAULT_RULES,
) -> tuple[list[str], list[str]]:
    """``(required, nice_to_have)``.

    A skill is nice-to-have when every mention of it sits in a
    ``rules.nice_categories`` section or on a line matching
    ``rules.nice_inline``; anything also named elsewhere is required (the
    schema drops nice entries that repeat a required one anyway).
    """
    raise NotImplementedError("#71: required / nice split")


def extract_years(jd_text: str, rules: DictionaryRules = DEFAULT_RULES) -> int:
    """Years demanded, 0 when the ad states none; capped at ``rules.years_cap``."""
    raise NotImplementedError("#71: years regex")


def extract_responsibilities(
    jd_text: str,
    sections: tuple[str, list[tuple[str, str]]],
    rules: DictionaryRules = DEFAULT_RULES,
) -> tuple[list[str], str]:
    """``(responsibilities, role_summary)``.

    Lines of the ``rules.duty_categories`` sections; with none, the JD text
    itself. ``role_summary`` is the same text clipped to ``rules.summary_chars``
    -- it feeds ``vec_match``, so it needs the role's content, not prose.
    """
    raise NotImplementedError("#71: responsibilities")


def build_parsed(job: AllJobs, dictionary: Dictionary, rules: DictionaryRules) -> JDParsed:
    """Run the steps above and assemble a validated :class:`JDParsed`."""
    raise NotImplementedError("#71: assemble JDParsed")


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


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
        raise NotImplementedError("#71: lazy dictionary load")

    def parse(self, job: AllJobs) -> JDParsed:
        return build_parsed(job, self.dictionary, self.rules)
