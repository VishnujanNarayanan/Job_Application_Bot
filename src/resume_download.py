"""How a tailored resume presents itself when the operator opens it (#42).

Both places that hand out a resume -- presigned S3 links on the Telegram
buttons, and the laptop endpoint's ``/resume/{job_id}.{ext}`` -- used to
serve it under its cache key (``linkedin-li-4475197639_30f55250-kb1.pdf``)
and, for S3, as ``binary/octet-stream``. A browser treats that as an unknown
file and downloads it on the spot, under a name nobody would submit.

Now:

- **PDF** goes out as ``application/pdf`` with ``inline``, so the browser
  OPENS it in its viewer instead of downloading it. Saving from the viewer is
  a deliberate act and starts from the name below.
- **DOCX** can't be viewed in a browser, so it stays an ``attachment`` -- but
  under the same readable name.

Whether the browser then asks WHERE to save is its own setting (Firefox:
Settings > General > Downloads > "Always ask you where to save files");
no header can force a save dialog.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TEMPLATE = "{name}_Resume"


@lru_cache(maxsize=1)
def _candidate_name() -> str:
    """``personal.name`` from the canonical profile JSON, or "" if unreadable."""
    try:
        data = json.loads((_ROOT / "master_profile.json").read_text())
        return str(data.get("personal", {}).get("name") or "")
    except (OSError, ValueError):
        return ""


def download_stem() -> str:
    """``Vishnujan_Narayanan_Resume``: the template from config, filled in and
    reduced to characters every filesystem and header accepts."""
    from src.config import settings

    template = str(settings.storage.get("resume_download_name") or _DEFAULT_TEMPLATE)
    stem = template.format(name=_candidate_name()).strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return stem or "Resume"


def content_disposition(ext: str) -> str:
    """The header value: view a PDF in the browser, download a DOCX."""
    mode = "inline" if ext == "pdf" else "attachment"
    return f'{mode}; filename="{download_stem()}.{ext}"'
