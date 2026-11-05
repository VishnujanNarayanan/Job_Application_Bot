"""The skills registry (#188): which terms may become JD keywords.

Before this, any word could become a keyword. The learned vocabulary, the LLM
parse and the skills pool each added terms with no check that they were
skills, and the vocabulary counted its own additions on every rebuild.
Measured 2026-11-05 on 1,755 match resumes: about 4 in 10 bolds were not
skills (application, team, client, quality, task...), and they also earned
weak bullets their place.

Every term now has one reviewed label (``config/skills_registry.yaml``, plus
``skills_registry.local.yaml`` for terms only in the operator's pool):

    approve     a specific skill or tool                 -> keyword
    sense       a skill whose name also has another       -> keyword only where
                meaning (Make, Go, Excel, Testing)           its sense is shown
    reject      never a skill alone (application, team)   -> never a keyword
    domain      an industry or a whole field of work      -> never a keyword
    soft        soft skills, spoken languages             -> never a keyword
    credential  degrees, certifications                   -> never a keyword

A term the registry does not know is ``pending``: it is kept as today until a
review labels it (the weekly review queue is the next step of #188).

Lookups use the matcher's own key (``keywords.phrase_key``), so "Teams",
"teams" and "team" are one entry: the bolder folds plurals, and the 2026-11-05
audit found 37% of junk bolds were singular forms of registry terms.

How a sense term proves its sense, in order (all of it data in the registry):

1. ``qualifiers``: regular expressions, any match in the advert counts
   ("Make.com", "Microsoft Teams"). ``qualified_only`` makes them the only way.
2. Tool casing: a term written with capitals ("Go", "Excel", "React") counts
   where the advert writes it in that exact casing, not at the start of a line
   or sentence ("Go through", "Excel at" are sentence starts).
3. A term written in lower case ("testing", "pipelines") counts in a
   requirements section or next to an approved term in a list ("Python,
   testing, CI/CD").
4. ``requirement_line``: whichever of 2-3 applies, the occurrence must also sit
   in a requirements or nice-to-have section when the advert has sections
   (keeps "a leader in AI" in the company intro out).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import structlog
import yaml

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
PUBLIC = _ROOT / "config" / "skills_registry.yaml"
LOCAL = _ROOT / "config" / "skills_registry.local.yaml"

APPROVE, SENSE, REJECT, DOMAIN, SOFT, CREDENTIAL, PENDING = (
    "approve", "sense", "reject", "domain", "soft", "credential", "pending")
LABELS = frozenset({APPROVE, SENSE, REJECT, DOMAIN, SOFT, CREDENTIAL})
#: Labels that never become a keyword.
BLOCKED = frozenset({REJECT, DOMAIN, SOFT, CREDENTIAL})

# Bullet markers and numbering at the start of a line are not words.
_LEAD = re.compile(r"^[\s*\-•·#>\d.)(]+")
_SENTENCE_END = re.compile(r"[.!?:]\s*$")


@dataclass(frozen=True)
class Entry:
    term: str
    label: str
    note: str = ""
    qualifiers: tuple[re.Pattern[str], ...] = ()
    qualified_only: bool = False
    requirement_line: bool = False


#: When two registry terms fold to one matching key ("React" / "React.js"),
#: the more restrictive label answers for the folded key.
_STRICTNESS = {REJECT: 0, SOFT: 0, CREDENTIAL: 0, DOMAIN: 0, SENSE: 1, APPROVE: 2}


@dataclass(frozen=True)
class Registry:
    #: Exact entries by lower-cased term, and the fallback by matching key.
    exact: dict[str, Entry] = field(default_factory=dict)
    folded: dict[str, Entry] = field(default_factory=dict)

    def entry(self, term: str) -> Entry | None:
        """The term's own entry, else the entry of its spelling family
        ("team" -> "Teams")."""
        from src.scorer.keywords import phrase_key

        t = term.strip()
        return self.exact.get(t.casefold()) or self.folded.get(phrase_key(t))

    def label(self, term: str) -> str:
        e = self.entry(term)
        return e.label if e else PENDING

    def admits(self, term: str, jd_text: str | None = None) -> bool:
        """Whether ``term`` may be a keyword of the advert ``jd_text``.

        A sense term needs the advert text to show its sense, so without it
        the term is not admitted."""
        e = self.entry(term)
        if e is None:
            return True  # pending: unchanged until reviewed
        if e.label == APPROVE:
            return True
        if e.label in BLOCKED:
            return False
        return bool(jd_text) and sense_shown(e, jd_text)


def _load_file(path: Path) -> dict[str, tuple[Entry, bool]]:
    """{lower-cased term: (entry, set by the operator)}."""
    if not path.exists():
        return {}
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("terms") or {}
    out: dict[str, tuple[Entry, bool]] = {}
    for key, e in raw.items():
        label = str(e.get("label", "")).strip()
        if label not in LABELS:
            raise ValueError(f"{path.name}: {key!r} has unknown label {label!r}")
        term = str(e.get("term") or key).strip()
        out[term.casefold()] = (Entry(
            term=term,
            label=label,
            note=str(e.get("note") or ""),
            qualifiers=tuple(re.compile(q) for q in e.get("qualifiers") or ()),
            qualified_only=bool(e.get("qualified_only")),
            requirement_line=bool(e.get("requirement_line")),
        ), e.get("by") == "operator")
    return out


def build(raw: dict[str, tuple[Entry, bool]]) -> Registry:
    from src.scorer.keywords import phrase_key

    exact = {k: e for k, (e, _op) in raw.items()}
    folded: dict[str, tuple[Entry, bool]] = {}
    for e, by_operator in raw.values():
        k = phrase_key(e.term)
        cur = folded.get(k)
        if cur is None or (by_operator, -_STRICTNESS[e.label]) > (cur[1], -_STRICTNESS[cur[0].label]):
            folded[k] = (e, by_operator)
    return Registry(exact, {k: e for k, (e, _op) in folded.items()})


@lru_cache(maxsize=1)
def load() -> Registry:
    """The committed registry with the local overlay on top. A missing file is
    an empty registry (every term pending, today's behaviour), logged once."""
    raw = _load_file(PUBLIC)
    raw.update(_load_file(LOCAL))
    if not raw:
        log.warning("skills_registry_empty", path=str(PUBLIC))
    return build(raw)


def enabled() -> bool:
    from src.config import settings

    reg = settings.scoring.get("registry")
    return bool(reg.get("enabled", True)) if reg is not None else True


def admits(term: str, jd_text: str | None = None) -> bool:
    """``load().admits`` when the registry is enabled, else always True."""
    return load().admits(term, jd_text) if enabled() else True


def admitted(terms, jd_text: str | None = None) -> list[str]:
    """``terms`` that may be keywords, order kept."""
    if not enabled():
        return list(terms)
    reg = load()
    return [t for t in terms if reg.admits(t, jd_text)]


# --- sense rules ------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _requirement_text(jd_text: str) -> str | None:
    """The advert's requirements and nice-to-have sections, or None when it has
    no recognised sections (then every line counts)."""
    from src.llm.sections import split_sections

    _pre, secs = split_sections(jd_text)
    if not secs:
        return None
    return "\n".join(body for cat, body in secs if cat in ("req", "nice"))


def _occurrences(term: str, text: str, *, exact_case: bool):
    """(text before the match on its line, text after it, the whole line)."""
    flags = 0 if exact_case else re.IGNORECASE
    pat = re.compile(rf"(?<![\w+#.-]){re.escape(term)}(?![\w+#-]|\.\w)", flags)
    for m in pat.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start: line_end if line_end >= 0 else len(text)]
        yield text[line_start:m.start()], line[m.end() - line_start:], line


