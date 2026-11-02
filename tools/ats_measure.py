"""Measure candidate ATS boards with the poller's own adapters, then upload
the ones worth polling (#159).

Candidates are board ids from Common Crawl (data/ats_probe/commoncrawl_ids.jsonl)
on an ATS the poller reads (src.scraper.ats.ATSES) and not already in
ats_boards. Each is fetched with src.scraper.ats_poller.fetch_board, the
exact code the poll runs, and its postings are placed with ats.place, so a
board is judged the way it will be polled: India postings, remote open to
India, remote locked to another country.

One paced lane per ATS (1 request/second, 429 back-off), ATSes in parallel.
Results go to data/ats_probe/measure_cc.jsonl (append-only; a rerun skips
boards already measured). Nothing touches the database until --upload.

USAGE
-----
    python -m tools.ats_measure                      # measure every candidate
    python -m tools.ats_measure --ats workday --limit 50
    python -m tools.ats_measure --report
    python -m tools.ats_measure --upload --dry-run   # rows and size, no write
    python -m tools.ats_measure --upload             # one batch insert

A board is uploaded with poll = true when it has India postings or any remote
postings (open or locked: the operator's rule, 2026-11-01). Existing rows
are never touched (insert ... on conflict do nothing).
"""

from __future__ import annotations

import argparse
import json
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

OUT = Path("data/ats_probe")
IDS = OUT / "commoncrawl_ids.jsonl"
# Guessed candidates from company lists (tools.ats_candidates), same shape.
EXTRA = OUT / "extra_ids.jsonl"
MEASURE = OUT / "measure_cc.jsonl"
_lock = threading.Lock()


