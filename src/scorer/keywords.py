"""JD keyword extraction and the literal-substring matcher (Headless method).

The resume template has no Skills section, so a qualification only counts when it
appears *inside a bullet*. This module defines what "appears" means, and it is the
single definition the whole pipeline uses:

  - Layer 4 selects bullets by greedily covering these keywords (src/scorer/selector.py)
  - Layer 4 scores the result by how many it covered (src/scorer/apply_decision.py)

``norm``, ``tokens_of`` and ``hit`` are ports of the offline grader at
``resume guide/score_coverage.py`` and are kept SEMANTICALLY IDENTICAL to it. The
grader measures a bullet_extract file against the 128-title sheet; this module
measures a built resume against a live JD. If the two drift, every coverage number
in either repo stops being comparable with the other, and the bullet-extract skill's
"measure, never assert" discipline loses its reference point. ``tests/test_keywords.py``
asserts the parity.

What counts as a keyword is a deliberate, narrow choice -- see ``jd_keywords``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Sequence

import structlog

from src.config import settings

log = structlog.get_logger(__name__)

# Content words this short or this common carry no signal in the prose fallback.
_STOP = frozenset(
    ("with", "that", "this", "from", "your", "have", "able",
     "using", "such", "other", "into")
)

_KEEP = re.compile(r"[^a-z0-9+#./ ]")
_SPLIT = re.compile(r"[,()]")
_SIC = re.compile(r"\[sic\]")
_ALNUM = re.compile(r"[a-z0-9]")


@lru_cache(maxsize=4096)
def _boundary_re(tok: str) -> re.Pattern[str]:
    """Match ``tok`` only at alphanumeric boundaries.

    A raw substring test is catastrophically wrong on real technology names:
    ``Java`` matches "javascript", ``SQL`` matches "postgresql", ``R`` matches
    "ran", and ``Go`` matches "django". Each one silently marks a keyword covered
    by a bullet that never mentions it, which is precisely the fabrication the
    method exists to prevent.

    The guard is applied only at the token's own alphanumeric edges, so names that
    legitimately end or begin in punctuation still match: ``C++`` needs no trailing
    boundary, ``.NET`` no leading one, and ``Node.js`` / ``CI/CD`` keep their
    internal punctuation because it survives ``norm``.
    """
    pat = re.escape(tok)
    if _ALNUM.match(tok[0]):
        pat = r"(?<![a-z0-9])" + pat
    if _ALNUM.match(tok[-1]):
        pat = pat + r"(?![a-z0-9])"
    return re.compile(pat)


#: A markdown backslash-escape: the scraper stores adverts as markdown, so 99% of
#: stored ads write ``C\+\+``, ``scikit\-learn``, ``end\-to\-end`` (measured
#: 2026-10-08 over 1,427 ads).
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|~>&])")
#: Typographic hyphens and dashes (54% of stored ads) and the minus sign.
_DASHES = re.compile("[‐‑‒–—―−]")
#: No-break, narrow and thin spaces (6% of stored ads).
_ODD_SPACES = re.compile("[    ]")


def clean_ad_text(s: str | None) -> str:
    """Undo the markup that hides a term from a literal match.

    The LLM returns "C++" and "end-to-end testing"; the stored advert says
    ``C\\+\\+`` and ``end‑to‑end``. Every literal test against the raw
    text -- grounding the parser's skills, the vocabulary and pool scans --
    missed those terms. This removes markdown escapes, maps typographic
    hyphens and dashes to ``-`` and odd spaces to a plain space, and leaves
    everything else (case included) untouched.
    """
    text = _MD_ESCAPE.sub(r"\1", s or "")
    text = _DASHES.sub("-", text)
    return _ODD_SPACES.sub(" ", text)


def norm(s: str | None) -> str:
    """Fold to the comparison form: NFKD, lowercase, punctuation to spaces.

    ``+``, ``#``, ``.`` and ``/`` survive because they are load-bearing in real
    technology names -- C++, C#, .NET, CI/CD, Node.js.
    """
    return _KEEP.sub(" ", unicodedata.normalize("NFKD", s or "").lower())


def hit(tok: str, text: str) -> bool:
    """True if ``tok`` is present in ``text``. ``text`` MUST already be normalised.

    Literal match first, at alphanumeric boundaries -- that is the whole rule for a
    technology token, and it is why the method insists on writing the checklist's
    literal words: "Tailwind does not count as CSS; Postgres does not count as SQL".
    The boundary guard is what makes that second clause actually true: a raw
    substring test matches SQL inside "postgresql" and Java inside "javascript".

    The second branch exists for prose qualifications like "experience with
    distributed systems at scale", which no resume ever repeats verbatim. It needs
    at least three content words, so a short technology token can never reach it:
    ``Python`` is matched by the literal string or not at all.
    """
    t = norm(tok).strip()
    if not t:
        return False
    if _boundary_re(t).search(text):
        return True
    words = [w for w in t.split() if len(w) > 3 and w not in _STOP]
    if len(words) >= 3:
        matched = sum(1 for w in words if _boundary_re(w).search(text))
        if matched / len(words) >= 0.6:
            # The fallback was tuned for hand-written checklist prose, not for
            # LLM-parsed required_skills. A long parsed phrase can register here
            # without really being covered, so leave a trail when it fires.
            log.debug("keyword_prose_match", token=tok, matched=matched, of=len(words))
            return True
    return False


# ---------------------------------------------------------------------------
# Keyword families (#23): one skill, many spellings
# ---------------------------------------------------------------------------
#
# ``hit`` is the grader-parity primitive and is deliberately left untouched.
# ``matches`` widens it in two ways, and is what coverage, selection and the bold
# pass use:
#
#   * RULES, applied to any phrase: spacing and hyphens, a ".js"/"js" suffix, a
#     plural on the last word, RESTful = REST. Both the advert's token and every
#     1-5 word window of the bullet are folded to a compact KEY, so "React.js"
#     meets "React", "REST APIs" meets "RESTful API", "Power BI" meets "PowerBI".
#   * FAMILIES (config/keyword_families.yaml) for meaning the rules cannot see:
#     variants match both ways (Postgres = PostgreSQL); members imply the family
#     one way only (a MongoDB bullet covers "NoSQL", never the reverse).
#
# Measured over 979 parsed adverts (2026-10-04): 7,619 distinct skill phrases,
# 142 groups of spelling variants -- "React.js" alone was missing every bullet
# that says "React".

_JS_SUFFIX = re.compile(r"(?<=[a-z0-9])\.?js$")
_MAX_NGRAM = 5


def _plural_to_singular(word: str) -> str:
    if len(word) <= 3 or word.endswith("ss") or not word.endswith("s"):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("es") and word[:-2].endswith(("ch", "sh", "x", "ss")):
        return word[:-2]
    return word[:-1]


def _key_words(words: Sequence[str]) -> str:
    """Fold already-normalised words to the compact matching key."""
    out = []
    for w in words:
        # Sentence punctuation clings to the last word ("…in PostgreSQL."), since
        # norm keeps "." for names like Node.js. Trailing only: a LEADING dot is
        # load-bearing (".NET" must not become the word "net").
        w = w.rstrip("./")
        w = "rest" if w == "restful" else w
        w = _JS_SUFFIX.sub("", w) or w
        out.append(w)
    if out:
        out[-1] = _plural_to_singular(out[-1])
    return "".join(out)


def phrase_key(phrase: str) -> str:
    """The compact key of a phrase: what spelling variants have in common."""
    return _key_words(norm(phrase).split())


@lru_cache(maxsize=8192)
def _ngram_keys(norm_text: str) -> frozenset[str]:
    words = norm_text.split()
    return frozenset(
        _key_words(words[i:i + n])
        for i in range(len(words))
        for n in range(1, _MAX_NGRAM + 1)
        if i + n <= len(words)
    )


@lru_cache(maxsize=1)
def _families() -> tuple[dict[str, set[int]], list[frozenset[str]], list[frozenset[str]]]:
    """(key -> family ids, per-family equivalent keys, per-family member keys)."""
    import yaml
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "config" / "keyword_families.yaml"
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    index: dict[str, set[int]] = {}
    forms: list[frozenset[str]] = []
    members: list[frozenset[str]] = []
    for i, fam in enumerate((raw or {}).get("families") or []):
        keys = {phrase_key(fam["head"])} | {phrase_key(v) for v in fam.get("variants") or []}
        keys.discard("")
        forms.append(frozenset(keys))
        members.append(frozenset(k for k in (phrase_key(m) for m in fam.get("members") or []) if k))
        for k in keys:
            index.setdefault(k, set()).add(i)
    return index, forms, members


@lru_cache(maxsize=8192)
def match_keys(tok: str) -> frozenset[str]:
    """Every key whose presence in a text covers ``tok``: its own spellings, its
    family's variants, and the family's members (one-way)."""
    k = phrase_key(tok)
    if not k:
        return frozenset()
    index, forms, members = _families()
    out = {k}
    for i in index.get(k, ()):
        out |= forms[i] | members[i]
    return frozenset(out)


def canonical_key(tok: str) -> str:
    """One key per family, for de-duplicating an advert's checklist."""
    k = phrase_key(tok)
    index, forms, _ = _families()
    ids = index.get(k)
    return min(min(forms[i]) for i in ids) if ids else k


