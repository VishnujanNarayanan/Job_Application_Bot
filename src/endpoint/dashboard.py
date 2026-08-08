"""Operator dashboard — Layer 6, browser surface (§6.3).

Read-only views over the pipeline's state plus a way to start a run:

    GET  /                     -> redirect to /dashboard
    GET  /dashboard            matched jobs, newest first
    GET  /dashboard/skipped    in-field jobs rejected on score
    GET  /api/jobs             the same match data as JSON
    POST /api/run              start a run (local subprocess or GitHub)
    GET  /api/run/status       poll for progress and log lines

The dashboard reads Postgres directly rather than the CSV index, so it shows
runs that happened anywhere — including a phone-triggered GitHub Actions run.
Loading ``/dashboard`` also regenerates the CSVs, which is what pulls remote
runs into the local files with no manual sync step.

Served by the same uvicorn process as the resume endpoint, so resume links
are same-origin. There is no authentication: the whole surface is reachable
only over the operator's private Tailscale network (see README).
"""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.config import settings
from src.endpoint import runner
from src.state.db import session_scope

log = structlog.get_logger(__name__)

_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

router = APIRouter()


# ---------------------------------------------------------------------------
# Data shaping
# ---------------------------------------------------------------------------

def _title_of(selection_json, fallback: str) -> str:
    """The LLM-chosen title alias, so the page agrees with the resume."""
    if isinstance(selection_json, dict):
        experiences = selection_json.get("experiences") or []
        if experiences and isinstance(experiences[0], dict):
            return experiences[0].get("title_alias") or fallback
    return fallback


def _match_views(session) -> list[dict]:
    """Matched jobs, highest score first."""
    from sqlalchemy import select

    from src.state.models import AllJobs, Applied

    stmt = (
        select(Applied, AllJobs)
        .join(AllJobs, Applied.job_id == AllJobs.job_id)
        .order_by(Applied.final_score.desc().nullslast(), Applied.built_at.desc())
    )
    views = []
    for applied, job in session.execute(stmt):
        views.append({
            "job_id": job.job_id,
            "company": job.company or "—",
            "title": _title_of(applied.selection_json, job.role or "—"),
            "score": applied.final_score,
            "fit": applied.fit_score,
            "success_prob": applied.success_prob,
            "location": job.location or "",
            "location_type": job.location_type or "",
            "salary": f"{job.salary_max_lpa:.0f} LPA" if job.salary_max_lpa else "",
            "source": job.site or "",
            "status": applied.user_status or "pending",
            "gap_skills": list(applied.gap_skills or []),
            "apply_url": job.job_url or "",
            "pdf_url": f"/resume/{job.job_id}.pdf",
            "docx_url": f"/resume/{job.job_id}.docx",
            "built_at": applied.built_at.isoformat() if applied.built_at else None,
        })
    return views


def _skipped_views(session, limit: int) -> list[dict]:
    """Jobs that were parsed and scored but fell below the threshold.

    Capped: the skipped table is by far the biggest and the operator only
    ever scans the near-misses at the top.
    """
    from sqlalchemy import select

    from src.reasons import LOW_SCORE
    from src.state.models import AllJobs, NotApplied

    stmt = (
        select(NotApplied, AllJobs)
        .join(AllJobs, NotApplied.job_id == AllJobs.job_id)
        .where(NotApplied.reason_category == LOW_SCORE)
        .order_by(NotApplied.not_applied_at.desc())
        .limit(limit)
    )
    views = []
    for na, job in session.execute(stmt):
        score = na.final_score
        if score is None and na.reason_detail:
            try:
                score = float(na.reason_detail)
            except ValueError:
                score = None
        views.append({
            "job_id": job.job_id,
            "company": job.company or "—",
            "title": job.role or "—",
            "score": score,
            "location": job.location or "",
            "source": job.site or "",
            "apply_url": job.job_url or "",
            "skipped_at": na.not_applied_at.isoformat() if na.not_applied_at else None,
        })
    return views


def _page_context(request: Request, session) -> dict:
    """Fields every page's chrome needs."""
    from sqlalchemy import func, select

    from src.state.models import Applied, NotApplied

    return {
        "request": request,
        "threshold": settings.scoring.apply_threshold,
        "match_count": session.scalar(select(func.count()).select_from(Applied)) or 0,
        "skipped_count": session.scalar(
            select(func.count()).select_from(NotApplied)
        ) or 0,
        "run": runner.get_state(),
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard")
def dashboard(request: Request):
    """Matched jobs, plus a refresh of the local CSV index.

    The export is what makes a phone-triggered run show up in the CSVs: it is
    a projection of Postgres, so regenerating it here costs one pass over the
    rows and needs no bookkeeping about which runs happened where.
    """
    from src.analytics import export_index

    with session_scope() as session:
        context = _page_context(request, session)
        context["matches"] = _match_views(session)
        export_index(session)

    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/skipped")
def skipped(request: Request):
    with session_scope() as session:
        context = _page_context(request, session)
        context["skipped"] = _skipped_views(
            session, int(settings.endpoint.dashboard.skipped_limit)
        )

    return templates.TemplateResponse(request, "skipped.html", context)


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@router.get("/api/jobs")
def api_jobs() -> JSONResponse:
    """Match data as JSON so the page can refresh without a reload."""
    with session_scope() as session:
        return JSONResponse({"matches": _match_views(session)})


@router.post("/api/run")
def api_run(payload: dict = Body(default={})) -> JSONResponse:
    """Start a pipeline run.

    ``target`` picks where: ``local`` streams logs back here but needs this
    machine awake; ``github`` dispatches the workflow, which keeps running if
    the laptop sleeps.
    """
    dry_run = bool(payload.get("dry_run", False))
    target = str(payload.get("target", "local"))

    if target == "github":
        ok, message = runner.dispatch_github_run(dry_run)
    elif target == "local":
        ok, message = runner.start_local_run(dry_run)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown target: {target!r}")

    if not ok:
        # 409 for "already running", 502 for anything the remote refused.
        status = 409 if "in progress" in message else 502
        return JSONResponse({"ok": False, "message": message}, status_code=status)

    return JSONResponse({"ok": True, "message": message, "run": runner.get_state()})


@router.get("/api/run/status")
def api_run_status() -> JSONResponse:
    return JSONResponse(runner.get_state())
