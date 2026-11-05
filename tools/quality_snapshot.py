"""#179 — build and freeze the match-quality benchmark.

The benchmark is a fixed set of ~150 jobs plus everything the pipeline reads
about them, saved locally so ``tools.quality_audit`` can replay parse -> type ->
match -> score -> resume WITHOUT the network or the database, and every fix is
judged on the same inputs.

What is frozen (``data/benchmark/<name>/``, gitignored: personal data):

    jobs.jsonl          the all_jobs row (no embedding), the stored verdict
                        (applied / not_applied) and the slices it was drawn for
    llm_parses.jsonl    one LLM parse per job, captured once, exactly as the
                        model returned it after schema validation and BEFORE
                        grounding / pool / vocabulary additions, so every step
                        after the model call is replayed by the code under test
    vocabulary.json     skill_vocabulary, every row (active or not)
    profile.json        master_profile.json as it was
    profile_rows.json   active master_bullets (with embeddings) + title aliases
    gold_ids.json       the ~50 jobs that get hand-written gold labels
    manifest.json       written by `freeze`: sha256 of every file above. The
                        audit refuses to run on a set whose files changed.

Why the LLM output is captured instead of read from all_jobs: the stored skill
lists are the FINAL ones (model + pool + vocabulary additions), so a fix to any
step after the model (#155: the vocabulary adding "make", "quality") could not
be replayed on them.

USAGE
-----
    python -m tools.quality_snapshot select            # pick jobs, dump rows
    python -m tools.quality_snapshot llm --limit 40    # capture parses (resumable)
    python -m tools.quality_snapshot status
    python -m tools.quality_snapshot freeze            # hash it; no edits after

Read-only on the database. The LLM calls use only the ATS run's free
providers (``ats_poll.llm_providers``), never the paid key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
BENCH = _ROOT / "data" / "benchmark"
SEED = 179
WINDOW_DAYS = 14

FILES = ("jobs.jsonl", "llm_parses.jsonl", "vocabulary.json", "profile.json",
         "profile_rows.json", "gold_ids.json")

# How many jobs each slice contributes. A job drawn for one slice is tagged
# with every slice it belongs to, so the slices overlap in the report.
QUOTA = {"match": 70, "low_score": 38, "hard_filter": 18, "long": 8}
# Every source should be present; portals are few, so they get a floor.
SOURCE_FLOOR = {"linkedin": 6, "naukri": 4}

# Ambiguous words (#155): a tool or a skill in one advert, plain English in
# another. Per term: a Postgres regex for the tool/skill sense and one for the
# everyday sense; up to 2 jobs are drawn for each. Gold labels decide what is
# right; these patterns only make sure both senses are in the set.
AMBIGUOUS = {
    "make": (r"Make\.com|Integromat|(Zapier|n8n)[,/ ]+(and |or )?Make\M|\mMake[,/ ]+(Zapier|n8n)",
             r"\mmake sure\M"),
    "go": (r"\mGolang\M|\mGo\M[,/ ]+(language|services|developer|microservices)|(Java|Python|Rust|C\+\+)[,/ ]+(and |or )?Go\M",
           r"\mgo-to\M|\mgo beyond\M|\mgo live\M"),
    "excel": (r"(MS|Microsoft|Advanced|advanced) Excel\M|\mExcel[,/ ]+(VBA|Power|macros)",
              r"\mexcel (in|at)\M"),
    "spark": (r"Apache Spark|\mSpark\M[ ,]+(jobs|SQL|Streaming|clusters)", r"\mspark (curiosity|interest|innovation|ideas)"),
    "r": (r"\min R\M(?!&)|(Python|SQL)[,/ ]+(and |or )?R\M(?!&)|\mR[,/ ]+(Python|SQL)", None),
    "swift": (r"\mSwift(UI)?\M[ ,]+(iOS|and|or)|iOS[ ,/]+(and )?Swift", r"\mswift(ly)?\M"),
    "rust": (r"\mRust\M[ ,]+(and|or|services|language)|(C\+\+|Go)[,/ ]+(and |or )?Rust\M", None),
    "express": (r"Express\.js|ExpressJS|(Node|Node\.js)[,/ ]+(and )?Express\M", r"\mexpress(ed)? (interest|consent)"),
    "teams": (r"Microsoft Teams|MS Teams", r"\mcross-functional teams\M"),
    "testing": (r"(automated|unit|integration|end-to-end) testing", None),
    "security": (r"security best practices|application security|\mOWASP\M", None),
    "monitoring": (r"monitoring and alerting|\mPrometheus\M", None),
    "quality": (r"\mdata quality\M|\mcode quality\M", r"high-quality|quality product"),
    "written": (None, r"written and verbal|written communication"),
}
PER_AMBIGUOUS_SENSE = 2
GOLD = {"match": 15, "low_score": 8, "hard_filter": 5, "long": 4, "ambiguous": 18}


def _out(name: str) -> Path:
    return BENCH / name


def _frozen(out: Path) -> bool:
    return (out / "manifest.json").exists()


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v


# --- select ----------------------------------------------------------------------------

_POP = f"""
    with verdict as (
        select job_id, 'match' as slice, built_at as at from applied
        where built_at > now() - interval '{WINDOW_DAYS} days'
        union all
        select job_id, case when reason_category = 'LOW_SCORE' then 'low_score'
                            else 'hard_filter' end, not_applied_at from not_applied
        where not_applied_at > now() - interval '{WINDOW_DAYS} days'
          and reason_category in ('LOW_SCORE', 'HARD_FILTER_LAYER_3', 'JOB_TYPE_DISALLOWED')
    )
    select j.job_id, v.slice, j.site, coalesce(j.parse_mode, '') as parse_mode,
           lower(j.company) as company, lower(j.role) as role, length(j.jd_text) as jd_len
    from verdict v join all_jobs j using (job_id)
    where j.jd_text is not null and length(j.jd_text) >= 300
