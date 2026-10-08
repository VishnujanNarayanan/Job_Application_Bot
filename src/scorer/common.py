"""Place ``common_bullets`` on a finished page (#30).

A common bullet is a claim true of the operator regardless of any one repo: a
soft skill, a way of working, the AI coding tools. It is written once in the
profile -- alone ("communication") or combined with a skill it naturally goes
with ("communication" + "collaboration") -- and rendered only when the advert
asks for every family it covers.

WHY THIS RUNS AFTER SELECTION, NOT INSIDE IT
--------------------------------------------
Inside an entry the zero-repeat rule bars any bullet that restates a covered
keyword, together with every other keyword it carries. Measured on the profile
(2026-10-08): 185 project bullets carry a generic keyword (CI/CD, Git, Agile,
documentation, Linux) alongside project-specific ones. Had a pooled bullet been
seeded into an entry before its search, it would have claimed the generic
keyword first and made 15-66 project keywords per family unreachable --
automated testing, DevOps, pytest, data visualization, monitoring and logging.

So a common bullet never competes with an entry's own bullets. Every entry
chooses first; the pool then picks the best small SET of common bullets for the
page and appends each to an entry that has not already said any of its
keywords. By construction it cannot remove a keyword the page would have shown.

WHY A SET, NOT ONE AT A TIME
----------------------------
When an advert asks for communication and teamwork, one combined line covers
both in one slot; two single lines spend two. Picking greedily, bullet by
bullet, takes whichever comes first in the profile. The pool is small and the
page allows at most ``max_per_page`` (3), so every combination is scored and
the best one wins: most asked-for weight covered, then fewest lines, then the
variant tied to a specific entry over a generic one.
"""

from __future__ import annotations

from itertools import combinations

import structlog

from src.config import settings
from src.scorer.keywords import Keyword, covered_by, coverage_of, hit, matches, norm, weight_of
from src.scorer.selector import (
    CommonCand,
    JDContext,
    SelectedBullet,
    SelectedEntry,
    capped_terms,
    is_gated,
)

log = structlog.get_logger(__name__)


def _fold(text: str) -> str:
    return " ".join(norm(text).split())


def _answers(trigger: str, token: str) -> bool:
    """A trigger answers a checklist token in either direction: "communication"
    answers "communication skills", and "Agile/Scrum" answers "Agile"."""
    nt, nk = _fold(trigger), _fold(token)
    return bool(nt) and (hit(nt, nk) or matches(token, nt))


def _asked(cb: CommonCand, keywords: tuple[Keyword, ...], ai_ask: bool) -> dict[str, set[str]] | None:
    """Per family, the checklist tokens it answers -- or None when any family of
    this bullet went unasked. A combined line renders only when the advert asks
    for everything it says; otherwise a single line is the accurate answer."""
    out: dict[str, set[str]] = {}
    for fam in cb.families:
        toks = {
            k.token for k in keywords
            if any(f == fam and _answers(t, k.token) for f, t in cb.triggers)
        }
        if not toks and not (cb.gated and ai_ask):
            return None
        out[fam] = toks
    return out


def _credit(cb: CommonCand, keywords: tuple[Keyword, ...], asked: dict[str, set[str]]) -> set[str]:
    """What the bullet honestly covers: what its text says, plus an asked token
    whose trigger the text itself states ("communication skills" is answered by
    a bullet that says "communication")."""
    credit = covered_by(cb.norm_text, keywords)
    for fam, toks in asked.items():
        for tok in toks:
            if any(f == fam and hit(_fold(t), cb.norm_text) and _answers(t, tok)
                   for f, t in cb.triggers):
                credit.add(tok)
    return credit


def _role(e: SelectedEntry) -> str:
    return e.block_id.rsplit("::", 1)[-1]


def _hosts(cb: CommonCand, entries: list[SelectedEntry]) -> list[SelectedEntry]:
    """Where this bullet may go, best first.

    A hosted bullet goes under one of its hosts or nowhere: the entry header is
    the claim's WHERE, so any other entry would misstate where the operator did
    it. A host-less bullet prefers the salaried job, always on the page in
    position 1 or 2. ``roles`` narrows either to entries led by a matching block.
    """
    if cb.hosts:
        pool = [e for e in entries if e.id in cb.hosts]
    else:
        job = [e for e in entries if e.kind == "work" and e.employment_type == "employment"]
        pool = [*job, *(e for e in entries if e not in job)]
    if cb.roles:
        pool = [e for e in pool if _role(e) in cb.roles]
    return pool


