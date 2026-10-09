"""Cut an over-long advert by SECTION, not by position (#72).

``clip_jd_text`` used to keep the first 3,000 and last 1,500 characters and drop
the middle. 44% of stored adverts (632 of 1,427, 2026-10-08) are longer than
that, and the middle is where requirements usually sit: adverts open with the
company and the role and close with benefits and legal text. In a 2026-10-07
audit, 133 of the 250 skills the parser missed were only in the cut middle.

This finds the advert's section headings and spends the same budget on the
sections a parser needs -- requirements, nice-to-haves, pay, the role's
metadata, the opening and responsibilities, in that priority -- and none on the
company intro, benefits, equal-opportunity text or application steps. Pay lines
are kept wherever they sit: pay is first stated a median 77% of the way into an
advert, often under "Benefits", which is why the old cut kept the tail at all.

Adverts with no recognisable headings return None, and the caller falls back to
the head-and-tail cut, so nothing gets worse where the structure is unknown.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

#: Categories, in the order a heading is tested against them. DROP and PAY come
#: first so "Company description" and "Compensation" are not read as a
#: description or as experience; NICE before REQ so "Preferred qualifications"
#: is a nice-to-have, not a requirement.
_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in (
        ("drop", r"^about (?!(the )?(role|job|position|opportunity|you)\b)|about us|company description"
                 r"|who we are|(about |our |the )team\b|culture|our values|benefits|perks|what we offer"
                 r"|why (join|work)|why you.?ll love|equal (employment )?opportunit|\beeo\b|diversity"
                 r"|inclusion|how to apply|application process|additional information|disclaimer"
                 r"|privacy|accommodation|life at|our story|our mission|our purpose|how we work"
                 r"|join us|working (with|for) (us|you)|recruitment fraud|fraud alert|drug and alcohol"
                 r"|drug.free|applicants with disabilit|disability (policy|statement)|wellbeing"
                 r"|well.being|professional development|what.?s in it for you|let.?s stay connected"
                 r"|our people|hybrid work|fair chance|lie detector|stay connected"),
        ("pay", r"compensation|salary|\bpay\b|remuneration|\bctc\b|stipend"),
        ("nice", r"preferred|nice.to.have|good.to.have|bonus|desired|desirable|\bplus\b|added advantage"
                 r"|stand out"),
        ("req", r"qualif|requirement|required|must.have|skill|experience|you.?ll bring|you bring"
                r"|looking for|who you are|about you|education|competenc|expertise|tech stack"
                r"|technical|ideal candidate|your profile|you have|basic|minimum|eligibility"
                r"|need to see|need to bring|essential|what you need"),
        ("meta", r"location|job type|employment type|work mode|job title|time type|job family"
                 r"|\bshift\b|schedule|workplace|reports to|department|job id|reference number"
                 r"|posting end date"),
        ("duty", r"responsib|you.?ll do|you will do|in this role|duties|\brole\b|job description"
                 r"|about the job|summary|overview|opportunity|day.to.day|your impact|description"
                 r"|work on|be doing|accountabilit|expectations|success looks like|job purpose"
                 r"|the position"),
    )
)

#: Budget priority once sections are known. Output keeps the advert's order.
#: Nothing is excluded outright, only ranked: when requirements are short the
#: budget left over goes to the rest of the opening and then to the company
#: intro and benefits, which on some adverts name the stack ("we run on AWS").
#: Measured on 632 long adverts: excluding those parts hid 520 known skills the
#: old head-and-tail cut had shown.
_PRIORITY = ("req", "nice", "pay", "meta", "preamble", "duty", "preamble_rest", "drop")
#: The opening before the first heading holds the title and seniority, but on
#: some adverts it is a long company intro, so only its start is kept.
_PREAMBLE_CAP = 700
#: A section is cut short only if at least this much of it fits.
_MIN_PARTIAL = 300

_PAY_LINE = re.compile(
    r"(salary|compensation|\bctc\b|\blpa\b|stipend|per annum|\bp\.a\b|per year|per hour|/\s?(yr|year|hr|hour)"
    r"|[₹$€£]|\binr\b|\busd\b|\beur\b|\bgbp\b)", re.I
)
_DIGIT = re.compile(r"\d")
_BOLD = re.compile(r"^\*\*(.+?)\*\*:?$")


def _category(text: str) -> str | None:
    text = re.sub(r"[*_:]+", " ", text).replace("\u2019", "'").strip().lower()
    if not text:
        return None
    for name, pattern in _CATEGORIES:
        if pattern.search(text):
            return name
    return None


def heading_category(line: str, *, plain: bool = False) -> str | None:
    """The section a heading line opens, or None if it is not a heading.

    A heading is a short line written as markdown ``#``, a whole-line ``**bold**``
    or a few words ending in a colon -- AND its text names a known section. An
    unrecognised bold line ("**Python**" in a list) is content, not a heading.

    ``plain`` also accepts an unstyled line of at most five words with no closing
    full stop ("Job Summary", "Role Overview"): some adverts write their headings
    as plain lines. Callers pass it only when a blank line follows, which is what
    separates a heading from a list item such as "Experience with Python".
    """
    raw = line.strip()
    if not 2 < len(raw) < 80:
        return None
    if raw.startswith("#"):
        text = raw.lstrip("#").strip()
    elif _BOLD.match(raw):
        text = _BOLD.match(raw).group(1)
    elif raw.endswith(":") and len(raw.split()) <= 7:
        text = raw[:-1]
    elif plain and len(raw.split()) <= 5 and not raw.endswith((".", ",", ";")) \
            and not raw.startswith(("-", "*", "•")):
        text = raw
    else:
        return None
    return _category(text)


def split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``(preamble, [(category, section_text), ...])``; each section's text
    starts with its own heading line."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        blank_after = i + 1 < len(lines) and not lines[i + 1].strip()
        cat = heading_category(line, plain=blank_after)
        if cat is not None:
            sections.append((cat, [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble).strip(), [(c, "\n".join(ls).strip()) for c, ls in sections]


def _pay_lines(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if _PAY_LINE.search(ln) and _DIGIT.search(ln)]


#: Words that look like a technology even when no list names them: CamelCase
#: (PyTorch, FastAPI), short acronyms (AWS, ETL, GCP), and tokens carrying
#: + # . / or a digit (C++, C#, Node.js, CI/CD, S3, GPT-4).
_TECHY = re.compile(
    r"\b(?:[A-Z][a-z]+[A-Z][A-Za-z]*|[A-Z]{2,6}s?|[A-Za-z]+(?:\+\+|#)|[A-Za-z]+\.(?:js|NET|io|ai)"
    r"|[A-Za-z]+/[A-Za-z]+|[A-Za-z]+-?\d+[A-Za-z]*)\b"
)


@lru_cache(maxsize=1)
def _skill_pattern() -> re.Pattern[str] | None:
    """One alternation over the curated skills: every spelling in
    keyword_families.yaml plus the operator's skills pool (short entries)."""
    terms: set[str] = set()
    path = Path(__file__).resolve().parents[2] / "config" / "keyword_families.yaml"
    try:
        for fam in (yaml.safe_load(path.read_text()) or {}).get("families") or []:
            for key in ("head", "variants", "members"):
                val = fam.get(key)
                terms.update([val] if isinstance(val, str) else map(str, val or []))
    except OSError:
        pass
    try:
        from src.parser import _pool_terms

        terms.update(t for t in _pool_terms() if len(t.split()) <= 3)
    except Exception:  # noqa: BLE001 - the pool is optional here
        pass
    terms = {t.strip() for t in terms if len(t.strip()) >= 2}
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![\w+#.])(?:{alts})(?![\w+#])", re.I)


def _line_score(line: str) -> int:
    """How much a line says about skills: a curated skill counts double, a
    technical-looking word once."""
    pat = _skill_pattern()
    known = len(pat.findall(line)) if pat else 0
    return 2 * known + len(_TECHY.findall(line))


_LONG_LINE = 300
_SENTENCE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z0-9•\-*])|\s+[•▪●]\s+")


