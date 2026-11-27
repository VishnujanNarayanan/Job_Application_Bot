"""Application-form autofill routes (#154).

    GET  /autofill/jab.user.js   the userscript, with this endpoint's address in it
    POST /autofill/match         form fields in, one decision per field out

The Telegram Apply button opens the posting itself with ``#jab=<job_id>``
appended (:func:`src.notifications.tag_apply_url`), so Apply works with the
laptop off. With it on, the userscript sees the tag and calls /autofill/match.

The response carries personal data and the endpoint has no login: it is
reachable only on the operator's tailnet (see README). Two more guards against
a web page in the same browser calling it:

* no CORS headers, so another site's script cannot read a response;
* a required ``X-JAB`` header, which a cross-site request can only send after a
  CORS preflight this app never approves -- so a page cannot even make the
  endpoint write a question to the bank. The userscript's own requests
  (``GM_xmlhttpRequest``) are not subject to CORS.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.autofill import bank
from src.autofill.facts import load_facts
from src.autofill.matcher import ats_of, match_form
from src.config import resolve_endpoint_base_url, settings

log = structlog.get_logger(__name__)

router = APIRouter()

_SCRIPT = Path(__file__).resolve().parent / "static" / "jab.user.js"
_MAX_FIELDS = 400


class FormField(BaseModel):
    ref: str = Field(..., max_length=64)
    label: str = Field("", max_length=2000)
    context: str = Field("", max_length=2000)
    name: str = Field("", max_length=300)
    id: str = Field("", max_length=300)
    automation_id: str = Field("", max_length=300)
    autocomplete: str = Field("", max_length=100)
    type: str = Field("", max_length=40)
    options: list[str] = Field(default_factory=list, max_length=400)
    required: bool = False


class MatchRequest(BaseModel):
    url: str = Field(..., max_length=4000)
    job_id: str | None = Field(None, max_length=200)
    #: True on the first call of a tab's session: logs the open (the "click").
    first: bool = False
    fields: list[FormField] = Field(default_factory=list, max_length=_MAX_FIELDS)


# job_id -> (fetched_at, job facts). Failures are cached too, so a locked or
# unreachable database costs one connect timeout per job, not one per page.
_JOB_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JOB_TTL = 600.0


def _job_facts(job_id: str | None) -> dict[str, Any]:
    """The advert's salary range, from the database when it is reachable."""
    if not job_id:
        return {}
    hit = _JOB_CACHE.get(job_id)
    if hit and time.monotonic() - hit[0] < _JOB_TTL:
        return hit[1]
    facts: dict[str, Any] = {}
    try:
        from src.state.db import session_scope
        from src.state.models import AllJobs

        with session_scope() as session:
            job = session.get(AllJobs, job_id)
            if job is not None:
                facts = {"found": True, "company": job.company, "role": job.role,
                         "salary_min_lpa": job.salary_min_lpa, "salary_max_lpa": job.salary_max_lpa}
            else:
                facts = {"found": False}
    except Exception as exc:  # noqa: BLE001 - the form still fills without it
        log.warning("autofill_job_lookup_failed", job_id=job_id, error=str(exc)[:200])
        facts = {"found": None}
    _JOB_CACHE[job_id] = (time.monotonic(), facts)
    return facts


_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _base_url(request: Request | None = None) -> str:
    """The address the browser reached this endpoint at.

    A request to localhost (Zen on the same laptop, no Tailscale needed) gets
    the local address, so a userscript installed from there calls back there.
    Anything else gets the configured ``endpoint.base_url``; a Host header is
    never trusted beyond the loopback names.
    """
    if request is not None and request.url.hostname in _LOOPBACK:
        return str(request.base_url).rstrip("/")
    return resolve_endpoint_base_url(str(settings.endpoint.get("base_url") or ""))


@router.get("/autofill/jab.user.js")
def userscript(request: Request) -> Response:
    """Install from this URL in Violentmonkey; updates come from the same URL."""
    base = _base_url(request)
    host = urlparse(base).hostname or "localhost"
    body = (_SCRIPT.read_text(encoding="utf-8")
            .replace("__JAB_ENDPOINT__", base).replace("__JAB_HOST__", host))
    return Response(body, media_type="text/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@router.post("/autofill/match")
def match(req: MatchRequest, request: Request,
          x_jab: str | None = Header(None)) -> dict[str, Any]:
    if x_jab != "1":
        raise HTTPException(status_code=403, detail="X-JAB header required")
    seen_at = ats_of(req.url)
    fields = [f.model_dump() for f in req.fields]
    job = _job_facts(req.job_id)
    facts = load_facts()
    decisions = match_form(fields, facts, job)

    by_ref = {f["ref"]: f for f in fields}
    unknown = [{**d, "type": by_ref[d["ref"]]["type"], "options": by_ref[d["ref"]]["options"]}
               for d in decisions if d["action"] == "unknown" and d.get("question")]
    try:
        if req.first:
            bank.record_open(req.job_id, req.url, seen_at)
        added = bank.record_unanswered(unknown, seen_at)
        bank.record_seen([{**d, "type": by_ref[d["ref"]]["type"], "options": by_ref[d["ref"]]["options"]}
                          for d in decisions if d.get("question")], seen_at)
    except Exception as exc:  # noqa: BLE001 - recording must never cost the fill
        log.warning("autofill_record_failed", error=str(exc)[:200])
        added = 0

    resume = None
    if req.job_id and any(d["action"] == "resume" for d in decisions):
        resume = {"url": f"{_base_url(request)}/resume/{req.job_id}.pdf",
                  "filename": settings.resume_filename}
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d["action"]] = counts.get(d["action"], 0) + 1
    log.info("autofill_match", seen_at=seen_at, job_id=req.job_id, fields=len(fields),
             new_questions=added, **counts)
    return {
        "decisions": decisions,
        "resume": resume,
        "job": {"found": job.get("found"),
                "salary_range": ([job.get("salary_min_lpa"), job.get("salary_max_lpa")]
                                 if job.get("salary_min_lpa") or job.get("salary_max_lpa") else None)},
        "new_questions": added,
    }
