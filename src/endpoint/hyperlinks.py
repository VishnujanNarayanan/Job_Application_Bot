"""Update hyperlink targets in a DOCX without opening it with python-docx.

DOCX files are ZIP archives. Relationship targets live in
``word/_rels/document.xml.rels`` as ``Target`` attributes on ``Relationship``
elements. This module edits that file in-place inside the zip to update
project "Code →" link targets (and optionally certificate "Verify Here →"
targets) after the assembler has built the output DOCX.

Hard rule #10: the header hyperlinks (contact row, rId6–11) are never
touched — only the project link rIds are updated here.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

_RELS_PATH = "word/_rels/document.xml.rels"
# The <Relationships>/<Relationship> CONTAINER elements live in the package
# relationships namespace. This MUST be registered as the default (prefix-less)
# namespace on re-serialization — emitting them under a prefix (e.g. `ns0:`)
# makes Word lenient but LibreOffice refuse to load the file. (The relationship
# *Type* values below use the separate officeDocument namespace.)
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_HYPERLINK_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)


def update_project_hyperlinks(docx_path: str | Path, rId_to_url: dict[str, str]) -> None:
    """Rewrite the Target for specific relationship IDs in ``docx_path``.

    ``rId_to_url`` maps relationship id (e.g. ``"rId12"``) to the new URL.
    Operates in-place: rewrites the DOCX zip with the updated rels XML.
    """
    docx_path = Path(docx_path)
    if not rId_to_url:
        return

    # Read the zip into memory, patch the rels, write back.
    buf = io.BytesIO(docx_path.read_bytes())
    out_buf = io.BytesIO()

    with zipfile.ZipFile(buf, "r") as zin:
        with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == _RELS_PATH:
                    data = _patch_rels(data, rId_to_url)
                zout.writestr(item, data)

    docx_path.write_bytes(out_buf.getvalue())


def _patch_rels(rels_bytes: bytes, rId_to_url: dict[str, str]) -> bytes:
    """Return patched rels XML bytes for the project hyperlinks.

    For each ``rId`` referenced by a project hyperlink in ``document.xml``:
      - if a ``Relationship`` with that Id already exists, update its ``Target``;
      - otherwise ADD a new external hyperlink ``Relationship``.

    The assembler mints synthetic ids (``rIdProj1`` …) for the cloned project
    "Code →" links, so they are absent from the template's rels and MUST be
    added — a dangling ``r:id`` makes Word lenient but LibreOffice refuse to load
    the file ("source file could not be loaded"), failing PDF render.
    """
    ET.register_namespace("", _REL_NS)
    root = ET.fromstring(rels_bytes)

    seen: set[str] = set()
    for rel in root.iter(f"{{{_REL_NS}}}Relationship"):
        rid = rel.get("Id")
        if rid in rId_to_url and rel.get("Type") == _HYPERLINK_TYPE:
            rel.set("Target", rId_to_url[rid])
            seen.add(rid)

    # Add relationships for ids referenced in document.xml but missing here.
    for rid, url in rId_to_url.items():
        if rid in seen:
            continue
        rel = ET.SubElement(root, f"{{{_REL_NS}}}Relationship")
        rel.set("Id", rid)
        rel.set("Type", _HYPERLINK_TYPE)
        rel.set("Target", url)
        rel.set("TargetMode", "External")

    # Preserve the original XML declaration if present.
    declaration = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
    patched = ET.tostring(root, encoding="unicode").encode("utf-8")
    return declaration + patched