def fit_lines(body: str, room: int) -> str:
    """Shorten one section to ``room`` characters keeping its most skill-dense
    lines, in their original order, with "…" where lines were skipped.

    Cutting at a fixed point kept a section's first lines whatever they said.
    On the long adverts still showing the parser under 90% of their skills,
    624 of the missed skills sat in responsibilities trimmed that way, while
    most lines kept were prose with no skill in them.
    """
    lines = []
    for ln in body.splitlines():
        if not ln.strip():
            continue
        # Some adverts arrive as a few giant lines with no breaks (one stored
        # advert: 4,794 characters on one line). Whole-line selection then kept
        # nothing but the heading, so a long line is split into sentences first.
        if len(ln) > _LONG_LINE:
            lines.extend(x for x in _SENTENCE.split(ln) if x.strip())
        else:
            lines.append(ln)
    if not lines or room <= 0:
        return ""
    head, rest = (lines[0], lines[1:]) if heading_category(lines[0]) else ("", lines)
    left = room - (len(head) + 1 if head else 0)
    if left <= 0:
        return head[:room]
    ranked = sorted(range(len(rest)), key=lambda i: (-_line_score(rest[i]), i))
    keep: set[int] = set()
    for i in ranked:
        cost = len(rest[i]) + 1
        if cost <= left:
            keep.add(i)
            left -= cost
        if left < 20:
            break
    def render() -> str:
        out = [head] if head else []
        gap = False
        for i, ln in enumerate(rest):
            if i in keep:
                if gap:
                    out.append("…")
                out.append(ln)
                gap = False
            else:
                gap = True
        if gap and keep:
            out.append("…")
        return "\n".join(out)

    if not keep:  # nothing fitted whole: a plain cut beats a bare heading
        return (head + "\n" if head else "") + " ".join(rest)[: max(0, left - 2)].rstrip() + " …"

    text = render()
    # The "…" markers cost characters too; shed the weakest kept line until
    # the section really fits its room.
    for i in reversed(ranked):
        if len(text) <= room:
            break
        if i in keep:
            keep.discard(i)
            text = render()
    return text if len(text) <= room else text[:room]