"""


def _stratified(rows: list[dict], n: int, key, rng: random.Random) -> list[dict]:
    """``n`` rows spread over the groups of ``key``: one per group in turn
    (round robin, groups shuffled), so small groups are represented."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    for g in groups.values():
        rng.shuffle(g)
    order = sorted(groups)
    rng.shuffle(order)
    out: list[dict] = []
    while len(out) < n and any(groups.values()):
        for g in order:
            if groups[g] and len(out) < n:
                out.append(groups[g].pop())
    return out


def select(args) -> int:
    from sqlalchemy import text

    from src.state.db import session_scope

    out = _out(args.name)
    if out.exists() and any(out.iterdir()) and not args.force:
        print(f"{out} already exists; it is the frozen set. Use a new --name.")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    with session_scope() as s:
        pop = [dict(r._mapping) for r in s.execute(text(_POP))]
        print(f"population: {len(pop)} jobs with a verdict in the last {WINDOW_DAYS} days")
        by_id = {}
        for r in pop:  # a job with two verdicts keeps the first (a match wins)
            by_id.setdefault(r["job_id"], r)

        chosen: dict[str, set[str]] = defaultdict(set)
        seen_pairs: set[tuple[str, str]] = set()

        def take(rows, slice_name):
            got = 0
            for r in rows:
                pair = (r["company"], r["role"])
                if r["job_id"] in chosen:
                    chosen[r["job_id"]].add(slice_name)
                    continue
                if pair in seen_pairs:  # the same advert reposted
                    continue
                seen_pairs.add(pair)
                chosen[r["job_id"]].add(slice_name)
                got += 1
            return got

        for name in ("match", "low_score", "hard_filter"):
            rows = [r for r in by_id.values() if r["slice"] == name]
            picked = _stratified(rows, QUOTA[name] * 2, lambda r: f"{r['site']}|{r['parse_mode']}", rng)
            take(picked[: QUOTA[name]], name)
        for src, floor in SOURCE_FLOOR.items():
            have = sum(1 for j in chosen if by_id[j]["site"] == src)
            rows = [r for r in by_id.values() if r["site"] == src and r["job_id"] not in chosen]
            rng.shuffle(rows)
            take(rows[: max(0, floor - have)], f"source:{src}")
        long_rows = sorted((r for r in by_id.values() if r["jd_len"] > 8000), key=lambda r: r["job_id"])
        rng.shuffle(long_rows)
        take(long_rows[: QUOTA["long"]], "long")

        ids = list(by_id)
        for term, (tool_rx, plain_rx) in AMBIGUOUS.items():
            for sense, rx in (("tool", tool_rx), ("plain", plain_rx)):
                if not rx:
                    continue
                hits = s.execute(text(
                    "select job_id from all_jobs where job_id = any(:ids) and jd_text ~ :rx"
                ), {"ids": ids, "rx": rx}).scalars().all()
                hits = sorted(hits)
                rng.shuffle(hits)
                # Prefer jobs already in the set (cheaper gold), then new ones.
                hits.sort(key=lambda j: j not in chosen)
                take([by_id[j] for j in hits[:PER_AMBIGUOUS_SENSE]], f"ambiguous:{term}:{sense}")
                print(f"  {term:<10} {sense:<5} {len(hits):>5} adverts")

        rows = s.execute(text("""
            select j.*, a.selection_json, a.fit_score as a_fit, a.final_score as a_final,
                   a.gap_skills as a_gaps, a.user_status, a.built_at,
                   n.reason_category, n.reason_detail, n.fit_score as n_fit,
                   n.final_score as n_final, n.not_applied_at
            from all_jobs j
            left join applied a using (job_id)
            left join not_applied n using (job_id)
            where j.job_id = any(:ids)
        """), {"ids": list(chosen)}).mappings().all()

        with (out / "jobs.jsonl").open("w") as f:
            for r in sorted(rows, key=lambda r: r["job_id"]):
                row = {k: _jsonable(v) for k, v in r.items() if k != "jd_embedding"}
                stored = ({"verdict": "match", "fit": row.pop("a_fit"), "final": row.pop("a_final"),
                           "gap_skills": row.pop("a_gaps"), "user_status": row.pop("user_status"),
                           "selection": row.pop("selection_json"), "at": row.pop("built_at")}
                          if r["a_final"] is not None or r["selection_json"] is not None else
                          {"verdict": r["reason_category"], "detail": row.pop("reason_detail"),
                           "fit": row.pop("n_fit"), "final": row.pop("n_final"),
                           "at": row.pop("not_applied_at")})
                for k in ("a_fit", "a_final", "a_gaps", "user_status", "selection_json", "built_at",
                          "reason_category", "reason_detail", "n_fit", "n_final", "not_applied_at"):
                    row.pop(k, None)
                f.write(json.dumps({"job": row, "stored": stored,
                                    "slices": sorted(chosen[r["job_id"]])}, default=str) + "\n")

        vocab = s.execute(text("select term_key, term, job_count, is_active from skill_vocabulary")).mappings().all()
        (out / "vocabulary.json").write_text(json.dumps([dict(v) for v in vocab]))
        bullets = s.execute(text(
            "select bullet_id, parent_id, parent_type, text, embedding::text as emb from master_bullets where is_active"
        )).mappings().all()
        aliases = s.execute(text(
            "select id, parent_id, block_id, alias, embedding::text as emb from master_title_aliases where is_active"
        )).mappings().all()
        (out / "profile_rows.json").write_text(json.dumps({
            "bullets": [{**{k: v for k, v in b.items() if k != "emb"}, "embedding": json.loads(b["emb"]) if b["emb"] else None} for b in bullets],
            "aliases": [{**{k: v for k, v in a.items() if k != "emb"}, "embedding": json.loads(a["emb"]) if a["emb"] else None} for a in aliases],
        }))
    shutil.copyfile(_ROOT / "master_profile.json", out / "profile.json")

    # Gold subset: chosen now, before anyone looks at a fix's output.
    gold: list[str] = []
    for name, n in GOLD.items():
        pool = sorted(j for j, sl in chosen.items()
                      if j not in gold and any(x == name or x.startswith(name + ":") for x in sl))
        rng.shuffle(pool)
        gold += pool[:n]
    (out / "gold_ids.json").write_text(json.dumps(sorted(gold), indent=1))
    (out / "llm_parses.jsonl").touch()
    (out / "selected_at.txt").write_text(datetime.now(timezone.utc).isoformat())

    counts = defaultdict(int)
    for sl in chosen.values():
        for x in sl:
            counts[x.split(":")[0] if not x.startswith("ambiguous") else "ambiguous"] += 1
    print(f"selected {len(chosen)} jobs ({dict(counts)}); gold {len(gold)} -> {out}")
    return 0


