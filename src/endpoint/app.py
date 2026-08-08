"""FastAPI resume endpoint — Layer 6 (§6.2).

Serves on-demand tailored resumes for matched jobs:

    GET /resume/{job_id}.pdf
    GET /resume/{job_id}.docx

Flow for each request:
  1. Look up applied.selection_json by job_id (404 if absent).
  2. Check render_cache + S3 for a valid cached render (instant).
  3. Cache miss: assemble DOCX from selection_json + master_profile.json,
     convert to PDF if needed, upload to S3, record in render_cache, serve.

Run locally:
    uvicorn src.endpoint.app:app --port 8000

On the Oracle VM (Iteration 5+) run behind a process manager (systemd /
supervisor) on port 8000 with a reverse proxy or direct access.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import Response as FastAPIResponse
from fastapi.staticfiles import StaticFiles

from src.endpoint import dashboard
from src.endpoint.cache import get_or_build
from src.state.db import session_scope

log = structlog.get_logger(__name__)

app = FastAPI(title="Job Bot", version="2.0")

# Only the dashboard's own assets are served statically. `data/` is never
# mounted: the CSV index holds every JD snippet, score and gap-skill list.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)
app.include_router(dashboard.router)

# Valid extensions
_VALID_EXT = re.compile(r"^(pdf|docx)$")


@app.get("/resume/{filename}")
def get_resume(filename: str) -> FastAPIResponse:
    """Serve a tailored resume PDF or DOCX for a matched job.

    ``filename`` must be of the form ``{job_id}.pdf`` or ``{job_id}.docx``.
    Returns 404 if no selection exists for the job, 400 for an unrecognised
    format, 500 if assembly or conversion fails.
    """
    # Parse job_id and extension from filename
    if "." not in filename:
        raise HTTPException(status_code=400, detail="Filename must end in .pdf or .docx")
    job_id, _, ext = filename.rpartition(".")

    if not _VALID_EXT.match(ext):
        raise HTTPException(status_code=400, detail=f"Unsupported format: {ext!r}")

    if not job_id:
        raise HTTPException(status_code=400, detail="Missing job_id in filename")

    log.info("resume_request", job_id=job_id, ext=ext)

    try:
        with session_scope() as session:
            data, content_type = get_or_build(job_id, ext, session)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No resume found for job_id={job_id!r}")
    except Exception as exc:
        # Catch-all endpoint failure: assembler error, propagated conversion
        # error, DB error, etc. Distinct from pdf_convert's `render_failed`
        # (LibreOffice-specific) so the two are not conflated in CloudWatch.
        log.error("resume_request_failed", job_id=job_id, ext=ext, status=500,
                  error=str(exc), exc_info=True)
        raise HTTPException(status_code=500, detail="Resume render failed") from exc

    return FastAPIResponse(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{job_id}.{ext}"',
            "Content-Length": str(len(data)),
        },
    )


@app.get("/health")
def health() -> dict:
    """Liveness probe."""
    return {"status": "ok"}
