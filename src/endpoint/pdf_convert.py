"""Convert a DOCX file to PDF using LibreOffice headless (Layer 6).

LibreOffice is the only free, high-fidelity DOCX→PDF converter that
preserves complex formatting (tab stops, native bullet numbering, fonts).
It must be installed on the Oracle VM: ``apt install libreoffice-writer``.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


class ConversionError(RuntimeError):
    """Raised when LibreOffice is missing or returns a non-zero exit code."""


def to_pdf(docx_path: str | Path, output_dir: str | Path) -> Path:
    """Convert ``docx_path`` → PDF in ``output_dir``; return the PDF path.

    Raises ``ConversionError`` if LibreOffice is not installed or fails.
    """
    docx_path = Path(docx_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    lo = shutil.which("libreoffice") or shutil.which("soffice")
    if not lo:
        raise ConversionError(
            "LibreOffice not found. Install with: apt install libreoffice-writer"
        )

    cmd = [lo, "--headless", "--convert-to", "pdf", "--outdir", str(output_dir), str(docx_path)]
    log.debug("pdf_convert_start", cmd=cmd)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        log.error(
            "render_failed",
            stage="pdf_convert",
            returncode=result.returncode,
            stderr=result.stderr[:500],
        )
        raise ConversionError(
            f"LibreOffice exited {result.returncode}: {result.stderr[:200]}"
        )

    pdf_path = output_dir / (docx_path.stem + ".pdf")
    if not pdf_path.exists():
        raise ConversionError(f"Expected PDF not found at {pdf_path}")

    log.info("pdf_converted", docx=str(docx_path), pdf=str(pdf_path))
    return pdf_path
