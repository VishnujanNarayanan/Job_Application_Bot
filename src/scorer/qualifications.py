"""The 128-title qualification sheet, used as a selection TIE-BREAK only.

``data/job_qualifications.md`` is the vendored copy of the Headless Headhunter
qualification sheet -- for each job title, the list of things a recruiter screens
for. The bullet-extract skill writes bullets against it offline; this module lets
Layer 4 consult it at selection time.

Its role is deliberately small. The JD is what the operator is applying to, so JD
keywords drive selection outright. The sheet only breaks ties: when two bullets
cover exactly the same weight of JD keywords, the one that also covers more of the
role's canonical tokens wins, on the reasoning that a Gemini parse of a JD is lossy
and the sheet is what recruiters for that title actually screen for.

A canonical token the JD never mentions can NEVER pull a bullet onto the resume. The
greedy stops at zero JD gain, and the tie-break only reorders candidates that already
have equal, positive JD gain. That is the whole guarantee, and
``tests/test_keywords.py`` pins it.

Vendored rather than read from the guide directory so the GitHub Actions runner has
it (hard rule #21: no operator-specific path in source). ``make refresh-quals``
re-vendors it; ``make check-quals`` fails when it has drifted.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import structlog

from src.config import settings
from src.scorer.keywords import norm, tokens_of

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]

# Lines under these prefixes are differentiators, not requirements. The offline
# grader excludes them from the required set and so do we.
_TIERS = ("rare:", "bonus:", "nice to have:", "extra credit:")

_VARIANT = re.compile(r" \(variant \d+\)$")
_SIC = re.compile(r" \[sic\]")


@lru_cache(maxsize=1)
def _sheet() -> dict[str, list[str]]:
    """title -> its REQUIRED (untiered) qualification lines. Empty if unavailable."""
    configured = getattr(settings.selection.keywords, "qualifications_path", None)
    path = _ROOT / (configured or "data/job_qualifications.md")
    try:
        text = path.read_text(encoding="utf8")
    except OSError as exc:
        # Not fatal: the sheet is a tie-break, so the pipeline runs without it.
        log.warning("qualifications_unavailable", path=str(path), error=str(exc))
        return {}

    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("### "):
            current = _SIC.sub("", line[4:].strip())
            out[current] = []
        elif line.startswith("- ") and current:
            body = line[2:].strip()
            if not body.lower().startswith(_TIERS):
                out[current].append(body)
    log.debug("qualifications_loaded", titles=len(out))
    return out


@lru_cache(maxsize=512)
def canonical_tokens(checklist: tuple[str, ...]) -> frozenset[str]:
    """Normalised required tokens for a role block's ``checklist:`` titles.

    Returns normalised forms because the only consumer compares them against
    already-normalised bullet text.
    """
    sheet = _sheet()
    if not sheet:
        return frozenset()
    lines: list[str] = []
    for title in checklist:
        # Exact first: the sheet's own headings CARRY the "(variant N)" suffix,
        # because refresh_qualifications.py adds it when a title appears twice in
        # the source. Stripping unconditionally made every variant lookup miss
        # and contribute nothing, silently.
        found = sheet.get(title.strip())
        if found is None:
            # A bare "DevOps Engineer" should still resolve to its variants.
            # SKILL.md 0.3 permits exactly this union -- variants of one title are
            # the same job as described by two recruiters. (It is unioning across
            # MARKETS that is forbidden, and those are separate titles.)
            base = _VARIANT.sub("", title).strip()
            variants = [v for k, v in sheet.items()
                        if k == base or k.startswith(f"{base} (variant ")]
            if variants:
                found = [ln for v in variants for ln in v]
        if not found:
            log.debug("qualifications_title_unknown", title=title)
            continue
        lines += found
    return frozenset(norm(t).strip() for t in tokens_of(lines) if norm(t).strip())


def canonical_overlap(norm_text: str, checklist: tuple[str, ...]) -> int:
    """How many of the role's canonical tokens appear in ``norm_text``.

    The tie-break value. Plain count, not weighted: this never decides selection on
    its own, so a second scale of weights would be false precision.
    """
    toks = canonical_tokens(checklist)
    if not toks:
        return 0
    return sum(1 for t in toks if t in norm_text)
