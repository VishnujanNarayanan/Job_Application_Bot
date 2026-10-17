"""#101 — rescore every stored LLM parse and compare fit formulas.

Read-only against the database, and no LLM calls: each job's stored parse is
rebuilt (``src.tailor.parsed_from_row``) and scored with the live
``evaluate`` exactly as ``src.tailor.score_job`` does.

WHY THIS EXISTS
---------------
Every part of fit is a share (covered / total), so an advert listing 3 skills,
all covered, outscores one listing 15 with 12 covered. Candidate formulas that
only change how coverage is AGGREGATED leave bullet selection untouched --
selection maximises absolute covered weight (``keywords.weight_of``) -- so one
``evaluate`` per job is enough: ``score`` stores what each job's page covers,
and ``compare`` recomputes fit under each candidate from that.

USAGE
-----
    python -m tools.fit_replay score                 # data/reports/fit_replay.json
    python -m tools.fit_replay score --sample 50     # quick run
    python -m tools.fit_replay compare               # tables for every candidate
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import structlog

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUT = _ROOT / "data" / "reports" / "fit_replay.json"


# --- score: one evaluate per job ---------------------------------------------------------

def _rows(session):
    from sqlalchemy import or_, select

    from src.state.models import AllJobs, Applied, NotApplied

    jobs = session.scalars(
        select(AllJobs)
        .where(AllJobs.required_skills.isnot(None), AllJobs.role_summary.isnot(None))
        # LLM parses only: a dictionary parse (#100) is not what fit was tuned on.
        .where(or_(AllJobs.parse_mode.is_(None), AllJobs.parse_mode == "llm"))
    ).all()
    applied = {a.job_id: a.user_status for a in session.scalars(select(Applied))}
    not_applied = dict(session.execute(select(NotApplied.job_id, NotApplied.reason_category)).all())
    return jobs, applied, not_applied


def score(args) -> int:
    from src.parser import with_pool_skills
    from src.scorer.apply_decision import evaluate, threshold_for
    from src.scorer.keywords import jd_keywords
    from src.scorer.selector import build_jd_context
    from src.state.db import session_scope
    from src.state.master_profile import load_profile
    from src.tailor import parsed_from_row

    from src.config import settings

    # The scorer logs per job at INFO; over hundreds of jobs that is noise.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))
    if args.formula:
        settings.scoring.fit.as_dict()["formula"] = args.formula
    print(f"formula: {settings.scoring.fit.get('formula')}")
    dictionary = None
    if args.dict:
        from src.parsing.dictionary import Dictionary, build_parsed

        dictionary = Dictionary.load()
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        profile = load_profile(session)
        jobs, applied, not_applied = _rows(session)
        session.expunge_all()
    if args.sample and args.sample < len(jobs):
        random.Random(args.seed).shuffle(jobs)
        jobs = jobs[: args.sample]
    print(f"scoring {len(jobs)} stored LLM parses ...")

    out = []
    started = datetime.now(timezone.utc)
    for i, job in enumerate(jobs):
        if i and i % 50 == 0:
            secs = (datetime.now(timezone.utc) - started).total_seconds()
            print(f"  {i}/{len(jobs)}  ({secs / i:.1f} s/job)", flush=True)
        parsed = parsed_from_row(job)
        if parsed is None:
            continue
        kws = jd_keywords(parsed, title=job.role, jd_text=job.jd_text)
        jd = build_jd_context(parsed, posted_at=job.posted_at, scraped_at=job.scraped_at,
                              applicants_count=job.applicants_count, jd_text=job.jd_text, title=job.role)
        res = evaluate(profile, jd, keywords=kws, now=now, threshold=threshold_for(job.site))
        lead = res.entries[0] if res.entries else None
        extra = {}
        if dictionary is not None:
            # The #100 parse floor is a cut on the DICTIONARY parse's score.
            dp = build_parsed(job, dictionary)
            dres = evaluate(profile, build_jd_context(dp, posted_at=job.posted_at, scraped_at=job.scraped_at,
                                                      applicants_count=job.applicants_count, jd_text=job.jd_text, title=job.role),
                            keywords=jd_keywords(dp, title=job.role, jd_text=job.jd_text), now=now,
                            threshold=threshold_for(job.site))
            extra = {"dict_fit": dres.fit, "dict_final": dres.final_score}
        out.append({**extra,
            "job_id": job.job_id, "company": job.company, "role": job.role, "site": job.site,
            "keywords": [[k.token, k.weight] for k in kws],
            # What each entry's selected bullets cover; the union is the page's.
            "entries": [sorted(e.covered) for e in res.entries],
            "lead_covered": sorted(lead.covered) if lead else [],
            "similarity_scaled": res.similarity_scaled,
            "lead_entry": res.lead_entry, "keyword_coverage": res.keyword_coverage,
            "keyword_repetition": res.keyword_repetition, "fit": res.fit,
            "success_prob": res.success_prob, "final": res.final_score,
            "threshold": threshold_for(job.site),
            # Pool skills the advert names: the part of the checklist the parser
            # adds whether or not the model returned it (Q6).
            "pool_named": with_pool_skills([], job.jd_text or ""),
            "required_n": len(parsed.required_skills or []), "nice_n": len(parsed.nice_to_have or []),
            "outcome": ("applied:" + (applied[job.job_id] or "pending")) if job.job_id in applied
                       else (not_applied.get(job.job_id) or "none"),
        })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"generated_at": now.isoformat(), "jobs": out}, indent=1))
    print(f"wrote {len(out)} jobs to {args.out}")
    return 0


# --- compare: fit under each candidate ------------------------------------------------------

def _weights():
    from src.config import settings

    f = settings.scoring.fit
    lw = f.lead_entry_weights
    return (float(f.lead_entry), float(f.keyword_coverage), float(f.keyword_repetition),
            int(f.repetition_cap), float(lw.similarity), float(lw.coverage))


def _parts(job: dict, cap: int) -> dict:
    """Covered and total weight for each of fit's three share terms."""
    w = dict((t, x) for t, x in job["keywords"])
    total = sum(w.values())
    union = set().union(*map(set, job["entries"])) if job["entries"] else set()
    required = [t for t, x in w.items() if x >= 1.0] or list(w)
    reps = [min(sum(1 for e in job["entries"] if t in e), cap) / cap for t in required]
    return {
        "cov": (sum(w.get(t, 0.0) for t in union), total),
        "lead": (sum(w.get(t, 0.0) for t in job["lead_covered"]), total),
        # Repetition as a share too: sum of per-keyword depth over the count.
        "rep": (sum(reps), float(len(required))),
        "n_covered": len(union), "n_total": len(w),
    }


