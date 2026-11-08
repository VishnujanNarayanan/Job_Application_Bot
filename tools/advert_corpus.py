"""A local corpus of current India tech adverts, fetched live (#177, #195).

The 50 hand-labelled benchmark adverts measure the parser; they are far too few
to show WHICH terms it misses. This builds a large, unlabelled corpus to search
instead, without the database (no Neon transfer):

- boards: every board ``data/ats_probe/measure_cc.jsonl`` measured with India
  jobs (the Common Crawl / discovery sweep), all ATSes the poller reads;
- lists and descriptions: the poller's own paced lanes (``ats_poller``:
  one lane per ATS, parallel lanes per host for Workday/Oracle/..., 429s
  back off), so no ATS is read faster than the live poll reads it;
- filters: the live poll's stages A-D (``ats_pipeline.classify``) and G
  (``decide``): India or open remote, allowed type, tech role families.
  Adverts stage G would drop are kept and tagged (``stage_g``): its
  relevance check drops unclear titles that name too few KNOWN skills, which
  is where unknown terms hide;
- at most ``--per-board`` adverts per board (newest first), so a few giant
  tenants don't dominate the term counts;
- benchmark adverts are left out, so discovery never feeds on the test set.

    python -m tools.advert_corpus [--per-board 8] [--max-boards 0] [--minutes 90]

Writes ``data/corpus/adverts.jsonl`` (one advert per line) and appends to
``data/corpus/fetch.log``. Re-running replaces the file. ``data/`` is
gitignored: the corpus never enters the repo.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
MEASURE = _ROOT / "data" / "ats_probe" / "measure_cc.jsonl"
BENCH_IDS = _ROOT / "data" / "benchmark" / "v1" / "gold_ids.json"
OUT = _ROOT / "data" / "corpus"


def india_boards(path: Path = MEASURE) -> list[tuple[str, str, int]]:
    """(ats, board, india jobs) for every board measured with India jobs; the
    latest measurement of a board wins. Most India jobs first."""
    latest: dict[tuple[str, str], dict] = {}
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("ats") and d.get("board"):
            key = (d["ats"], d["board"])
            if key not in latest or (d.get("ts") or "") >= (latest[key].get("ts") or ""):
                latest[key] = d
    # Workday board ids differ only in case for the same site; keep one.
    seen, out = set(), []
    for (a, b), d in sorted(latest.items(), key=lambda kv: -(kv[1].get("india") or 0)):
        if d.get("error") or not (d.get("india") or 0) > 0 or (a, b.lower()) in seen:
            continue
        seen.add((a, b.lower()))
        out.append((a, b, int(d["india"])))
    return out


def benchmark_ids() -> set[str]:
    try:
        return set(json.loads(BENCH_IDS.read_text()))
    except (OSError, ValueError):
        return set()


def main(argv: list[str] | None = None) -> int:
    from src import ats_pipeline as pipe
    from src.config import settings
    from src.scraper import ats, ats_poller

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--per-board", type=int, default=8, help="adverts kept per board, newest first")
    ap.add_argument("--max-boards", type=int, default=0, help="0 = every India board")
    ap.add_argument("--minutes", type=float, default=90, help="budget for each of the list and detail phases")
    ap.add_argument("--atses", default="", help="comma list; default: every ATS the poller reads")
    args = ap.parse_args(argv)

    a = settings.get("ats_poll") or {}
    delay = float(a.get("delay_seconds", 0.5))
    parallel = int(a.get("parallel_hosts", 8))
    wanted = set(args.atses.split(",")) if args.atses else set(a.get("atses") or ats.ATSES)
    boards = [ats_poller.Board(x, b) for x, b, _n in india_boards() if x in wanted]
    if args.max_boards:
        boards = boards[:args.max_boards]
    OUT.mkdir(parents=True, exist_ok=True)
    log_f = (OUT / "fetch.log").open("a")

    def note(msg: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()

    note(f"boards: {len(boards)} ({dict(Counter(b.ats for b in boards))})")
    lanes: dict = {}
    t0 = time.monotonic()
    results = ats_poller.fetch_lists(boards, lanes, budget_s=60 * args.minutes, sr_max_pages=3,
                                     delay=delay, parallel=parallel)
    note(f"lists: {len(results)} boards read, {sum(1 for r in results if r.error)} failed, "
         f"{time.monotonic() - t0:.0f}s")

    rules = pipe.rules_from(settings)
    skip_ids = benchmark_ids()
    kept, reasons = [], Counter()
    for res in results:
        if res.error:
            continue
        mine = []
        for p in res.postings:
            if p.job_id in skip_ids:
                reasons["benchmark"] += 1
                continue
            r = pipe.classify(p, rules)
            if r is None:
                mine.append(p)
            else:
                reasons[r.split(":")[0] if r != ats_poller.DROP else "drop"] += 1
        mine.sort(key=lambda p: -(p.posted_at.timestamp() if p.posted_at else 0.0))
        kept += mine[:args.per_board]
    note(f"after stages A-D: {len(kept)} adverts; skipped {dict(reasons)}")

    t1 = time.monotonic()
    fetched = ats_poller.fetch_details(kept, lanes, delay=delay, budget_s=60 * args.minutes, parallel=parallel)
    note(f"details: {sum(fetched.values())} of {len(kept)} fetched, {time.monotonic() - t1:.0f}s")

    out, dropped = [], Counter()
    for p in kept:
        if not fetched.get(p.job_id) or not (p.description or "").strip():
            dropped[f"no_description:{p.ats}"] += 1
            continue
        # Kept even when stage G would drop it: an unclear title is dropped
        # when the advert names too few skills we know, which is exactly
        # where unknown terms hide. Tagged, so discovery can report both.
        r = pipe.decide(p, rules)
        if r:
            dropped[r.split("=")[0]] += 1
        out.append({"job_id": p.job_id, "ats": p.ats, "board": p.board_id, "company": p.company,
                    "stage_g": r, "title": p.title, "location": p.location, "country": p.country,
                    "posted_at": p.posted_at.isoformat() if p.posted_at else None,
                    "url": p.url, "description": p.description})
    with (OUT / "adverts.jsonl").open("w") as f:
        for row in out:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    note(f"corpus: {len(out)} adverts from {len({(r['ats'], r['board']) for r in out})} boards "
         f"({dict(Counter(r['ats'] for r in out))}); stage G would drop {dict(dropped)}; "
         f"{sum(l.requests for l in lanes.values())} requests, "
         f"{sum(l.bytes for l in lanes.values()) / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