def place_common_bullets(
    entries: list[SelectedEntry],
    common: list[CommonCand],
    keywords: tuple[Keyword, ...],
    jd: JDContext,
) -> list[str]:
    """Append the best asked-for set of common bullets. Returns the placed ids.

    Mutates ``entries`` in place: each host's ``bullets``, ``covered`` and
    ``coverage``. Nothing is placed when the advert asks for no family, when the
    page already shows everything a bullet would add, or when no entry can take
    it without restating one of its own keywords or breaking a limit.
    """
    cfg = getattr(settings.selection, "common_bullets", None)
    if not common or not entries or (cfg is not None and not cfg.enabled):
        return []
    max_per_page = int(getattr(cfg, "max_per_page", 0) or 0) if cfg is not None else 0
    max_per_page = max_per_page or len(common)
    # Host-less bullets all prefer the salaried job; without a per-entry ceiling
    # several soft-skill lines would stack under it and read as padding.
    max_per_entry = int(getattr(cfg, "max_per_entry", 0) or 0) if cfg is not None else 0

    kw_cap = int(getattr(settings.selection.bullets, "max_keyword_renders", 0) or 0)
    capped = tuple(_fold(c) for c in capped_terms())
    page_gated = any(is_gated(_fold(b.text)) for e in entries for b in e.bullets)
    page_covered: set[str] = set().union(*(e.covered for e in entries))

    # --- candidates: asked for, and placeable somewhere on this page ----------
    cands: list[tuple[int, CommonCand, set[str], list[SelectedEntry]]] = []
    for i, cb in enumerate(common):
        if cb.gated and page_gated:
            continue  # the page already shows an AI-tooling line
        asked = _asked(cb, keywords, jd.ai_tooling_asked)
        if asked is None:
            continue
        credit = _credit(cb, keywords, asked)
        # A filler word already shown in its maximum number of entries stays off.
        if kw_cap and capped and any(
            any(hit(c, _fold(t)) for c in capped)
            and sum(1 for e in entries if t in e.covered) >= kw_cap
            for t in credit
        ):
            continue
        hosts = [
            e for e in _hosts(cb, entries)
            if len(e.bullets) < e.cap and not (credit & e.covered)
        ]
        if hosts:
            cands.append((i, cb, credit, hosts))

    # --- choose the best set --------------------------------------------------
    def assign(combo) -> dict[str, SelectedEntry] | None:
        """Hosts for every bullet in ``combo``, or None if the limits forbid it.
        Hosted (specific) bullets claim their entry before host-less ones."""
        used: dict[str, int] = {}
        out: dict[str, SelectedEntry] = {}
        for _, cb, _, hosts in sorted(combo, key=lambda c: not c[1].hosts):
            host = next(
                (e for e in hosts
                 if not (max_per_entry and used.get(e.id, 0) >= max_per_entry)
                 and len(e.bullets) + used.get(e.id, 0) < e.cap),
                None,
            )
            if host is None:
                return None
            used[host.id] = used.get(host.id, 0) + 1
            out[cb.id] = host
        return out

    best = None
    for size in range(1, min(max_per_page, len(cands)) + 1):
        for combo in combinations(cands, size):
            fams = [f for _, cb, _, _ in combo for f in cb.families]
            if len(fams) != len(set(fams)):
                continue  # one bullet per family per page
            # Every line must add something the page and the other picks lack.
            seen, ok = set(page_covered), True
            for _, cb, credit, _ in combo:
                if not (credit - seen) and not cb.gated:
                    ok = False
                    break
                seen |= credit
            if not ok:
                continue
            hosts = assign(combo)
            if hosts is None:
                continue
            gained = set().union(*(c[2] for c in combo)) - page_covered
            key = (
                round(weight_of(gained, keywords), 9),
                sum(1 for c in combo if c[1].gated),   # the AI line answers an ask
                -size,                                  # fewer lines for the same coverage
                sum(1 for c in combo if c[1].hosts),    # specific evidence over generic
                -sum(c[0] for c in combo),              # then profile order
            )
            if best is None or key > best[0]:
                best = (key, combo, hosts)

    if best is None:
        return []
    _, combo, hosts = best
    placed: list[str] = []
    for _, cb, credit, _ in sorted(combo, key=lambda c: c[0]):
        host = hosts[cb.id]
        host.bullets.append(
            SelectedBullet(cb.id, cb.text, 0.0, False, sorted(credit - host.covered), via="common")
        )
        host.covered |= credit
        host.coverage = coverage_of(host.covered, keywords)
        placed.append(cb.id)
    log.info("common_bullets_placed", ids=placed)
    return placed
