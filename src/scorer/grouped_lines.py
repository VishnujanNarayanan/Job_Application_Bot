"""Grouped keyword lines (#138): several of an entry's keywords in one line.

An extra bullet spends a whole line on one keyword. A grouped line holds two to
four, from the hand-written ``keyword_lines`` of the entry (see
:class:`src.state.master_profile.KeywordLines`). Two kinds, never mixed:

    frame line    one group's keywords with commas, closed by THAT frame's ending:
                  "Shipped the collectors with Docker and CI/CD, so every
                   release ran the same reviewed code."
    phrase line   unrelated keywords, each in its own phrase, joined as a list;
                  no ending, the phrases already say what was done:
                  "Versioned every change in Git, queued live news in SQS and
                   indexed the warehouse on natural keys."

Nothing is generated: the builder only chooses and joins approved text, so a
line is reproducible and a stored selection replays word for word. A line's id
encodes its parts (:func:`line_id`), and the assembler rebuilds the text from
the profile with the same :func:`compose`.

WHERE IT RUNS. After fit is computed (``apply_decision.select_and_score``), so
lines change what is PRINTED, never the score or the verdict. Per entry, the
extras on the page are taken off, lines are built from their keywords and from
asked keywords the entry proves but the page does not show (a main bullet the
repetition rule skipped, for one), and an extra comes back only for a keyword no
line could hold. The entry never gets longer, never loses a keyword it showed,
and never repeats a keyword inside itself. If any of that fails, the entry keeps
its original bullets.

Off unless ``selection.grouped_lines.enabled``. Extras stay in the profile; they
are the fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

import structlog

from src.config import settings
from src.scorer.keywords import Keyword, covered_by, hit, matches, norm, weight_of

log = structlog.get_logger(__name__)

#: Separates the entry id from the parts in a line id. Bullet ids never hold it.
MARK = "~gl~"

#: A real figure (money, hours, people, a percentage): the strongest proof a
#: bullet carries, so a main bullet holding one is never swapped for a line.
_FIGURE = re.compile(
    r"[₹$€£]\s?\d|\d[\d,.]*\s?(%|x\b|\+|k\b|lakh|million|hours?\b|members\b|applicants\b"
    r"|signals\b|seconds?\b|minutes?\b|years?\b|transactions\b|photos\b)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Composition (shared by the selector and the assembler)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Part:
    """A phrase on its own (``frame`` empty) or a frame holding phrases."""

    phrases: tuple[str, ...]
    frame: str = ""

    def code(self) -> str:
        if self.frame:
            return "f." + ".".join((self.frame, *self.phrases))
        return "p." + self.phrases[0]


def line_id(entry_id: str, parts: Sequence[Part]) -> str:
    return entry_id + MARK + "+".join(p.code() for p in parts)


def is_line(bullet_id: str) -> bool:
    return MARK in bullet_id


def parse_line_id(bullet_id: str) -> tuple[str, list[Part]]:
    entry_id, _, rest = bullet_id.partition(MARK)
    parts = []
    for code in rest.split("+"):
        kind, _, body = code.partition(".")
        bits = body.split(".")
        if kind == "f" and len(bits) >= 2:
            parts.append(Part(tuple(bits[1:]), bits[0]))
        elif kind == "p" and len(bits) == 1 and bits[0]:
            parts.append(Part((bits[0],)))
        else:
            raise ValueError(f"malformed grouped line id {bullet_id!r}")
    return entry_id, parts


def join_and(items: Sequence[str], *, serial: bool = False) -> str:
    """"a", "a and b", "a, b and c". ``serial`` adds the comma before the last
    "and", for clauses that already contain one."""
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    last = ", and " if serial else " and "
    return ", ".join(items[:-1]) + last + items[-1]


def compose(kl: Any, parts: Sequence[Part]) -> str:
    """The line's text. ``kl`` is a ``KeywordLines`` model. Raises KeyError for
    a phrase or frame the profile no longer has.

    One frame part: the frame's clause with its keywords, then the frame's own
    ending. Otherwise a list of phrases, first word capitalised, a full stop at
    the end and no ending: each phrase already says what was done.
    """
    phrases = {p.id: p for p in kl.phrases}
    frames = {f.id: f for f in kl.frames}
    if len(parts) == 1 and parts[0].frame and len(parts[0].phrases) > 1:
        f = frames[parts[0].frame]
        kws = join_and([phrases[i].kw for i in parts[0].phrases])
        body = f.text.strip().replace("{kws}", kws)
        end = f.ending.strip()
        text = body + ("" if end[:1] in ",;" else " ") + end
    else:
        clauses = [phrases[i].text.strip() for part in parts for i in part.phrases]
        text = join_and(clauses, serial=len(clauses) > 2) + "."
    return text[:1].upper() + text[1:]


def text_of(bullet_id: str, lines_by_entry: dict[str, Any]) -> str:
    entry_id, parts = parse_line_id(bullet_id)
    kl = lines_by_entry.get(entry_id)
    if kl is None:
        raise KeyError(entry_id)
    return compose(kl, parts)


# ---------------------------------------------------------------------------
# Building lines for a selection
# ---------------------------------------------------------------------------


def _cfg() -> Any:
    return getattr(settings.selection, "grouped_lines", None)


def enabled() -> bool:
    cfg = _cfg()
    return bool(cfg is not None and getattr(cfg, "enabled", False))


@dataclass
class _Cand:
    phrase: Any
    hits: set[str]
    useful: set[str]
    weight: float


def _fits(kl: Any, parts: list[Part], max_chars: int) -> bool:
    return len(compose(kl, parts)) <= max_chars


#: Reading order inside a phrase line: the work as it happened. What was
#: built, then how data came in, then the product, then checking and review,
#: then running it; documentation last.
_STAGES = {
    "domain": 0,
    "collection": 1, "scraping": 1, "sources": 1, "ingestion": 1,
    "frontend": 2, "mobile": 2, "app": 2, "viz": 2, "realtime": 2, "content": 2,
    "testing": 4, "process": 4, "delivery": 4, "reliability": 4,
    "devops": 5, "cloud": 5, "runtime": 5, "hosting": 5, "observability": 5,
}


def _stage(ph: Any) -> int:
    if "document" in ph.kw.lower() or ph.id == "documentation":
        return 6
    return _STAGES.get(ph.group, 3)


def build_lines(
    entry_id: str,
    kl: Any,
    shown: set[str],
    candidates: list[_Cand],
    *,
    weights: dict[str, float],
    max_lines: int,
    max_chars: int,
    min_keywords: int = 1,
    min_phrases: int = 3,
    fillers: Sequence[Any] = (),
    overflow_chars: int = 0,
    phrase_min_groups: int = 3,
    priority: set[str] | frozenset = frozenset(),
    filler_hits: dict[str, set[str]] | None = None,
) -> list[tuple[list[Part], set[str]]]:
    """Pack the asked keywords into lines, heaviest first.

    FRAME lines are the default: the frame holding the most asked keywords,
    completed with its other members (keywords the advert did not ask for) up
    to ``min_phrases``. One asked keyword is enough, so keywords from two
    unrelated groups become two good frame lines rather than one glued list.

    A PHRASE line (separate phrases joined as a list) is built only to compact:
    when the asked keywords left span ``phrase_min_groups`` or more groups and
    there are not enough lines left to give each its own frame. Its phrases
    read in the order the work happened (``_stage``), and any filler shares a
    group with one of its asked phrases.

    ``max_chars`` is the target, not a ceiling: an asked keyword may take a line
    up to ``overflow_chars`` past it, a filler never does, and nothing pads a
    line toward it. No addition may say what the entry or line has said.

    ``priority`` (required skills no bullet on the page shows) ranks first: a
    line that saves one beats a line that merely holds more keywords.
    """
    used = set(shown)
    taken: set[str] = set()
    out: list[tuple[list[Part], set[str]]] = []
    phrase_by_id = {ph.id: ph for ph in kl.phrases}
    filler_hits = filler_hits or {}
    order = {ph.id: i for i, ph in enumerate(kl.phrases)}
    hard = max_chars + overflow_chars

    def worth(cs: list[_Cand]) -> float:
        return sum(weights.get(t, 0.0) for t in set().union(*(c.useful for c in cs))) if cs else 0.0

    def count(cs: list[_Cand]) -> int:
        return len(set().union(*(c.useful for c in cs))) if cs else 0

    def urgent(cs: list[_Cand]) -> int:
        """Required skills this line would put on a page that lacks them."""
        return len(set().union(*(c.useful for c in cs)) & priority) if cs else 0

    def frame_parts(f, ids) -> list[Part]:
        ids = [i for i in f.allowed if i in set(ids)]
        return [Part(tuple(ids), f.id)] if len(ids) > 1 else [Part((ids[0],))]

    def phrase_parts(ids) -> list[Part]:
        ids = sorted(ids, key=lambda i: (_stage(phrase_by_id[i]), order[i]))
        return [Part((i,)) for i in ids]

    def pack(pool: list[_Cand], make) -> list[_Cand]:
        got: list[_Cand] = []
        hits: set[str] = set()
        for c in pool:
            if c.hits & hits:
                continue
            if not _fits(kl, make([x.phrase.id for x in [*got, c]]), hard):
                continue
            got.append(c)
            hits |= c.hits
        return got

    def fill(ids: list[str], make, pool) -> list[str]:
        """Complete a line with phrases the advert did not ask for."""
        ids = list(ids)
        said = set(used)
        for i in ids:
            said |= filler_hits.get(i, set())
        # An asked keyword another entry shows completes a line before an
        # unasked one: it is also repetition evidence.
        for ph in sorted(pool, key=lambda ph: not filler_hits.get(ph.id)):
            if len(ids) >= min_phrases:
                break
            if ph.id in taken or ph.id in ids or filler_hits.get(ph.id, set()) & said:
                continue
            if _fits(kl, make([*ids, ph.id]), max_chars):
                ids.append(ph.id)
                said |= filler_hits.get(ph.id, set())
        return ids

    while len(out) < max_lines:
        avail = sorted(
            (c for c in candidates if c.phrase.id not in taken and not (c.hits & used)),
            key=lambda c: (-c.weight, c.phrase.id),
        )
        if not avail:
            break
        options = []
        for f in kl.frames:
            pool = [c for c in avail if c.phrase.id in f.allowed]
            got = pack(pool, lambda ids, f=f: frame_parts(f, ids))
            if not got or count(got) < min_keywords:
                continue
            spare = sorted((ph for ph in fillers if ph.id in f.allowed), key=lambda ph: f.allowed.index(ph.id))
            ids = fill([c.phrase.id for c in got], lambda ids, f=f: frame_parts(f, ids), spare)
            if len(ids) < 2:
                continue
            options.append(("frame", count(got), worth(got), f, got, ids))

        groups_left = {c.phrase.group for c in avail}
        lines_left = max_lines - len(out)
        in_a_frame = {c.phrase.id for o in options for c in o[4]}
        frameless = [c for c in avail if c.phrase.id not in in_a_frame]
        # A frameless asked keyword may still form a SAME-GROUP line: its
        # phrase completed by other phrases of its group (asked keywords
        # another entry shows first, then unasked ones). One group reads as
        # one thought, unlike a list of unrelated facts.
        for c in frameless:
            group_pool = [ph for ph in fillers if ph.group == c.phrase.group]
            ids = fill([c.phrase.id], phrase_parts, group_pool)
            if len(ids) >= 2:
                options.append(("group", count([c]), worth([c]), None, [c], ids))
        compact = len(groups_left) >= phrase_min_groups and (len(groups_left) > lines_left or frameless)
        if compact:
            # One phrase per group first (missing required skills leading), so
            # the list reaches its groups instead of two phrases of one group.
            first, rest, seen = [], [], set()
            for c in sorted(avail, key=lambda c: (not (c.useful & priority), -c.weight, c.phrase.id)):
                (rest if c.phrase.group in seen else first).append(c)
                seen.add(c.phrase.group)
            got = pack([*first, *rest], phrase_parts)
            if len({c.phrase.group for c in got}) >= phrase_min_groups:
                gs = {c.phrase.group for c in got}
                spare = sorted((ph for ph in fillers if ph.group in gs), key=lambda ph: order[ph.id])
                ids = fill([c.phrase.id for c in got], phrase_parts, spare)
                # The compacting line competes with the frames on the same terms:
                # missing required skills first, then keyword count (where it
                # usually wins), so it never displaces a frame that saves more.
                options.append(("phrase", count(got), worth(got), None, got, ids))
        if not options:
            break
        # A required skill the page would otherwise lose outranks keyword count.
        kind, _, _, f, got, ids = max(options, key=lambda o: (urgent(o[4]), o[1], o[2]))
        parts = frame_parts(f, ids) if kind == "frame" else phrase_parts(ids)  # "phrase" or "group"
        out.append((parts, set().union(*(c.useful for c in got))))
        used |= set().union(*(c.hits for c in got))
        for i in ids:
            used |= filler_hits.get(i, set())
        taken |= set(ids)
    return out


def _pairs(kl: Any, parts: Sequence[Part], new: set[str], keywords: Sequence[Keyword]) -> bool:
    """May this line take a main bullet's slot (#190)? Only if it carries two
    or more asked keywords that belong together: a frame holding two asked
    members, or a phrase line with two asked phrases from one group. A frame
    of one asked keyword plus fillers, or a list of unrelated phrases, may not.
    """
    phrases = {ph.id: ph for ph in kl.phrases}
    ids = [i for part in parts for i in part.phrases]
    asked = [i for i in ids if covered_by(norm(phrases[i].text), keywords) & new]
    if len(parts) == 1 and parts[0].frame and len(parts[0].phrases) > 1:
        return len(asked) >= 2
    groups: dict[str, int] = {}
    for i in asked:
        groups[phrases[i].group] = groups.get(phrases[i].group, 0) + 1
    return max(groups.values(), default=0) >= 2


def apply_grouped_lines(entries: list, profile: Any, keywords: Sequence[Keyword]) -> None:
    """Rewrite each selected entry's extras into grouped lines, in place."""
    if not enabled() or not keywords:
        return
    from src.scorer.selector import SelectedBullet, capped_terms, gated_terms, is_gated

    cfg = _cfg()
    max_lines = int(getattr(cfg, "max_lines", 3) or 3)
    max_chars = int(getattr(cfg, "max_chars", 172) or 172)
    overflow = int(getattr(cfg, "overflow_chars", 0) or 0)
    min_kw = int(getattr(cfg, "min_keywords", 1) or 1)
    min_phrases = int(getattr(cfg, "min_phrases", 3) or 3)
    min_groups = int(getattr(cfg, "phrase_min_groups", 3) or 3)
    lead_cap = int(getattr(cfg, "lead_entry_cap", 0) or 0)
    lead_n = int(getattr(cfg, "lead_entries", 0) or 0)
    top = max((k.weight for k in keywords), default=0.0)
    required = {k.token for k in keywords
                if k.required or (k.required is None and k.weight >= top - 1e-9)}
    kw_cap = int(getattr(settings.selection.bullets, "max_keyword_renders", 0) or 0)
    c_terms = capped_terms()
    g_terms = gated_terms()
    by_id = {e.id: e for e in (*profile.work, *profile.projects)}

    def kws_in(texts) -> set[str]:
        return covered_by(norm(" \n ".join(texts)), keywords)

    for idx, se in enumerate(entries):
        cand = by_id.get(se.id)
        kl = getattr(cand, "lines", None) if cand is not None else None
        if kl is None:
            continue
        extra_ids = {b.id for blk in cand.blocks for b in blk.bullets if b.is_extra}
        movable = [
            b for b in se.bullets
            if b.id in extra_ids and not b.is_summary
            and not (g_terms and is_gated(norm(b.text), g_terms))
        ]
        # A line may also take a slot the entry has free under its cap, but
        # only for a required skill the page would otherwise not show. The
        # entries leading the page may hold more (lead_entry_cap).
        cap = int(getattr(se, "cap", 0) or 0)
        if idx < lead_n:
            cap = max(cap, lead_cap)
        spare = max(0, cap - len(se.bullets))
        stay = [b for b in se.bullets if b not in movable]
        before = kws_in(b.text for b in se.bullets)
        shown = kws_in(b.text for b in stay)
        others = [o for j, o in enumerate(entries) if j != idx]
        elsewhere = kws_in(b.text for o in others for b in o.bullets)
        # #190: a WEAK main bullet (every asked keyword it says is shown by
        # another bullet on the page) may give its slot to a line carrying a
        # required skill the page lacks. Never the lead (summary) bullet,
        # never a gated bullet, never one holding a real figure.
        main_ids = {b.id for blk in cand.blocks for b in blk.bullets if not b.is_extra}
        weak = []
        for b in stay:
            if b.is_summary or b.id not in main_ids or _FIGURE.search(b.text):
                continue
            if g_terms and is_gated(norm(b.text), g_terms):
                continue
            says = kws_in([b.text])
            rest = kws_in(x.text for x in stay if x is not b) | elsewhere
            # Only a main carrying at most one asked keyword, all shown elsewhere.
            if len(says) <= 1 and says <= rest:
                weak.append(b)
        weak.sort(key=lambda b: b.score)
        # An entry with room for a single line may take one more, over its
        # cap, when that line saves a required skill the page would lose.
        bonus = 1 if len(movable) + spare + len(weak) == 1 else 0
        if not movable and not spare and not weak:
            continue
        capped_full = {
            t for t in elsewhere
            if kw_cap and c_terms and any(hit(c, norm(t)) for c in c_terms)
            and sum(t in kws_in(b.text for b in o.bullets) for o in others) >= kw_cap
        }

        cands: list[_Cand] = []
        for ph in kl.phrases:
            hits = covered_by(norm(ph.text), keywords)
            # What the keyword itself implies ("API testing" says Testing) is
            # not a repeat; anything else the phrase says must be new.
            own = covered_by(norm(ph.kw), keywords) & shown
            hits -= own
            if not hits or hits & shown or hits & capped_full:
                continue
            if g_terms and is_gated(norm(ph.text), g_terms):
                continue
            # Worth a place: what the extras showed, or what the page lacks.
            useful = {t for t in hits if t in before or t not in elsewhere}
            if not useful:
                continue
            cands.append(_Cand(ph, hits, useful, weight_of(useful, keywords)))

        # Fillers complete a short line: phrases saying no advert keyword, or
        # asked keywords only ANOTHER entry shows (repetition across entries is
        # allowed and counts as evidence), and no tool this entry already names.
        stay_norm = norm(" \n ".join(b.text for b in stay))
        cand_ids = {c.phrase.id for c in cands}
        fillers, filler_hits = [], {}
        for ph in kl.phrases:
            if ph.id in cand_ids or (g_terms and is_gated(norm(ph.text), g_terms)):
                continue
            if matches(ph.kw, stay_norm):
                continue
            h = covered_by(norm(ph.text), keywords)
            if h & shown or h & capped_full or (h - elsewhere):
                continue
            fillers.append(ph)
            filler_hits[ph.id] = h
        lines = build_lines(
            se.id, kl, shown, cands,
            weights={k.token: k.weight for k in keywords},
            max_lines=min(max_lines, len(movable) + spare + len(weak) + bonus), max_chars=max_chars,
            min_keywords=min_kw,
            overflow_chars=overflow, min_phrases=min_phrases, fillers=fillers,
            phrase_min_groups=min_groups, priority=required - before - elsewhere,
            filler_hits=filler_hits,
        )
        # Lines past the extras they replace take free slots: each must carry
        # a required skill no bullet on the page shows.
        lost = required - before - elsewhere
        kept, extra_slots, swapped = [], 0, []
        for parts, new in lines:
            if len(kept) < len(movable):
                kept.append((parts, new))
            elif not new & lost:
                continue
            elif extra_slots < spare:
                kept.append((parts, new))
                extra_slots += 1
            elif len(swapped) < len(weak) and _pairs(kl, parts, new, keywords):
                kept.append((parts, new))
                swapped.append(weak[len(swapped)])
            elif bonus:
                kept.append((parts, new))
                extra_slots += 1
                bonus = 0
        lines = kept
        if swapped:
            stay = [b for b in stay if b not in swapped]
            shown = kws_in(b.text for b in stay)
        if not lines:
            continue
        line_bullets = []
        now_shown = set(shown)
        for parts, new in lines:
            text = compose(kl, parts)
            line_bullets.append(SelectedBullet(line_id(se.id, parts), text, 0.0, False, sorted(new), via="jd"))
            now_shown |= covered_by(norm(text), keywords)
        # An extra comes back only for a keyword no line holds, and only into a
        # freed slot, and only if it repeats nothing.
        # An unused bonus slot (single-line entries) may keep an extra instead.
        slots = len(movable) - (len(line_bullets) - extra_slots - len(swapped)) + bonus
        restored = []
        for b in sorted(movable, key=lambda b: -weight_of(kws_in([b.text]) - now_shown, keywords)):
            hits = kws_in([b.text])
            if slots <= 0 or not (hits - now_shown) or hits & now_shown:
                continue
            restored.append(b)
            now_shown |= hits
            slots -= 1
        new_bullets = stay[:]
        if movable:
            at = se.bullets.index(movable[0])
            at = sum(1 for b in se.bullets[:at] if b in stay)
        elif swapped:
            at = sum(1 for b in se.bullets[:se.bullets.index(swapped[0])] if b in stay)
        else:
            at = len(new_bullets)
        new_bullets[at:at] = [*line_bullets, *restored]
        after = kws_in(b.text for b in new_bullets)
        # Nothing the page showed may leave it: a keyword may move out of this
        # entry only when another entry still shows it.
        gone = (before - after) - elsewhere
        if gone or any(b.is_summary for b in swapped):
            log.debug("grouped_lines_reverted", entry=se.id, lost=sorted(gone))
            continue
        se.bullets = new_bullets
        se.covered = set(se.covered) | (after - before)
        log.debug("grouped_lines", entry=se.id, lines=len(line_bullets),
                  extras_kept=len(restored), extras_before=len(movable),
                  mains_swapped=[b.id for b in swapped])
