"""Before/after for date-only posting times (#180).

For recent ATS jobs whose source gave only a date (ats_postings.posted_at at
exactly midnight), recompute the recency multiplier with the effective
posted time (first-seen, for a same-day date) and list what changes:

- scored jobs (applied / not_applied): final score, old -> new, and every
  verdict flip across the ATS apply bar;
- jobs skipped before scoring by age (POSTING_TOO_OLD) or because a perfect
  fit could not reach the bar: whether they would now be scored.

final = fit x success multiplier x junior boost, and the success multiplier
of an ATS job is recency_multiplier(posted_at, scored_at), so the new final is
the old one x m(new age) / m(old age). Read-only.

    python -m tools.date_only_replay [--days 7]
"""

from __future__ import annotations

import argparse
from datetime import timedelta, timezone

from sqlalchemy import text

from src.config import settings
from src.scorer.apply_decision import recency_multiplier
from src.scraper import ats
from src.state.db import session_scope

SQL = text("""
SELECT j.job_id, j.site, j.company, j.role, p.posted_at, p.first_seen_at,
       a.final_score AS a_final, a.built_at,
       n.final_score AS n_final, n.not_applied_at, n.reason_category
FROM all_jobs j
JOIN ats_postings p ON p.job_id = j.job_id
LEFT JOIN applied a ON a.job_id = j.job_id
LEFT JOIN not_applied n ON n.job_id = j.job_id
WHERE j.site LIKE 'ats:%'
  AND p.first_seen_at >= :since
  AND p.posted_at IS NOT NULL
  AND p.posted_at = date_trunc('day', p.posted_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
ORDER BY p.first_seen_at
""")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    bar = float(settings.ats_poll.apply_threshold)
    with session_scope() as s:
        since = s.execute(text("SELECT now()")).scalar() - timedelta(days=args.days)
        rows = s.execute(SQL, {"since": since}).mappings().all()

    changed, flips, unblocked = 0, [], []
    for r in rows:
        posted = ats.PostedDate(r["posted_at"].year, r["posted_at"].month, r["posted_at"].day,
                                tzinfo=timezone.utc)
        scored_at = r["built_at"] or r["not_applied_at"] or r["first_seen_at"]
        new_posted = ats.effective_posted_at(posted, r["first_seen_at"], scored_at)
        if new_posted == posted:
            continue
        changed += 1
        old_m, new_m = recency_multiplier(posted, scored_at), recency_multiplier(new_posted, scored_at)
        final = r["a_final"] if r["a_final"] is not None else r["n_final"]
        label = f"{r['site']:<22} {r['company'][:24]:<24} {r['role'][:40]:<40}"
        age_old = (scored_at - posted).total_seconds() / 3600
        age_new = (scored_at - new_posted).total_seconds() / 3600
        if final is not None:
            new_final = final * new_m / old_m
            line = (f"{label} age {age_old:5.1f}h -> {age_new:4.1f}h  x{old_m:.2f} -> x{new_m:.2f}  "
                    f"final {final:.3f} -> {new_final:.3f}")
            if (final >= bar) != (new_final >= bar):
                flips.append(line + ("  NOW MATCHES" if new_final >= bar else "  NOW BELOW BAR"))
            print(line)
        elif r["reason_category"]:
            unblocked.append(f"{label} {r['reason_category']}: age {age_old:.1f}h -> {age_new:.1f}h")

    print(f"\n{len(rows)} date-only postings in {args.days} days; {changed} get a later effective time.")
    print(f"\nVerdict flips at the {bar} bar ({len(flips)}):")
    print("\n".join(flips) or "  none")
    print(f"\nSkipped before scoring, now younger ({len(unblocked)}); re-check whether they would be scored:")
    print("\n".join(unblocked) or "  none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
