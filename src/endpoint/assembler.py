"""Layer 6 — assemble a tailored DOCX from a StoredSelection + the template.

Design principle, unchanged from v1: **the template is the layout authority.** The
assembler never builds a paragraph from style names. It deep-clones the template's
own paragraphs as formatting *prototypes* and replaces only the text inside their
runs, so tab stops, numbering references, fonts, spacing and colour survive exactly
as the operator laid them out in Word.

WHAT THE HEADLESS TEMPLATE CHANGED
----------------------------------
Every structural assumption the v1 assembler made is wrong for this template:

  v1                                  Headless
  ----------------------------------  -----------------------------------------
  sections found by Heading 1 text    section headings are BOLD Normal paragraphs
  ("WORK EXPERIENCE", "SKILLS", ...)  whose text is a placeholder, so it cannot
                                      be matched on
  summary written to paragraphs[2]    there is no summary paragraph at all
  6 prototypes incl. a SKILLS one     4 prototypes; a missing SKILLS section is
                                      the normal case, not an error
  Education frozen as a SUFFIX        Education sits at the TOP, so the frozen
  (EDUCATION -> end of body)          region is a PREFIX (start -> Work History)
  project links via <w:hyperlink>     the template ships no hyperlinks at all, so
  and hand-patched r:id rels          a project's "Code ->" link is minted at
                                      render time through python-docx's own
                                      relate_to(), never by editing the saved zip

Detection is therefore structural, never textual:

    section heading  Normal, no numPr, non-empty, every non-empty run bold
    entry line       Heading 3 WITHOUT numPr  (Education lines HAVE numPr)
    entry bullet     Normal with numId == 1   (numId 2 is Education)

The template ships ONE section heading ("Work History OR Projects"), so the
assembler mints the second by cloning it — "Work History", then "Projects".

HARD RULES #9 AND #10
---------------------
They collapse into one property, which is a real simplification over v1. The
assembler never touches any body element before the second section heading: name,
contact, citizenship, and the whole Education & Certificates block. That prefix is
canonicalised (lxml c14n) before and after assembly and must be byte-identical.
Everything after it is rebuilt from scratch, so there is no "permitted region"
nuance left to police.

Education & Certificates is static by design (PIVOT_V3.md D9): the method caps it
at three lines and the operator hand-writes it into their template copy, which is
also why no code chooses which of nine education/certification records appear.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import structlog
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from src.config import settings
from src.llm.schemas import StoredSelection

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

_WORK_HEADING = "Work History"
_PROJECTS_HEADING = "Projects"

#: What a project's repo link reads as. Short by necessity — see _set_hyperlink.
_LINK_TEXT = "Code \u2192"

#: Character style that makes LibreOffice emit a PDF link annotation.
_LINK_STYLE = "Hyperlink"


class AssemblerError(RuntimeError):
    """Raised when a template constraint is violated."""


def apply_link_style(doc, run_elem) -> None:
    """Give ``run_elem`` the ``Hyperlink`` character style.

    This is what decides whether a link survives the PDF conversion, and it is
    the only thing that does. LibreOffice emits a ``/Subtype /Link`` annotation
    for a ``<w:hyperlink>`` **only when the run inside it carries a
    ``<w:rStyle>``**; with direct colour/underline formatting and no character
    style the link is intact in the DOCX, renders as ordinary text in the PDF,
    and is silently unclickable in the copy a recruiter opens.

    Measured against both templates: a bare run gives 0 annotations,
    ``w:history="1"`` 0, an empty ``rPr`` 0, an ``rPr`` holding only ``rFonts``
    0 -- an ``rPr`` holding only ``rStyle`` gets all of them. Provenance is
    irrelevant: a hand-minted element exports, and a Word-authored one whose run
    has no ``rStyle`` (which is what a Google Docs export writes) does not.

    The style is defined if the operator's template lacks it, so this holds for
    any template rather than only the one in the repo (hard rule #21).
    """
    if not any(s.style_id == _LINK_STYLE for s in doc.styles):
        doc.styles.add_style(_LINK_STYLE, WD_STYLE_TYPE.CHARACTER)
    rPr = run_elem.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        run_elem.insert(0, rPr)
    if rPr.find(qn("w:rStyle")) is not None:
        return  # the template already styles this run; don't override it
    style = OxmlElement("w:rStyle")
    style.set(qn("w:val"), _LINK_STYLE)
    # w:rStyle is the first child of rPr in the schema's ordering.
    rPr.insert(0, style)


# ---------------------------------------------------------------------------
# Structural predicates
# ---------------------------------------------------------------------------


def _style_of(p_elem) -> str:
    pPr = p_elem.find(qn("w:pPr"))
    if pPr is None:
        return ""
    pStyle = pPr.find(qn("w:pStyle"))
    return pStyle.get(qn("w:val"), "") if pStyle is not None else ""


def _num_id(p_elem) -> str | None:
    pPr = p_elem.find(qn("w:pPr"))
    if pPr is None:
        return None
    numPr = pPr.find(qn("w:numPr"))
    if numPr is None:
        return None
    numId = numPr.find(qn("w:numId"))
    return numId.get(qn("w:val")) if numId is not None else None


def _direct_runs(p_elem) -> list:
    """Direct ``<w:r>`` children (excludes runs nested in ``<w:hyperlink>``)."""
    return p_elem.findall(qn("w:r"))


def _text_of(p_elem) -> str:
    return "".join(t.text or "" for t in p_elem.iter(qn("w:t")))


def _is_bold(r_elem) -> bool:
    rPr = r_elem.find(qn("w:rPr"))
    if rPr is None:
        return False
    b = rPr.find(qn("w:b"))
    return b is not None and b.get(qn("w:val")) not in ("0", "false")


def _is_section_heading(p_elem) -> bool:
    """A section heading: Heading 2, or a bold Normal paragraph.

    Heading 2 is the preferred form and what the operator's template uses. It
    gives the paragraph an outline level, so the section collapses in Word,
    appears in the navigation pane, and presents real structure to a document
    parser — none of which a merely-bolded Normal paragraph does.

    Bold-Normal is still accepted, because the pristine upstream template ships
    the headings that way and a hand-made template may too. The method's "bold
    ONLY the section headings" rule makes boldness unambiguous on an unnumbered
    Normal paragraph.

    Text is deliberately NOT matched in either case: the template ships the
    placeholder "Work History OR Projects", and an operator's copy may say
    anything.
    """
    if _num_id(p_elem) is not None:
        return False
    style = _style_of(p_elem)
    if style in ("Heading2", "Heading 2"):
        return bool(_text_of(p_elem).strip())
    if style not in ("Normal", ""):
        return False
    runs = [r for r in _direct_runs(p_elem) if (r.find(qn("w:t")) is not None
                                                and (r.find(qn("w:t")).text or "").strip())]
    return bool(runs) and all(_is_bold(r) for r in runs)


def _is_entry_line(p_elem) -> bool:
    """Heading 3 WITHOUT numPr — the italic 'Title at Company \\t Dates' line.

    Education lines are also Heading 3 but carry numId 2, which is what separates
    them.
    """
    return _style_of(p_elem) in ("Heading3", "Heading 3") and _num_id(p_elem) is None


def _is_entry_bullet(p_elem) -> bool:
    return _num_id(p_elem) == str(settings.endpoint.render.bullet_num_id)


def _is_spacer(p_elem) -> bool:
    return (
        p_elem.tag == qn("w:p")
        and _num_id(p_elem) is None
        and not _text_of(p_elem).strip()
    )


# ---------------------------------------------------------------------------
# Region split
# ---------------------------------------------------------------------------


def _body_paragraphs(body) -> list:
    return [c for c in body if c.tag == qn("w:p")]


def _tailored_start(body) -> int:
    """Index into ``body`` of the SECOND section heading.

    The first is "Education & Certificates" (static, frozen); the second opens the
    tailored region. Everything from here to the ``sectPr`` is rebuilt.
    """
    seen = 0
    for i, child in enumerate(body):
        if child.tag == qn("w:p") and _is_section_heading(child):
            seen += 1
            if seen == 2:
                return i
    raise AssemblerError(
        "Template has fewer than two bold section headings. It needs "
        "'Education & Certificates' and a work/projects heading, both bold."
    )


def _frozen_canonical(body, start: int) -> bytes:
    """Canonical XML of the frozen prefix (hard rules #9 + #10)."""
    return b"".join(etree.tostring(c, method="c14n") for c in list(body)[:start])


def _assert_education_within_cap(body, start: int, template_name: str) -> None:
    """The method caps Education & Certificates at three lines.

    The region is static, so this cannot drift at runtime — but it CAN drift when
    the operator edits their template, which is exactly when nobody is checking.
    """
    cap = int(settings.endpoint.render.education_max_lines)
    edu_num_id = str(settings.endpoint.render.education_num_id)
    lines = [c for c in list(body)[:start]
             if c.tag == qn("w:p") and _num_id(c) == edu_num_id]
    if len(lines) > cap:
        raise AssemblerError(
            f"Education & Certificates has {len(lines)} lines; the method caps it "
            f"at {cap}. Trim {template_name}."
        )


# ---------------------------------------------------------------------------
# Prototype capture
# ---------------------------------------------------------------------------


def _capture_prototypes(body, start: int) -> dict:
    """Deep-copy the four paragraphs the assembler clones from.

    Captured BEFORE any mutation, because the originals live inside the region
    that gets deleted.
    """
    tail = [c for c in list(body)[start:] if c.tag == qn("w:p")]
    protos = {
        "section_heading": next((p for p in tail if _is_section_heading(p)), None),
        "entry_line": next((p for p in tail if _is_entry_line(p)), None),
        "entry_bullet": next((p for p in tail if _is_entry_bullet(p)), None),
        "spacer": next((p for p in tail if _is_spacer(p)), None),
    }
    missing = [k for k, v in protos.items() if v is None and k != "spacer"]
    if missing:
        raise AssemblerError(
            f"Template prototype(s) not found: {missing}. The tailored region must "
            "contain at least one bold section heading, one Heading-3 entry line "
            f"with a tab stop, and one bullet with numId="
            f"{settings.endpoint.render.bullet_num_id}."
        )
    if not _has_tab(protos["entry_line"]):
        raise AssemblerError(
            "The entry-line prototype has no tab run, so dates and project links "
            "would have nowhere to sit. Keep the template's right-aligned tab stop."
        )
    return {k: (copy.deepcopy(v) if v is not None else None) for k, v in protos.items()}


def _has_tab(p_elem) -> bool:
    return any(r.find(qn("w:tab")) is not None for r in _direct_runs(p_elem))


# ---------------------------------------------------------------------------
# Run-level text substitution (preserves rPr, tabs, numPr)
# ---------------------------------------------------------------------------


def _set_run_text(r_elem, text: str) -> None:
    """Set a run's ``<w:t>``, preserving its ``<w:rPr>``."""
    t = r_elem.find(qn("w:t"))
    if t is None:
        t = OxmlElement("w:t")
        r_elem.append(t)
    t.text = text
    if text != text.strip():
        t.set(_XML_SPACE, "preserve")


def _blank_run_text(r_elem) -> None:
    t = r_elem.find(qn("w:t"))
    if t is not None:
        t.text = ""


def _set_text(p_elem, text: str) -> None:
    """Single-text paragraph (a bullet): first run gets it, the rest are blanked."""
    runs = _direct_runs(p_elem)
    if not runs:
        return
    _set_run_text(runs[0], text)
    for r in runs[1:]:
        _blank_run_text(r)


def _set_keep(p_elem, *, keep_next: bool, keep_lines: bool = True) -> None:
    """Set ``w:keepNext`` / ``w:keepLines`` on a paragraph.

    ``keepLines`` stops a single bullet's own wrapped lines from splitting across
    a page break. ``keepNext`` binds this paragraph to the one after it, and a
    chain of them makes Word move the whole group to the next page rather than
    break it -- which is how the "don't strand an entry" rule below is enforced.
    """
    pPr = p_elem.find(qn("w:pPr"))
    if pPr is None:
        pPr = OxmlElement("w:pPr")
        p_elem.insert(0, pPr)
    for tag, want in (("w:keepNext", keep_next), ("w:keepLines", keep_lines)):
        existing = pPr.find(qn(tag))
        if existing is not None:
            pPr.remove(existing)
        el = OxmlElement(tag)
        el.set(qn("w:val"), "1" if want else "0")
        # Word reads these in schema order; both belong at the front of pPr.
        pPr.insert(0, el)


def _set_hyperlink(doc, p_elem, text: str, url: str) -> None:
    """Replace everything right of the tab with a hyperlinked ``text``.

    The relationship is minted through python-docx's own ``part.relate_to``,
    which writes a correct entry into ``word/_rels/document.xml.rels``. That
    matters: the previous implementation hand-patched the rels XML inside the
    saved zip, and a dangling ``r:id`` makes LibreOffice refuse the file, which
    fails the PDF render rather than just losing a link.

    Formatting is cloned from the run being replaced, so the link inherits the
    template's font and size; only colour and underline are added, which the
    method permits for a portfolio link. The run is then given the ``Hyperlink``
    character style, without which LibreOffice drops the link from the PDF --
    see ``apply_link_style``.
    """
    runs = _direct_runs(p_elem)
    tab_idx = next(
        (i for i, r in enumerate(runs) if r.find(qn("w:tab")) is not None), -1
    )
    if tab_idx < 0 or tab_idx + 1 >= len(runs):
        return
    proto = runs[tab_idx + 1]
    for r in runs[tab_idx + 1:]:
        p_elem.remove(r)

    run = copy.deepcopy(proto)
    for existing in run.findall(qn("w:t")):
        run.remove(existing)
    rPr = run.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        run.insert(0, rPr)
    for tag in ("w:color", "w:u"):
        found = rPr.find(qn(tag))
        if found is not None:
            rPr.remove(found)
    colour = OxmlElement("w:color")
    colour.set(qn("w:val"), "0563C1")
    rPr.append(colour)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rPr.append(underline)
    apply_link_style(doc, run)
    t_el = OxmlElement("w:t")
    t_el.text = text
    run.append(t_el)

    link = OxmlElement("w:hyperlink")
    # Word's own marker for a link that has been followed. It has no bearing on
    # whether the PDF gets an annotation -- apply_link_style decides that -- but
    # it is what Word writes, so we write it too.
    link.set(qn("w:history"), "1")
    link.set(
        qn("r:id"),
        doc.part.relate_to(
            url,
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            is_external=True,
        ),
    )
    link.append(run)
    p_elem.append(link)


def _set_entry_line(p_elem, left: str, right: str) -> None:
    """Left of the tab := ``left``; right of the tab := ``right``.

    The template splits the left side over two italic runs ("Title at Company, " +
    "Company, State "). The whole header goes into the first and the second is
    blanked rather than trying to re-split it: ``entry_header`` arrives from the
    extractor as one string, and both runs carry identical ``rPr``, so the
    rendered result is indistinguishable.
    """
    runs = _direct_runs(p_elem)
    tab_idx = next(
        (i for i, r in enumerate(runs) if r.find(qn("w:tab")) is not None), -1
    )
    if tab_idx < 0:
        _set_text(p_elem, left)
        return
    pre, post = runs[:tab_idx], runs[tab_idx + 1:]
    if pre:
        _set_run_text(pre[0], left)
        for r in pre[1:]:
            _blank_run_text(r)
    if post:
        _set_run_text(post[0], right)
        for r in post[1:]:
            _blank_run_text(r)


# ---------------------------------------------------------------------------
# Profile indexing
# ---------------------------------------------------------------------------


def _index_profile(profile_json_path: Path) -> dict[str, str]:
    """bullet_id -> text, across every role_block's render set AND recovery pool."""
    data = json.loads(Path(profile_json_path).read_text())
    bullet_text: dict[str, str] = {}
    for key in ("work_experience", "projects"):
        for entry in data.get(key) or []:
            for rb in entry.get("role_blocks") or []:
                for b in (*(rb.get("bullets") or []), *(rb.get("extra_bullets") or [])):
                    bullet_text[b["id"]] = b["text"]
    return bullet_text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def assemble_docx(
    selection: StoredSelection,
    profile_json_path: Path,
    template_path: Path,
    output_path: Path,
) -> Path:
    """Render ``selection`` into a DOCX at ``output_path``."""
    bullet_text = _index_profile(profile_json_path)

    doc = Document(str(template_path))
    body = doc.element.body

    start = _tailored_start(body)
    frozen_before = _frozen_canonical(body, start)
    _assert_education_within_cap(body, start, Path(template_path).name)
    protos = _capture_prototypes(body, start)

    new_elems: list = []
    sections = (
        (_WORK_HEADING, selection.work_entries()),
        (_PROJECTS_HEADING, selection.project_entries()),
    )
    for heading, entries in sections:
        if not entries:
            continue
        h = copy.deepcopy(protos["section_heading"])
        _set_text(h, heading)
        new_elems.append(h)
        for entry in entries:
            line = copy.deepcopy(protos["entry_line"])
            link = entry.header_right if entry.kind == "project" else ""
            if link.startswith(("http://", "https://")):
                # A project's slot holds a repo URL where a job holds dates. The
                # full URL overflows the line (measured: 112 chars against a
                # ~89-char budget at Arial 10.5 across 6.5in), so it renders as a
                # short hyperlink instead.
                _set_entry_line(line, entry.header_left, "")
                _set_hyperlink(doc, line, _LINK_TEXT, link)
            else:
                _set_entry_line(line, entry.header_left, entry.header_right)
            new_elems.append(line)

            # Never strand an entry across a page break. The header is bound to
            # the first `keep_together_ratio` of its bullets; if that group does
            # not fit in the remaining space, Word moves the whole group to the
            # next page instead of leaving a title with one orphaned bullet. The
            # remainder may flow, so a long entry still uses the page it has.
            ratio = float(settings.endpoint.render.keep_together_ratio)
            bound = max(1, math.ceil(ratio * len(entry.bullet_ids)))
            _set_keep(line, keep_next=True)

            for position, bid in enumerate(entry.bullet_ids):
                text = bullet_text.get(bid)
                if text is None:
                    raise AssemblerError(
                        f"bullet {bid!r} is in the selection but not in the profile "
                        "— rebuild master_profile.json (`python -m src.cli.reparse`)"
                    )
                bp = copy.deepcopy(protos["entry_bullet"])
                _set_text(bp, text)
                # keepNext on every bullet except the last of the bound group,
                # which is where the chain is allowed to break.
                _set_keep(bp, keep_next=position < bound - 1)
                new_elems.append(bp)
            if protos["spacer"] is not None:
                new_elems.append(copy.deepcopy(protos["spacer"]))

    # No trailing spacer: a blank paragraph at the end can push an empty page 2.
    while new_elems and _is_spacer(new_elems[-1]):
        new_elems.pop()

    _replace_tail(body, start, new_elems)

    if _frozen_canonical(body, _tailored_start(body)) != frozen_before:
        raise AssemblerError(
            "The frozen region (header + Education & Certificates) was modified. "
            "The assembler must never touch anything before the work/projects "
            "heading (hard rules #9 and #10)."
        )

    doc.save(str(output_path))
    log.info(
        "docx_assembled",
        job_id=selection.job_id,
        entries=len(selection.entries),
        bullets=sum(len(e.bullet_ids) for e in selection.entries),
        output=str(output_path),
    )
    return output_path


def _replace_tail(body, start: int, new_elems: list) -> None:
    """Delete every body child from ``start`` up to ``sectPr``, insert the new ones."""
    children = list(body)
    doomed = [c for c in children[start:] if c.tag != qn("w:sectPr")]
    for c in doomed:
        body.remove(c)
    for offset, elem in enumerate(new_elems):
        body.insert(start + offset, elem)
