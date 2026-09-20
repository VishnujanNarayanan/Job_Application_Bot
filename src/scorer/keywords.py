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
    """One checklist item, and how much of the coverage score it is worth."""

    token: str
    weight: float


def jd_keywords(parsed) -> tuple[Keyword, ...]:
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
    out = [Keyword(t, float(cfg.weight_required)) for t in required]

    nice_weight = float(cfg.weight_nice_to_have)
    if nice_weight > 0:
        seen = {norm(t).strip() for t in required}
        out += [
            Keyword(t, nice_weight)
            for t in tokens_of(list(parsed.nice_to_have or []))
            if norm(t).strip() not in seen
        ]

    if cfg.include_responsibilities:
        # Off by default and documented above as the wrong choice; the key exists so
        # the decision is auditable and reversible rather than buried in code.
        seen = {norm(k.token).strip() for k in out}
        out += [
            Keyword(t, nice_weight)
            for t in tokens_of(list(parsed.responsibilities or []))
            if norm(t).strip() not in seen
        ]
    return tuple(out)


def covered_by(norm_text: str, keywords: Sequence[Keyword]) -> set[str]:
    """Which keywords appear in already-normalised ``norm_text``."""
    return {k.token for k in keywords if hit(k.token, norm_text)}


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
