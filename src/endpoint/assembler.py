"""DOCX assembler — structural detection + XML fill (Layer 6 / §6.3).

Produces a tailored DOCX from a ``StoredSelection`` and the current template.

Algorithm:
  1. Load a fresh copy of the template (original never modified).
  2. Verify header: paragraphs 0-1 byte-identical to template (hard rule #10).
  3. Fill summary paragraph (para 2) with the selected summary text.
  4. WORK EXPERIENCE: remove template experience blocks; insert one block per
     selected experience (title/dates, company/location, 3 bullets).
  5. SKILLS: remove template skill lines; insert one HTML-Preformatted
     paragraph per category (bold name, comma-joined skills) + optional
     Familiar With line.
  6. PROJECTS: remove template project blocks; insert one block per selected
     project (name + "Code →" hyperlink, 2-3 bullets).
  7. EDUCATION + CERTIFICATES: unchanged (deep-cloned from template).
  8. Reorder Skills ↔ Projects per ``selection.section_order`` if needed.
  9. Update project hyperlink targets in word/_rels/document.xml.rels.
 10. Final header diff-check.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import structlog
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from src.endpoint.hyperlinks import update_project_hyperlinks
from src.llm.schemas import StoredSelection

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]

_WORK = "WORK EXPERIENCE"
_SKILLS = "SKILLS"
_PROJECTS = "PROJECTS"
_EDUCATION = "EDUCATION"
_CERTS = "CERTIFICATES"


class AssemblerError(RuntimeError):
    """Raised when a template constraint is violated."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def assemble_docx(
    selection: StoredSelection,
    profile_json_path: Path,
    template_path: Path,
    output_path: Path,
) -> None:
    """Build a tailored DOCX at ``output_path``.

    ``profile_json_path`` is the canonical ``master_profile.json``.
    Bullet texts, company/dates/location, project names/links are read
    from it; no DB query required at render time.
    """
    profile = json.loads(profile_json_path.read_text())
    bullet_text, exp_map, proj_map, summary_map = _index_profile(profile)

    # Load template fresh — doc A is our working copy, doc B is the immutable
    # reference used only for the header diff-check.
    doc = Document(str(template_path))
    ref = Document(str(template_path))

    # Hard rule #10: header must be untouched throughout
    _assert_header_unchanged(doc, ref)

    # --- summary (para 2) ---
    summary_text = summary_map.get(selection.summary_id, "")
    _fill_para_text(doc.paragraphs[2], summary_text)

    # --- section indices ---
    sec = _section_map(doc)

    # --- rebuild variable sections ---
    _replace_section(
        doc, sec, _WORK, _SKILLS,
        _exp_paras(doc, selection, exp_map, bullet_text),
    )
    _replace_section(
        doc, sec, _SKILLS, _PROJECTS,
        _skills_paras(doc, selection),
    )
    proj_rids: dict[str, str] = {}
    _replace_section(
        doc, sec, _PROJECTS, _EDUCATION,
        _proj_paras(doc, selection, proj_map, bullet_text, proj_rids),
    )

    # --- section reorder (Skills ↔ Projects) ---
    _apply_order(doc, selection.section_order)

    # --- final header diff-check ---
    _assert_header_unchanged(doc, ref)

    # --- save ---
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))

    # --- update project hyperlinks in rels XML ---
    if proj_rids:
        update_project_hyperlinks(output_path, proj_rids)

    log.info(
        "docx_assembled",
        job_id=selection.job_id,
        template_version=selection.template_version,
    )


# ---------------------------------------------------------------------------
# Profile indexing
# ---------------------------------------------------------------------------


def _index_profile(profile: dict):
    bullet_text: dict[str, str] = {}
    exp_map: dict[str, dict] = {}
    proj_map: dict[str, dict] = {}
    summary_map: dict[str, str] = {}

    for exp in profile.get("work_experience", []):
        exp_map[exp["id"]] = exp
        for b in exp.get("bullet_pool", []):
            bullet_text[b["id"]] = b["text"]

    for proj in profile.get("projects", []):
        proj_map[proj["id"]] = proj
        for b in proj.get("bullet_pool", []):
            bullet_text[b["id"]] = b["text"]

    for s in profile.get("summaries", []):
        summary_map[s["id"]] = s["text"]

    return bullet_text, exp_map, proj_map, summary_map


# ---------------------------------------------------------------------------
# Header diff-check
# ---------------------------------------------------------------------------


