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

from dataclasses import dataclass
from typing import Any, Sequence

import structlog

from src.config import settings
from src.scorer.keywords import Keyword, covered_by, hit, matches, norm, weight_of

log = structlog.get_logger(__name__)

#: Separates the entry id from the parts in a line id. Bullet ids never hold it.
MARK = "~gl~"


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


def build_lines(
    entry_id: str,
    kl: Any,
    shown: set[str],
    candidates: list[_Cand],
    *,
    weights: dict[str, float],
    max_lines: int,
    max_chars: int,
    min_keywords: int,
    min_phrases: int = 3,
    fillers: Sequence[Any] = (),
    overflow_chars: int = 0,
) -> list[tuple[list[Part], set[str]]]:
    """Pack candidates into lines, heaviest keywords first.

    Each line is one of two kinds, never a mix:
      * a FRAME line: one group's phrases in its frame, closed by its ending.
        Preferred: the frame holding the most asked keywords is taken, so
        related keywords share one dense sentence;
      * a PHRASE line: separate phrases joined as a list, only when no frame
        can hold two of the keywords left.

    A line needs ``min_keywords`` asked keywords, or the extra it would replace
    says the same in its own sentence. A line is never left short: it is
    completed from ``fillers`` (phrases of the entry the advert did not ask
    for): a frame with its other members, a phrase line with the best-matching
    phrases, same group first, until it holds ``min_phrases`` phrases or the
    next would not fit. A phrase line that still has fewer is not built.

    ``max_chars`` is the target, not a ceiling: an asked keyword may take a line
    up to ``overflow_chars`` past it, a filler never does, and nothing pads a
    line toward it. No addition may say what the entry or line has said.
    """
    used = set(shown)
    taken: set[str] = set()
    out: list[tuple[list[Part], set[str]]] = []
    order = {ph.id: i for i, ph in enumerate(kl.phrases)}
    frames = {f.id: f for f in kl.frames}

    def worth(cs: list[_Cand]) -> float:
        return sum(weights.get(t, 0.0) for t in set().union(*(c.useful for c in cs))) if cs else 0.0

    def frame_parts(f, ids) -> list[Part]:
        ids = [i for i in f.allowed if i in set(ids)]
        return [Part(tuple(ids), f.id)] if len(ids) > 1 else [Part((ids[0],))]

    def fill(ids: list[str], frame) -> list[str]:
        """Complete a line with phrases the advert did not ask for."""
        ids = list(ids)
        if len(ids) >= min_phrases:
            return ids
        groups = {ph.group for ph in kl.phrases if ph.id in ids}
        pool = [ph for ph in fillers if ph.id not in taken and ph.id not in ids]
        if frame is not None:
            pool = [ph for ph in pool if ph.id in frame.allowed]
            pool.sort(key=lambda ph: frame.allowed.index(ph.id))
        else:
            pool.sort(key=lambda ph: (ph.group not in groups, order[ph.id]))
        for ph in pool:
            if len(ids) >= min_phrases:
                break
            trial = [*ids, ph.id]
            parts = frame_parts(frame, trial) if frame is not None else [Part((i,)) for i in trial]
            if _fits(kl, parts, max_chars):
                ids = trial
        return ids

    while len(out) < max_lines:
        avail = sorted(
            (c for c in candidates if c.phrase.id not in taken and not (c.hits & used)),
            key=lambda c: (-c.weight, c.phrase.id),
        )

        def pack(pool: list[_Cand], make) -> list[_Cand]:
            got: list[_Cand] = []
            hits: set[str] = set()
            for c in pool:
                if c.hits & hits:
                    continue
                if not _fits(kl, make([*got, c]), max_chars + overflow_chars):
                    continue
                got.append(c)
                hits |= c.hits
            return got

        def count(cs: list[_Cand]) -> int:
            return len(set().union(*(c.useful for c in cs))) if cs else 0

        # Frames first: the frame that holds the MOST asked keywords wins,
        # weight breaking ties. A phrase line is the last resort, for keywords
        # no frame of the entry can group.
        options = []
        for f in kl.frames:
            pool = [c for c in avail if c.phrase.id in f.allowed]
            got = pack(pool, lambda cs, f=f: frame_parts(f, [c.phrase.id for c in cs]))
            if len(got) > 1 and count(got) >= min_keywords:
                options.append((count(got), worth(got), f, got))
        if not options:
            got = pack(avail, lambda cs: [Part((c.phrase.id,)) for c in cs])
            if got and count(got) >= min_keywords:
                options.append((count(got), worth(got), None, got))
        if not options:
            break
        _, _, f, got = max(options, key=lambda o: (o[0], o[1]))
        ids = fill([c.phrase.id for c in got], f)
        if f is None and len(ids) < min_phrases:
            break
        parts = frame_parts(f, ids) if f is not None else [Part((i,)) for i in ids]
        new = set().union(*(c.useful for c in got))
        out.append((parts, new))
        used |= set().union(*(c.hits for c in got))
        taken |= set(ids)
    return out