# --- llm -------------------------------------------------------------------------------

def _job_obj(row: dict):
    from src.state.models import AllJobs

    cols = {c.key for c in AllJobs.__table__.columns}
    vals = {k: v for k, v in row.items() if k in cols and k != "jd_embedding"}
    for k in ("posted_at", "scraped_at", "routed_at", "outcome_at"):
        if isinstance(vals.get(k), str):
            vals[k] = datetime.fromisoformat(vals[k])
    return AllJobs(**vals)


def load_jobs(out: Path) -> list[dict]:
    return [json.loads(line) for line in (out / "jobs.jsonl").read_text().splitlines() if line.strip()]


def load_parses(out: Path) -> dict[str, dict]:
    p = out / "llm_parses.jsonl"
    if not p.exists():
        return {}
    rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return {r["job_id"]: r for r in rows}


def llm(args) -> int:
    from src.config import settings
    from src.llm.client import LLMBudgetError, LLMError, complete, observe, only_providers
    from src.llm.prompts import jd_parse_prompt, jd_parse_system
    from src.llm.schemas import JDParsed

    out = _out(args.name)
    if _frozen(out):
        print("frozen; nothing to capture")
        return 1
    done = load_parses(out)
    todo = [j for j in load_jobs(out) if j["job"]["job_id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    providers = list(settings.ats_poll.get("llm_providers") or ["groq", "gemini-free"])
    print(f"{len(done)} captured, {len(todo)} to go, providers {providers}")
    served: dict = {}

    def on(event, f):
        if event == "served":
            served.update(provider=f.get("provider"), model=f.get("model"))

    ok = 0
    with only_providers(providers), observe(on), (out / "llm_parses.jsonl").open("a") as fh:
        for i, rec in enumerate(todo, 1):
            job = _job_obj(rec["job"])
            served.clear()
            try:
                parsed = complete(JDParsed, jd_parse_prompt(job), system=jd_parse_system(),
                                  prompt_fn=lambda cfg, job=job: jd_parse_prompt(job, provider_cfg=cfg))
            except LLMBudgetError as exc:
                print(f"budget spent after {ok}: {str(exc)[:160]}  (run again later)")
                break
            except LLMError as exc:
                print(f"  {job.job_id}: failed, {str(exc)[:120]}")
                continue
            fh.write(json.dumps({"job_id": job.job_id, "parsed": parsed.model_dump(mode="json"),
                                 "provider": served.get("provider"), "model": served.get("model"),
                                 "captured_at": datetime.now(timezone.utc).isoformat()}) + "\n")
            fh.flush()
            ok += 1
            print(f"  {i}/{len(todo)} {job.job_id} via {served.get('provider')}", flush=True)
    print(f"captured {ok}; total {len(done) + ok}")
    return 0


# --- status / freeze -------------------------------------------------------------------

def status(args) -> int:
    out = _out(args.name)
    jobs, parses = load_jobs(out), load_parses(out)
    gold = json.loads((out / "gold_ids.json").read_text())
    labelled = 0
    gpath = out / "gold.yaml"
    if gpath.exists():
        import yaml

        labelled = len((yaml.safe_load(gpath.read_text()) or {}).get("jobs") or {})
    print(f"{out}: {len(jobs)} jobs, {len(parses)} LLM parses, gold {labelled}/{len(gold)} labelled,"
          f" {'FROZEN' if _frozen(out) else 'not frozen'}")
    return 0


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(args) -> int:
    out = _out(args.name)
    if _frozen(out):
        print("already frozen")
        return 1
    missing = len(load_jobs(out)) - len(load_parses(out))
    if missing and not args.allow_missing:
        print(f"{missing} jobs have no LLM parse yet; run `llm` again or pass --allow-missing")
        return 1
    manifest = {"name": args.name, "frozen_at": datetime.now(timezone.utc).isoformat(),
                "selected_at": (out / "selected_at.txt").read_text().strip(),
                "files": {f: sha(out / f) for f in FILES}}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"frozen: {out / 'manifest.json'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="v1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("select")
    p.add_argument("--force", action="store_true", help="overwrite an unfrozen set")
    p = sub.add_parser("llm")
    p.add_argument("--limit", type=int)
    sub.add_parser("status")
    p = sub.add_parser("freeze")
    p.add_argument("--allow-missing", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "select" and args.force and _frozen(_out(args.name)):
        print("a frozen set is never re-selected")
        return 1
    return {"select": select, "llm": llm, "status": status, "freeze": freeze}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
