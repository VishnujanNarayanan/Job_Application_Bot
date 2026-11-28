"""One auto-apply run: inside the window, under the caps, one LinkedIn match.

    python -m src.cli.autoapply run           # one run now (respects hours / caps)
    python -m src.cli.autoapply loop          # every 30 min while the laptop is on

State (what was tried today, and how) is kept in data/autoapply/state.json
(gitignored). Applied jobs are also marked ``applied`` in the database, exactly
as the Telegram "Mark applied" button does.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

from src.config import settings

log = structlog.get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "data" / "autoapply" / "state.json"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def cfg():
    return settings.autoapply


def now_local() -> datetime:
    return datetime.now(ZoneInfo(str(cfg().timezone)))


def in_window(at: datetime | None = None) -> bool:
    at = at or now_local()
    start, end = (int(x) for x in cfg().hours)
    return DAYS[at.weekday()] in [str(d).lower()[:3] for d in cfg().days] and start <= at.hour < end


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"tried": {}, "days": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def applied_today(state: dict, day: str) -> int:
    return int(state.get("days", {}).get(day, {}).get("applied", 0))


def notify(text: str) -> None:
    """A Telegram message, whatever the progress-message setting. Never raises.

    Sent from its own thread: Playwright's sync API keeps an event loop running
    in this one, where asyncio.run() refuses to start another.
    """
    import threading

    def send() -> None:
        try:
            from src.notifications import _send

            asyncio.run(_send(text))
        except Exception as exc:  # noqa: BLE001
            log.warning("autoapply_notify_failed", error=str(exc)[:200])

    t = threading.Thread(target=send, daemon=True)
    t.start()
    t.join(timeout=30)


def find_jobs(context) -> Path | None:
    """The search term's Easy Apply postings from the last hour, read in the Job Bot Edge.

    LinkedIn throttles the pipeline's plain-Python scrape (429) but not a real
    browser, so the bot finds its jobs here and hands them to the pipeline.
    """
    from src.autoapply.discover import discover, write

    c = cfg()
    page = context.new_page()
    try:
        rows = discover(page, str(c.search_term), c.get("location"),
                        int(c.get("posted_within_minutes", 60)) * 60,
                        max_jobs=int(c.get("max_found_per_run", 40)), log=log.info)
    finally:
        page.close()
    log.info("autoapply_found", jobs=len(rows))
    return write(rows, ROOT / "data" / "autoapply" / "found.json")


def run_pipeline(jobs_file: Path | None = None) -> None:
    """Score and tailor this run's jobs, as a normal pipeline run would.

    With ``jobs_file`` (found in Edge) nothing is scraped; without it the pipeline
    scrapes the term itself (LinkedIn, Easy Apply, last hour).
    """
    out = ROOT / "data" / "logs" / "autoapply-pipeline.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M} =====\n")
        fh.flush()
        hours = max(1, -(-int(cfg().get("posted_within_minutes", 60)) // 60))
        args = [sys.executable, "-m", "src.main", "--term", str(cfg().search_term), "--hours-old", str(hours)]
        if bool(cfg().get("ignore_applicants", True)):
            args.append("--ignore-applicants")  # applying costs nothing: a crowded posting still counts
        if jobs_file is not None:
            args += ["--jobs-file", str(jobs_file)]
        elif bool(cfg().get("easy_apply_only", True)):
            args.append("--easy-apply")  # LinkedIn's Easy Apply postings only: for this bot, never other runs
        import os

        # Its matches' PDFs go into the endpoint's cache: the upload reuses them, no re-render.
        env = {**os.environ, "JOBBOT_CACHE_RENDERS": "1"}
        subprocess.run(args, env=env,
                       cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, timeout=40 * 60, check=False)


def prefetch_resumes(job_ids: list[str]) -> dict:
    """Render each job's tailored PDF in the background, in order; an Event per job is set
    when its PDF is in the endpoint's cache (or the attempt failed: the upload retries)."""
    import threading

    from src.autoapply.linkedin import ENDPOINT, _forward

    ready = {j: threading.Event() for j in job_ids}

    def work() -> None:
        for j in job_ids:
            try:
                started = datetime.now()
                r = _forward({"url": f"{ENDPOINT}/resume/{j}.pdf", "blob": True})
                log.info("autoapply_resume_ready", job_id=j, status=r.get("status"),
                         seconds=round((datetime.now() - started).total_seconds(), 1))
            except Exception as exc:  # noqa: BLE001
                log.warning("autoapply_resume_prefetch_failed", job_id=j, error=str(exc)[:200])
            finally:
                ready[j].set()

    threading.Thread(target=work, daemon=True).start()
    return ready


