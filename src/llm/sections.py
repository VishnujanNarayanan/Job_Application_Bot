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
                kept[order] = body[: left - 2].rstrip() + " …"
                left = 0

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
