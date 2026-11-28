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
    """A Telegram message, whatever the progress-message setting. Never raises."""
    try:
        from src.notifications import _send

        asyncio.run(_send(text))
    except Exception as exc:  # noqa: BLE001
        log.warning("autoapply_notify_failed", error=str(exc)[:200])


def run_pipeline() -> None:
    """Search, score and tailor for the search term, as a normal pipeline run would."""
    out = ROOT / "data" / "logs" / "autoapply-pipeline.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M} =====\n")
        fh.flush()
        hours = max(1, -(-int(cfg().get("posted_within_minutes", 60)) // 60))
        subprocess.run([sys.executable, "-m", "src.main", "--term", str(cfg().search_term), "--hours-old", str(hours)],
                       cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, timeout=40 * 60, check=False)


def candidates(tried: dict, only_ids: list[str] | None = None) -> list[dict]:
    """New LinkedIn matches for the title. With ``only_ids`` (this run's Easy Apply
    search results), only those jobs, in the search's order."""
    from sqlalchemy import select

    from src.state.db import session_scope
    from src.state.models import AllJobs, Applied

    since = datetime.now(timezone.utc) - timedelta(hours=float(cfg().max_match_age_hours))
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
    if only_ids is not None:
        order = {lid: i for i, lid in enumerate(only_ids)}
        out = [j for j in out if linkedin_id(j["job_id"], j["url"]) in order]
        out.sort(key=lambda j: order[linkedin_id(j["job_id"], j["url"])])
    return out


def linkedin_id(job_id: str, url: str | None = "") -> str:
    import re

    m = re.search(r"(\d{8,12})", f"{job_id} {url or ''}")
    return m.group(1) if m else ""


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

    if bool(c.run_pipeline_first) if pipeline is None else pipeline:
        log.info("autoapply_pipeline_start", term=str(c.search_term))
        run_pipeline()

    # Dry runs keep their own list: each one moves to a new job, and none blocks a real apply later.
    tried = {**state.get("tried", {}), **(state.get("dry_tried", {}) if dry else {})}
    done = []
    with edge.attached() as context:
        page = context.new_page()
        try:
            # Only the search's own results: "data engineer", Easy Apply, posted in the last hour.
            from src.autoapply.linkedin import search_job_ids

            found = search_job_ids(page, str(c.search_term), int(c.get("posted_within_minutes", 60)),
                                   str(c.get("location", "India")))
            jobs = candidates(tried, only_ids=found)
            log.info("autoapply_candidates", search_results=len(found), matched=len(jobs))
            if not jobs:
                return {"ran": True, "applied": 0, "search_results": len(found),
                        "why": "no Easy Apply result from the last hour that the pipeline matched"}
            for job in jobs:
                if len([d for d in done if d["status"] in ("applied", "dry_run")]) >= int(c.per_run):
                    break
                try:
                    outcome = easy_apply(page, job["job_id"], job["url"], dry_run=dry, log=log)
                except Exception as exc:  # noqa: BLE001 - one job must not stop the run
                    from src.autoapply.linkedin import Outcome
                    outcome = Outcome("error", str(exc)[:200])
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
                    notify(f"✅ Applied (Easy Apply): {title}\n{job['url']}\nApplied today: {today['applied']}/{int(c.max_per_day)}")
                elif outcome.status == "dry_run":
                    notify(f"🧪 Dry run, ready to submit: {title}\n{job['url']}\nScreenshot: {outcome.screenshot}")
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