def candidates(atses: list[str]) -> dict[str, list[str]]:
    from sqlalchemy import text

    from src.state.db import session_scope

    ids: dict[str, list[str]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for f in (IDS, EXTRA):
        if f.exists():
            # File order is kept: candidate lists put their likeliest first.
            for r in map(json.loads, f.read_text().splitlines()):
                # Case-insensitive: Ashby "G2" is the stored "g2"; Workday
                # sites "Search" / "search" are one board.
                k = (r["ats"], r["id"].lower())
                if r["ats"] in atses and k not in seen:
                    seen.add(k)
                    ids[r["ats"]].append(r["id"])
    with session_scope() as s:
        known = {(a, b.lower()) for a, b in s.execute(text("SELECT ats, board_id FROM ats_boards"))}
        # Ids discovery already asked about and found missing.
        known |= {(a, b.lower()) for a, b in s.execute(text("SELECT ats, board_id FROM ats_probes"))}
    measured = set()
    if MEASURE.exists():
        measured = {(r["ats"], r["board"].lower()) for r in map(json.loads, MEASURE.read_text().splitlines())}
    # Board ids are compared as stored: Greenhouse and SmartRecruiters ids
    # are lower-cased by board_from_url, like discovery's.
    return {a: [b for b in v if (a, b.lower()) not in known and (a, b.lower()) not in measured]
            for a, v in ids.items()}


def measure_one(lane, ats_name: str, board_id: str) -> dict:
    from src.scraper import ats, ats_poller

    now = datetime.now(timezone.utc)
    res = ats_poller.fetch_board(lane, ats_poller.Board(ats_name, board_id), sr_max_pages=3)
    row = {"ats": ats_name, "board": board_id, "error": res.error, "jobs": len(res.postings),
           "ts": now.isoformat(timespec="seconds")}
    if res.error:
        return row
    places = Counter(ats.place(p) for p in res.postings)
    week = now - timedelta(days=7)
    row.update(
        india=places["india"], remote_open=places["remote_open"], remote_locked=places["remote_locked"],
        unknown=places["unknown"],
        posted_7d=sum(1 for p in res.postings if p.posted_at and p.posted_at >= week),
        company=next((p.company for p in res.postings if p.company), None),
        last_posted_at=max((p.posted_at for p in res.postings if p.posted_at), default=None),
    )
    if row["last_posted_at"]:
        row["last_posted_at"] = row["last_posted_at"].isoformat()
    return row


def measure(atses: list[str], limit: int | None, delay: float) -> None:
    from src.scraper import ats_poller

    work = candidates(atses)
    if limit:
        work = {a: v[:limit] for a, v in work.items()}
    print({a: len(v) for a, v in work.items()}, flush=True)

    def run(a: str) -> None:
        lane = ats_poller.Lane(a, delay)
        if not ats_poller.canary_ok(lane):
            print(f"{a}: canary failed, skipped", flush=True)
            return
        for i, b in enumerate(work[a]):
            try:
                row = measure_one(lane, a, b)
            except ats_poller.LaneStopped:
                print(f"{a}: stopped after repeated 429s", flush=True)
                return
            with _lock, MEASURE.open("a") as f:
                f.write(json.dumps(row) + "\n")
            if i % 200 == 0:
                print(f"{a}: {i}/{len(work[a])}", flush=True)
        print(f"{a}: done", flush=True)

    threads = [threading.Thread(target=run, args=(a,)) for a in work if work[a]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    report()


def worth_polling(r: dict) -> bool:
    return not r.get("error") and bool(r.get("india") or r.get("remote_open") or r.get("remote_locked"))


def report() -> None:
    rows = list(map(json.loads, MEASURE.read_text().splitlines())) if MEASURE.exists() else []
    by = defaultdict(Counter)
    for r in rows:
        c = by[r["ats"]]
        c["measured"] += 1
        c["error"] += bool(r.get("error"))
        c["with_jobs"] += bool(r.get("jobs"))
        c["india"] += bool(r.get("india"))
        c["remote"] += bool(r.get("remote_open") or r.get("remote_locked"))
        c["poll"] += worth_polling(r)
        c["india_postings"] += r.get("india") or 0
    for a, c in sorted(by.items()):
        print(f"{a:16} " + "  ".join(f"{k} {c[k]}" for k in
                                     ("measured", "error", "with_jobs", "india", "remote", "poll", "india_postings")))


def upload(dry_run: bool) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from src.scraper import ats
    from src.state.db import session_scope
    from src.state.models import AtsBoard

    rows, taken = [], set()
    for r in map(json.loads, MEASURE.read_text().splitlines()):
        if r.get("error") or (r["ats"], r["board"].lower()) in taken:
            continue
        taken.add((r["ats"], r["board"].lower()))
        poll = worth_polling(r)
        state = ("active_india" if r.get("india") else "active_remote" if poll
                 else "active_other" if r.get("jobs") else "empty")
        name = r.get("company") or r["board"]
        rows.append(dict(
            ats=r["ats"], board_id=r["board"], source="commoncrawl", company=r.get("company"),
            company_key=ats.key(name) or r["board"], state=state, job_count=r.get("jobs"),
            india_job_count=r.get("india"),
            remote_job_count=(r.get("remote_open") or 0) + (r.get("remote_locked") or 0),
            remote_open_jobs=r.get("remote_open"), remote_locked_jobs=r.get("remote_locked"),
            posted_7d=r.get("posted_7d"), jobs_url=ats.list_url(r["ats"], r["board"]), poll=poll,
            last_posted_at=r.get("last_posted_at"), measured_at=r["ts"], checked_at=r["ts"]))
    size = len(json.dumps(rows, default=str))
    print(f"rows {len(rows)}  poll {sum(r['poll'] for r in rows)}  ~{size / 1e6:.2f} MB")
    if dry_run or not rows:
        return
    with session_scope() as s:
        for i in range(0, len(rows), 1000):
            s.execute(insert(AtsBoard).values(rows[i:i + 1000]).on_conflict_do_nothing())
        s.commit()
    print("uploaded")


def main() -> None:
    from src.scraper import ats

    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", nargs="+", choices=ats.ATSES, default=list(ats.ATSES))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.delay < 1.0:
        raise SystemExit("--delay below 1.0s is not allowed")
    OUT.mkdir(parents=True, exist_ok=True)
    if a.report:
        report()
    elif a.upload:
        upload(a.dry_run)
    else:
        measure(a.ats, a.limit, a.delay)


if __name__ == "__main__":
    main()