def _sentence_start(before: str) -> bool:
    lead = _LEAD.sub("", before)
    return not lead.strip() or bool(_SENTENCE_END.search(lead))


#: What may follow a tool name that starts a line or sentence when it is a
#: list item, not a verb: "- Rust and WebAssembly", "Excel (pivot tables)",
#: "Go: 3+ years". "Excel at", "Make sure", "Go through" do not qualify.
_ITEM_FOLLOW = re.compile(r"^\s*(?:$|[,/;:(|&\-–]|\.\s*$|(?:and|or)\b(?!\s+(?:a|an|the)\b))", re.IGNORECASE)


_ITEM_SPLIT = re.compile(r"\s*(?:[,/;|&]|\band\b|\bor\b)\s*", re.IGNORECASE)


def _listed_with_approved(term: str, line: str) -> bool:
    """The term is an item of a list ("Python, testing, CI/CD") whose next or
    previous item is an approved term."""
    items = [i.strip(" ()[]:*.-") for i in _ITEM_SPLIT.split(line)]
    if len(items) < 2:
        return False
    reg = load()
    key = term.casefold()
    for i, item in enumerate(items):
        if item.casefold() != key:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(items) and items[j] and len(items[j]) <= 40:
                e = reg.entry(items[j])
                if e is not None and e.label == APPROVE:
                    return True
    return False


def sense_shown(e: Entry, jd_text: str) -> bool:
    """Whether the advert uses the sense term ``e`` in its skill sense."""
    from src.scorer.keywords import clean_ad_text

    text = clean_ad_text(jd_text)
    if any(q.search(text) for q in e.qualifiers):
        return True
    if e.qualified_only:
        return False
    req = _requirement_text(text)
    where = req if (e.requirement_line and req is not None) else text
    # Casing is evidence only for tool names ("Go", "Excel"); a concept word
    # checked by its requirements line ("Testing", "AI") matches in any case.
    has_caps = any(c.isupper() for c in e.term) and not e.requirement_line
    for before, after, line in _occurrences(e.term, where, exact_case=has_caps):
        if has_caps:
            # Tool casing, not starting a line or sentence ("Go through", "Excel
            # at"), unless it is a list item there ("- Rust and WebAssembly").
            if not _sentence_start(before) or _ITEM_FOLLOW.match(after):
                return True
        elif req is None or where is req or line.strip() in req:
            # A lower-case term counts on a requirements line (any line when the
            # advert has no sections) ...
            return True
        elif _listed_with_approved(e.term, line):
            # ... or listed beside an approved skill anywhere.
            return True
    return False
