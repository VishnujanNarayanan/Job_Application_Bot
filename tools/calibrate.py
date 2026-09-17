"""Stage 6 — run Layer 4 selection over every parsed job and dump the distributions.

Read-only against the database. Nothing is written except the JSON report.

WHY THIS EXISTS
---------------
Every threshold in ``config.yaml`` is a percentile of a distribution that only
exists once selection has been run over a real corpus:

    scoring.apply_threshold          a percentile of `final_score`
    selection.{work,freelance,project}.threshold   percentiles of `entry.score`
    selection.work.match_then_recency_gap          a percentile of the best-second gap

Those numbers are therefore STALE the moment anything upstream of them moves --
the bullet pool, the selection rules, the caps, the matcher. They were last
measured on 2026-09-01 against the pre-v3 flat pool, and since then the profile
was rebuilt in role-block shape, phase 2 was added, repetition became a rule,
and the greedy walk became a beam search. Re-run this before trusting any of
them again.

USAGE
-----
    python -m tools.calibrate                      # writes data/reports/calibration.json
    python -m tools.calibrate --out /tmp/cal.json
    python -m tools.calibrate --limit 100          # quick smoke run

Interpreting the output: pick a threshold from the percentile you want to
APPLY to. `apply_threshold` at p75 of `final_score` means roughly one job in
four produces a notification. Nothing here chooses for you -- it reports the
shape and leaves the judgement where it belongs.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import select

from src.llm.schemas import JDParsed
from src.scorer.apply_decision import evaluate
from src.scorer.keywords import jd_keywords
from src.scorer.selector import build_jd_context, score_entry
from src.state.db import session_scope
from src.state.master_profile import load_profile
from src.state.models import AllJobs

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_OUT = _ROOT / "data" / "reports" / "calibration.json"


def _pct(xs: list[float], *ps: int) -> dict[int, float]:
    xs = sorted(xs)
    return {
        p: xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] for p in ps
    }


def _show(name: str, xs: list[float], ps=(5, 25, 50, 75, 90, 95, 99)) -> None:
    if not xs:
        print(f"{name:<24} (empty)")
        return
    q = _pct(xs, *ps)
    print(
        f"{name:<24} n={len(xs):<6} min={min(xs):.3f} max={max(xs):.3f} "
        f"mean={st.mean(xs):.3f}  " + "  ".join(f"p{p}={q[p]:.3f}" for p in ps)
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=0, help="score only the first N jobs")
    args = ap.parse_args()

    # The scorer logs per-job at INFO; over several hundred jobs that is noise.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))

    with session_scope() as session:
        # The canonical loader, not a hand-built profile: this script used to
        # reconstruct EntryCands itself because migration 0009 had not been
        # applied when it was first written. It has, so anything the loader does
        # -- is_extra, is_summary, block ids, skipping inactive rows -- is done
        # once, here, the same way the live pipeline does it.
        profile = load_profile(session)
        rows = session.execute(
            select(
                AllJobs.job_id, AllJobs.company, AllJobs.role, AllJobs.role_summary,
                AllJobs.role_category, AllJobs.role_level, AllJobs.years_required,
                AllJobs.required_skills, AllJobs.nice_to_have, AllJobs.responsibilities,
                AllJobs.posted_at, AllJobs.scraped_at,
            ).where(AllJobs.role_summary.isnot(None))
        ).all()

    if args.limit:
        rows = rows[: args.limit]

    employment = [e for e in profile.work if e.employment_type != "freelance"]
    freelance = [e for e in profile.work if e.employment_type == "freelance"]
    print(
        f"profile: {len(employment)} employment, {len(freelance)} freelance, "
        f"{len(profile.projects)} projects"
    )
    print(f"scoring {len(rows)} parsed ads ...")

    now = datetime.now(timezone.utc)
    E: list[float] = []
    F: list[float] = []
    P: list[float] = []
    COV: list[float] = []
    LEAD: list[float] = []
    FINAL: list[float] = []
    FIT: list[float] = []
    GAPS: list[float] = []
    BULLETS: list[int] = []
    ENTRIES: list[int] = []
    per_job: list[dict] = []

    for i, r in enumerate(rows):
        if i and i % 100 == 0:
            print(f"  {i}/{len(rows)}")
        parsed = JDParsed(
            role_summary=r.role_summary,
            role_category=r.role_category or "ml",
            role_level=r.role_level or "mid",
            years_required=r.years_required or 0,
            required_skills=list(r.required_skills or []),
            nice_to_have=list(r.nice_to_have or []),
            responsibilities=list(r.responsibilities or []),
        )
        jd = build_jd_context(parsed, posted_at=r.posted_at, scraped_at=r.scraped_at)
        kws = jd_keywords(parsed)

        E += [score_entry(e, jd, kws, now=now).score for e in employment]
        F += [score_entry(e, jd, kws, now=now).score for e in freelance]
        P += [score_entry(e, jd, kws, now=now).score for e in profile.projects]

        res = evaluate(profile, jd, keywords=kws, now=now)
        COV.append(res.keyword_coverage)
        LEAD.append(res.lead_entry_coverage)
        FINAL.append(res.final_score)
        FIT.append(res.fit)
        ENTRIES.append(len(res.entries))
        BULLETS.append(sum(len(e.bullets) for e in res.entries))
        ranked = sorted((s.score for s in res.entries), reverse=True)
        if len(ranked) > 1:
            GAPS.append(ranked[0] - ranked[1])
        per_job.append({
            "job_id": r.job_id, "company": r.company, "role": r.role,
            "final": res.final_score, "cov": res.keyword_coverage,
            "lead_cov": res.lead_entry_coverage, "n_entries": len(res.entries),
            "n_bullets": sum(len(e.bullets) for e in res.entries),
        })

    print()
    _show("entry.score employment", E)
    _show("entry.score freelance", F)
    _show("entry.score project", P)
    _show("keyword_coverage", COV)
    _show("lead_entry_coverage", LEAD)
    _show("fit", FIT)
    _show("final_score", FINAL)
    _show("best-second gap", GAPS)
    _show("entries per resume", [float(x) for x in ENTRIES])
    _show("bullets per resume", [float(x) for x in BULLETS])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "generated_at": now.isoformat(),
        "jobs": len(rows),
        "employment": E, "freelance": F, "project": P,
        "coverage": COV, "lead": LEAD, "fit": FIT, "final": FINAL,
        "gaps": GAPS, "entries": ENTRIES, "bullets": BULLETS,
        "per_job": per_job,
    }, indent=1))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
