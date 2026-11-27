"""Application-form autofill routes (#154).

    GET  /autofill/jab.user.js   the userscript, with this endpoint's address in it
    POST /autofill/match         form fields in, one decision per field out
    POST /autofill/learn         what was submitted: learn answers (src/autofill/learn.py)
    POST /autofill/opened        a job opened from Telegram: its company and role, and its
                                 resume copied to the Downloads folder
    POST /autofill/debug         what a form looks like to the script, saved under data/logs
    GET  /autofill/site          how to find this site's fields the generic scan misses

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

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import structlog
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.autofill import bank
from src.autofill.facts import load_facts
from src.autofill.learn import record_submission
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
    placeholder: str = Field("", max_length=300)
    #: The repeated section a field sits in ("Work Experience 1") and Workday's field id.
    group: str = Field("", max_length=200)
    field_id: str = Field("", max_length=200)
    type: str = Field("", max_length=40)
    options: list[str] = Field(default_factory=list, max_length=400)
    required: bool = False


class MatchRequest(BaseModel):
    url: str = Field(..., max_length=4000)
    job_id: str | None = Field(None, max_length=200)
    #: True on the first call of a tab's session: logs the open (the "click").
    first: bool = False
    fields: list[FormField] = Field(default_factory=list, max_length=_MAX_FIELDS)
    #: Labels of fields the page had already answered (Workday pre-selects the phone code).
    prefilled: list[str] = Field(default_factory=list, max_length=100)


Value = str | list[str] | bool | None


class SubmittedField(BaseModel):
    question: str = Field("", max_length=2000)
    type: str = Field("", max_length=40)
    options: list[str] = Field(default_factory=list, max_length=400)
    #: What the field holds at submit, and what the autofill put there (None: nothing).
    value: Value = None
    filled: Value = None
    key: str | None = Field(None, max_length=60)
    source: str | None = Field(None, max_length=400)
    required: bool = False


class OpenedRequest(BaseModel):
    job_id: str = Field(..., max_length=200)


class DebugRequest(BaseModel):
    url: str = Field("", max_length=4000)
    title: str = Field("", max_length=1000)
    fields: list[dict[str, Any]] = Field(default_factory=list, max_length=400)
    refs: list[dict[str, Any]] = Field(default_factory=list, max_length=800)


class MissedField(BaseModel):
    selector: str = Field(..., max_length=300)
    question: str = Field("", max_length=300)


class LearnRequest(BaseModel):
    url: str = Field(..., max_length=4000)
    job_id: str | None = Field(None, max_length=200)
    fields: list[SubmittedField] = Field(default_factory=list, max_length=_MAX_FIELDS)
    #: Fields the scan didn't recognise but the operator filled: how to find them next time.
    missed: list[MissedField] = Field(default_factory=list, max_length=40)


# job_id -> (fetched_at, job facts). Failures are cached too, so a locked or
# unreachable database costs one connect timeout per job, not one per page.
_JOB_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_JOB_TTL = 600.0


def _job_facts(job_id: str | None) -> dict[str, Any]:
    """The advert's company, site, location and salary range, from the database when reachable."""
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
                facts = {"found": True, "company": job.company, "role": job.role, "site": job.site,
                         "location": job.location,
                         "salary_min_lpa": job.salary_min_lpa, "salary_max_lpa": job.salary_max_lpa}
            else:
                facts = {"found": False}
    except Exception as exc:  # noqa: BLE001 - the form still fills without it
        log.warning("autofill_job_lookup_failed", job_id=job_id, error=str(exc)[:200])
        facts = {"found": None}
    _JOB_CACHE[job_id] = (time.monotonic(), facts)
    return facts


# ---------------------------------------------------------------------------
# The tailored resume, copied to the Downloads folder
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[2]
_SKIP_USERS = {"Default", "Default User", "Public", "All Users"}
_SAVED: dict[str, float] = {}
_SAVE_EVERY = 600.0


def downloads_dir() -> Path | None:
    """``autofill.save_resume_to``: a folder, ``auto`` (the one Windows user's
    Downloads under /mnt/c, as on this WSL laptop), or off."""
    want = (settings.get("autofill") or {}).get("save_resume_to", "auto")
    if not want:
        return None
    if str(want) != "auto":
        path = Path(str(want)).expanduser()
        return path if path.is_dir() else None
    users = Path("/mnt/c/Users")
    try:
        found = [d / "Downloads" for d in users.iterdir()
                 if d.name not in _SKIP_USERS and (d / "Downloads").is_dir()]
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


def save_resume_copy(job_id: str) -> Path | None:
    """Write the job's tailored PDF to Downloads, replacing the last one. None if not possible."""
    folder = downloads_dir()
    if folder is None:
        return None
    target = folder / settings.resume_filename
    try:
        from src.endpoint.cache import get_or_build
        from src.state.db import session_scope

        with session_scope() as session:
            data, _ = get_or_build(job_id, "pdf", session)
        tmp = folder / f".{settings.resume_filename}.part"
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except Exception as exc:  # noqa: BLE001 - the form fills without it
        log.warning("autofill_resume_copy_failed", job_id=job_id, error=str(exc)[:200])
        return None
    log.info("autofill_resume_copied", job_id=job_id, path=str(target), bytes=len(data))
    return target


