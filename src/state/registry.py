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

A sense term counts only where the advert uses it as a skill, decided from
each occurrence's situation (list neighbours, part of speech, nearby skills,
section), never from a list of phrasings: see ``sense_shown``.
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



@dataclass(frozen=True)
class Entry:
    term: str
    label: str
    note: str = ""
    #: The skill(s) this term means, written by hand ("Agile/Scrum
    #: methodologies" -> Agile, Scrum; "AWS Glue" -> Glue). Empty: itself.
    means: tuple[str, ...] = ()


#: When two registry terms fold to one matching key ("React" / "React.js"),
#: the more restrictive label answers for the folded key.
_STRICTNESS = {REJECT: 0, SOFT: 0, CREDENTIAL: 0, DOMAIN: 0, SENSE: 1, APPROVE: 2}


@dataclass(frozen=True)
class Registry:
    #: Exact entries by lower-cased term, and the fallback by matching key.
    exact: dict[str, Entry] = field(default_factory=dict)
    folded: dict[str, Entry] = field(default_factory=dict)
    #: Skill (lower-cased) -> every term that means it, the skill included.
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def skills(self, term: str) -> tuple[str, ...]:
        """The skill(s) ``term`` means: its hand-written mapping, else itself."""
        e = self.entry(term)
        return e.means if e is not None and e.means else (term,)

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
            return pending_admitted()  # not reviewed yet
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
            means=tuple(str(m) for m in e.get("means") or ()),
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
    aliases: dict[str, set[str]] = {}
    for e, _op in raw.values():
        # Only an approved term proves a skill in a profile line: "Word",
        # "backup", "TURN" are ordinary words there.
        if e.label != APPROVE:
            continue
        for skill in e.means or (e.term,):
            aliases.setdefault(skill.casefold(), {skill}).add(e.term)
    return Registry(exact, {k: e for k, (e, _op) in folded.items()},
                    {k: tuple(sorted(v)) for k, v in aliases.items()})


@lru_cache(maxsize=1)
def load() -> Registry:
    """The committed registry with the local overlay on top. A missing file is
    an empty registry (every term pending, today's behaviour), logged once."""
    raw = _load_file(PUBLIC)
    raw.update(_load_file(LOCAL))
    if not raw:
        log.warning("skills_registry_empty", path=str(PUBLIC))
    return build(raw)


def pending_admitted() -> bool:
    """``scoring.registry.pending``: "keep" (default) treats a term the registry
    does not know as a keyword until it is reviewed; "drop" leaves it out."""
    from src.config import settings

    reg = settings.scoring.get("registry")
    return str(reg.get("pending", "keep") if reg is not None else "keep") != "drop"


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


def skills_of(terms, jd_text: str | None = None) -> list[str]:
    """The advert's terms as the skills they mean, admitted terms only, each
    skill once, in first-mention order.

    The mapping is written by hand in the registry ("CI/CD practices" ->
    CI/CD, "Agile/Scrum methodologies" -> Agile + Scrum, "AWS Glue" -> Glue),
    so a skill is one keyword however the advert spells it. A mapped skill
    the registry blocks (a domain such as responsible AI) is not a keyword."""
    if not enabled():
        return list(terms)
    reg = load()
    out: list[str] = []
    seen: set[str] = set()
    for t in terms:
        if not reg.admits(t, jd_text):
            continue
        for skill in reg.skills(t):
            e = reg.entry(skill)
            if e is not None and e.label in BLOCKED:
                continue
            k = skill.casefold()
            if k not in seen:
                seen.add(k)
                out.append(skill)
    return out


def aliases(skill: str) -> tuple[str, ...]:
    """Every term the registry maps to ``skill`` ("Glue" -> AWS Glue, Amazon
    Glue...), so a profile line in any of those spellings shows the skill."""
    if not enabled():
        return ()
    return load().aliases.get(skill.casefold(), ())


