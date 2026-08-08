"""Operator dashboard — Layer 6, browser surface (§6.3).

Views over the pipeline's state, a way to start a run, and the operator's own
decision record:

    GET  /                        -> redirect to /dashboard
    GET  /dashboard               matches awaiting a decision
    GET  /dashboard/applied       jobs the operator has applied to
    GET  /dashboard/skipped       jobs that scored below the threshold
    GET  /api/jobs                match data as JSON
    POST /api/jobs/{job_id}/status   record applied / pending / dismissed
    POST /api/run                 start a run (local subprocess or GitHub)
    GET  /api/run/status          poll for progress and log lines

The applied flow exists because the system deliberately never submits an
application: the operator does that on the portal, so only they know it
happened. Clicking Apply opens the posting and arms a confirmation; when they
come back to this tab the page asks whether they applied, and a yes moves the
job into Applied with a timestamp.

The dashboard reads Postgres directly rather than the CSV index, so it shows
runs that happened anywhere — including a phone-triggered GitHub Actions run.
Loading ``/dashboard`` also regenerates the CSVs, which is what pulls remote
runs into the local files with no manual sync step.

There is no authentication: the whole surface is reachable only over the
operator's private Tailscale network (see README).
"""

from __future__ import annotations

from datetime import datetime, timezone
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

# Values `applied.user_status` may take. "dismissed" is the operator saying
# "not interested" — distinct from "skipped", which is the scorer's verdict.
_STATUSES = ("pending", "applied", "dismissed")


# ---------------------------------------------------------------------------
# Data shaping
# ---------------------------------------------------------------------------

def _title_of(selection_json, fallback: str) -> str:
    """The chosen title alias, so the page agrees with the resume."""
    if isinstance(selection_json, dict):
        experiences = selection_json.get("experiences") or []
        if experiences and isinstance(experiences[0], dict):
            return experiences[0].get("title_alias") or fallback
    return fallback


def _view(applied, job) -> dict:
    """One row, shared by every view and the JSON API."""
    return {
        "job_id": job.job_id,
        "company": job.company or "—",
        "title": _title_of(applied.selection_json, job.role or "—"),
        "scraped_role": job.role or "",
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
        "actioned_at": (
            applied.user_status_at.isoformat() if applied.user_status_at else None
        ),
    }


def _match_views(session, status: str = "pending") -> list[dict]:
    """Matched jobs with the given operator status.

    Pending sorts by score (what to look at next); applied sorts by when the
    operator acted (a record, read newest first).
    """
    from sqlalchemy import select

    from src.state.models import AllJobs, Applied

    stmt = (
        select(Applied, AllJobs)
        .join(AllJobs, Applied.job_id == AllJobs.job_id)
        .where(Applied.user_status == status)
    )
    if status == "applied":
        stmt = stmt.order_by(
            Applied.user_status_at.desc().nullslast(), Applied.built_at.desc()
        )
    else:
        stmt = stmt.order_by(
            Applied.final_score.desc().nullslast(), Applied.built_at.desc()
        )

    return [_view(applied, job) for applied, job in session.execute(stmt)]


def _skipped_views(session, limit: int) -> list[dict]:
    """Jobs parsed and scored but below the threshold, newest first.

    Capped: this is the biggest table and only the near-misses at the top
    carry information (a cluster just under the line means the threshold wants
    moving).
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


def _counts(session) -> dict[str, int]:
    """Row counts per view, for the nav badges."""
    from sqlalchemy import func, select

    from src.reasons import LOW_SCORE
    from src.state.models import Applied, NotApplied

    by_status = dict(
        session.execute(
            select(Applied.user_status, func.count()).group_by(Applied.user_status)
        ).all()
    )
    skipped = session.scalar(
        select(func.count())
        .select_from(NotApplied)
        .where(NotApplied.reason_category == LOW_SCORE)
    ) or 0

    return {
        "pending": int(by_status.get("pending", 0)),
        "applied": int(by_status.get("applied", 0)),
        "dismissed": int(by_status.get("dismissed", 0)),
        "skipped": int(skipped),
    }


def _page_context(request: Request, session) -> dict:
    return {
        "request": request,
        "threshold": settings.scoring.apply_threshold,
        "counts": _counts(session),
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
    """Matches awaiting a decision, plus a refresh of the local CSV index.

    The export is what makes a phone-triggered run show up in the CSVs: it is
    a projection of Postgres, so regenerating it here costs one pass over the
    rows and needs no bookkeeping about which runs happened where.
    """
    from src.analytics import export_index

    with session_scope() as session:
        context = _page_context(request, session)
        context["matches"] = _match_views(session, "pending")
        export_index(session)

    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/applied")
def applied(request: Request):
    with session_scope() as session:
        context = _page_context(request, session)
        context["matches"] = _match_views(session, "applied")

    return templates.TemplateResponse(request, "applied.html", context)


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
def api_jobs(status: str = "pending") -> JSONResponse:
    """Match data as JSON so the page can refresh without a reload."""
    if status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status: {status!r}")
    with session_scope() as session:
        return JSONResponse(
            {"status": status, "matches": _match_views(session, status)}
        )


@router.post("/api/jobs/{job_id}/status")
def api_set_status(job_id: str, payload: dict = Body(default={})) -> JSONResponse:
    """Record the operator's decision on a matched job.

    The system never applies to anything, so this is the only way it can know
    an application happened. Called when the operator confirms "yes, I
    applied" after returning from the posting.
    """
    status = str(payload.get("status", "")).strip()
    if status not in _STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {_STATUSES}, got {status!r}",
        )

    from src.state.models import Applied

    with session_scope() as session:
        row = session.get(Applied, job_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"No matched job with id {job_id!r}"
            )

        previous = row.user_status
        row.user_status = status
        # Returning something to pending clears the action date rather than
        # leaving a stale one behind.
        row.user_status_at = (
            None if status == "pending" else datetime.now(timezone.utc)
        )
        session.commit()

        counts = _counts(session)

    log.info(
        "job_status_changed", job_id=job_id, was=previous, now=status
    )
    return JSONResponse({"ok": True, "job_id": job_id, "status": status, "counts": counts})


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
        status = 409 if "in progress" in message else 502
        return JSONResponse({"ok": False, "message": message}, status_code=status)

    return JSONResponse({"ok": True, "message": message, "run": runner.get_state()})


@router.get("/api/run/status")
def api_run_status() -> JSONResponse:
    return JSONResponse(runner.get_state())