def matches(tok: str, norm_text: str) -> bool:
    """``hit``, widened by spelling rules and keyword families. ``norm_text`` MUST
    already be normalised."""
    if hit(tok, norm_text):
        return True
    keys = match_keys(tok)
    return bool(keys) and not keys.isdisjoint(_ngram_keys(norm_text))


def _family_span(norm_text: str, keys: frozenset[str]) -> tuple[int, int] | None:
    """First word window of ``norm_text`` whose key is in ``keys`` (norm offsets)."""
    spans = [(m.start(), m.end()) for m in re.finditer(r"\S+", norm_text)]
    words = [norm_text[a:b] for a, b in spans]
    for i in range(len(words)):
        for n in range(_MAX_NGRAM, 0, -1):
            if i + n <= len(words) and _key_words(words[i:i + n]) in keys:
                start, end = spans[i][0], spans[i + n - 1][1]
                while end > start and norm_text[end - 1] in "./":
                    end -= 1  # leave the sentence's full stop unbolded
                return start, end
    return None


def _norm_with_offsets(s: str) -> tuple[str, list[int]]:
    """``norm(s)`` plus, for every output character, the index it came from in ``s``.

    NFKD can expand one character into several (an accented letter becomes letter +
    combining mark), so the normalised string is not index-aligned with the
    original. The offset map lets a match found in normalised space be cut out of
    the original text without drifting.
    """
    out: list[str] = []
    src: list[int] = []
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKD", ch).lower():
            out.append(c)
            src.append(i)
    return _KEEP.sub(" ", "".join(out)), src