def share(c: float, t: float, **_) -> float:
    """Today: covered / total."""
    return c / t if t > 0 else 0.0


def shrink(c: float, t: float, *, a: float, p0: float, **_) -> float:
    """Bayesian average toward ``p0``: a short checklist is weak evidence."""
    return (c + a * p0) / (t + a) if t + a > 0 else p0


def blend(c: float, t: float, *, k: float, lam: float, **_) -> float:
    """Share, plus a saturating count of matches: 12 beats 3, 40 doesn't run away."""
    return (1 - lam) * share(c, t) + lam * (c / (c + k) if c + k > 0 else 0.0)


def fit_with(job: dict, agg, weights, **params) -> float:
    w_lead, w_cov, w_rep, cap, s_w, c_w = weights
    p = _parts(job, cap)
    lead = s_w * job["similarity_scaled"] + c_w * agg(*p["lead"], **params)
    return w_lead * lead + w_cov * agg(*p["cov"], **params) + w_rep * agg(*p["rep"], **params)


def candidates(jobs: list[dict], weights) -> dict:
    """name -> (aggregation, params). ``p0`` is the corpus median coverage."""
    cap = weights[3]
    p0 = st.median(share(*_parts(j, cap)["cov"]) for j in jobs)
    out = {"A share (today)": (share, {})}
    for a in (3, 5, 8):
        out[f"B shrink a={a}"] = (shrink, {"a": a, "p0": round(p0, 3)})
    for k, lam in ((5, 0.3), (8, 0.3), (8, 0.5)):
        out[f"C blend k={k} λ={lam}"] = (blend, {"k": k, "lam": lam})
    return out