def _para_canonical(p) -> bytes:
    """Canonical byte representation of a paragraph element (lxml c14n)."""
    return etree.tostring(p._p, method="c14n")


def _assert_header_unchanged(doc: Document, ref: Document) -> None:
    """Assert paras 0-1 in ``doc`` match ``ref`` exactly (hard rule #10)."""
    for i in range(2):
        if _para_canonical(doc.paragraphs[i]) != _para_canonical(ref.paragraphs[i]):
            raise AssemblerError(
                f"Header paragraph {i} was modified. "
                "The assembler must never touch paragraphs before WORK EXPERIENCE "
                "(hard rule #10)."
            )


# ---------------------------------------------------------------------------
# Section map — heading index + content range
# ---------------------------------------------------------------------------


def _section_map(doc: Document) -> dict[str, dict]:
    """Return {section_name: {heading_para_idx, content_start, content_end}}."""
    heading_positions: list[tuple[int, str]] = []
    for i, p in enumerate(doc.paragraphs):
        if p.style.name == "Heading 1":
            name = p.text.strip().rstrip("\t").strip()
            heading_positions.append((i, name))

    result: dict[str, dict] = {}
    for j, (idx, name) in enumerate(heading_positions):
        next_idx = (
            heading_positions[j + 1][0]
            if j + 1 < len(heading_positions)
            else len(doc.paragraphs)
        )
        result[name] = {
            "heading_idx": idx,
            "content_start": idx + 1,
            "content_end": next_idx,
        }
    return result


# ---------------------------------------------------------------------------
# Section replacement
# ---------------------------------------------------------------------------


def _replace_section(
    doc: Document,
    sec: dict,
    section_name: str,
    next_section_name: str,
    new_paras: list,
) -> None:
    """Remove existing section content; insert ``new_paras`` in its place."""
    info = sec.get(section_name)
    if info is None:
        return

    body = doc.element.body
    all_body = list(body)

    # Identify the heading element and the next section's heading element.
    heading_elem = doc.paragraphs[info["heading_idx"]]._p
    next_info = sec.get(next_section_name)
    next_heading_elem = (
        doc.paragraphs[next_info["heading_idx"]]._p if next_info else None
    )

    # Collect elements to remove (between headings, not including them).
    to_remove = []
    collecting = False
    for child in list(body):
        if child is heading_elem:
            collecting = True
            continue
        if collecting:
            if next_heading_elem is not None and child is next_heading_elem:
                break
            to_remove.append(child)

    for elem in to_remove:
        body.remove(elem)

    # Insert new paragraphs after the heading.
    heading_pos = list(body).index(heading_elem)
    for i, p_elem in enumerate(new_paras):
        body.insert(heading_pos + 1 + i, p_elem)


# ---------------------------------------------------------------------------
# Paragraph builders
# ---------------------------------------------------------------------------


def _make_para(style_val: str) -> OxmlElement:
    """Return a bare <w:p> with the given style."""
    p = OxmlElement("w:p")
    pPr = OxmlElement("w:pPr")
    pStyle = OxmlElement("w:pStyle")
    pStyle.set(qn("w:val"), style_val)
    pPr.append(pStyle)
    p.append(pPr)
    return p


def _add_run(p_elem, text: str, bold: bool = False) -> None:
    """Append a text run to ``p_elem``."""
    r = OxmlElement("w:r")
    if bold:
        rPr = OxmlElement("w:rPr")
        rPr.append(OxmlElement("w:b"))
        r.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    if text != text.strip():
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)
    p_elem.append(r)


def _fill_para_text(para, text: str) -> None:
    """Replace the text of an existing paragraph (preserves pPr)."""
    p_elem = para._p
    for child in list(p_elem):
        if child.tag != qn("w:pPr"):
            p_elem.remove(child)
    if text:
        _add_run(p_elem, text, bold=False)


# ---------------------------------------------------------------------------
# Experience paragraphs
# ---------------------------------------------------------------------------


def _exp_paras(doc, selection, exp_map, bullet_text) -> list:
    paras = []
    for entry in selection.experiences:
        exp = exp_map.get(entry.exp_id, {})
        company = exp.get("company", "")
        location = exp.get("location") or ""
        start = exp.get("start_date", "")
        end = exp.get("end_date", "")
        dates = f"{start} – {end}" if start else end

        # Title + dates
        p = _make_para("Normal")
        _add_run(p, entry.title_alias, bold=True)
        _add_run(p, "\t", bold=True)
        _add_run(p, dates, bold=False)
        paras.append(p)

        # Company + location
        company_str = f"{company} - {location}" if location else company
        p = _make_para("Normal")
        _add_run(p, company_str, bold=False)
        paras.append(p)

        # Bullets
        for bid in entry.bullet_ids:
            text = bullet_text.get(bid, "")
            if text:
                p = _make_para("ListParagraph")
                _add_run(p, text, bold=False)
                paras.append(p)

    return paras