_DOTTED_TAIL = re.compile(r"(?:\.[A-Za-z0-9]+)+")
_DOTTED_HEAD = re.compile(r"(?:[A-Za-z0-9]+\.)+$")


def _whole_dotted_name(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow a match across dots joined to alphanumerics on both sides.

    ``.`` is a boundary to the matcher, so ``Node`` legitimately covers "Node.js"
    -- but bolding only "Node" leaves a visibly broken **Node**.js. A sentence's
    full stop is never joined to a following letter, so it is never absorbed.
    Slashes are deliberately NOT crossed: ``Python`` in "Python/SQL" must not
    bold SQL, which may be no keyword at all.
    """
    if start and text[start - 1] == ".":
        m = _DOTTED_HEAD.search(text, 0, start)
        if m and m.end() == start:
            start = m.start()
    m = _DOTTED_TAIL.match(text, end)
    if m:
        end = m.end()
    return start, end


def keyword_spans(
    text: str, tokens: Iterable[str]
) -> tuple[list[tuple[int, int]], set[str]]:
    """Where ``tokens`` literally appear in ``text``, as original-text offsets.

    Returns ``(spans, shown)``: non-overlapping ``(start, end)`` pairs sorted by
    start, and the tokens those spans display. Uses the literal, boundary-guarded
    branch of :func:`hit`, then the keyword-family spellings (#23) so an advert's
    "React.js" bolds the bullet's "React" -- never the prose fallback, which
    matches scattered content words and would mark fragments, not a keyword.

    Each token is located at its first occurrence. Overlaps resolve longest-first,
    so "machine learning" wins over "learning"; a token lying wholly inside a kept
    span counts as shown, because it is on the page inside that span. A token that
    only partly overlaps a kept span is neither kept nor shown.
    """
    norm_text, src = _norm_with_offsets(text)
    found: list[tuple[int, int, str]] = []
    for tok in tokens:
        t = norm(tok).strip()
        if not t:
            continue
        m = _boundary_re(t).search(norm_text)
        span = (m.start(), m.end()) if m else _family_span(norm_text, match_keys(tok))
        if span:
            start, end = _whole_dotted_name(text, src[span[0]], src[span[1] - 1] + 1)
            found.append((start, end, tok))
    found.sort(key=lambda s: (-(s[1] - s[0]), s[0]))
    kept: list[tuple[int, int]] = []
    shown: set[str] = set()
    for start, end, tok in found:
        if all(end <= k0 or start >= k1 for k0, k1 in kept):
            kept.append((start, end))
            shown.add(tok)
        elif any(k0 <= start and end <= k1 for k0, k1 in kept):
            shown.add(tok)
    return sorted(kept), shown


def family_hit(tok: str, norm_text: str) -> bool:
    """``literal_hit`` widened by spelling rules and keyword families, WITHOUT
    the prose fallback: "RAG" covers "retrieval-augmented generation", but
    "machine learning pipelines" is not covered by "learning" and "pipelines"
    a sentence apart. For checks that guard against invented terms, where
    ``matches``' partial-word acceptance is too generous. ``norm_text`` MUST
    already be normalised."""
    if literal_hit(tok, norm_text):
        return True
    keys = match_keys(tok)
    return bool(keys) and not keys.isdisjoint(_ngram_keys(norm_text))


def literal_hit(tok: str, norm_text: str) -> bool:
    """``hit`` without the prose fallback: the literal, boundary-guarded match only.

    For phrases that must appear as written -- a gate opener like "AI-assisted"
    must not fire because "assisted" and "AI" turn up three sentences apart.
    ``norm_text`` MUST already be normalised.
    """
    t = norm(tok).strip()
    return bool(t) and bool(_boundary_re(t).search(norm_text))


def tokens_of(lines: Iterable[str]) -> list[str]:
    """Split qualification lines into individually searchable tokens.

    "Python (NumPy, pandas)" -> ["Python", "NumPy", "pandas"]. Deduped on the
    normalised form, original order and original casing preserved.
    """
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for part in _SPLIT.split(_SIC.sub("", line or "")):
            p = part.strip(" .;:")
            if len(p) < 2:
                continue
            k = norm(p).strip()
            if k and k not in seen:
                seen.add(k)
                out.append(p)
    return out


@dataclass(frozen=True)
class Keyword:
    """One checklist item, and how much of the coverage score it is worth.

    ``required`` says which list it came from. Left unset it is read off the
    weight (full weight = required), which is all the share formula needs; the
    weighted_count formula (#101) sets it, because a required generic skill
    can weigh less than a nice-to-have tool.
    """

    token: str
    weight: float
    required: bool | None = None
    #: What one match counts for in weighted_count's "how many matched" half:
    #: 1, except in a short advert (:func:`short_ad_scale`).
    unit: float = 1.0

    def __post_init__(self) -> None:
        if self.required is None:
            object.__setattr__(self, "required", self.weight >= 1.0)


# --- The weighted_count formula (#101) ----------------------------------------------

def fit_formula() -> str:
    """``share`` (covered / total, as before #101) or ``weighted_count``."""
    value = str(settings.scoring.fit.get("formula") or "share")
    if value not in ("share", "weighted_count"):
        raise ValueError(f"scoring.fit.formula must be share or weighted_count, got {value!r}")
    return value


# Single words ending like these are abstract nouns ("Security", "networking",
# "analytics"), not names of a tool.
_ABSTRACT = re.compile(r"(ing|tion|sion|ment|ity|ness|ics|ance|ence|ogy|ism)$", re.I)
# A phrase with one of these is a field of work ("Machine Learning"), even in
# title case.
_CONCEPT_NOUNS = frozenset({
    "learning", "engineering", "development", "analysis", "analytics", "science", "pipelines",
    "pipeline", "design", "testing", "modeling", "modelling", "architecture", "management",
    "processing", "integration", "systems", "data", "intelligence", "vision", "language",
    "programming", "computing", "practices", "optimization", "optimisation", "governance",
    "control", "services", "models", "applications", "methods", "research",
})
# Soft skills, matched anywhere in what is left of the term once the filler
# words are stripped ("Strong Client Communication Skills").
_SOFT = (
    "communication", "teamwork", "team player", "collaborat", "leadership", "stakeholder",
    "problem solving", "problem-solving", "interpersonal", "attention to detail",
    "self-motivated", "ownership", "english", "mentor", "adaptab", "time management",
    "presentation", "analytical", "critical thinking", "fast-paced", "organizational",
    "organising", "organizing", "multitask", "curiosity", "passion", "prioritiz", "accuracy",
    "numerical skill", "quantitative skill", "work under pressure", "proactive", "resourceful",
)
# Words that are neither a skill nor a field: "skills", "data", "tools". Alone,
# such a term is generic.
_VAGUE = frozenset({
    "skills", "skill", "experience", "knowledge", "data", "software", "tools", "tool",
    "frameworks", "framework", "platforms", "platform", "services", "systems", "engineering",
    "development", "abilities", "ability", "etc",
})
# Stripped before typing: "Advanced SQL skills" is typed as "SQL".
_FILLER = re.compile(
    r"\b(strong|excellent|good|great|advanced|solid|proven|deep|exceptional|robust|basic|"
    r"working|hands-on|in-depth|demonstrated|skills?|abilit(?:y|ies)|proficiency|proficient|"
    r"knowledge|experience|expertise|familiarity|understanding|capabilit(?:y|ies)|"
    r"of|in|with|and|or|the|a|an)\b", re.I)
_NAMED = re.compile(r"[0-9+#./]")


@lru_cache(maxsize=1)
def _known_names() -> frozenset[str]:
    """Lower-case names in the operator's skills_pool: lets "pandas" count as a
    tool while "storage" does not."""
    from src.parser import _pool_terms

    return frozenset(t.casefold() for t in _pool_terms() if t[:1].islower() and " " not in t)


@lru_cache(maxsize=1)
def _usual_spelling() -> dict[str, str]:
    """Each vocabulary term's most common spelling across past adverts, by its
    lower-case key. Tools are written capitalised ("Kafka"); a word adverts
    usually write in lower case ("analytics", "healthcare") is not a tool."""
    from src.parser import _vocabulary

    return {k: t for t, k in _vocabulary()}


def _terms(kind: str) -> frozenset[str]:
    """``scoring.fit.weighted_count.terms.<kind>``: overrides, lower case."""
    terms = settings.scoring.fit.weighted_count.get("terms") or {}
    return frozenset(str(t).casefold() for t in (terms.get(kind) or []))


def _word_is_tool(w: str) -> bool:
    """A single word that names a tool: AWS, C++, PySpark, Python, Kafka, pandas."""
    low = w.casefold()
    if low in _terms("tool"):
        return True
    if low in _terms("concept") or low in _terms("generic") or low in _VAGUE:
        return False
    if _NAMED.search(w) or sum(c.isupper() for c in w) >= 2:
        return True
    if _ABSTRACT.search(w) or _ABSTRACT.search(w.rstrip("sS")):
        return False
    usual = _usual_spelling().get(low)
    if usual is not None and usual.islower() and low not in _known_names():
        return False
    return w[:1].isupper() or low in _known_names()


def _word_is_named(w: str) -> bool:
    """Stricter than :func:`_word_is_tool`: a word that is a name wherever it
    appears -- an acronym, a symbol, a pool name, or a capitalised vocabulary
    term ("Python"). "Client" in "Client Communication" is not."""
    low = w.casefold()
    if low in _terms("concept") or low in _terms("generic") or low in _VAGUE:
        return False
    if low in _terms("tool") or low in _known_names() or _NAMED.search(w) or sum(c.isupper() for c in w) >= 2:
        return True
    usual = _usual_spelling().get(low)
    return bool(usual and usual[:1].isupper() and not _ABSTRACT.search(w))


def term_type(token: str) -> str:
    """``tool`` (Python, AWS S3, Apache Spark), ``concept`` (machine learning,
    data pipelines, Cloud, Testing) or ``generic`` (communication, teamwork),
    from the term's own shape and how adverts usually spell it. The
    ``scoring.fit.weighted_count.terms`` lists override it."""
    t = token.strip()
    low = t.casefold()
    for kind in ("tool", "concept", "generic"):
        if low in _terms(kind):
            return kind
    core = " ".join(_FILLER.sub(" ", t).split())
    if not core or core.casefold() in _VAGUE:
        return "generic"
    words = core.split()
    if any(s in low for s in _SOFT) and not any(_word_is_named(w) for w in words):
        return "generic"
    if core.casefold() != low:
        return term_type(core)  # "Advanced SQL skills" -> "SQL"
    if len(words) == 1:
        return "tool" if _word_is_tool(t) else "concept"
    if words[0].casefold() in _terms("brand"):
        return "tool"  # "Azure Data Factory", "Apache Spark"
    if any(_word_is_named(w) for w in words):
        return "tool"  # "REST APIs", "Node.js", "GitHub Actions", "Python programming"
    if any(w.casefold() in _CONCEPT_NOUNS or _ABSTRACT.search(w) for w in words):
        return "concept"
    return "tool" if all(w[:1].isupper() for w in words) else "concept"


_MUST = re.compile(r"\b(must|strong|expert\w*|deep|proficien\w*|mandatory|hands-on|solid|advanced)\b", re.I)


def emphasis_signals(token: str, title: str | None, jd_text: str | None) -> int:
    """How many ways the advert stresses a skill, 0-3: it is in the job title;
    it is on a "must have / strong / expert" line; it is named 3+ times."""
    n = 0
    if title and matches(token, norm(title)):
        n += 1
    text = jd_text or ""
    if any(_MUST.search(line) and matches(token, norm(line)) for line in text.splitlines()):
        n += 1
    key = norm(token).strip()
    if key and len(re.findall(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", norm(text))) >= 3:
        n += 1
    return n


def type_factor(token: str) -> float:
    """``scoring.fit.weighted_count.type`` for the term, divided by the concept
    value: only the ratios matter to a share, and this keeps a concept at the
    weight it has under the share formula."""
    types = settings.scoring.fit.weighted_count.type
    return float(types.get(term_type(token))) / float(types.get("concept"))


def short_ad_scale(token: str, size: int) -> float:
    """weighted_count (#101): in a short advert a tool counts for more, a
    concept for somewhat more and a generic skill for less, fading to 1 at ``short_ad.below`` skills
    (``power`` > 1 fades it faster, keeping it for the shortest adverts).
    It scales both the skill's weight and what its match counts for, since a
    short all-tools list is fully covered by weight alone."""
    cfg = settings.scoring.fit.weighted_count.get("short_ad") or {}
    below = float(cfg.get("below", 25))
    s = max(0.0, 1.0 - size / below) ** float(cfg.get("power", 1.0)) if below > 0 else 0.0
    kind = term_type(token)
    if kind == "tool":
        return 1.0 + float(cfg.get("tool_boost", 0.0)) * s
    if kind == "generic":
        return 1.0 - float(cfg.get("generic_cut", 0.0)) * s
    return 1.0 + float(cfg.get("concept_boost", 0.0)) * s


def jd_keywords(parsed, *, title: str | None = None, jd_text: str | None = None) -> tuple[Keyword, ...]:
    """The checklist this JD is graded against.

    ``required_skills`` at full weight, ``nice_to_have`` at reduced weight, and
    ``responsibilities`` NOT included at all.

    That last exclusion is the important one. ``resume_method.md``: "In a JD, ignore
    everything except the Qualifications section. Job title, salary, day-to-day
    duties, EEOC statements -- none of it matters." ``responsibilities`` is exactly
    the duty prose. Tokenising it would flood the checklist with sentences no bullet
    can match, deflate every coverage ratio toward zero, and turn the metric into a
    measure of prose overlap rather than qualification coverage. It keeps its real
    job elsewhere: it is part of ``vec_match`` in ``build_jd_context``, where it
    drives the embedding tie-break.

    ``nice_to_have`` is halved because the offline grader drops "Nice to Have:"
    lines from the required token set entirely; half weight is the softest honest
    version of the same judgement.
    """
    cfg = settings.selection.keywords
    required = tokens_of(list(parsed.required_skills or []))
    # De-duplicate on the family key, not the spelling: "REST APIs" and
    # "RESTful APIs" in one checklist are one requirement, not two (#23).
    seen: set[str] = set()

    def _fresh(tokens):
        for t in tokens:
            k = canonical_key(t)
            if k and k not in seen:
                seen.add(k)
                yield t

    out = [Keyword(t, float(cfg.weight_required), True) for t in _fresh(required)]

    nice_weight = float(cfg.weight_nice_to_have)
    if nice_weight > 0:
        out += [
            Keyword(t, nice_weight, False)
            for t in _fresh(tokens_of(list(parsed.nice_to_have or [])))
        ]

    if cfg.include_responsibilities:
        # Off by default and documented above as the wrong choice; the key exists so
        # the decision is auditable and reversible rather than buried in code.
        out += [
            Keyword(t, nice_weight, False)
            for t in _fresh(tokens_of(list(parsed.responsibilities or [])))
        ]
    if fit_formula() == "weighted_count":
        # weight = kind x type x emphasis (#101): a tool counts more than a
        # concept, a concept more than a generic skill, and a skill the advert
        # stresses more again.
        emph = float(settings.scoring.fit.weighted_count.emphasis)
        size = len(out)
        out = [Keyword(k.token, k.weight * type_factor(k.token) * scale
                       * (1 + emph * emphasis_signals(k.token, title, jd_text)), k.required, scale)
               for k in out for scale in (short_ad_scale(k.token, size),)]
    return tuple(out)


def covered_by(norm_text: str, keywords: Sequence[Keyword]) -> set[str]:
    """Which keywords appear in already-normalised ``norm_text``, in any spelling
    or family form (:func:`matches`)."""
    return {k.token for k in keywords if matches(k.token, norm_text)}


def coverage_of(covered: set[str], keywords: Sequence[Keyword]) -> float:
    """Weighted fraction of the checklist that ``covered`` accounts for.

    Weighted, not a plain count, so a resume covering three required skills scores
    above one covering three nice-to-haves.
    """
    total = sum(k.weight for k in keywords)
    if total <= 0:
        return 0.0
    weight_of = {k.token: k.weight for k in keywords}
    return sum(weight_of.get(t, 0.0) for t in covered) / total


def weight_of(covered: set[str], keywords: Sequence[Keyword]) -> float:
    """Absolute weight of a covered set -- the greedy's gain function."""
    lookup = {k.token: k.weight for k in keywords}
    return sum(lookup.get(t, 0.0) for t in covered)