def candidates(tried: dict, since: datetime | None = None) -> list[dict]:
    """LinkedIn matches built since ``since`` (this run's scrape of the search term),
    not tried before, best first. Easy Apply is checked on each job's page."""
    from sqlalchemy import select

    from src.state.db import session_scope
    from src.state.models import AllJobs, Applied

    since = since or datetime.now(timezone.utc) - timedelta(hours=float(cfg().max_match_age_hours))
    musts = [str(m).lower() for m in cfg().title_must_contain]
    with session_scope() as s:
        rows = s.execute(
            select(Applied.job_id, Applied.final_score, Applied.built_at, AllJobs.role, AllJobs.company,
                   AllJobs.job_url, AllJobs.site)
            .join(AllJobs, AllJobs.job_id == Applied.job_id)
            .where(AllJobs.site == "linkedin", Applied.user_status == "pending", Applied.built_at >= since)
            .order_by(Applied.final_score.desc().nullslast(), Applied.built_at.desc())
        ).all()
    out = []
    for r in rows:
        if r.job_id in tried or not r.job_url:
            continue
        if musts and not any(m in (r.role or "").lower() for m in musts):
            continue
        out.append({"job_id": r.job_id, "role": r.role, "company": r.company, "url": r.job_url,
                    "score": r.final_score})
    return out


def linkedin_id(job_id: str, url: str | None = "") -> str:
    import re

    m = re.search(r"(\d{8,12})", f"{job_id} {url or ''}")
    return m.group(1) if m else ""


def _defaults_text(defaults: list[str]) -> str:
    if not defaults:
        return ""
    return ("\n\nAnswered by default (no stored answer; tell me if any is wrong):\n"
            + "\n".join("• " + d for d in defaults[:10]))


def mark_applied(job_id: str) -> None:
    from src.state.db import session_scope
    from src.state.job_status import set_job_status

    with session_scope() as s:
        set_job_status(s, job_id, "applied")


