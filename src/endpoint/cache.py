"""S3 render-cache orchestrator (Layer 6 / §7.2 + §6.2).

Checks the ``render_cache`` Postgres table for a valid (non-expired,
matching template version) cached render. On a hit, downloads from S3 and
returns bytes. On a miss, assembles the DOCX, converts to PDF if needed,
uploads to S3, records in ``render_cache``, and returns bytes.

S3 failure is non-fatal: if the upload fails, the file is served directly
without caching (degraded-but-functional per §7.5).
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.aws.s3 import cache_get_bytes, cache_put
from src.config import settings
from src.endpoint.assembler import assemble_docx
from src.endpoint.pdf_convert import to_pdf, ConversionError
from src.llm.schemas import StoredSelection
from src.state.models import Applied, RenderCache

log = structlog.get_logger(__name__)

_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_CACHE_TTL_DAYS = 30
_ROOT = Path(__file__).resolve().parents[2]


def get_or_build(
    job_id: str,
    ext: str,
    db_session: Session,
) -> tuple[bytes, str]:
    """Return ``(file_bytes, content_type)`` for the requested resume format.

    Checks render_cache → S3 → assemble on miss; falls back to serving
    without caching if S3 operations fail (§7.5 degraded mode).
    """
    if ext not in _CONTENT_TYPES:
        raise ValueError(f"Unsupported format: {ext!r}")

    # Look up the stored selection
    applied_row: Applied | None = db_session.get(Applied, job_id)
    if applied_row is None or applied_row.selection_json is None:
        raise KeyError(f"No selection found for job_id={job_id!r}")

    selection = StoredSelection.model_validate(applied_row.selection_json)
    cache_key = f"{job_id}_{selection.template_version}"

    # Check render_cache table for a valid hit
    s3_uri = _check_render_cache(db_session, cache_key, ext)
    if s3_uri:
        data = cache_get_bytes(cache_key, ext)
        if data is not None:
            return data, _CONTENT_TYPES[ext]
        # Cache table entry exists but S3 object was deleted — fall through

    # Cache miss: assemble the DOCX
    data, s3_uri = _assemble_and_cache(selection, cache_key, ext, db_session)
    return data, _CONTENT_TYPES[ext]


def _check_render_cache(
    session: Session, cache_key: str, ext: str
) -> str | None:
    """Return S3 URI from render_cache if valid and not expired."""
    row = session.get(RenderCache, f"{cache_key}_{ext}")
    if row is None:
        return None
    if row.expires_at and row.expires_at < datetime.now(timezone.utc):
        return None
    # Also check template version match
    tmpl_ver = cache_key.split("_")[-1] if "_" in cache_key else ""
    if row.template_version and row.template_version != tmpl_ver:
        return None
    return row.s3_uri


def _assemble_and_cache(
    selection: StoredSelection,
    cache_key: str,
    ext: str,
    session: Session,
) -> tuple[bytes, str | None]:
    """Assemble the DOCX (and convert to PDF), upload to S3, record in render_cache."""
    profile_json = _ROOT / "master_profile.json"
    template_path = _ROOT / settings.endpoint.template_path

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        docx_path = tmp / f"{cache_key}.docx"

        assemble_docx(
            selection=selection,
            profile_json_path=profile_json,
            template_path=template_path,
            output_path=docx_path,
        )

        if ext == "pdf":
            try:
                pdf_path = to_pdf(docx_path, tmp)
                serve_path = pdf_path
            except ConversionError as exc:
                log.error("render_failed", job_id=selection.job_id, error=str(exc))
                raise
        else:
            serve_path = docx_path

        data = serve_path.read_bytes()

        # Upload to S3 (non-fatal on failure)
        s3_uri: str | None = None
        try:
            s3_uri = cache_put(serve_path, cache_key, ext)
            _record_render_cache(session, cache_key, ext, selection.template_version, s3_uri, selection.job_id)
            session.commit()
        except Exception as exc:
            log.error("s3_cache_failed", op="put", cache_key=cache_key, error=str(exc))
            # Degraded mode: serve without caching

        log.info(
            "resume_rendered",
            job_id=selection.job_id,
            fmt=ext,
            cache="miss",
        )
        return data, s3_uri


def _record_render_cache(
    session: Session,
    cache_key: str,
    ext: str,
    template_version: str,
    s3_uri: str,
    job_id: str,
) -> None:
    pk = f"{cache_key}_{ext}"
    expires = datetime.now(timezone.utc) + timedelta(days=_CACHE_TTL_DAYS)
    row = session.get(RenderCache, pk)
    if row is None:
        session.add(
            RenderCache(
                cache_key=pk,
                job_id=job_id,
                format=ext,
                template_version=template_version,
                s3_uri=s3_uri,
                expires_at=expires,
            )
        )
    else:
        row.s3_uri = s3_uri
        row.template_version = template_version
        row.expires_at = expires