def _spearman(x: list[float], y: list[float]) -> float:
    def ranks(v):
        order = sorted(range(len(v)), key=v.__getitem__)
        r = [0.0] * len(v)
        for i, j in enumerate(order):
            r[j] = float(i)
        return r
    rx, ry = ranks(x), ranks(y)
    mx, my = st.mean(rx), st.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


_BUCKETS = ((1, 9), (10, 19), (20, 29), (30, 44), (45, 10**6))


def compare(args) -> int:
    data = json.loads(args.out.read_text())
    jobs = data["jobs"]
    weights = _weights()
    cands = candidates(jobs, weights)
    cap = weights[3]
    today = [j["fit"] for j in jobs]
    # The replay must reproduce today's fit before anything else means much.
    worst = max(abs(fit_with(j, share, weights) - j["fit"]) for j in jobs)
    print(f"{len(jobs)} jobs; candidate A reproduces stored fit to within {worst:.4f}\n")

    fits = {name: [fit_with(j, agg, weights, **p) for j in jobs] for name, (agg, p) in cands.items()}
    print(f"{'candidate':24} {'p25':>5} {'p50':>5} {'p75':>5} {'p90':>5}  {'rank ρ':>6}  "
          f"{'apply':>5} {'in':>4} {'out':>4}")
    base_apply = {j["job_id"] for j in jobs if j["fit"] * j["success_prob"] >= j["threshold"]}
    for name, f in fits.items():
        q = st.quantiles(f, n=20)
        apply = {j["job_id"] for j, x in zip(jobs, f) if min(1.0, x * j["success_prob"]) >= j["threshold"]}
        print(f"{name:24} {q[4]:5.2f} {q[9]:5.2f} {q[14]:5.2f} {q[17]:5.2f}  {_spearman(today, f):6.3f}  "
              f"{len(apply):5} {len(apply - base_apply):4} {len(base_apply - apply):4}")

    print("\nmedian fit by checklist size (n keywords):")
    print(f"{'':24}" + "".join(f"{f'{lo}-{hi}' if hi < 10**6 else f'{lo}+':>9}" for lo, hi in _BUCKETS))
    for name, f in fits.items():
        cells = []
        for lo, hi in _BUCKETS:
            xs = [x for j, x in zip(jobs, f) if lo <= len(j["keywords"]) <= hi]
            cells.append(f"{st.median(xs):9.2f}" if xs else f"{'-':>9}")
        print(f"{name:24}" + "".join(cells))

    print("\nthe issue's two extremes, median fit:")
    small = [i for i, j in enumerate(jobs) if len(j["keywords"]) <= 8
             and share(*_parts(j, cap)["cov"]) >= 0.9]
    big = [i for i, j in enumerate(jobs) if len(j["keywords"]) >= 25 and _parts(j, cap)["n_covered"] >= 15]
    for name, f in fits.items():
        print(f"  {name:24} small&full (n={len(small)}) {st.median(f[i] for i in small) if small else float('nan'):.2f}"
              f"   big&15+ (n={len(big)}) {st.median(f[i] for i in big) if big else float('nan'):.2f}")

    applied = [i for i, j in enumerate(jobs) if j["outcome"] == "applied:applied"]
    if applied:
        print(f"\njobs you marked applied ({len(applied)}): median percentile of their fit")
        for name, f in fits.items():
            order = sorted(f)
            pct = [100 * order.index(f[i]) / len(order) for i in applied]
            print(f"  {name:24} {st.median(pct):5.1f}")

    pool_share = [len({p.casefold() for p in j["pool_named"]} & {t for t, _ in j["keywords"]}) / len(j["keywords"])
                  for j in jobs if j["keywords"]]
    print(f"\nQ6: checklist share that is a pool skill the ad names: median {st.median(pool_share):.2f}")
    nice = [sum(x for _, x in j["keywords"] if x < 1) / max(sum(x for _, x in j["keywords"]), 1e-9) for j in jobs]
    print(f"Q4: nice-to-have share of checklist weight: median {st.median(nice):.2f}, p90 {st.quantiles(nice, n=10)[8]:.2f}")
    return 0