# --- sense rules -----------------------------------------------------------------------
#
# A sense term ("Make", "Go", "Excel", "Teams", "Testing", "AI") counts only
# where the advert uses it as a skill. The decision reads the situation of each
# occurrence, never a list of phrasings, so it holds for adverts not yet seen:
#
#   1. listed beside an approved skill ("n8n, Make, Zapier", "Exchange,
#      SharePoint and Teams")                                    -> skill sense
#   2. otherwise not used as a verb, adverb or preposition ("make sure", "excel
#      at", "go through", "react quickly", "harness the power"; spaCy's part of
#      speech), and at least one of: an approved skill in the same sentence, a
#      requirements/skills section, or a capitalised name spaCy reads as a
#      proper noun                                                -> skill sense
#   3. never from company-intro, benefits or EEO sections unless listed (1).

_VERBISH = frozenset({"VERB", "AUX", "ADV", "ADP", "PART", "SCONJ", "CCONJ", "DET", "PRON"})
_WORD = re.compile(r"[A-Za-z0-9][\w+#./-]*")
_ITEM_SPLIT = re.compile(r"\s*(?:[,;|&]|\band\b|\bor\b|\s/\s|(?<=\w)/(?=\w))\s*", re.IGNORECASE)
_MAX_GRAM = 4
#: "A skill nearby" means within this many words either side, not anywhere in
#: the sentence ("coordinate with partner teams ... incident response" is not
#: Microsoft Teams just because a skill shares the sentence).
_NEAR_WORDS = 5


@lru_cache(maxsize=64)
def _sections(text: str) -> tuple[tuple[int, int, str], ...]:
    """(start, end, category) spans of the advert's sections; empty when the
    advert has no recognised headings."""
    from src.llm.sections import split_sections

    _pre, secs = split_sections(text)
    spans, pos = [], 0
    for cat, body in secs:
        i = text.find(body, pos)
        if i >= 0:
            spans.append((i, i + len(body), cat))
            pos = i + len(body)
    return tuple(spans)


def _section_at(text: str, offset: int) -> str | None:
    for a, b, cat in _sections(text):
        if a <= offset < b:
            return cat
    return None


@lru_cache(maxsize=64)
def _doc(text: str):
    from src.parser import _nlp

    return _nlp()(text)


def _approved_in(span: str, exclude: str) -> bool:
    """Whether an approved registry term other than ``exclude`` is named in
    ``span`` (word n-grams up to four words)."""
    reg = load()
    words = _WORD.findall(span)
    ex = exclude.casefold()
    for n in range(_MAX_GRAM, 0, -1):
        for i in range(len(words) - n + 1):
            g = " ".join(words[i:i + n]).strip(".").casefold()
            if g == ex or len(g) < 2:
                continue
            e = reg.exact.get(g)
            if e is not None and e.label == APPROVE:
                return True
    return False


def _listed_with_approved(term: str, line: str) -> bool:
    """The term is an item of a list whose next or previous item is an approved
    term ("Python, testing, CI/CD")."""
    items = [i.strip(" ()[]:*.-•") for i in _ITEM_SPLIT.split(line)]
    if len(items) < 2:
        return False
    key = term.casefold()
    for i, item in enumerate(items):
        if item.casefold() != key:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(items) and items[j] and len(items[j]) <= 40 and _approved_in(items[j], term):
                return True
    return False


