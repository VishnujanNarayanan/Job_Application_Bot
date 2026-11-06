"""The ATS run (#97): companies' own job boards, polled on a schedule.

Usage:
    python -m src.ats_pipeline              # poll due boards, notify matches
    python -m src.ats_pipeline --dry-run    # no notifications; results in the log only
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
                  unclear title -> enough of the operator's skills named
                  (in the title or description)? a description too short
                  to name them goes to the parser unchecked
"""

from __future__ import annotations

import argparse
import dataclasses
from concurrent.futures import ThreadPoolExecutor
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from src import ats_dedupe
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
    r"application engineer|graduate engineer|solutions engineer|forward deployed|"
    # Tech titles a business word used to outvote (title audit, 2026-11-01):
    # "Senior Cloud Operations Specialist", "Staff Engineer - Linux Kernel and
    # Device Driver", "Revenue Operations Lead (GTM Engineer)", "Supply Chain
    # Security Engineer", "Principal Engineer (Warehouse Engineering)".
    r"\bcloud\b|kernel|device driver|embedded|firmware|security engineer|gtm engineer|"
    r"\bdata specialist|(staff|principal) (software )?engineer\b|\bkafka\b|\besb\b|"
    r"integration (engineer|developer|architect|lead)",
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
# Banks title individual engineers "VP" / "AVP" ("VP, Data Engineer (KDB
# Developer)", "Vice President, Senior Backend Developer"): with an engineer
# noun and no "of" it is a senior IC role, not an executive one. "VP of
# Engineering", "VP Engineering" and any director / head / chief stay exec.
_VP = re.compile(r"\b(a?vp|vice president)\b", re.I)
_VP_OF = re.compile(r"\b(a?vp|vice president) (of|for)\b", re.I)
_IC_NOUN = re.compile(r"\b(engineer|developer|programmer|scientist|architect)\b", re.I)
_NOT_VP_EXEC = re.compile(r"\b(director|head of|chief|cto|ceo|cfo|coo)\b", re.I)
# "Chief Technical Architect (IC Role)": the title says it is not a manager.
_IC_ROLE = re.compile(r"\bIC role\b|individual contributor", re.I)
# "Principal Engineer (AI, Office of the CEO)" is not an executive role.
_OFFICE_OF = re.compile(r"office of the \w+", re.I)
# Civil and industrial engineering, which shares words with software
# ("infrastructure", "automation", "QA"): non-tech unless a software word is
# also in the title (gate analysis, 2026-10-16).
CIVIL = re.compile(r"\b(utility|utilities|water|wastewater|hydraulic|mep|hvac|plc|substation|structural|"
                   r"qa/qc|qa ?& ?qc|geotech\w*|surveyor|piping|instrumentation)\b", re.I)
SOFTWARE = re.compile(r"software|developer|\bdata\b|cloud|devops|\bml\b|\bai\b|python|java|\bweb\b|\bapp\b|"
                      r"embedded|firmware|c\+\+", re.I)
# A manager title with no tech word: not the operator's kind of role. A tech
# manager ("Data Engineering Manager") goes on to the scorer.
MANAGER = re.compile(r"\bmanager\b", re.I)


def _is_exec(title: str) -> bool:
    t = _OFFICE_OF.sub(" ", title)
    if not EXEC.search(t) or _IC_ROLE.search(t):
        return False
    vp_ic = (_VP.search(t) and _IC_NOUN.search(t) and not _VP_OF.search(t)
             and not _NOT_VP_EXEC.search(t))
    return not vp_ic


def role_family(title: str) -> str:
    """tech / nontech / exec / manager / unclear, from the title alone."""
    tech = bool(TECH.search(title))
    if NONTECH.search(title) and not tech:
        return "nontech"
    if CIVIL.search(title) and not SOFTWARE.search(title):
        return "nontech"
    if _is_exec(title):
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
    if too_old(p.posted_at, rules.get("oldest")):
        return "age"
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


