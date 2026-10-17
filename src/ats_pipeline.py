"""The ATS run (#97): companies' own job boards, polled on a schedule.

Usage:
    python -m src.ats_pipeline              # poll due boards, notify matches
    python -m src.ats_pipeline --dry-run    # notify to the test chat only
    python -m src.ats_pipeline --ats lever  # only these ATSes (testing)

A separate pipeline from the JobSpy run (``src.main``), because what an ATS
board returns is different: every job at the company rather than ones
matching a search term, no applicant count, no company name on Lever and
Ashby, several locations per job. The FRONT is ATS-specific; from the parse
on, every step is the shared one (``src.parser``, ``src.tailor``,
``src.notifications``, ``src.main``'s verdict writers), so a job is judged
the same whichever way it was found.

Front, in order (each skip is recorded in ``ats_postings.skipped``):

  A place         india / remote_open go on; remote_locked is recorded;
                  elsewhere is passed over; unknown waits for the description
  B structured    employment type, title blocklist, seniority label
  C location      excluded regions only when every location is excluded
  D role          clearly non-tech or executive titles are recorded, not sent
  E dedupe        already found on LinkedIn/Naukri: that job gets the ATS link
  F description   fetched for Greenhouse/SmartRecruiters
  G decide        unknown place -> India named in the description?
                  unclear title -> enough of the operator's skills named?
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from src.scraper import ats, ats_poller

_ROOT = Path(__file__).resolve().parents[1]

# --- Role families (stage D / G) -----------------------------------------------------

TECH = re.compile(
    r"machine learning|\bml\b|\bai\b|deep learning|\bnlp\b|computer vision|data scien|applied scien|"
    r"\bllm\b|gen ?ai|data engineer|analytics engineer|\betl\b|data platform|big data|data analyst|"
    r"bi engineer|business intelligence|data warehouse|\bquant\w*|algorithmic trading|devops|\bsre\b|"
    r"site reliability|platform engineer|infrastructure|cloud engineer|build engineer|release engineer|"
    r"kubernetes|\bqa\b|quality assurance|\btest\b|testing|\bsdet\b|automation engineer|front[- ]?end|"
    r"\bui\b engineer|\breact\b|\bangular\b|\bandroid\b|\bios\b|\bmobile\b|flutter|full[- ]?stack|back[- ]?end|"
    r"\bapi\b|\bjava\b|python|golang|\bgo\b developer|\bnode(\.?js)?\b|\.net\b|c\+\+|\brust\b|\bscala\b|"
    r"software|developer|"
    r"\bsde\b|programmer|engineer i\b|engineer ii\b|member of technical staff|\bmts\b|"
    r"application engineer|graduate engineer|solutions engineer|forward deployed",
    re.I)
NONTECH = re.compile(
    r"\b(sales|account (executive|manager)|business development|marketing|recruit\w*|talent acquisition|"
    r"hr|human resources|payroll|accountant|accounting|finance manager|audit\w*|tax|legal|counsel|"
    r"paralegal|nurse|clinical|pharmac\w*|physician|doctor|medical|therapist|chef|cook|hotel|"
    r"front office|housekeep\w*|waiter|bartender|receptionist|civil|construction|mechanical|"
    r"electrical engineer|driver|warehouse|logistics|supply chain|procurement|call center|telecaller|"
    r"content writer|copywriter|graphic designer|teacher|tutor|laboratory|chemist|biolog\w*|"
    r"store manager|retail|cashier|security guard|facility|customer (support|service|success)|trainer|"
    # Business roles that scored like tech ones in the dry runs (2026-10-16).
    r"finance|fp&a|treasury|controller|bizops|business operations|program operations|"
    r"product operations|revenue operations|sales operations|people operations|"
    r"operations (specialist|manager|executive|associate|coordinator)|service desk|help ?desk)\b",
    re.I)
EXEC = re.compile(r"\b(director|vice president|vp|head of|chief|cto|ceo|cfo|coo)\b", re.I)
# "Principal Engineer (AI, Office of the CEO)" is not an executive role.
_OFFICE_OF = re.compile(r"office of the \w+", re.I)
# Civil and industrial engineering, which shares words with software
# ("infrastructure", "automation", "QA"): non-tech unless a software word is
# also in the title (gate analysis, 2026-10-16).
CIVIL = re.compile(r"\b(utility|utilities|water|wastewater|hydraulic|mep|hvac|plc|substation|structural|"
                   r"qa/qc|qa ?& ?qc|geotech\w*|surveyor|piping|instrumentation)\b", re.I)
SOFTWARE = re.compile(r"software|developer|\bdata\b|cloud|devops|\bml\b|\bai\b|python|java|\bweb\b|\bapp\b", re.I)
# A manager title with no tech word: not the operator's kind of role.
MANAGER = re.compile(r"\bmanager\b", re.I)


def role_family(title: str) -> str:
    """tech / nontech / exec / manager / unclear, from the title alone."""
    tech = bool(TECH.search(title))
    if NONTECH.search(title) and not tech:
        return "nontech"
    if CIVIL.search(title) and not SOFTWARE.search(title):
        return "nontech"
    if EXEC.search(_OFFICE_OF.sub(" ", title)):
        return "exec"
    if MANAGER.search(title) and not tech:
        return "manager"
    return "tech" if tech else "unclear"


def classify(p: ats.Posting, rules: dict) -> str | None:
    """Stages A-D on the light list. None = send on (fetch the description),
    a reason = record as skipped, DROP = neither record nor send."""
    from src.scraper import filters

    where = ats.place(p)
    if where == "elsewhere":
        return ats_poller.DROP
    if where == "remote_locked":
        return "place:remote_locked" if "remote_locked" in rules["record_places"] else ats_poller.DROP
    kind = ats.employment_type(p.employment_type)
    if kind and filters.job_type_disallowed(kind, rules["job_type"]):
        return f"type:{kind}"
    if filters.title_disallowed(p.title, rules["title_patterns"]):
        return "title"
    if filters.job_level_disallowed(p.level, rules["level_blocklist"]):
        return "level"
    if ats.location_disallowed(p, rules["disallowed_regions"]):
        return "location"
    family = role_family(p.title)
    if family in ("nontech", "exec", "manager"):
        return f"role:{family}"
    return None


def decide(p: ats.Posting, rules: dict) -> str | None:
    """Stage G, once the description is in. None = send to the parser."""
    from src.parser import with_pool_skills

    if ats.place(p) == "unknown" and not ats.mentions_india(p.description):
        return "place:unknown"
    if role_family(p.title) == "unclear":
        named = len(with_pool_skills([], p.description or ""))
        if named < rules["min_pool_skills"]:
            return f"relevance:skills={named}"
    return None


def rules_from(cfg) -> dict:
    a = cfg.get("ats_poll") or {}
    return {
        "record_places": list(a.get("record_places") or ["remote_locked"]),
        "job_type": cfg.filters.get("job_type"),
        "title_patterns": list(cfg.filters.get("title_blocklist") or []),
        "level_blocklist": list(cfg.filters.get("job_level_blocklist") or []),
        "disallowed_regions": list(cfg.filters.get("disallowed_regions") or []),
        "min_pool_skills": int(a.get("min_pool_skills", 3)),
    }


# --- Collect: poll + front stages ------------------------------------------------------

def collect(session, cfg, *, atses=None, now=None, lanes=None, stats=None, log=None) -> list:
    """Poll due boards and run stages A-G; return the new jobs as AllJobs."""
    log = log or structlog.get_logger()
    a = cfg.get("ats_poll") or {}
    now = now or datetime.now(timezone.utc)
    stats = stats if stats is not None else ats_poller.PollStats()
    lanes = lanes if lanes is not None else {}
    delay = float(a.get("delay_seconds", 0.5))
    rules = rules_from(cfg)
    # The config section is a wrapper, not a dict: read the keys it has.
    tiers = a.get("tier_hours") or {}
    tier_hours = {k: float(tiers.get(k)) for k in ats_poller.TIER_HOURS if tiers.get(k) is not None}

    boards = ats_poller.due_boards(session, now, atses or a.get("atses") or ats.ATSES)
    # No transaction open while fetching: Neon ends a session left idle in
    # one for 5 minutes.
    session.commit()
    log.info("ats_poll_started", due=len(boards))
    results = ats_poller.fetch_lists(boards, lanes, budget_s=60 * float(a.get("time_budget_minutes", 10)),
                                     sr_max_pages=int(a.get("sr_max_pages", 30)), delay=delay, stats=stats)

    candidates: list[tuple[ats.Posting, ats_poller.Board]] = []
    ledger = ats_poller.Ledger.load(session, [r.board for r in results], now, tier_hours)
    for res in results:
        stats.boards += 1
        if res.error:
            stats.failed += 1
            log.warning("ats_board_failed", ats=res.board.ats, board=res.board.board_id, error=res.error)
        else:
            stats.postings += len(res.postings)
            sent = ledger.record(res, lambda p: classify(p, rules),
                                 baseline_hours=float(a.get("baseline_hours", 24)), stats=stats)
            candidates += [(p, res.board) for p in sent]
        ledger.schedule(res, stats)
    statements = ledger.flush(session)
    session.commit()
    reached = {(r.board.ats, r.board.board_id) for r in results}
    by_tier: dict[str, list[int]] = {}
    for b in boards:
        counts = by_tier.setdefault(b.priority, [0, 0])
        counts[0 if (b.ats, b.board_id) in reached else 1] += 1
    log.info("ats_recorded", boards=len(results), statements=statements,
             reached_and_left={t: f"{r}/{r + left}" for t, (r, left) in by_tier.items()})

    # E: already on LinkedIn/Naukri -> keep that job, give it the ATS link.
    from src.state.models import AllJobs

    portal = ats_poller.portal_jobs(session, now, int(a.get("dedupe_days", 30)))
    fresh = []
    for p, board in candidates:
        dup = ats_poller.portal_duplicate(portal, board.company_key or ats.key(p.company or board.board_id), p.title)
        if dup:
            stats.duplicates += 1
            ats_poller.mark_skipped(session, p, f"duplicate:{dup}")
            job = session.get(AllJobs, dup)
            if job is not None and not job.job_url_direct:
                job.job_url_direct = p.apply_url or p.url
            continue
        fresh.append((p, board))
    session.commit()

    # F: descriptions, for the lists that leave them out.
    fetched = ats_poller.fetch_details([p for p, _b in fresh], lanes, delay=delay)

    jobs = []
    for p, board in fresh:
        if not fetched.get(p.job_id) or not (p.description or "").strip():
            # Unrecorded, so the next poll of this board tries it again.
            stats.no_description += 1
            ats_poller.forget(session, p)
            continue
        reason = decide(p, rules)  # G
        if reason:
            stats.skip(reason.split("=")[0])
            ats_poller.mark_skipped(session, p, reason)
            continue
        jobs.append(ats.to_job(p, company=company_name(board, p)))
    pruned = ats_poller.prune(session, now=now, days=int(a.get("closed_retention_days", 60)))
    session.commit()
    log_db_size(session, log, warn_mb=float(a.get("db_size_warn_mb", 300)))
    stats.new = len(jobs)
    stats.requests = sum(lane.requests for lane in lanes.values())
    log.info("ats_poll_done", pruned=pruned, **vars(stats))
    return jobs


def log_db_size(session, log, *, warn_mb: float) -> float:
    """Log the database's size and ats_postings' share of it (#115); a
    warning above ``warn_mb``, well before Neon's 512 MB free tier."""
    from sqlalchemy import text

    total, postings = session.execute(text(
        "SELECT pg_database_size(current_database()), pg_total_relation_size('ats_postings')")).one()
    mb = total / 1e6
    (log.warning if mb > warn_mb else log.info)(
        "db_size", mb=round(mb, 1), ats_postings_mb=round(postings / 1e6, 1), warn_mb=warn_mb)
    return mb


def company_name(board: ats_poller.Board, p: ats.Posting) -> str:
    """The board's company name; else the posting's; else the id, tidied
    ("acme-labs" -> "Acme Labs")."""
    name = board.name or p.company
    if name:
        return name
    words = re.split(r"[-_.\s]+", board.board_id)
    return " ".join(w[:1].upper() + w[1:] for w in words if w) or board.board_id


# --- Run: the shared back end ------------------------------------------------------------

def _backlog(session, now, days: int, exclude: set[str]) -> list:
    """ATS jobs from the last ``days`` with no verdict yet (a run that ran out
    of time, or a failed parse). An ATS posting is fetched once, so without
    this such a job would be lost."""
    from sqlalchemy import exists, select

    from src.state.models import AllJobs, Applied, NotApplied

    if days <= 0:
        return []
    rows = session.scalars(
        select(AllJobs)
        .where(AllJobs.site.like("ats:%"), AllJobs.scraped_at >= now - timedelta(days=days))
        .where(AllJobs.jd_text.isnot(None), AllJobs.jd_text != "")
        .where(~exists().where(Applied.job_id == AllJobs.job_id))
        .where(~exists().where(NotApplied.job_id == AllJobs.job_id))
        .order_by(AllJobs.scraped_at)
    ).all()
    return [r for r in rows if r.job_id not in exclude]


def run(dry_run: bool, log, *, atses=None) -> int:
    from src.config import resolve_endpoint_base_url, settings
    from src.endpoint.cache import prerender
    from src.llm.client import LLMBudgetError, LLMError
    from src.main import _upsert_scraped, _write_not_applied
    from src.notifications import ats_poll_done_text, send_match_notification, send_status
    from src.parser import apply_to_row, parse
    from src.scorer.apply_decision import threshold_for
    from src.reasons import (
        BUILD_FAILURE,
        COMPANY_BLOCKED,
        DUPLICATE,
        HARD_FILTER_LAYER_3,
        JOB_CLOSED,
        JOB_TYPE_DISALLOWED,
        LOW_SCORE,
    )
    from src.scorer.embeddings import embed_documents
    from src.scraper import filters
    from src.state import master_profile
    from src.state.db import session_scope
    from src.tailor import build_applied, score_job

    cfg = settings
    a = cfg.get("ats_poll") or {}
    now = datetime.now(timezone.utc)
    stats = ats_poller.PollStats()

    with session_scope() as session:
        try:
            master_profile.rebuild(session)
        except Exception as exc:
            log.error("master_profile_validation_failure", error=str(exc), exc_info=True)
            return 1
        profile = master_profile.load_profile(session)

        jobs = collect(session, cfg, atses=atses, now=now, stats=stats, log=log)
        backlog = _backlog(session, now, int(a.get("backlog_days", 2)), {j.job_id for j in jobs})
        closed = ats_poller.closed_job_ids(session, [j.job_id for j in backlog])
        if jobs or stats.failed or stats.atses_skipped:
            send_status(ats_poll_done_text(stats))
        raw = backlog + jobs
        started = time.monotonic()  # the processing budget starts after the poll

        existing = filters.existing_job_ids(session, [j.job_id for j in raw])
        blocklist = list(cfg.filters.get("company_blocklist") or [])
        queue: list = []
        passing = []
        for job in raw:
            if job.job_id in existing:
                queue.append((job, DUPLICATE, None, None))
            elif job.job_id in closed:
                queue.append((job, JOB_CLOSED, None, None))
            elif filters.company_blocked(job.company, blocklist):
                queue.append((job, COMPANY_BLOCKED, job.company, None))
            else:
                passing.append(job)

        if passing:
            to_embed = [j for j in passing if j.jd_embedding is None]
            for job, emb in zip(to_embed, embed_documents([j.jd_text or "" for j in to_embed]) if to_embed else []):
                job.jd_embedding = emb
            passing = _upsert_scraped(session, passing)

        hours = float(a.get("recency_window_hours", 24))
        budget = 60.0 * float(a.get("process_budget_minutes", 25))
        matched = skipped = not_reached = parse_failed = 0
        aborted = False
        for index, job in enumerate(passing):
            if time.monotonic() - started > budget:
                not_reached = len(passing) - index  # no verdict: back next run
                log.warning("time_budget_reached", not_reached=not_reached)
                break
            session.commit()  # no transaction open across the slow calls
            log.info("job_started", n=index + 1, of=len(passing), job_id=job.job_id,
                     company=job.company, role=job.role, source=job.site)
            try:
                parsed = parse(job)
            except LLMBudgetError as exc:
                log.error("run_aborted", reason="llm_budget_exhausted", error=str(exc))
                aborted = True
                not_reached = len(passing) - index
                break
            except LLMError as exc:
                log.error("parse_failed_carried_over", job_id=job.job_id, error=str(exc))
                parse_failed += 1
                skipped += 1
                continue
            apply_to_row(job, parsed)
            if filters.job_type_disallowed(parsed.job_type, cfg.filters.get("job_type")):
                queue.append((job, JOB_TYPE_DISALLOWED, str(parsed.job_type), None))
                skipped += 1
                continue
            if filters.exceeds_years_ceiling(parsed.years_required, int(cfg.filters.years_ceiling)):
                queue.append((job, HARD_FILTER_LAYER_3, str(parsed.years_required), None))
                skipped += 1
                continue
            result = score_job(profile, job, parsed, scrape_window_hours=hours)
            log.info("job_scored", job_id=job.job_id, company=job.company, role=job.role, source=job.site,
                     score=round(result.final_score, 3), threshold=threshold_for(job.site),
                     matched=result.apply, fit=round(result.fit, 3), success_prob=round(result.success_prob, 3),
                     years_required=parsed.years_required, role_level=parsed.role_level)
            if not result.apply:
                queue.append((job, LOW_SCORE, str(result.final_score), result))
                skipped += 1
                continue
            built = build_applied(profile, job, parsed, result)
            if built is None:
                queue.append((job, BUILD_FAILURE, None, result))
                skipped += 1
                continue
            session.add(built.applied)
            job.outcome, job.outcome_at = "matched", now
            session.flush()
            if not dry_run:
                urls = {}
                if bool(cfg.prerender.enabled):
                    urls = prerender(job.job_id, session,
                                     expires_seconds=int(cfg.prerender.link_expiry_days) * 86400)
                try:
                    send_match_notification(
                        job=job, parsed=parsed, result=result, gap_skills=built.gap_skills,
                        endpoint_base_url=resolve_endpoint_base_url(str(cfg.endpoint.base_url)),
                        title_alias=built.title_alias, resume_urls=urls)
                except Exception as exc:
                    log.error("notification_error", job_id=job.job_id, error=str(exc))
            else:
                log.info("dry_run_match", job_id=job.job_id, company=job.company, role=job.role,
                         score=result.final_score)
            matched += 1

        _write_not_applied(session, queue, now, dry_run=dry_run)
        session.commit()

    outcomes: dict[str, int] = {}
    for _job, reason, _d, _s in queue:
        outcomes[reason] = outcomes.get(reason, 0) + 1
    if parse_failed:
        outcomes["PARSE_FAILED"] = parse_failed
    if not_reached:
        outcomes["NOT_REACHED"] = not_reached
    # An hourly poll that matched nothing stays quiet on Telegram.
    if matched or aborted:
        from src.notifications import send_dry_run_summary

        try:
            send_dry_run_summary(scraped=len(jobs), skipped=skipped, applied=matched,
                                 backlog=len(backlog), outcomes=outcomes, dry_run=dry_run)
        except Exception as exc:
            log.error("telegram_summary_error", error=str(exc))
    log.info("run_complete", source="ats", dry_run=dry_run, new=len(jobs), backlog=len(backlog),
             matched=matched, skipped=skipped, outcomes=outcomes,
             aborted="llm_budget_exhausted" if aborted else None)
    return 2 if aborted else 0


def main(argv: list[str] | None = None) -> int:
    from src.main import _configure_logging

    parser = argparse.ArgumentParser(prog="src.ats_pipeline")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ats", nargs="+", choices=ats.ATSES, help="poll only these ATSes")
    args = parser.parse_args(argv)
    _configure_logging()
    log = structlog.get_logger()
    log.info("run_started", source="ats", dry_run=args.dry_run)

    # The same lock as the JobSpy run: the two never overlap on one machine
    # (on Actions the shared concurrency group does this).
    lock_path = _ROOT / "data" / "run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
        lock_file = open(lock_path, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        log.info("run_skipped", reason="lock_held")
        return 0
    try:
        return run(args.dry_run, log, atses=args.ats)
    finally:
        import fcntl as _fcntl
        _fcntl.flock(lock_file, _fcntl.LOCK_UN)
        lock_file.close()


if __name__ == "__main__":
    sys.exit(main())