def _occurrence_is_skill(e: Entry, text: str, start: int, end: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line = text[line_start: line_end if line_end >= 0 else len(text)]
    if _listed_with_approved(text[start:end], line):
        return True
    if _section_at(text, start) == "drop":
        return False
    doc = _doc(text)
    span = doc.char_span(start, end, alignment_mode="expand")
    if span is None or not len(span):
        return False
    # A term the registry writes as a name ("Teams", "Word", "Power") is not
    # that name where the advert writes it in lower case ("backend teams").
    written = text[start:end]
    if e.term[:1].isupper() and written.islower():
        return False
    head = span.root
    nxt = doc[span.end].text if span.end < len(doc) else ""
    # A word directly followed by ":" or "(" is a label or a named item
    # ("Go: 3+ years", "Excel (pivot tables)"), whatever its tagged part of speech.
    if head.pos_ in _VERBISH and nxt not in (":", "("):
        return False
    # A line labelled by the term ("Go: 3+ years", "- Excel: pivot tables") is
    # a skill item.
    if nxt == ":" and not text[line_start:start].strip(" \t*-•·#>"):
        return True
    near = doc[max(span.start - _NEAR_WORDS, head.sent.start): min(span.end + _NEAR_WORDS, head.sent.end)]
    if _approved_in(near.text, text[start:end]):
        return True
    if _section_at(text, start) in ("req", "nice"):
        return True
    return text[start].isupper() and head.pos_ == "PROPN"


def sense_shown(e: Entry, jd_text: str) -> bool:
    """Whether the advert uses the sense term ``e`` in its skill sense anywhere."""
    from src.scorer.keywords import clean_ad_text

    text = clean_ad_text(jd_text)
    pat = re.compile(rf"(?<![\w+#.-]){re.escape(e.term)}(?![\w+#-]|\.\w)", re.IGNORECASE)
    return any(_occurrence_is_skill(e, text, m.start(), m.end()) for m in pat.finditer(text))


# --- sense terms in the operator's own profile ---------------------------------------
#
# The matcher sees normalised (lower-cased) text, which has lost what tells a
# name from a word: "SQL window functions" folds to "window" and met Windows,
# "PyTorch (as a gradient oracle)" met Oracle, "the power to repoint" met
# Power. The profile's own lines are registered with their original text, so a
# sense skill is judged in a profile line exactly as in an advert.

_PROFILE_RAW: dict[str, str] = {}


def remember_profile_text(raw: str) -> None:
    """Register a profile line (bullet or skills-pool entry) by its normalised
    form, so ``shown_in_profile`` can read its original text."""
    from src.scorer.keywords import norm

    if raw:
        _PROFILE_RAW.setdefault(norm(raw), raw)


@lru_cache(maxsize=65536)
def _sense_in_line(term: str, raw: str) -> bool:
    """A profile line is the operator's own careful writing, so a sense skill
    counts unless the line shows the everyday sense: the term absent as itself
    ("window functions" is not Windows), a name written in lower case
    ("gradient oracle", "backend teams", "the power to"), or a verb ("make").
    Adverts get the stricter rule, which needs positive evidence
    (``sense_shown``)."""
    from src.scorer.keywords import clean_ad_text

    e = load().entry(term)
    if e is None:
        return True
    text = clean_ad_text(raw)
    # A dotted name counts as the term itself: "Node.js" for Node.
    pat = re.compile(rf"(?<![\w+#.-]){re.escape(e.term)}(?![\w+#-])", re.IGNORECASE)
    for m in pat.finditer(text):
        written = text[m.start():m.end()]
        if e.term[:1].isupper() and written.islower():
            continue
        doc = _doc(text)
        span = doc.char_span(m.start(), m.end(), alignment_mode="expand")
        if span is not None and len(span):
            nxt = doc[span.end].text if span.end < len(doc) else ""
            if span.root.pos_ in _VERBISH and nxt not in (":", "(", "."):
                continue
        return True
    return False


def shown_in_profile(tok: str, norm_text: str) -> bool | None:
    """For a sense skill and a registered profile line: whether the line uses
    the term in its skill sense. None when the rule does not apply (not a sense
    term, the registry off, or text that is not a profile line)."""
    if not enabled():
        return None
    raw = _PROFILE_RAW.get(norm_text)
    if raw is None:
        return None
    e = load().entry(tok)
    if e is None or e.label != SENSE:
        return None
    return _sense_in_line(e.term, raw)