def too_old(posted_at: datetime | None, oldest: datetime | None) -> bool:
    """Dated before ``oldest`` (``ats_poll.max_posting_age_days``). An
    undated posting is never too old: there is nothing to judge it by."""
    return bool(oldest and posted_at and posted_at < oldest)


def cannot_reach_bar(posted_at: datetime | None, now: datetime, bar: float) -> bool:
    """True when even a perfect fit couldn't clear ``bar`` at this posting's age:
    best final = max_score x recency multiplier x the junior boost. The ATS
    twin of LinkedIn's TOO_MANY_APPLICANTS pre-check; saves the LLM call."""
    from src.config import settings
    from src.scorer.apply_decision import recency_multiplier

    if posted_at is None:
        return False
    best = (float(settings.scoring.final.max_score) * recency_multiplier(posted_at, now)
            * float(settings.scoring.junior_boost.multiplier))
    return best < bar


def newest_first(jobs: list) -> list:
    """Order jobs for processing: newest posting first, undated last. A fresh
    posting is worth most early, so it must not wait behind the backlog for a
    later run (the run stops starting jobs at process_budget_minutes)."""
    return sorted(jobs, key=lambda j: (j.posted_at is None,
                                       -(j.posted_at.timestamp() if j.posted_at else 0.0)))


def decide(p: ats.Posting, rules: dict) -> str | None:
    """Stage G, once the description is in. None = send to the parser."""
    from src.parser import with_pool_skills

    if ats.place(p) == "unknown" and not ats.mentions_india(p.description):
        return "place:unknown"
    if role_family(p.title) == "unclear":
        description = (p.description or "").strip()
        # Too short to name the skills it wants: the parser reads it instead
        # of the job being dropped unread (title audit, 2026-11-01).
        if len(description) < rules.get("short_description_chars", 500):
            return None
        # The title counts too: "Integration Lead - ESB & Kafka".
        named = len(with_pool_skills([], f"{p.title}\n{description}"))
        if named < rules["min_pool_skills"]:
            return f"relevance:skills={named}"
    return None