def apply_grouped_lines(entries: list, profile: Any, keywords: Sequence[Keyword]) -> None:
    """Rewrite each selected entry's extras into grouped lines, in place."""
    if not enabled() or not keywords:
        return
    from src.scorer.selector import SelectedBullet, capped_terms, gated_terms, is_gated

    cfg = _cfg()
    max_lines = int(getattr(cfg, "max_lines", 3) or 3)
    max_chars = int(getattr(cfg, "max_chars", 172) or 172)
    overflow = int(getattr(cfg, "overflow_chars", 0) or 0)
    min_kw = int(getattr(cfg, "min_keywords", 2) or 2)
    min_phrases = int(getattr(cfg, "min_phrases", 3) or 3)
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
        if not movable:
            continue
        stay = [b for b in se.bullets if b not in movable]
        before = kws_in(b.text for b in se.bullets)
        shown = kws_in(b.text for b in stay)
        others = [o for j, o in enumerate(entries) if j != idx]
        elsewhere = kws_in(b.text for o in others for b in o.bullets)
        capped_full = {
            t for t in elsewhere
            if kw_cap and c_terms and any(hit(c, norm(t)) for c in c_terms)
            and sum(t in kws_in(b.text for b in o.bullets) for o in others) >= kw_cap
        }

        cands: list[_Cand] = []
        for ph in kl.phrases:
            hits = covered_by(norm(ph.text), keywords)
            if not hits or hits & shown or hits & capped_full:
                continue
            if g_terms and is_gated(norm(ph.text), g_terms):
                continue
            # Worth a place: what the extras showed, or what the page lacks.
            useful = {t for t in hits if t in before or t not in elsewhere}
            if not useful:
                continue
            cands.append(_Cand(ph, hits, useful, weight_of(useful, keywords)))

        # Fillers complete a short line: phrases saying no advert keyword, and
        # no tool the entry already names.
        stay_norm = norm(" \n ".join(b.text for b in stay))
        fillers = [
            ph for ph in kl.phrases
            if not covered_by(norm(ph.text), keywords)
            and not (g_terms and is_gated(norm(ph.text), g_terms))
            and not matches(ph.kw, stay_norm)
        ]
        lines = build_lines(
            se.id, kl, shown, cands,
            weights={k.token: k.weight for k in keywords},
            max_lines=min(max_lines, len(movable)), max_chars=max_chars, min_keywords=min_kw,
            overflow_chars=overflow,
            min_phrases=min_phrases, fillers=fillers,
        )
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
        slots = len(movable) - len(line_bullets)
        restored = []
        for b in sorted(movable, key=lambda b: -weight_of(kws_in([b.text]) - now_shown, keywords)):
            hits = kws_in([b.text])
            if slots <= 0 or not (hits - now_shown) or hits & now_shown:
                continue
            restored.append(b)
            now_shown |= hits
            slots -= 1
        new_bullets = stay[:]
        at = se.bullets.index(movable[0])
        at = sum(1 for b in se.bullets[:at] if b in stay)
        new_bullets[at:at] = [*line_bullets, *restored]
        after = kws_in(b.text for b in new_bullets)
        if before - after:
            log.debug("grouped_lines_reverted", entry=se.id, lost=sorted(before - after))
            continue
        se.bullets = new_bullets
        se.covered = set(se.covered) | (after - before)
        log.debug("grouped_lines", entry=se.id, lines=len(line_bullets),
                  extras_kept=len(restored), extras_before=len(movable))