def run_once(*, dry_run: bool | None = None, force: bool = False, pipeline: bool | None = None) -> dict:
    """One run. ``force`` ignores the window and ``enabled`` (for a manual try)."""
    from src.autoapply import edge
    from src.autoapply.linkedin import easy_apply

    c = cfg()
    dry = bool(c.dry_run) if dry_run is None else dry_run
    local = now_local()
    day = local.strftime("%Y-%m-%d")
    if not force and not (bool(c.enabled) or dry):
        return {"ran": False, "why": "autoapply.enabled is false"}
    if not force and not in_window(local):
        return {"ran": False, "why": "outside working hours"}
    state = load_state()
    if not dry and applied_today(state, day) >= int(c.max_per_day):
        return {"ran": False, "why": "daily cap reached"}

    # A Job Bot Edge that stopped navigating (it happens after long sessions) is restarted first.
    with edge.attached() as context:
        stuck = not edge.healthy(context)
    if stuck:
        log.warning("autoapply_edge_restart", why="a new tab did not navigate")
        edge.restart()

    since = None
    if bool(c.run_pipeline_first) if pipeline is None else pipeline:
        log.info("autoapply_pipeline_start", term=str(c.search_term))
        since = datetime.now(timezone.utc) - timedelta(minutes=1)
        found = None
        if str(c.get("find_jobs_in", "edge")) == "edge":
            with edge.attached() as context:
                found = find_jobs(context)
        run_pipeline(found)  # the search term, last hour only: its matches are this run's jobs

    # Dry runs keep their own list: each one moves to a new job, and none blocks a real apply later.
    tried = {**state.get("tried", {}), **(state.get("dry_tried", {}) if dry else {})}
    done = []
    with edge.attached() as context:
        page = context.new_page()
        try:
            # This run's scrape of the search term (last hour), matched and tailored by the
            # pipeline: every one with an Easy Apply button is applied to, up to the daily cap.
            jobs = candidates(tried, since=since)
            log.info("autoapply_candidates", matched=len(jobs))
            if not jobs:
                return {"ran": True, "applied": 0, "why": "no LinkedIn match in this run's scrape"}
            ready = prefetch_resumes([j["job_id"] for j in jobs])
            for job in jobs:
                # Its tailored PDF is rendered before LinkedIn is opened, never while the
                # resume step waits (a render takes ~30 s); the next job's renders meanwhile.
                ready[job["job_id"]].wait(timeout=180)
                filled = len([d for d in done if d["status"] in ("applied", "dry_run")])
                if int(c.per_run) and filled >= int(c.per_run):
                    break
                if not dry and applied_today(state, day) >= int(c.max_per_day):
                    break
                try:
                    outcome = easy_apply(page, job["job_id"], job["url"], dry_run=dry, log=log)
                except Exception as exc:  # noqa: BLE001 - one job must not stop the run
                    from src.autoapply.linkedin import Outcome, submitted
                    outcome = Outcome("error", str(exc)[:200])
                    # An error after Submit (LinkedIn redrawing the page) is still a sent application.
                    if not dry and submitted(page):
                        outcome = Outcome("applied", "Easy Apply submitted (confirmed on the job page)")
                    else:
                        from src.autoapply.linkedin import record_failure
                        try:
                            outcome.screenshot = record_failure(page, job["job_id"], "exception", str(exc)[:500],
                                                                job_url=job["url"])
                        except Exception:  # noqa: BLE001
                            pass
                log.info("autoapply_outcome", job_id=job["job_id"], status=outcome.status, detail=outcome.detail)
                record = {"status": outcome.status, "detail": outcome.detail, "at": local.isoformat(),
                          "role": job["role"], "company": job["company"], "screenshot": outcome.screenshot,
                          "questions": outcome.questions}
                state.setdefault("dry_tried" if dry else "tried", {})[job["job_id"]] = record
                done.append({**job, **record})
                title = f"{job['company']} — {job['role']}"
                if outcome.status == "applied":
                    mark_applied(job["job_id"])
                    today = state.setdefault("days", {}).setdefault(day, {"applied": 0})
                    today["applied"] = int(today.get("applied", 0)) + 1
                    notify(f"✅ Applied (Easy Apply): {title}\n{job['url']}\nApplied today: {today['applied']}/{int(c.max_per_day)}"
                           + _defaults_text(outcome.defaults))
                elif outcome.status == "dry_run":
                    notify(f"🧪 Dry run, ready to submit: {title}\n{job['url']}\nScreenshot: {outcome.screenshot}"
                           + _defaults_text(outcome.defaults))
                elif outcome.status == "skipped":
                    qs = "\n".join("• " + q for q in outcome.questions[:6])
                    notify(f"⏭ Skipped {title}: {outcome.detail}\n{qs}\n{job['url']}")
                elif outcome.status == "login":
                    notify("🔒 LinkedIn wants you to log in again in the Job Bot Edge:\n"
                           "python -m src.cli.autoapply login")
                    break
                elif outcome.status == "error":
                    notify(f"⚠ Easy Apply failed for {title}: {outcome.detail}\n{job['url']}")
                save_state(state)
        finally:
            page.close()
    return {"ran": True, "dry_run": dry, "results": [{k: d[k] for k in ("job_id", "status", "detail")} for d in done]}