# --- sweep: weights and shrink, tested on the stored results ----------------------------------
# Offline approximation: the matched skills are taken as stored, so a new
# weighting changes how they are counted, not which bullets were picked.

import re as _re

from src.scorer.keywords import term_type  # noqa: E402 - the live typing, not a copy


_MUST = _re.compile(r"\b(must|strong|expert\w*|deep|proficien\w*|mandatory|hands-on|solid|advanced)\b", _re.I)


def emphasis_signals(token: str, title: str, text: str) -> int:
    """0-3: in the title; on a must-have / strong line; named 3+ times."""
    from src.scorer.keywords import matches, norm

    n = 0
    if matches(token, norm(title)):
        n += 1
    if any(_MUST.search(line) and matches(token, norm(line)) for line in text.splitlines()):
        n += 1
    pat = _re.compile(rf"(?<![a-z0-9]){_re.escape(norm(token).strip())}(?![a-z0-9])")
    if len(pat.findall(norm(text))) >= 3:
        n += 1
    return n


def _ads(jobs: list[dict]) -> dict:
    """job_id -> (title, jd_text), read-only."""
    from sqlalchemy import select

    from src.state.db import session_scope
    from src.state.models import AllJobs

    ids = [j["job_id"] for j in jobs]
    with session_scope() as session:
        return {r[0]: (r[1] or "", r[2] or "") for r in session.execute(
            select(AllJobs.job_id, AllJobs.role, AllJobs.jd_text).where(AllJobs.job_id.in_(ids)))}


def reweight(job: dict, info: dict, *, tool: float, generic: float, nice: float, emph: float) -> dict:
    """The job with each skill's weight = kind x type x (1 + emph x signals)."""
    kinds = [(t, 1.0 if w >= 1.0 else nice) for t, w in job["keywords"]]
    out = []
    for t, base in kinds:
        f = {"tool": tool, "generic": generic, "concept": 1.0}[info["type"][t]]
        out.append([t, base * f * (1 + emph * info["signals"][t])])
    return {**job, "keywords": out}


def evidence_fit(job: dict, weights, *, lam: float, cap_at: float, a: float = 0.0, p0: float = 0.0) -> float:
    """Weighted share (optionally shrunk) blended with min(matched weight, cap_at) / cap_at."""
    w_lead, w_cov, w_rep, cap, s_w, c_w = weights
    p = _parts_weighted(job, cap)

    def agg(c, t):
        sh = (c + a * p0) / (t + a) if t + a > 0 else 0.0
        return (1 - lam) * sh + lam * min(c, cap_at) / cap_at

    lead = s_w * job["similarity_scaled"] + c_w * agg(*p["lead"])
    return w_lead * lead + w_cov * agg(*p["cov"]) + w_rep * agg(*p["rep"])


def _parts_weighted(job: dict, cap: int) -> dict:
    """As :func:`_parts`, with repetition weighted by each skill's weight."""
    w = dict((t, x) for t, x in job["keywords"])
    union = set().union(*map(set, job["entries"])) if job["entries"] else set()
    req = [t for t, x in w.items() if x >= min(w.values(), default=0) and t in w]
    reps = {t: min(sum(1 for e in job["entries"] if t in e), cap) / cap for t in w}
    return {
        "cov": (sum(w[t] for t in union if t in w), sum(w.values())),
        "lead": (sum(w[t] for t in job["lead_covered"] if t in w), sum(w.values())),
        "rep": (sum(w[t] * reps[t] for t in w), sum(w.values())),
        "req": req,
    }