def _copy_resume_soon(job_id: str | None) -> bool:
    """Start the copy in the background, at most once per job every ten minutes."""
    if not job_id or downloads_dir() is None:
        return False
    if time.monotonic() - _SAVED.get(job_id, -1e9) < _SAVE_EVERY:
        return True
    _SAVED[job_id] = time.monotonic()
    threading.Thread(target=save_resume_copy, args=(job_id,), daemon=True).start()
    return True


def _with_source(job: dict[str, Any], url: str) -> dict[str, Any]:
    """The board a posting was found on, from the job or the page's ``?source=LinkedIn``."""
    if job.get("site"):
        return job
    query = parse_qs(urlparse(url or "").query)
    for name in ("source", "utm_source", "src", "ref"):
        if query.get(name):
            return {**job, "site": query[name][0].lower()}
    return job


# ---------------------------------------------------------------------------
# Site recipes: fields the generic scan missed, learned per site
# ---------------------------------------------------------------------------

_SITES_LOCK = threading.Lock()
_MAX_PER_SITE = 40
_SELECTOR = re.compile(r"^[a-z][a-z0-9-]*([#.\[][^<>{}]*)?$", re.I)


def _sites_path() -> Path:
    return _ROOT / "data" / "ats_sites.json"


def _load_sites() -> dict[str, list[dict[str, str]]]:
    try:
        data = json.loads(_sites_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember_site_fields(host: str, missed: list[dict[str, str]]) -> int:
    """Keep how to find fields the operator filled that the scan missed, per host."""
    host = (host or "").lower()
    if not host:
        return 0
    with _SITES_LOCK:
        sites = _load_sites()
        known = sites.setdefault(host, [])
        added = 0
        for m in missed:
            sel = (m.get("selector") or "").strip()
            if not _SELECTOR.match(sel) or any(k["selector"] == sel for k in known):
                continue
            known.append({"selector": sel, "question": (m.get("question") or "")[:200],
                          "added": time.strftime("%Y-%m-%d")})
            added += 1
        sites[host] = known[-_MAX_PER_SITE:]
        if added:
            path = _sites_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(sites, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
            log.info("autofill_site_fields_learned", host=host, added=added)
    return added


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
    job = _with_source(_job_facts(req.job_id), req.url)
    facts = load_facts()
    decisions = match_form(fields, facts, job, site=seen_at, prefilled=req.prefilled)
    if req.first:
        _copy_resume_soon(req.job_id)

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
        "job": {"found": job.get("found"), "company": job.get("company"), "role": job.get("role"),
                "salary_range": ([job.get("salary_min_lpa"), job.get("salary_max_lpa")]
                                 if job.get("salary_min_lpa") or job.get("salary_max_lpa") else None)},
        "new_questions": added,
    }


@router.post("/autofill/learn")
def learn(req: LearnRequest, x_jab: str | None = Header(None)) -> dict[str, int]:
    """The userscript reports a submitted page; answers worth keeping are learned."""
    if x_jab != "1":
        raise HTTPException(status_code=403, detail="X-JAB header required")
    job = _job_facts(req.job_id)
    if req.missed:
        try:
            remember_site_fields(urlparse(req.url).hostname or "", [m.model_dump() for m in req.missed])
        except Exception as exc:  # noqa: BLE001
            log.warning("autofill_site_recipe_failed", error=str(exc)[:200])
    try:
        return record_submission([f.model_dump() for f in req.fields], req.url, job)
    except Exception as exc:  # noqa: BLE001 - a failed learn must not error the page
        log.warning("autofill_learn_failed", error=str(exc)[:200])
        return {"added": 0, "corrected": 0, "confirmed": 0}


@router.post("/autofill/opened")
def opened(req: OpenedRequest, x_jab: str | None = Header(None)) -> dict[str, Any]:
    """A job opened from Telegram (on a board, or remembered from one): name it,
    and copy its resume to Downloads."""
    if x_jab != "1":
        raise HTTPException(status_code=403, detail="X-JAB header required")
    job = _job_facts(req.job_id)
    copying = _copy_resume_soon(req.job_id) if job.get("found") is not False else False
    return {"found": job.get("found"), "company": job.get("company"), "role": job.get("role"),
            "saved_to": settings.resume_filename if copying else None}


@router.post("/autofill/debug")
def debug(req: DebugRequest, x_jab: str | None = Header(None)) -> dict[str, str]:
    """Keep what a form looked like to the script, to see why something wasn't filled.

    Saved locally under data/logs/autofill_debug/ (gitignored): it may hold
    what was typed into the form.
    """
    if x_jab != "1":
        raise HTTPException(status_code=403, detail="X-JAB header required")
    folder = _ROOT / "data" / "logs" / "autofill_debug"
    folder.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^a-z0-9]+", "-", ats_of(req.url).lower())[:60] + ".json"
    (folder / name).write_text(json.dumps(req.model_dump(), indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("autofill_debug_saved", path=str(folder / name), fields=len(req.fields))
    return {"saved": name}


@router.get("/autofill/site")
def site(host: str = "", x_jab: str | None = Header(None)) -> dict[str, list[str]]:
    """Selectors for this site's fields that the generic scan misses (learned from submits)."""
    if x_jab != "1":
        raise HTTPException(status_code=403, detail="X-JAB header required")
    return {"selectors": [k["selector"] for k in _load_sites().get((host or "").lower(), [])]}
