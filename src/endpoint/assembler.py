"""DOCX assembler — clone the template, substitute text only (Layer 6 / §6.3).

Produces a tailored DOCX from a ``StoredSelection`` and the current template.

Design principle: **the template is the layout authority.** The assembler never
builds paragraphs from style names — that loses the template's direct formatting
(tab stops, list/bullet references, indentation, spacing, run fonts/sizes). Instead
it deep-clones the template's own paragraphs as formatting *prototypes* and replaces
ONLY the text inside their runs. Everything else — `<w:tabs>`, `<w:numPr>`, `<w:ind>`,
`<w:spacing>`, each run's `<w:rPr>` — is preserved byte-for-byte.

Tailored regions: summary text (para 2), WORK EXPERIENCE, SKILLS, PROJECTS.
Static regions (left verbatim): header (paras 0-1), EDUCATION, CERTIFICATES.

Algorithm:
  1. Load a fresh template copy (original never modified); load a reference copy.
  2. Assert header (paras 0-1) unchanged (hard rule #10).
  3. Fill the summary paragraph (para 2) text.
  4. Capture deep-copy prototypes of the first experience block (title/company/
     bullet), the first project block (name/bullet), and a skill paragraph —
     BEFORE any mutation, so edits cannot corrupt the prototypes.
  5. Build WORK / SKILLS / PROJECTS by cloning prototypes and substituting text;
     replace each section's content between its heading and the next (by live
     element identity — never stale indices).
  6. Reorder Skills <-> Projects per ``selection.section_order`` if needed.
  7. Re-assert header unchanged; diff-validate EDUCATION + CERTIFICATES unchanged
     (hard rule #9).
  8. Save; update project hyperlink targets in document.xml.rels by r:id (#11).
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

_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


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

    ``profile_json_path`` is the canonical ``master_profile.json``. Bullet texts,
    company/dates/location, and project names/links are read from it.
    """
    profile = json.loads(profile_json_path.read_text())
    bullet_text, exp_map, proj_map, summary_map = _index_profile(profile)

    # doc = working copy; ref = immutable reference for the header diff-check.
    doc = Document(str(template_path))
    ref = Document(str(template_path))

    # Hard rule #10: header (paras 0-1) untouched throughout.
    _assert_header_unchanged(doc, ref)

    # Hard rule #9: snapshot the static EDUCATION+CERTIFICATES region to verify
    # it is byte-identical after assembly.
    static_before = _static_region_canonical(doc)

    # --- summary (para 2): replace text, keep its paragraph formatting ---
    _set_text(doc.paragraphs[2]._p, summary_map.get(selection.summary_id, ""))

    # --- capture prototypes BEFORE mutating the body ---
    protos = _capture_prototypes(doc)

    # --- build tailored sections from cloned prototypes ---
    proj_rids: dict[str, str] = {}
    work_elems = _build_work(selection, exp_map, bullet_text, protos)
    skills_elems = _build_skills(selection, protos)
    proj_elems = _build_projects(selection, proj_map, bullet_text, protos, proj_rids)

    _replace_content(doc, _WORK, _SKILLS, work_elems)
    _replace_content(doc, _SKILLS, _PROJECTS, skills_elems)
    _replace_content(doc, _PROJECTS, _EDUCATION, proj_elems)

    # --- section reorder (Skills <-> Projects) ---
    _apply_order(doc, selection.section_order)

    # --- header diff-check + static-region diff-check (hard rules #10, #9) ---
    _assert_header_unchanged(doc, ref)
    if _static_region_canonical(doc) != static_before:
        raise AssemblerError(
            "EDUCATION/CERTIFICATES region was modified. The assembler must leave "
            "static sections byte-identical to the template (hard rule #9)."
        )

    # --- save ---
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))

    # --- update project hyperlink targets in rels XML (hard rule #11) ---
    if proj_rids:
        update_project_hyperlinks(output_path, proj_rids)

    log.info(
        "docx_assembled",
        job_id=selection.job_id,
        template_version=selection.template_version,
        experiences=len(selection.experiences),
        projects=len(selection.projects),
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
# Run-level text substitution (preserves rPr, tabs, numPr, hyperlinks)
# ---------------------------------------------------------------------------


def _direct_runs(p_elem) -> list:
    """Direct <w:r> children of a paragraph (excludes runs inside <w:hyperlink>)."""
    return p_elem.findall(qn("w:r"))


def _set_run_text(r_elem, text: str) -> None:
    """Set a run's <w:t> text, preserving its <w:rPr>."""
    t = r_elem.find(qn("w:t"))
    if t is None:
        t = OxmlElement("w:t")
        r_elem.append(t)
    t.text = text
    if text != text.strip():
        t.set(_XML_SPACE, "preserve")


def _blank_run_text(r_elem) -> None:
    """Empty a run's <w:t> (keeps the run + rPr so structure/formatting persists)."""
    t = r_elem.find(qn("w:t"))
    if t is not None:
        t.text = ""


def _has_tab(p_elem) -> bool:
    return any(r.find(qn("w:tab")) is not None for r in _direct_runs(p_elem))


def _has_numpr(p_elem) -> bool:
    pPr = p_elem.find(qn("w:pPr"))
    return pPr is not None and pPr.find(qn("w:numPr")) is not None


def _set_text(p_elem, text: str) -> None:
    """Single-text paragraph (bullet / company): set first run, blank the rest."""
    runs = _direct_runs(p_elem)
    if not runs:
        return
    _set_run_text(runs[0], text)
    for r in runs[1:]:
        _blank_run_text(r)


def _set_tabbed(p_elem, before: str, after: str | None) -> None:
    """Tab-separated paragraph: set the text before and (optionally) after the tab.

    Runs are split at the run containing ``<w:tab/>``. ``after=None`` leaves
    everything past the tab untouched (used for project name + "Code →" link).
    """
    runs = _direct_runs(p_elem)
    tab_idx = next(
        (i for i, r in enumerate(runs) if r.find(qn("w:tab")) is not None), -1
    )
    if tab_idx < 0:
        _set_text(p_elem, before)
        return

    pre, post = runs[:tab_idx], runs[tab_idx + 1:]
    if pre:
        _set_run_text(pre[0], before)
        for r in pre[1:]:
            _blank_run_text(r)
    if after is not None and post:
        _set_run_text(post[0], after)
        for r in post[1:]:
            _blank_run_text(r)


def _set_skill(p_elem, name: str, skills_csv: str) -> None:
    """Skill category paragraph: bold "Name: " run + list run; blank extras."""
    runs = _direct_runs(p_elem)
    if not runs:
        return
    _set_run_text(runs[0], f"{name}: ")
    if len(runs) > 1:
        _set_run_text(runs[1], skills_csv)
    for r in runs[2:]:
        _blank_run_text(r)


# ---------------------------------------------------------------------------
# Prototype capture
# ---------------------------------------------------------------------------


def _content_elems(doc: Document, start_name: str, end_name: str) -> list:
    """Paragraph elements between two Heading 1s (excluding the headings)."""
    body = doc.element.body
    elems = _collect_section_elems(body, start_name, end_name)
    return [e for e in elems[1:] if e.tag == qn("w:p")]  # drop the heading itself


def _capture_prototypes(doc: Document) -> dict:
    """Deep-copy the formatting prototypes from the first block of each section."""
    work = _content_elems(doc, _WORK, _SKILLS)
    skills = _content_elems(doc, _SKILLS, _PROJECTS)
    projects = _content_elems(doc, _PROJECTS, _EDUCATION)

    exp_title = next((p for p in work if _has_tab(p) and not _has_numpr(p)), None)
    exp_bullet = next((p for p in work if _has_numpr(p)), None)
    # company = the paragraph right after the first title that is not a bullet.
    exp_company = None
    if exp_title is not None:
        after = work[work.index(exp_title) + 1:]
        exp_company = next((p for p in after if not _has_numpr(p)), None)

    skill = skills[0] if skills else None

    proj_name = next(
        (p for p in projects if p.find(qn("w:hyperlink")) is not None), None
    )
    proj_bullet = next((p for p in projects if _has_numpr(p)), None)

    missing = [
        n for n, v in {
            "exp_title": exp_title, "exp_company": exp_company,
            "exp_bullet": exp_bullet, "skill": skill,
            "proj_name": proj_name, "proj_bullet": proj_bullet,
        }.items() if v is None
    ]
    if missing:
        raise AssemblerError(
            f"Template prototype(s) not found: {missing}. The template must contain "
            "at least one full experience block, skill line, and project block."
        )

    return {
        "exp_title": copy.deepcopy(exp_title),
        "exp_company": copy.deepcopy(exp_company),
        "exp_bullet": copy.deepcopy(exp_bullet),
        "skill": copy.deepcopy(skill),
        "proj_name": copy.deepcopy(proj_name),
        "proj_bullet": copy.deepcopy(proj_bullet),
    }


# ---------------------------------------------------------------------------
# Section builders (clone prototype -> substitute text)
# ---------------------------------------------------------------------------


def _build_work(selection, exp_map, bullet_text, protos) -> list:
    out = []
    for entry in selection.experiences:
        exp = exp_map.get(entry.exp_id, {})
        company = exp.get("company", "")
        location = exp.get("location") or ""
        start = exp.get("start_date", "")
        end = exp.get("end_date", "")
        dates = f"{start} – {end}" if start else end
        company_str = f"{company} - {location}" if location else company

        # Titles are UPPERCASE in the template (the IntenseReference char-style
        # adds smallCaps on top); match that so the rendering is uniform full
        # caps rather than small-caps on mixed-case text.
        title_p = copy.deepcopy(protos["exp_title"])
        _set_tabbed(title_p, entry.title_alias.upper(), dates)
        out.append(title_p)

        company_p = copy.deepcopy(protos["exp_company"])
        _set_text(company_p, company_str)
        out.append(company_p)

        for bid in entry.bullet_ids:
            text = bullet_text.get(bid, "")
            if not text:
                continue
            bp = copy.deepcopy(protos["exp_bullet"])
            _set_text(bp, text)
            out.append(bp)
    return out


def _build_skills(selection, protos) -> list:
    out = []
    for cat in selection.skills.categories:
        p = copy.deepcopy(protos["skill"])
        _set_skill(p, cat.name, ", ".join(cat.skills))
        out.append(p)

    fw = selection.skills.familiar_with
    if fw:
        p = copy.deepcopy(protos["skill"])
        _set_skill(p, "Familiar With", ", ".join(fw))
        out.append(p)
    return out


def _build_projects(selection, proj_map, bullet_text, protos, proj_rids) -> list:
    out = []
    for i, entry in enumerate(selection.projects):
        proj = proj_map.get(entry.proj_id, {})
        name = proj.get("name", entry.proj_id)
        link = entry.link or proj.get("link", "")

        # Project names are UPPERCASE in the template (same smallCaps reason).
        name_p = copy.deepcopy(protos["proj_name"])
        _set_tabbed(name_p, name.upper(), None)  # leave the "Code →" hyperlink alone
        if link:
            rid = f"rIdProj{i + 1}"
            hl = name_p.find(qn("w:hyperlink"))
            if hl is not None:
                hl.set(qn("r:id"), rid)
                proj_rids[rid] = link
        out.append(name_p)

        for bid in entry.bullet_ids:
            text = bullet_text.get(bid, "")
            if not text:
                continue
            bp = copy.deepcopy(protos["proj_bullet"])
            _set_text(bp, text)
            out.append(bp)
    return out


# ---------------------------------------------------------------------------
# Section content replacement (by live element identity — no stale indices)
# ---------------------------------------------------------------------------


def _replace_content(
    doc: Document, start_name: str, end_name: str, new_elems: list
) -> None:
    """Replace content between two Heading 1s with ``new_elems`` (headings kept)."""
    body = doc.element.body
    start_h = _find_heading(body, start_name)
    if start_h is None:
        return
    end_h = _find_heading(body, end_name)

    to_remove = []
    collecting = False
    for child in list(body):
        if child is start_h:
            collecting = True
            continue
        if collecting:
            if end_h is not None and child is end_h:
                break
            to_remove.append(child)
    for e in to_remove:
        body.remove(e)

    pos = list(body).index(start_h)
    for i, e in enumerate(new_elems):
        body.insert(pos + 1 + i, e)


# ---------------------------------------------------------------------------
# Header + static-region diff-checks (hard rules #10, #9)
# ---------------------------------------------------------------------------


def _para_canonical(p) -> bytes:
    """Canonical byte representation of a paragraph element (lxml c14n)."""
    return etree.tostring(p._p, method="c14n")


def _assert_header_unchanged(doc: Document, ref: Document) -> None:
    """Assert paras 0-1 in ``doc`` match ``ref`` exactly (hard rule #10)."""
    for i in range(2):
        if _para_canonical(doc.paragraphs[i]) != _para_canonical(ref.paragraphs[i]):
            raise AssemblerError(
                f"Header paragraph {i} was modified. The assembler must never touch "
                "paragraphs before WORK EXPERIENCE (hard rule #10)."
            )


def _static_region_canonical(doc: Document) -> bytes:
    """c14n of every element from the EDUCATION heading to the document end.

    Covers EDUCATION + CERTIFICATES (the static, non-tailored tail). Used to verify
    the assembler left those sections byte-identical to the template (hard rule #9).
    """
    body = doc.element.body
    edu = _find_heading(body, _EDUCATION)
    if edu is None:
        return b""
    parts: list[bytes] = []
    collecting = False
    for child in body:
        if child is edu:
            collecting = True
        if collecting:
            parts.append(etree.tostring(child, method="c14n"))
    return b"".join(parts)


# ---------------------------------------------------------------------------
# Section reorder (Skills <-> Projects)
# ---------------------------------------------------------------------------


def _apply_order(doc: Document, section_order: list[str]) -> None:
    """Swap the Skills and Projects sections when the order demands it."""
    if len(section_order) < 3 or section_order[1] != "Projects":
        return  # default order (Skills before Projects) — no change needed

    body = doc.element.body
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
        val = pStyle.get(qn("w:val"), "")
        if val not in ("Heading1", "Heading 1"):
            continue
        text = "".join(
            t.text or "" for t in child.iter(qn("w:t"))
        ).strip().rstrip("\t").strip()
        if text == section_name:
            return child
    return None