def sweep(args) -> int:
    data = json.loads(args.out.read_text())
    jobs = data["jobs"]
    weights = _weights()
    cap = weights[3]
    ads = _ads(jobs)
    info = {}
    types = {"tool": 0, "concept": 0, "generic": 0}
    for j in jobs:
        title, text = ads.get(j["job_id"], ("", ""))
        tt = {t: term_type(t) for t, _ in j["keywords"]}
        for v in tt.values():
            types[v] += 1
        info[j["job_id"]] = {"type": tt, "signals": {t: emphasis_signals(t, title, text) for t, _ in j["keywords"]}}
    total = sum(types.values())
    print("skill types across all ads: " + ", ".join(f"{k} {v} ({100 * v / total:.0f}%)" for k, v in types.items()))
    if args.show_types:
        seen: dict[str, set] = {"tool": set(), "concept": set(), "generic": set()}
        for j in jobs:
            for t, kind in info[j["job_id"]]["type"].items():
                seen[kind].add(t)
        for kind, terms in seen.items():
            print(f"\n{kind} (sample): " + ", ".join(sorted(terms, key=str.casefold)[:: max(1, len(terms) // 60)]))

    matched = [len(set().union(*map(set, j["entries"])) & {t for t, _ in j["keywords"]}) if j["entries"] else 0
               for j in jobs]
    sizes = [len(j["keywords"]) for j in jobs]

    def pcts(xs):
        q = st.quantiles(xs, n=20)
        return f"median {st.median(xs):.0f}, p75 {q[14]:.0f}, p90 {q[17]:.0f}, p95 {q[18]:.0f}, max {max(xs)}"

    print(f"\nskills matched per job ({len(jobs)} jobs): {pcts(matched)}")
    print(f"skills listed per ad:               {pcts(sizes)}")
    for c in (10, 15, 20, 25, 30, 40):
        print(f"  cap {c:2}: {sum(m >= c for m in matched):4} jobs ({100 * sum(m >= c for m in matched) / len(jobs):4.1f}%) "
              f"reach it")

    applied = [i for i, j in enumerate(jobs) if j["outcome"] == "applied:applied"]
    small = [i for i, j in enumerate(jobs) if len(j["keywords"]) <= 8 and share(*_parts(j, cap)["cov"]) >= 0.9]
    big = [i for i, j in enumerate(jobs) if len(j["keywords"]) >= 25 and _parts(j, cap)["n_covered"] >= 15]
    p0 = st.median(share(*_parts(j, cap)["cov"]) for j in jobs)
    base = [j["fit"] for j in jobs]
    base_apply = {j["job_id"] for j in jobs if j["fit"] * j["success_prob"] >= j["threshold"]}

    def report(label, f):
        order = sorted(f)
        pct = st.median(100 * order.index(f[i]) / len(order) for i in applied) if applied else float("nan")
        s = st.median(f[i] for i in small) if small else float("nan")
        b = st.median(f[i] for i in big) if big else float("nan")
        apply = {j["job_id"] for j, x in zip(jobs, f) if min(1.0, x * j["success_prob"]) >= j["threshold"]}
        print(f"{label:46} {pct:7.1f} {s:6.2f} {b:6.2f} {'big' if b > s else 'small':>6} "
              f"{st.median(f):5.2f} {_spearman(base, f):6.3f} {len(apply):5}")

    print(f"\n{'setting':46} {'applied%':>7} {'small':>6} {'big':>6} {'wins':>6} {'p50':>5} {'ρ':>6} {'apply':>5}")
    print(f"  (applied% = median rank of the {len(applied)} jobs you marked applied; small n={len(small)}, big n={len(big)})")
    report("A today", base)
    for a in (2, 3, 5, 8, 12):
        report(f"B shrink a={a} p0={p0:.2f}", [fit_with(j, shrink, weights, a=a, p0=p0) for j in jobs])
    for lam in (0.3, 0.5):
        report(f"blend λ={lam} cap {args.cap_at}, flat weights",
               [evidence_fit(j, weights, lam=lam, cap_at=args.cap_at) for j in jobs])
    for tool in (1.0, 1.25, 1.5, 2.0):
        for generic_w in (0.2, 0.3, 0.5, 1.0):
            for nice in (0.3, 0.5, 0.7):
                for emph in (0.0, 0.25, 0.5):
                    if (tool, generic_w, nice, emph) != (1.0, 1.0, 0.5, 0.0) and not args.full and \
                            sum(x != d for x, d in zip((tool, generic_w, nice, emph), (1.5, 0.3, 0.5, 0.25))) > 1:
                        continue  # one knob at a time around the proposal, unless --full
                    rj = [reweight(j, info[j["job_id"]], tool=tool, generic=generic_w, nice=nice, emph=emph) for j in jobs]
                    report(f"W t={tool} g={generic_w} n={nice} e={emph} +blend λ={args.lam}",
                           [evidence_fit(j, weights, lam=args.lam, cap_at=args.cap_at * 1.0) for j in rj])
    return 0


# --- impact: what moves when the formula changes -------------------------------------------

def _equivalent(old: list[float], new: list[float], cut: float) -> float:
    """The value on the new scale that passes as many jobs as ``cut`` does on the old."""
    n = sum(x >= cut for x in old)
    if n == 0:
        return max(new) + 0.01
    return sorted(new, reverse=True)[n - 1]


def impact(args) -> int:
    from src.config import settings

    old = {j["job_id"]: j for j in json.loads(args.old.read_text())["jobs"]}
    new = {j["job_id"]: j for j in json.loads(args.new.read_text())["jobs"]}
    ids = [i for i in old if i in new]
    o = [old[i] for i in ids]
    w = [new[i] for i in ids]
    print(f"{len(ids)} jobs scored both ways\n")

    def dist(name, xs, ys):
        qx, qy = st.quantiles(xs, n=20), st.quantiles(ys, n=20)
        print(f"{name:12} old p25 {qx[4]:.2f} p50 {qx[9]:.2f} p75 {qx[14]:.2f} p90 {qx[17]:.2f} max {max(xs):.2f}   "
              f"new p25 {qy[4]:.2f} p50 {qy[9]:.2f} p75 {qy[14]:.2f} p90 {qy[17]:.2f} max {max(ys):.2f}   "
              f"rank ρ {_spearman(xs, ys):.3f}")

    dist("fit", [j["fit"] for j in o], [j["fit"] for j in w])
    dist("final", [j["final"] for j in o], [j["final"] for j in w])
    if "dict_final" in o[0]:
        dist("dict final", [j["dict_final"] for j in o], [j["dict_final"] for j in w])

    print("\napply thresholds (final >= cut):")
    groups = {
        "scoring.apply_threshold (LinkedIn/Naukri)": (lambda j: not j["site"].startswith("ats:"),
                                                       float(settings.scoring.apply_threshold)),
        "ats_poll.apply_threshold (ATS)": (lambda j: j["site"].startswith("ats:"),
                                           float(settings.ats_poll.apply_threshold)),
    }
    for name, (keep, cut) in groups.items():
        xs = [(a, b) for a, b in zip(o, w) if keep(a)]
        if not xs:
            continue
        of, nf = [a["final"] for a, _ in xs], [b["final"] for _, b in xs]
        eq = _equivalent(of, nf, cut)
        old_set = {a["job_id"] for a, _ in xs if a["final"] >= cut}
        same = {b["job_id"] for _, b in xs if b["final"] >= cut}
        eq_set = {b["job_id"] for _, b in xs if b["final"] >= eq}
        print(f"  {name}: {cut:.2f} (jobs {len(xs)})")
        print(f"    old formula at {cut:.2f}: {len(old_set)} match")
        print(f"    new formula at {cut:.2f}: {len(same)} match ({len(same - old_set)} new, {len(old_set - same)} lost)")
        print(f"    new formula at {eq:.3f} (same count): {len(eq_set)} match "
              f"({len(eq_set - old_set)} new, {len(old_set - eq_set)} lost)")
        for c in (cut - 0.10, cut - 0.05, cut + 0.05):
            print(f"      at {c:.2f}: {sum(x >= c for x in nf)} match")

    best = float(settings.filters.best_realistic_fit)
    nf = [j["fit"] for j in w]
    print(f"\nfilters.best_realistic_fit {best:.2f} (the best fit a job could reach, for the applicant skip):")
    print(f"  old max fit {max(j['fit'] for j in o):.3f}; new max fit {max(nf):.3f}, p99 {st.quantiles(nf, n=100)[98]:.3f}")

    if "dict_final" in o[0]:
        r = settings.scoring.parse_routing
        print("\nparse routing (#100 floor, #71 bands) on the dictionary score:")
        for name in ("floor", "skip_below", "trust_above"):
            cut = float(r.get(name))
            od, nd = [j["dict_final"] for j in o], [j["dict_final"] for j in w]
            eq = _equivalent(od, nd, cut)
            print(f"  {name} {cut:.2f}: old {sum(x < cut for x in od)} jobs below; "
                  f"new {sum(x < cut for x in nd)} below at {cut:.2f}; same count at {eq:.3f}")
        # The floor must not skip jobs the LLM parse would match.
        for label, js in (("old", o), ("new", w)):
            applies = [j for j in js if j["final"] >= j["threshold"]]
            low = sorted(j["dict_final"] for j in applies)[:3]
            print(f"  {label}: {len(applies)} LLM matches; lowest dictionary scores among them {[round(x, 3) for x in low]}")

    changed = sum(a["entries"] != b["entries"] for a, b in zip(o, w))
    lead = sum(a["lead_covered"] != b["lead_covered"] for a, b in zip(o, w))
    print(f"\nresume content: {changed} of {len(ids)} jobs select different bullets "
          f"(the weights change what the beam search prefers); lead entry differs on {lead}")

    moved = sorted(zip(o, w), key=lambda p: p[1]["fit"] - p[0]["fit"])
    print("\nbiggest fit drops:")
    for a, b in moved[:6]:
        print(f"  {a['fit']:.2f} -> {b['fit']:.2f}  {len(a['keywords']):3} skills  {a['company'][:24]:24} {a['role'][:50]}")
    print("biggest fit gains:")
    for a, b in moved[-6:][::-1]:
        print(f"  {a['fit']:.2f} -> {b['fit']:.2f}  {len(a['keywords']):3} skills  {a['company'][:24]:24} {a['role'][:50]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("--sample", type=int, default=0)
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--formula", choices=("share", "weighted_count"), help="override scoring.fit.formula")
    s.add_argument("--dict", action="store_true", help="also score the dictionary parse (#100 floor)")
    c = sub.add_parser("compare")
    w = sub.add_parser("sweep")
    w.add_argument("--cap-at", type=float, default=25)
    w.add_argument("--lam", type=float, default=0.5)
    w.add_argument("--full", action="store_true", help="every weight combination, not one knob at a time")
    w.add_argument("--show-types", action="store_true")
    for p in (s, c, w):
        p.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    i = sub.add_parser("impact", help="old vs new formula: thresholds, routing, resumes")
    i.add_argument("--old", type=Path, required=True)
    i.add_argument("--new", type=Path, required=True)
    args = ap.parse_args()
    return {"score": score, "compare": compare, "sweep": sweep, "impact": impact}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