def clip_by_sections(text: str, budget: int, elision: str) -> str | None:
    """``text`` cut to about ``budget`` characters by section, or None when the
    advert has no requirement, nice-to-have or responsibility heading to go by."""
    preamble, sections = split_sections(text)
    if not any(c in ("req", "nice", "duty") for c, _ in sections):
        return None

    # Candidate pieces in advert order: (order, category, text). Orders are
    # spaced so a section can carry a pay piece just before itself.
    pieces: list[tuple[int, str, str]] = []
    if preamble:
        pieces.append((0, "preamble", preamble[:_PREAMBLE_CAP]))
        if len(preamble) > _PREAMBLE_CAP:
            pieces.append((1, "preamble_rest", preamble[_PREAMBLE_CAP:]))
    for i, (cat, body) in enumerate(sections, start=1):
        order = 10 * i
        if cat == "drop":
            pay = _pay_lines(body)
            if pay:  # pay stated under "Benefits" still matters
                pieces.append((order - 1, "pay", "\n".join(pay)))
        pieces.append((order, cat, body))

    kept: dict[int, str] = {}
    # Room for the omission markers between kept pieces, so the result never
    # outgrows the head-and-tail cut it replaces.
    left = budget - 3 * len(elision)
    for cat in _PRIORITY:
        for order, c, body in pieces:
            if c != cat or left <= 0:
                continue
            cost = len(body) + 1
            if cost <= left:
                kept[order] = body
                left -= cost
            elif left >= _MIN_PARTIAL:
                # Keep its most skill-dense lines rather than its first ones.
                part = fit_lines(body, left - 4)
                if part:
                    kept[order] = part
                    left -= len(part) + 1

    out: list[str] = []
    orders = [o for o, _, _ in sorted(pieces)]
    prev = None
    for order in orders:
        if order in kept:
            if out and prev is None:
                out.append(elision.strip())  # something between was left out
            out.append(kept[order])
            prev = order
        else:
            prev = None
    text_out = "\n".join(out)
    return text_out if len(text_out) <= budget else text_out[:budget].rstrip() + " …"


#: Around each pay mention in an unstructured advert, this much text either side.
_PAY_CONTEXT = 80
#: At most this much of the budget goes to pay snippets.
_PAY_CAP = 400


def clip_unstructured(text: str, budget: int, elision: str) -> str:
    """Cut an advert with no recognisable headings: its opening, its middle, and
    any pay mention.

    The old fallback kept the first 3,000 and last 1,500 characters. On the 12
    long stored adverts with no usable headings, that showed the parser 70% of
    the known skills they name; the opening plus the middle shows 96%, because
    an unstructured advert still front-loads the company and back-loads the
    benefits. The opening (title, seniority) is kept, and so is any pay
    mention, wherever it sits -- the reason the old cut kept the tail at all.
    """
    if len(text) <= budget:
        return text
    room = budget - 3 * len(elision)
    if room <= 0:  # a budget too small for markers: a plain cut
        return text[:budget]
    head_len = min(_PREAMBLE_CAP, room // 4)
    head = text[:head_len]

    # Pay snippets, merged where they overlap, outside the opening.
    spans: list[list[int]] = []
    for m in _PAY_LINE.finditer(text, head_len):
        near = text[max(0, m.start() - 40): m.end() + 40]
        if not _DIGIT.search(near):
            continue
        a, b = max(head_len, m.start() - _PAY_CONTEXT), min(len(text), m.end() + _PAY_CONTEXT)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    pay: list[tuple[int, str]] = []
    used = 0
    for a, b in spans:
        if used + (b - a) > _PAY_CAP:
            break
        pay.append((a, text[a:b]))
        used += b - a

    mid_len = max(0, room - head_len - used)
    start = max(head_len, (len(text) - mid_len) // 2)
    middle = (start, text[start:start + mid_len])

    parts = [(0, head)] + sorted([middle, *pay])
    out: list[str] = []
    end = 0
    for at, chunk in parts:
        if not chunk:
            continue
        if at > end:
            out.append(elision.strip())
        out.append(chunk)
        end = at + len(chunk)
    if end < len(text):
        out.append(elision.strip())
    return "\n".join(out)