def rules_from(cfg, *, now: datetime | None = None) -> dict:
    a = cfg.get("ats_poll") or {}
    max_age = a.get("max_posting_age_days")
    now = now or datetime.now(timezone.utc)
    return {
        # Postings dated before this are skipped (None = no age limit).
        "oldest": now - timedelta(days=float(max_age)) if max_age else None,
        "record_places": list(a.get("record_places") or ["remote_locked"]),
        "job_type": cfg.filters.get("job_type"),
        "title_patterns": list(cfg.filters.get("title_blocklist") or []),
        "level_blocklist": list(cfg.filters.get("job_level_blocklist") or []),
        "disallowed_regions": list(cfg.filters.get("disallowed_regions") or []),
        "min_pool_skills": int(a.get("min_pool_skills", 3)),
        "short_description_chars": int(a.get("short_description_chars", 500)),
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
    rules = rules_from(cfg, now=now)
    # The config section is a wrapper, not a dict: read the keys it has.
    tiers = a.get("tier_hours") or {}
    tier_hours = {k: float(tiers.get(k)) for k in ats_poller.TIER_HOURS if tiers.get(k) is not None}

    # Postings an earlier run sent on but never saved to all_jobs (it stopped
    # in between): make their boards due so this run sends them again.
    stranded = ats_poller.requeue_pending(session)
    session.commit()
    if stranded:
        log.warning("ats_requeue_pending", postings=stranded)
    boards = ats_poller.due_boards(session, now, atses or a.get("atses") or ats.ATSES)
    # No transaction open while fetching: Neon ends a session left idle in
    # one for 5 minutes.
    session.commit()
    log.info("ats_poll_started", due=len(boards))
    results = ats_poller.fetch_lists(boards, lanes, budget_s=60 * float(a.get("time_budget_minutes", 10)),
                                     sr_max_pages=int(a.get("sr_max_pages", 30)), delay=delay, stats=stats,
                                     parallel=int(a.get("parallel_hosts", 1)))

    candidates: list[tuple[ats.Posting, ats_poller.Board]] = []
    ledger = ats_poller.Ledger.load(session, results, now, tier_hours)
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

    # Names for boards that have none, from their careers page (#182): read
    # once and saved, before the dedupe below, which matches on the name.
    names = ats_poller.fetch_names(list({(b.ats, b.board_id): b for _p, b in candidates}.values()), lanes,
                                   delay=delay, budget_s=60 * float(a.get("name_budget_minutes", 2)),
                                   parallel=int(a.get("parallel_hosts", 1)))
    if names:
        ats_poller.save_names(session, names)
        session.commit()
        candidates = [(p, _named(b, names.get((b.ats, b.board_id)))) for p, b in candidates]
    log.info("ats_board_names", found=len(names))

    # E: already on LinkedIn/Naukri -> keep that job, give it the ATS link.
    from src.state.models import AllJobs

    portal = ats_poller.portal_jobs(session, now, int(a.get("dedupe_days", 30)))
    # D2: the same requisition on another board or site (#186): applying to
    # one is applying to all, so the later copy is never parsed or sent.
    repeats = ats_dedupe.repeat_requisitions(session, candidates, now)
    fresh = []
    for p, board in candidates:
        if p.job_id in repeats:
            stats.repeat_requisitions += 1
            ats_poller.mark_skipped(session, p, f"duplicate_req:{repeats[p.job_id]}")
            continue
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
    fetched = ats_poller.fetch_details([p for p, _b in fresh], lanes, delay=delay,
                                       budget_s=60 * float(a.get("detail_budget_minutes", 8)),
                                       parallel=int(a.get("parallel_hosts", 1)))
    stats.details_deferred = sum(1 for p, _b in fresh if p.job_id not in fetched)

    jobs = []
    for p, board in fresh:
        if not fetched.get(p.job_id) or not (p.description or "").strip():
            # Unrecorded, so the next poll of this board tries it again.
            stats.no_description += 1
            ats_poller.forget(session, p)
            continue
        # One malformed posting must never end the run (a list where a string
        # was expected stopped every run for six hours, 2026-11-04): log it,
        # forget it so the next poll retries, and carry on.
        try:
            reason = decide(p, rules)  # G
            if reason:
                stats.skip(reason.split("=")[0])
                ats_poller.mark_skipped(session, p, reason)
                continue
            jobs.append(ats.to_job(p, company=company_name(board, p)))
        except Exception as exc:  # noqa: BLE001
            log.warning("ats_posting_failed", ats=p.ats, board=p.board_id, posting=p.posting_id,
                        error=f"{type(exc).__name__}: {exc}"[:200])
            stats.postings_failed += 1
            ats_poller.forget(session, p)
    pruned = ats_poller.prune(session, now=now, days=int(a.get("closed_retention_days", 60)))
    session.commit()
    log_db_size(session, log, warn_mb=float(a.get("db_size_warn_mb", 300)))
    stats.new = len(jobs)
    stats.requests = sum(lane.requests for lane in lanes.values())
    stats.bytes = sum(lane.bytes for lane in lanes.values())
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


def _named(board: ats_poller.Board, name: str | None) -> ats_poller.Board:
    if not name:
        return board
    return dataclasses.replace(board, name=name, company_key=ats.key(name) or board.board_id)


def company_name(board: ats_poller.Board, p: ats.Posting) -> str:
    """The board's company name; else the posting's; else the name
    config/ats_company_names.yaml gives the Workday tenant / Oracle pod;
    else the id, tidied ("acme-labs" -> "Acme Labs", "gh" -> "GH")."""
    name = board.name or p.company or ats.known_name(board.ats, board.board_id)
    if name:
        return name
    ident = board.board_id
    # Workday "accenture/wd103/AccentureCareers" -> its tenant, "Accenture";
    # Oracle "jpmc.fa.oraclecloud.com/CX_1001" -> its pod, the best there is
    # until the board's careers page names it (fetch_names, #182).
    if board.ats == "workday":
        ident = ats.workday_parts(ident)[0]
    elif board.ats == "oracle":
        ident = ats.oracle_parts(ident)[0].split(".")[0]
    if len(ident) <= 3 and ident.isalpha():   # an initialism: "wf" -> "WF", not "Wf"
        return ident.upper()
    words = re.split(r"[-_.\s/]+", ident)
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


def _report_end_of_run(stats, *, new: int, backlog: int, matched: int, skipped: int,
                       aborted: bool, outcomes: dict[str, int], dry_run: bool, log) -> None:
    """The run's Telegram messages, all sent once the run is over.

    * the poll line ("ATS poll: N new jobs ...") when the poll found something or a
      board failed; it used to go out mid-run, before any job was scored;
    * the run summary when a job matched or the run aborted. A scheduled poll that
      matched nothing stays quiet.

    A dry run sends nothing: it is a test, and it used to post to the real chat.
    Both lines go to the log instead.
    """
    from src.notifications import ats_poll_done_text, send_dry_run_summary, send_status

    poll_line = ats_poll_done_text(stats) if (new or stats.failed or stats.atses_skipped) else None
    if dry_run:
        log.info("dry_run_telegram_suppressed", poll_line=poll_line,
                 summary=bool(matched or aborted), matched=matched)
        return
    if poll_line:
        send_status(poll_line)
    if matched or aborted:
        try:
            send_dry_run_summary(scraped=new, skipped=skipped, applied=matched,
                                 backlog=backlog, outcomes=outcomes, dry_run=dry_run)
        except Exception as exc:
            log.error("telegram_summary_error", error=str(exc))


def _role_key(job) -> tuple[str, str]:
    return ats_dedupe.group_key(ats.key(job.company or ""), job.role)


def _share(head, siblings_of: dict, reason: str) -> list:
    """The verdicts of ``head``'s siblings: they have its description, so
    they share its verdict (reason SIBLING, detail the head's job id)."""
    return [(j, reason, head.job_id, None) for j in siblings_of.get(head.job_id) or []]


def _opening(job) -> tuple:
    """(location, apply url) of a sibling, as its role's message lists it."""
    return (job.location, job.job_url_direct or job.job_url)


def _sent_parents(session, clusters: list, now) -> dict:
    """{head job_id: the earlier sent match it is a sibling of}, for roles
    whose match went out in an earlier run (last SIBLING_DAYS days): same
    company key and title, and the same description. Only matches: a
    sibling of a rejected job is judged on its own (its posting date, and so
    its score, can differ)."""
    from datetime import timedelta
    from types import SimpleNamespace

    from sqlalchemy import text

    heads = [c.head for c in clusters]
    if not heads:
        return {}
    companies = sorted({(j.company or "").lower() for j in heads})
    rows = session.execute(text("""
        SELECT a.job_id, a.company, a.role, a.location, a.jd_text, a.job_url, a.job_url_direct,
               ap.user_status, ap.telegram_message, ap.built_at
        FROM all_jobs a JOIN applied ap ON ap.job_id = a.job_id
        WHERE a.site LIKE 'ats:%' AND ap.built_at >= :since AND lower(a.company) = ANY(CAST(:cos AS text[]))
          AND a.job_id <> ALL(CAST(:mine AS text[]))
        ORDER BY ap.built_at"""), {"since": now - timedelta(days=ats_dedupe.SIBLING_DAYS), "cos": companies,
                                   "mine": [j.job_id for c in clusters for j in [c.head, *c.siblings]]}).all()
    by_key: dict = {}
    for r in rows:
        by_key.setdefault(_role_key(r), []).append(SimpleNamespace(**r._mapping))
    out = {}
    for head in heads:
        for parent in by_key.get(_role_key(head), []):
            if ats_dedupe.are_siblings(parent.jd_text, parent.location, head.jd_text, head.location):
                out[head.job_id] = parent
                break
    return out


def _tell_late_siblings(session, parent, members: list, log) -> None:
    """A sent match's role has new openings (#186): rewrite its message to
    list them all and, for a city it did not list yet, send a silent note
    threaded under it. Nothing for a match the operator dismissed. A match
    sent before its message was kept gets only the note."""
    from sqlalchemy import text

    from src.notifications import send_quiet_note, update_match_siblings

    if parent.user_status == "dismissed":
        return
    ids = {m.job_id for m in members}
    rows = session.execute(text("""
        SELECT a.job_id, a.location, coalesce(a.job_url_direct, a.job_url) AS url
        FROM not_applied n JOIN all_jobs a ON a.job_id = n.job_id
        WHERE n.reason_category = 'SIBLING' AND n.reason_detail = :parent
        ORDER BY n.job_id"""), {"parent": parent.job_id}).all()
    openings = [(r.location, r.url) for r in rows]
    earlier = [parent.location] + [r.location for r in rows if r.job_id not in ids]
    stored = parent.telegram_message or {}
    edited = update_match_siblings(stored, parent.location, openings) if stored else False
    new_cities, known = [], list(earlier)
    for m in members:
        if ats_dedupe.is_new_city(m.location, known):
            new_cities.append(_opening(m))
            known.append(m.location)
    log.info("ats_late_siblings", parent=parent.job_id, members=len(members), edited=edited,
             new_cities=len(new_cities))
    if not new_cities:
        return
    lines = ats_dedupe.sibling_lines(None, new_cities)   # "Also open in: ..."
    head = f"Same role, {len(new_cities)} more {'city' if len(new_cities) == 1 else 'cities'}"
    if not stored.get("id"):
        head += f": *{ats_dedupe._md(parent.role)}* at *{ats_dedupe._md(parent.company)}*"
    try:
        send_quiet_note(head + "\n" + "\n".join(lines), reply_to=stored.get("id"))
    except Exception as exc:  # noqa: BLE001 - the openings are saved; the note is a courtesy
        log.warning("late_sibling_note_failed", parent=parent.job_id, error=str(exc)[:200])


def _detached(job):
    """A session-free copy of an all_jobs row for a worker thread to read."""
    from src.state.models import AllJobs

    return AllJobs(**{c.key: getattr(job, c.key) for c in AllJobs.__table__.columns})


def _safe_commit(session, log) -> bool:
    """Commit; on a dropped connection roll back and say so. The pool hands
    the next query a fresh connection (pool_pre_ping), so the run goes on."""
    from sqlalchemy.exc import DBAPIError

    try:
        session.commit()
        return True
    except DBAPIError as exc:
        log.warning("db_commit_failed_reconnecting", error=str(exc)[:200])
        try:
            session.rollback()
        except DBAPIError:
            pass
        return False


def run(dry_run: bool, log, *, atses=None) -> int:
    from src.config import resolve_endpoint_base_url, settings
    from src.endpoint.cache import for_notification
    from src.llm.client import LLMBudgetError, LLMError, only_providers
    from src.main import _upsert_scraped, _write_not_applied
    from src.notifications import send_match_notification
    from src.parser import apply_to_row
    from src.parsing import router
    from src.parsing.pipeline import parse_job, record_route
    from src.parsing.router import Route
    from src.scorer.apply_decision import threshold_for
    from src.reasons import (
        BUILD_FAILURE,
        COMPANY_BLOCKED,
        DUPLICATE,
        HARD_FILTER_LAYER_3,
        JOB_CLOSED,
        JOB_TYPE_DISALLOWED,
        POSTING_TOO_OLD,
        LOW_SCORE,
        SIBLING,
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

        run_started = time.monotonic()
        jobs = collect(session, cfg, atses=atses, now=now, stats=stats, log=log)
        backlog = _backlog(session, now, int(a.get("backlog_days", 2)), {j.job_id for j in jobs})
        closed = ats_poller.closed_job_ids(session, [j.job_id for j in backlog])
        # One row per job: a job can come back both from the backlog and as a
        # re-sent stranded posting, and two instances of one job id in a batch
        # fail the insert (2026-11-04).
        raw = list({j.job_id: j for j in reversed(backlog + jobs)}.values())[::-1]
        started = time.monotonic()  # the processing budget starts after the poll

        existing = filters.existing_job_ids(session, [j.job_id for j in raw])
        blocklist = list(cfg.filters.get("company_blocklist") or [])
        oldest = rules_from(cfg, now=now)["oldest"]
        ats_bar = threshold_for("ats:")
        queue: list = []
        passing = []
        for job in raw:
            if job.job_id in existing:
                queue.append((job, DUPLICATE, None, None))
            elif job.job_id in closed:
                queue.append((job, JOB_CLOSED, None, None))
            elif too_old(job.posted_at, oldest):
                # Mostly backlog: new postings this old are stopped at classify.
                queue.append((job, POSTING_TOO_OLD, job.posted_at.date().isoformat(), None))
            elif cannot_reach_bar(job.posted_at, now, ats_bar):
                # Even a perfect fit at this age can't reach the bar: skip the LLM.
                queue.append((job, POSTING_TOO_OLD,
                              f"{job.posted_at.date().isoformat()} (cannot reach {ats_bar})", None))
            elif filters.company_blocked(job.company, blocklist):
                queue.append((job, COMPANY_BLOCKED, job.company, None))
            else:
                passing.append(job)

        if passing:
            to_embed = [j for j in passing if j.jd_embedding is None]
            for job, emb in zip(to_embed, embed_documents([j.jd_text or "" for j in to_embed]) if to_embed else []):
                job.jd_embedding = emb
            passing = _upsert_scraped(session, passing)
        # Saved (or already known, closed or blocked): from here the backlog
        # guarantees a verdict, so these postings are no longer at risk.
        ats_poller.mark_done(session, [j.job_id for j in jobs])
        session.commit()
        passing = newest_first(passing)

        # #186: one role, one message. Postings with the same description
        # (another city, or another opening) are siblings: only the first
        # (the head) is parsed, scored and sent; the rest share its verdict
        # and are listed in its message. A role whose match was already sent
        # in an earlier run edits that message instead (late siblings).
        clusters = ats_dedupe.cluster(passing, key=_role_key)
        parents = _sent_parents(session, clusters, now)
        for c in clusters:
            parent = parents.get(c.head.job_id)
            if parent is not None:
                queue.extend((j, SIBLING, parent.job_id, None) for j in [c.head, *c.siblings])
        late = [(parents[c.head.job_id], [c.head, *c.siblings]) for c in clusters if c.head.job_id in parents]
        clusters = [c for c in clusters if c.head.job_id not in parents]
        siblings_of = {c.head.job_id: c.siblings for c in clusters}
        passing = [c.head for c in clusters]
        if late:
            _write_not_applied(session, queue, now, dry_run=dry_run)
            if _safe_commit(session, log):
                if not dry_run:
                    for parent, members in late:
                        _tell_late_siblings(session, parent, members, log)
            else:
                queue.clear()   # unsaved: they come back next run
        log.info("ats_siblings", roles=len(clusters), grouped=sum(len(v) for v in siblings_of.values()),
                 late=sum(len(m) for _p, m in late))

        hours = float(a.get("recency_window_hours", 24))
        budget = 60.0 * float(a.get("process_budget_minutes", 25))
        # The whole run's deadline, from its start: no new job after it, so
        # the run saves and reports inside the workflow's 58-minute limit.
        deadline = 60.0 * float(a.get("run_deadline_minutes", 48))
        matched = skipped = not_reached = parse_failed = 0
        aborted = False
        routing_rules = router.rules_from_config()
        # Free tiers only, whatever the config says is missing: the paid key
        # must never be reachable from this run.
        allowed_providers = list(a.get("llm_providers") or ["groq", "gemini-free"])
        llm_available = True
        written = len(queue) if late else 0  # verdicts in `queue` already saved
        # Parses run ahead in worker threads (the LLM call is most of a job's
        # time; ~8 s each, 2026-11-03) while this thread scores, saves and
        # sends in order. Workers get a detached copy of the row (no session,
        # so nothing can lazy-load from another thread), and re-enter
        # only_providers themselves: a context variable does not cross into a
        # thread, and without it the paid provider would be reachable.
        workers = max(1, int(a.get("parse_workers", 1)))
        pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
        if pool is not None:
            # Built once here, not raced for by the first workers.
            from src.parsing.modes import ParseMode, get_strategy

            get_strategy(ParseMode.DICTIONARY)
            get_strategy(ParseMode.LLM)
        ahead: dict[int, object] = {}

        def _parse_ahead(i: int) -> None:
            copy, avail = _detached(passing[i]), llm_available

            def work():
                with only_providers(allowed_providers):
                    return parse_job(copy, profile=profile, rules=routing_rules,
                                     scrape_window_hours=hours, llm_available=avail)
            ahead[i] = pool.submit(work)

        for index, job in enumerate(passing):
            if time.monotonic() - started > budget or time.monotonic() - run_started > deadline:
                not_reached = len(passing) - index  # no verdict: back next run
                log.warning("time_budget_reached", not_reached=not_reached,
                            run_minutes=round((time.monotonic() - run_started) / 60, 1))
                break
            # Save the verdicts so far, then commit: no transaction stays open
            # across the slow calls, and a dropped connection costs at most
            # the job in hand (it comes back next run), not the whole run's
            # verdicts (Neon closed the socket 2.5 h into a run, 2026-11-02).
            if written < len(queue):
                _write_not_applied(session, queue[written:], now, dry_run=dry_run)
            if _safe_commit(session, log):
                written = len(queue)
            else:
                del queue[written:]   # unsaved verdicts: those jobs come back next run
            log.info("job_started", n=index + 1, of=len(passing), job_id=job.job_id,
                     company=job.company, role=job.role, source=job.site)
            # Layer 3, routed exactly as in the main run (#71, #100): the free
            # dictionary parse is scored first and picks the route. The LLM may
            # only use the free providers in ats_poll.llm_providers; once they
            # are out of quota, the rest of this run uses the dictionary parse.
            if pool is not None:
                for k in range(index, min(index + workers, len(passing))):
                    if k not in ahead:
                        _parse_ahead(k)
            try:
                if pool is not None:
                    try:
                        routed = ahead.pop(index).result()
                    except (LLMBudgetError, LLMError):
                        raise
                    except Exception as exc:  # noqa: BLE001 - a worker failure must not end the run
                        log.warning("parse_worker_failed", job_id=job.job_id, error=str(exc)[:200])
                        with only_providers(allowed_providers):
                            routed = parse_job(job, profile=profile, rules=routing_rules,
                                               scrape_window_hours=hours, llm_available=llm_available)
                else:
                    with only_providers(allowed_providers):
                        routed = parse_job(job, profile=profile, rules=routing_rules,
                                           scrape_window_hours=hours, llm_available=llm_available)
            except LLMBudgetError as exc:
                log.warning("ats_free_llm_exhausted", job_id=job.job_id, error=str(exc)[:200],
                            allowed=sorted(allowed_providers))
                llm_available = False
                routed = parse_job(job, profile=profile, rules=routing_rules,
                                   scrape_window_hours=hours, llm_available=False)
            except LLMError as exc:
                log.error("parse_failed_carried_over", job_id=job.job_id, error=str(exc))
                parse_failed += 1
                skipped += 1
                continue
            record_route(job, routed, routing_rules, now=now)
            if routed.decision.route is Route.CARRIED_OVER:
                not_reached += 1  # no verdict: back next run
                skipped += 1
                continue
            if routed.decision.route is Route.SKIPPED_FLOOR:
                apply_to_row(job, routed.parsed)
                queue.append((job, LOW_SCORE, str(routed.dict_score), routed.dict_result))
                queue.extend(_share(job, siblings_of, SIBLING))
                skipped += 1
                continue
            parsed = routed.parsed
            apply_to_row(job, parsed)
            if filters.job_type_disallowed(parsed.job_type, cfg.filters.get("job_type")):
                queue.append((job, JOB_TYPE_DISALLOWED, str(parsed.job_type), None))
                queue.extend(_share(job, siblings_of, SIBLING))
                skipped += 1
                continue
            if filters.exceeds_years_ceiling(parsed.years_required, int(cfg.filters.years_ceiling)):
                queue.append((job, HARD_FILTER_LAYER_3, str(parsed.years_required), None))
                queue.extend(_share(job, siblings_of, SIBLING))
                skipped += 1
                continue
            result = score_job(profile, job, parsed, scrape_window_hours=hours)
            log.info("job_scored", job_id=job.job_id, company=job.company, role=job.role, source=job.site,
                     score=round(result.final_score, 3), threshold=threshold_for(job.site),
                     matched=result.apply, fit=round(result.fit, 3), success_prob=round(result.success_prob, 3),
                     years_required=parsed.years_required, role_level=parsed.role_level)
            if not result.apply:
                queue.append((job, LOW_SCORE, str(result.final_score), result))
                queue.extend(_share(job, siblings_of, SIBLING))
                skipped += 1
                continue
            built = build_applied(profile, job, parsed, result)
            if built is None:
                queue.append((job, BUILD_FAILURE, None, result))
                skipped += 1
                continue
            session.add(built.applied)
            job.outcome, job.outcome_at = "matched", now
            group = siblings_of.get(job.job_id) or []
            queue.extend(_share(job, siblings_of, SIBLING))
            # Saved BEFORE the message goes out: a commit that fails leaves the
            # job unsent and unrecorded (it comes back next run), never sent
            # but unrecorded (which would send it again).
            if written < len(queue):
                _write_not_applied(session, queue[written:], now, dry_run=dry_run)
            if not _safe_commit(session, log):
                del queue[written:]
                not_reached += 1
                skipped += 1
                continue
            written = len(queue)
            if not dry_run:
                resume = for_notification(job.job_id, session)
                try:
                    sent = send_match_notification(
                        job=job, parsed=parsed, result=result, gap_skills=built.gap_skills,
                        endpoint_base_url=resolve_endpoint_base_url(str(cfg.endpoint.base_url)),
                        title_alias=built.title_alias, siblings=[_opening(j) for j in group], **resume)
                    if sent:
                        built.applied.telegram_message = sent
                        _safe_commit(session, log)
                except Exception as exc:
                    log.error("notification_error", job_id=job.job_id, error=str(exc))
            else:
                log.info("dry_run_match", job_id=job.job_id, company=job.company, role=job.role,
                         score=result.final_score)
            matched += 1

        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if written < len(queue):
            _write_not_applied(session, queue[written:], now, dry_run=dry_run)
        if not _safe_commit(session, log):
            del queue[written:]

    outcomes: dict[str, int] = {}
    for _job, reason, _d, _s in queue:
        outcomes[reason] = outcomes.get(reason, 0) + 1
    if parse_failed:
        outcomes["PARSE_FAILED"] = parse_failed
    if not_reached:
        outcomes["NOT_REACHED"] = not_reached
    _report_end_of_run(stats, new=len(jobs), backlog=len(backlog), matched=matched,
                       skipped=skipped, aborted=aborted, outcomes=outcomes,
                       dry_run=dry_run, log=log)
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