# ---------------------------------------------------------------------------
# Skills paragraphs
# ---------------------------------------------------------------------------


def _skills_paras(doc, selection) -> list:
    paras = []
    for cat in selection.skills.categories:
        p = _make_para("HTMLPreformatted")
        _add_run(p, f"{cat.name}: ", bold=True)
        _add_run(p, ", ".join(cat.skills), bold=False)
        paras.append(p)

    fw = selection.skills.familiar_with
    if fw:
        p = _make_para("HTMLPreformatted")
        _add_run(p, "Familiar With: ", bold=True)
        _add_run(p, ", ".join(fw), bold=False)
        paras.append(p)

    return paras


# ---------------------------------------------------------------------------
# Project paragraphs
# ---------------------------------------------------------------------------


def _proj_paras(doc, selection, proj_map, bullet_text, proj_rids: dict) -> list:
    paras = []
    for i, entry in enumerate(selection.projects):
        proj = proj_map.get(entry.proj_id, {})
        name = proj.get("name", entry.proj_id)
        link = entry.link or proj.get("link", "")

        # Project name + hyperlink
        rId = f"rIdProj{i + 1}"
        if link:
            proj_rids[rId] = link

        p = _make_para("ListParagraph")
        _add_run(p, name, bold=False)
        _add_run(p, "\t", bold=False)

        if link:
            hl = OxmlElement("w:hyperlink")
            hl.set(qn("r:id"), rId)
            r = OxmlElement("w:r")
            rPr = OxmlElement("w:rPr")
            rs = OxmlElement("w:rStyle")
            rs.set(qn("w:val"), "Hyperlink")
            rPr.append(rs)
            r.append(rPr)
            t = OxmlElement("w:t")
            t.text = "Code →"
            r.append(t)
            hl.append(r)
            p.append(hl)
        else:
            _add_run(p, "Code →", bold=False)

        paras.append(p)

        # Bullets
        for bid in entry.bullet_ids:
            text = bullet_text.get(bid, "")
            if text:
                bp = _make_para("ListParagraph")
                _add_run(bp, text, bold=False)
                paras.append(bp)

    return paras


# ---------------------------------------------------------------------------
# Section reorder
# ---------------------------------------------------------------------------


def _apply_order(doc: Document, section_order: list[str]) -> None:
    """Swap Skills and Projects sections when the order demands it."""
    if len(section_order) < 3 or section_order[1] != "Projects":
        return  # default order (Skills before Projects) — no change needed

    body = doc.element.body
    # Collect elements belonging to each section.
    skills_elems = _collect_section_elems(body, _SKILLS, _PROJECTS)
    proj_elems = _collect_section_elems(body, _PROJECTS, _EDUCATION)
    if not skills_elems or not proj_elems:
        return

    insert_pos = list(body).index(skills_elems[0])

    for e in skills_elems + proj_elems:
        body.remove(e)

    for i, e in enumerate(proj_elems + skills_elems):
        body.insert(insert_pos + i, e)


def _collect_section_elems(body, start_name: str, end_name: str) -> list:
    """Body elements from the named Heading 1 up to (not including) the next."""
    start_elem = _find_heading(body, start_name)
    end_elem = _find_heading(body, end_name)
    if start_elem is None:
        return []
    collecting = False
    result = []
    for child in list(body):
        if child is start_elem:
            collecting = True
        if collecting:
            if end_elem is not None and child is end_elem:
                break
            result.append(child)
    return result


def _find_heading(body, section_name: str):
    """Find the Heading 1 element whose stripped text matches ``section_name``."""
    for child in body:
        if child.tag != qn("w:p"):
            continue
        pPr = child.find(qn("w:pPr"))
        if pPr is None:
            continue
        pStyle = pPr.find(qn("w:pStyle"))
        if pStyle is None:
            continue
        # python-docx uses "Heading1" as the style val for "Heading 1"
        val = pStyle.get(qn("w:val"), "")
        if val not in ("Heading1", "Heading 1"):
            continue
        text = "".join(
            t.text or "" for t in child.iter(qn("w:t"))
        ).strip().rstrip("\t").strip()
        if text == section_name:
            return child
    return None
