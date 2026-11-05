"""#179 — replay the pipeline over the frozen benchmark and measure every stage.

Offline by construction: the network is blocked (sockets refused, Hugging Face
offline), the database is never opened. Every input comes from the snapshot
``tools.quality_snapshot`` froze; everything after the model call is the code
under test: grounding, pool and vocabulary additions, dictionary parse,
routing, hard filters, keyword typing, matching, fit, selection, gaps, bolding.

    python -m tools.quality_audit run --label v0          # report on this checkout
    python -m tools.quality_audit compare v0 my-fix       # before/after + verdict flips

Reports: ``data/benchmark/<set>/reports/<label>.json`` (per job) and ``.md``.

GOLD LABELS (``data/benchmark/<set>/gold.yaml``, hand-written, see
``gold_template``) are what "correct" means. Metrics that need them are left
blank for jobs without a label. Format::

    jobs:
      <job_id>:
        years: 3                 # what the advert asks; 0 = not stated
        level: mid               # junior | mid | senior | lead ...
        job_type: full_time      # full_time | contract | internship | part_time
        skills:                  # every skill the advert really asks for
          - {name: Python, req: true, type: tool, has: yes}
          - {name: Kafka, req: false, type: tool, has: no}
        must_drop: [make, quality]   # words in this advert that are NOT skills here
        notes: free text

``has`` is whether the operator's profile truly shows the skill (yes/no/partial).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics as st
import sys
from collections import Counter, defaultdict
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]
BENCH = _ROOT / "data" / "benchmark"


# --- offline guard ---------------------------------------------------------------------

def _go_offline() -> None:
    """No request leaves the machine: the replay must only see frozen inputs."""
    import socket

    for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ[k] = "1"
    real_connect = socket.socket.connect

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and (host.startswith("127.") or host in ("localhost", "::1")
                                      or self.family == getattr(socket, "AF_UNIX", None)):
            return real_connect(self, address)
        raise ConnectionRefusedError(f"quality_audit is offline; refused {address!r}")

    socket.socket.connect = connect


# --- snapshot wiring -------------------------------------------------------------------

_CURRENT: ContextVar[str | None] = ContextVar("bench_job", default=None)


class _ProfileSession:
    """Just enough of a Session for ``master_profile.load_profile``: the two
    selects it makes, answered from the frozen rows."""

    def __init__(self, rows: dict):
        self.rows = rows

    def scalars(self, stmt):
        from src.state.models import MasterBullets, MasterTitleAliases

        entity = stmt.column_descriptions[0]["entity"]
        if entity is MasterBullets:
            items = [SimpleNamespace(**b) for b in self.rows["bullets"]]
        elif entity is MasterTitleAliases:
            items = [SimpleNamespace(**a) for a in self.rows["aliases"]]
        else:
            raise RuntimeError(f"unexpected query in the offline audit: {entity}")
        return SimpleNamespace(all=lambda: items)


def _wire(set_dir: Path):
    """Point every DB / file read the pipeline makes at the snapshot. Returns
    (profile, captured LLM parses, extra-bullet ids)."""
    from src import parser
    from src.llm.client import LLMError
    from src.llm.schemas import JDParsed
    from src.parsing.llm import LLMParser
    from src.parsing.modes import register, reset_registry
    from src.scorer import keywords
    from src.state import master_profile

    from tools.quality_snapshot import load_parses

    profile_json = set_dir / "profile.json"
    master_profile._JSON_PATH = profile_json
    vocab = json.loads((set_dir / "vocabulary.json").read_text())
    terms = tuple(sorted(((v["term"], v["term_key"]) for v in vocab if v["is_active"]),
                         key=lambda p: len(p[1]), reverse=True))
    parser._vocabulary = lambda: terms
    for fn in (parser._pool_terms, keywords._known_names, keywords._usual_spelling):
        fn.cache_clear()

    parses = load_parses(set_dir)

    def replay(response_model, prompt=None, *, system=None, prompt_fn=None):
        rec = parses.get(_CURRENT.get())
        if rec is None:
            raise LLMError("no captured LLM parse for this job")
        return JDParsed.model_validate(rec["parsed"])

    reset_registry()
    register(LLMParser(complete=replay), replace=True)

    rows = json.loads((set_dir / "profile_rows.json").read_text())
    profile = master_profile.load_profile(_ProfileSession(rows), json_path=profile_json)
    pj = json.loads(profile_json.read_text())
    extras = {b["id"] for e in (*pj.get("work_experience", []), *pj.get("projects", []))
              for rb in e.get("role_blocks", []) for b in rb.get("extra_bullets", [])}
    # Every bullet the profile could show, for "has it, but not on the page".
    from src.scorer.keywords import norm

    evidence = [(e.label, b.id, norm(b.text)) for e in (*profile.work, *profile.projects)
                for blk in e.blocks for b in blk.bullets]
    return profile, parses, extras, evidence


# --- one job ---------------------------------------------------------------------------

def _required_tokens(required: list[str]) -> list[str]:
    """The split ``tailor.compute_gap_skills`` uses, so matched + gap = required."""
    return list(dict.fromkeys(
        p.strip(" ;:") for line in required for p in re.split(r"[,()]", line or "")
        if len(p.strip(" ;:")) >= 2))


def replay_job(rec: dict, profile, parses: dict, extras: set[str], evidence: list) -> dict:
    from src.config import settings
    from src.parsing.pipeline import parse_job
    from src.parsing.router import Route
    from src.reasons import HARD_FILTER_LAYER_3, JOB_TYPE_DISALLOWED, LOW_SCORE
    from src.scorer.apply_decision import threshold_for
    from src.scorer.keywords import covered_by, keyword_spans, matches, norm, term_type
    from src.scraper import filters
    from src.tailor import build_applied, score_job

    from tools.quality_snapshot import _job_obj

    job = _job_obj(rec["job"])
    at = rec["stored"].get("at")
    now = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
    token = _CURRENT.set(job.job_id)
    try:
        routed = parse_job(job, profile=profile, now=now)
    finally:
        _CURRENT.reset(token)
    parsed = routed.parsed
    out = {
        "job_id": job.job_id, "company": job.company, "role": job.role, "site": job.site,
        "slices": rec["slices"], "stored_verdict": rec["stored"].get("verdict"),
        "stored_final": rec["stored"].get("final"),
        "route": routed.decision.route.value, "route_reason": routed.decision.reason.value,
        "llm_captured": job.job_id in parses,
        "dict_score": round(routed.dict_score, 4),
        "years": parsed.years_required, "level": parsed.role_level, "job_type": parsed.job_type,
        "required_skills": list(parsed.required_skills), "nice_to_have": list(parsed.nice_to_have),
    }
    result = None
    if routed.decision.route is Route.SKIPPED_FLOOR:
        verdict, result = LOW_SCORE, routed.dict_result
    elif filters.job_type_disallowed(parsed.job_type, settings.filters.get("job_type")):
        verdict = JOB_TYPE_DISALLOWED
    elif filters.exceeds_years_ceiling(parsed.years_required, int(settings.filters.years_ceiling)):
        verdict = HARD_FILTER_LAYER_3
    else:
        result = score_job(profile, job, parsed, now=now)
        verdict = "match" if result.apply else LOW_SCORE
    out["verdict"] = verdict
    out["threshold"] = threshold_for(job.site)
    if result is None:
        return out

    kws = result.jd_keywords
    out.update(fit=round(result.fit, 4), final=round(result.final_score, 4),
               keyword_coverage=round(result.keyword_coverage, 4),
               keywords=[{"token": k.token, "weight": round(k.weight, 3), "required": bool(k.required),
                          "type": term_type(k.token)} for k in kws])
    if verdict != "match":
        return out

    built = build_applied(profile, job, parsed, result)
    gaps = list(built.gap_skills) if built else []
    req_tokens = _required_tokens(list(parsed.required_skills or []))
    out["gap_skills"] = gaps
    out["matched_skills"] = [t for t in req_tokens if t not in set(gaps)]

    # The page: what renders, what each bullet was picked for, what is bolded.
    to_bold = [k.token for k in kws]
    page, page_norm = [], []
    for e in result.entries:
        bl = []
        for b in e.bullets:
            n = norm(b.text)
            page_norm.append(n)
            spans, shown = keyword_spans(b.text, to_bold) if to_bold else ([], set())
            to_bold = [k for k in to_bold if k not in shown]
            bl.append({"id": b.id, "extra": b.id in extras, "summary": b.is_summary, "via": b.via,
                       "picked_for": list(b.new_keywords) or list(b.new_canonical),
                       "jd_keywords_in_text": sorted(covered_by(n, kws)),
                       "bolded": sorted(shown), "chars": len(b.text)})
        page.append({"entry": e.header_left, "kind": e.kind, "bullets": bl})
    out["page"] = page
    on_page = covered_by(" \n ".join(page_norm), kws)
    req_kw = [k.token for k in kws if k.required]
    out["required_on_page"] = [t for t in req_kw if t in on_page]
    out["required_kw"] = req_kw
    # Required keywords the profile proves somewhere but the page never shows
    # (TypeScript asked by a data role, PyTorch by a full-stack one), with the
    # entries that could have shown them.
    shown_entries = {e.label for e in result.entries}
    missing = {}
    for t in req_kw:
        if t in on_page:
            continue
        where = sorted({lab for lab, _bid, n in evidence if matches(t, n)})
        if where:
            missing[t] = {"entries": where, "entry_on_page": any(w in shown_entries for w in where)}
    out["recoverable_missing"] = missing
    return out


# --- gold ------------------------------------------------------------------------------

def load_gold(set_dir: Path) -> dict:
    p = set_dir / "gold.yaml"
    if not p.exists():
        return {}
    import yaml

    return (yaml.safe_load(p.read_text()) or {}).get("jobs") or {}


def _same(a: str, b: str) -> bool:
    """A predicted keyword and a gold skill name are the same skill: same family
    key, or one names the other ("Python programming" / "Python")."""
    from src.scorer.keywords import canonical_key, matches, norm

    return (canonical_key(a) == canonical_key(b)
            or matches(a, norm(b)) or matches(b, norm(a)))


def _first(items, pred):
    return next((x for x in items if pred(x)), None)


def score_gold(job: dict, gold: dict) -> dict:
    """Per-job parse / type / match numbers against its gold label."""
    g_sk = gold.get("skills") or []
    pred = job.get("keywords")
    if pred is None:  # never scored (hard filter): fall back to the parse lists
        pred = ([{"token": t, "required": True, "type": None} for t in _required_tokens(job["required_skills"])]
                + [{"token": t, "required": False, "type": None} for t in _required_tokens(job["nice_to_have"])])
    found, missed, buckets_ok, types = [], [], 0, []
    for g in g_sk:
        p = _first(pred, lambda p, g=g: _same(p["token"], str(g["name"])))
        if p is None:
            missed.append(g["name"])
            continue
        found.append(g["name"])
        buckets_ok += bool(p["required"]) == bool(g.get("req", True))
        if g.get("type") and p.get("type"):
            types.append((g["name"], g["type"], p["type"]))
    extra = [p["token"] for p in pred if not any(_same(p["token"], str(g["name"])) for g in g_sk)]
    drop = [str(w) for w in gold.get("must_drop") or []]
    # Exact key, not _same: "excel at" must not flag the tool "Excel".
    from src.scorer.keywords import canonical_key

    drop_keys = {canonical_key(w) for w in drop}
    wrong_sense = [p["token"] for p in pred if canonical_key(p["token"]) in drop_keys]
    out = {"gold_skills": len(g_sk), "found": found, "missed": missed, "extraneous": extra,
           "bucket_ok": buckets_ok, "wrong_sense": wrong_sense, "types": types}
    if "years" in gold:
        out["years_gold"], out["years_ok"] = gold["years"], job["years"] == gold["years"]
        out["years_close"] = abs((job["years"] or 0) - (gold["years"] or 0)) <= 1
    if gold.get("level"):
        out["level_ok"] = str(job["level"]).lower() == str(gold["level"]).lower()
    if gold.get("job_type"):
        out["type_ok"] = str(job["job_type"] or "full_time").lower() == str(gold["job_type"]).lower()
    # Match: for each required gold skill the profile does or does not have,
    # did the pipeline say matched (not a gap)?
    if "gap_skills" in job:
        conf = Counter()
        for g in g_sk:
            if not g.get("req", True) or g.get("has") not in ("yes", "no", True, False):
                continue
            truth = g["has"] in ("yes", True)
            said = any(_same(m, str(g["name"])) for m in job["matched_skills"])
            conf[(truth, said)] += 1
        out["match_conf"] = {f"{t}/{s}": n for (t, s), n in conf.items()}
        # Junk-justified bullets: picked only for keywords that are not real
        # skills of this advert (not in gold, or a must-drop word).
        junk = []
        for e in job["page"]:
            for b in e["bullets"]:
                if b["summary"] or not b["picked_for"]:
                    continue
                if all(not any(_same(k, str(g["name"])) for g in g_sk) or k in wrong_sense
                       for k in b["picked_for"]):
                    junk.append({"bullet": b["id"], "picked_for": b["picked_for"]})
        out["junk_bullets"] = junk
    return out


# --- report ----------------------------------------------------------------------------

def _q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


def _pct(n, d):
    return f"{100 * n / d:.0f}% ({n}/{d})" if d else "–"


def summarize(jobs: list[dict], gold: dict) -> dict:
    s: dict = {"jobs": len(jobs)}
    s["verdicts"] = dict(Counter(j["verdict"] for j in jobs))
    s["routes"] = dict(Counter(j["route"] for j in jobs))
    s["llm_missing"] = sum(not j["llm_captured"] for j in jobs)
    s["matches_per_100"] = round(100 * s["verdicts"].get("match", 0) / max(1, len(jobs)), 1)
    fits = [j["fit"] for j in jobs if "fit" in j]
    s["fit_q"] = [round(_q(fits, p), 3) for p in (0.1, 0.5, 0.9)] if fits else []

    # Keyword stage, no gold needed: what enters checklists, and its types.
    kw = Counter()
    types = Counter()
    single_lower = Counter()
    for j in jobs:
        for k in j.get("keywords", []):
            kw[k["token"].casefold()] += 1
            types[k["type"]] += 1
            if " " not in k["token"] and k["token"].islower():
                single_lower[k["token"]] += 1
    s["keyword_types"] = dict(types)
    s["top_single_lowercase_keywords"] = single_lower.most_common(25)

    # Resume stage.
    m = [j for j in jobs if j["verdict"] == "match"]
    per_entry, per_page, extras_e, thin, picked_one = [], [], [], 0, Counter()
    total_b, qual_b = 0, 0
    req_on, req_all = 0, 0
    for j in m:
        n = 0
        for e in j["page"]:
            bs = e["bullets"]
            per_entry.append(len(bs))
            extras_e.append(sum(b["extra"] for b in bs))
            n += len(bs)
            for b in bs:
                if b["summary"]:
                    continue
                total_b += 1
                qual_b += b["via"] == "qualification"
                if len(b["picked_for"]) <= 1:
                    thin += 1
                    if b["picked_for"]:
                        picked_one[b["picked_for"][0]] += 1
        per_page.append(n)
        req_on += len(j["required_on_page"])
        req_all += len(j["required_kw"])
    s["bullets_per_page"] = [_q(per_page, p) for p in (0.1, 0.5, 0.9)] if per_page else []
    s["bullets_per_entry"] = dict(sorted(Counter(per_entry).items()))
    s["extras_per_entry"] = dict(sorted(Counter(extras_e).items()))
    s["bullets_non_summary"] = total_b
    s["bullets_picked_for_one_or_none"] = thin
    s["bullets_via_qualification"] = qual_b
    s["one_keyword_bullets_by_keyword"] = picked_one.most_common(25)
    s["required_on_page"] = [req_on, req_all]
    rec_n, rec_other, rec_kw = 0, 0, Counter()
    for j in m:
        for t, v in j.get("recoverable_missing", {}).items():
            rec_n += 1
            rec_other += not v["entry_on_page"]
            rec_kw[t] += 1
    s["recoverable_missing"] = [rec_n, rec_other]
    s["recoverable_missing_keywords"] = rec_kw.most_common(25)
    # Bold by entry position: how many bullets carry no bold keyword, and bold
    # keywords per bullet, for the lead entry, the 2nd, the 3rd...
    pos = defaultdict(lambda: [0, 0, 0])  # bullets, unbolded, bold keywords
    for j in m:
        for i, e in enumerate(j["page"]):
            for b in e["bullets"]:
                pos[min(i, 5)][0] += 1
                pos[min(i, 5)][1] += not b["bolded"]
                pos[min(i, 5)][2] += len(b["bolded"])
    bold = Counter(k for j in m for e in j["page"] for b in e["bullets"] for k in b["bolded"])
    s["bolded_keywords"] = bold.most_common()  # every distinct one (#185 hand review)
    s["bold_by_position"] = {f"#{k + 1}{'+' if k == 5 else ''}": v for k, v in sorted(pos.items())}

    # Gold.
    g = {j["job_id"]: score_gold(j, gold[j["job_id"]]) for j in jobs if j["job_id"] in gold}
    s["gold_jobs"] = len(g)
    if g:
        tot = sum(x["gold_skills"] for x in g.values())
        found = sum(len(x["found"]) for x in g.values())
        extra = sum(len(x["extraneous"]) for x in g.values())
        s["gold"] = {
            "skill_recall": [found, tot],
            "skill_precision": [found, found + extra],
            "bucket_ok": [sum(x["bucket_ok"] for x in g.values()), found],
            "years_exact": [sum(x.get("years_ok", 0) for x in g.values()), sum("years_ok" in x for x in g.values())],
            "years_pm1": [sum(x.get("years_close", 0) for x in g.values()), sum("years_close" in x for x in g.values())],
            "level_ok": [sum(x.get("level_ok", 0) for x in g.values()), sum("level_ok" in x for x in g.values())],
            "job_type_ok": [sum(x.get("type_ok", 0) for x in g.values()), sum("type_ok" in x for x in g.values())],
            "type_agree": [sum(a == b for x in g.values() for _, a, b in x["types"]),
                           sum(len(x["types"]) for x in g.values())],
            "wrong_sense": Counter(w for x in g.values() for w in x["wrong_sense"]).most_common(20),
            "missed": Counter(w for x in g.values() for w in x["missed"]).most_common(25),
            "extraneous": Counter(w for x in g.values() for w in x["extraneous"]).most_common(25),
        }
        conf = Counter()
        for x in g.values():
            conf.update(x.get("match_conf", {}))
        s["gold"]["match_conf"] = dict(conf)
        junk = [b for x in g.values() for b in x.get("junk_bullets", [])]
        s["gold"]["junk_bullets"] = [len(junk), sum(
            sum(not b["summary"] for e in j["page"] for b in e["bullets"])
            for j in jobs if j["job_id"] in g and "page" in j)]
        s["gold"]["junk_keywords"] = Counter(k for b in junk for k in b["picked_for"]).most_common(20)
        # By route (parse source).
        by = defaultdict(lambda: [0, 0])
        for jid, x in g.items():
            r = next(j["route"] for j in jobs if j["job_id"] == jid)
            by[r][0] += len(x["found"])
            by[r][1] += x["gold_skills"]
        s["gold"]["recall_by_route"] = dict(by)
        s["per_job_gold"] = g
    return s


def render_md(label: str, s: dict) -> str:
    L = [f"# Match-quality audit: {label}", "",
         f"{s['jobs']} jobs, {s['gold_jobs']} with gold labels; LLM parse missing for {s['llm_missing']}.", "",
         "| Stage | Metric | Value |", "|---|---|---|"]
    g = s.get("gold")
    if g:
        L += [f"| Parse | skill recall vs gold | {_pct(*g['skill_recall'])} |",
              f"| Parse | skill precision vs gold | {_pct(*g['skill_precision'])} |",
              f"| Parse | required/nice bucket right | {_pct(*g['bucket_ok'])} |",
              f"| Parse | years exact / ±1 | {_pct(*g['years_exact'])} / {_pct(*g['years_pm1'])} |",
              f"| Parse | level / job type right | {_pct(*g['level_ok'])} / {_pct(*g['job_type_ok'])} |",
              f"| Parse | recall by route | {', '.join(f'{r} {_pct(*v)}' for r, v in g['recall_by_route'].items())} |",
              f"| Type | agrees with gold type | {_pct(*g['type_agree'])} |"]
        c = g["match_conf"]
        tp, fp, fn = c.get("True/True", 0), c.get("False/True", 0), c.get("True/False", 0)
        L += [f"| Match | precision (said has, truly has) | {_pct(tp, tp + fp)} |",
              f"| Match | recall (truly has, said has) | {_pct(tp, tp + fn)} |",
              f"| Match | wrong-sense words in checklists | {sum(n for _, n in g['wrong_sense'])} |",
              f"| Resume | bullets picked only for non-skills | {_pct(*g['junk_bullets'])} |"]
    L += [f"| Score | verdicts | {s['verdicts']} |",
          f"| Score | fit p10/p50/p90 | {s['fit_q']} |",
          f"| Score | routes | {s['routes']} |",
          f"| Resume | bullets per page p10/p50/p90 | {s['bullets_per_page']} |",
          f"| Resume | bullets per entry | {s['bullets_per_entry']} |",
          f"| Resume | extras per entry | {s['extras_per_entry']} |",
          f"| Resume | non-summary bullets picked for ≤1 keyword | {_pct(s['bullets_picked_for_one_or_none'], s['bullets_non_summary'])} |",
          f"| Resume | bullets filled from the title checklist (no JD keyword) | {_pct(s['bullets_via_qualification'], s['bullets_non_summary'])} |",
          f"| Resume | required keywords on the page | {_pct(*s['required_on_page'])} |",
          f"| Resume | required keywords the profile proves but the page omits | {s['recoverable_missing'][0]}"
          f" ({s['recoverable_missing'][1]} only in an entry not on the page) |",
          f"| Volume | matches per 100 jobs | {s['matches_per_100']} |", "",
          "## Bold by entry position", "",
          "| Entry | Bullets | With no bold keyword | Bold keywords per bullet |", "|---|---|---|---|"]
    L += [f"| {k} | {b} | {_pct(u, b)} | {k2 / b:.2f} |" for k, (b, u, k2) in s["bold_by_position"].items() if b]
    L += ["", f"## Bolded keywords ({len(s['bolded_keywords'])} distinct; all in the .json)", "",
          ", ".join(f"{k} ({n})" for k, n in s["bolded_keywords"][:60]) or "–"]
    L += ["", "## Required keywords the profile proves but the page omits", "",
          ", ".join(f"{k} ({n})" for k, n in s["recoverable_missing_keywords"]) or "–", ""]
    L += [""]
    L += ["## Keywords that alone earn a bullet its slot", "",
          ", ".join(f"{k} ({n})" for k, n in s["one_keyword_bullets_by_keyword"]) or "–", "",
          "## Single lower-case words in JD checklists", "",
          ", ".join(f"{k} ({n})" for k, n in s["top_single_lowercase_keywords"]) or "–", ""]
    if g:
        for name in ("wrong_sense", "junk_keywords", "missed", "extraneous"):
            L += [f"## Gold: {name.replace('_', ' ')}", "",
                  ", ".join(f"{k} ({n})" for k, n in g[name]) or "–", ""]
    return "\n".join(L)


# --- commands --------------------------------------------------------------------------

def run(args) -> int:
    from tools.quality_snapshot import load_jobs, sha, FILES

    set_dir = BENCH / args.set
    manifest = set_dir / "manifest.json"
    if manifest.exists():
        want = json.loads(manifest.read_text())["files"]
        bad = [f for f in FILES if sha(set_dir / f) != want[f]]
        if bad:
            print(f"the frozen set changed: {bad}. The benchmark is never edited.")
            return 1
    elif not args.unfrozen:
        print("the set is not frozen yet; pass --unfrozen to try the audit on it anyway")
        return 1
    _go_offline()
    import structlog

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))
    if args.no_registry:
        # The pre-#188 behaviour, for a before/after on the same inputs.
        import src.state.registry as _registry

        _registry.enabled = lambda: False
    profile, parses, extras, evidence = _wire(set_dir)
    jobs = []
    recs = load_jobs(set_dir)
    if args.limit:
        recs = recs[: args.limit]
    for i, rec in enumerate(recs, 1):
        jobs.append(replay_job(rec, profile, parses, extras, evidence))
        if i % 20 == 0:
            print(f"  {i}/{len(recs)}", flush=True)
    s = summarize(jobs, load_gold(set_dir))
    rep = set_dir / "reports"
    rep.mkdir(exist_ok=True)
    import subprocess

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                          cwd=_ROOT).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "src", "config"], capture_output=True,
                                text=True, cwd=_ROOT).stdout.strip())
    meta = {"label": args.label, "git": head + ("+dirty" if dirty else ""),
            "at": datetime.now(timezone.utc).isoformat()}
    (rep / f"{args.label}.json").write_text(json.dumps({"meta": meta, "summary": s, "jobs": jobs},
                                                       indent=1, default=str))
    md = render_md(args.label, s)
    (rep / f"{args.label}.md").write_text(md)
    print(md)
    print(f"\n-> {rep / (args.label + '.md')}")
    return 0


def compare(args) -> int:
    rep = BENCH / args.set / "reports"
    a, b = (json.loads((rep / f"{x}.json").read_text()) for x in (args.before, args.after))
    print(f"# {args.before} ({a['meta']['git']}) -> {args.after} ({b['meta']['git']})\n")
    ma = render_md(args.before, a["summary"]).splitlines()
    mb = render_md(args.after, b["summary"]).splitlines()
    rows_a = {r.split("|")[2].strip(): r.split("|")[3].strip() for r in ma if r.startswith("| ") and r.count("|") == 4}
    print("| Metric | Before | After |\n|---|---|---|")
    for r in mb:
        if r.startswith("| ") and r.count("|") == 4 and not r.startswith("| Stage"):
            k, v = r.split("|")[2].strip(), r.split("|")[3].strip()
            mark = "" if rows_a.get(k) == v else " ◀"
            print(f"| {k} | {rows_a.get(k, '–')} | {v}{mark} |")
    ja = {j["job_id"]: j for j in a["jobs"]}
    flips = []
    for j in b["jobs"]:
        o = ja.get(j["job_id"])
        if o and (o["verdict"] == "match") != (j["verdict"] == "match"):
            ka = {k["token"] for k in o.get("keywords", [])}
            kb = {k["token"] for k in j.get("keywords", [])}
            flips.append(f"- {j['job_id']} ({j['role']} at {j['company']}): {o['verdict']} -> {j['verdict']},"
                         f" final {o.get('final')} -> {j.get('final')}; keywords"
                         f" -{sorted(ka - kb)[:8]} +{sorted(kb - ka)[:8]}")
    print(f"\n## Verdict flips ({len(flips)})\n")
    print("\n".join(flips) or "none")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default="v1")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--label", required=True)
    p.add_argument("--limit", type=int)
    p.add_argument("--unfrozen", action="store_true")
    p.add_argument("--no-registry", action="store_true", help="run as before the skills registry (#188)")
    p = sub.add_parser("compare")
    p.add_argument("before")
    p.add_argument("after")
    args = ap.parse_args(argv)
    return {"run": run, "compare": compare}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
