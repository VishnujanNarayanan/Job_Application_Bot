"""CLI — build and inspect the technology vocabulary Layer 3 scans JDs against.

    python -m src.cli.vocabulary --rebuild
    python -m src.cli.vocabulary --list
    python -m src.cli.vocabulary --retire "communication skills"

The vocabulary is learned from the corpus of already-parsed ads: terms the
parser has emitted across many unrelated listings are real technologies, since
a hallucination does not recur in five different jobs. Rebuild after a run has
added jobs; nothing rebuilds it automatically, so the set Layer 3 uses only
changes when someone asks for it.
"""

from __future__ import annotations

import argparse
import logging
import sys

import structlog
from sqlalchemy import select, update

from src.state.db import session_scope
from src.state.models import SkillVocabulary
from src.state.vocabulary import rebuild


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
    structlog.configure(
        processors=[structlog.dev.ConsoleRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    ap = argparse.ArgumentParser(description="Build or inspect the skill vocabulary.")
    ap.add_argument("--rebuild", action="store_true", help="recompute from all parsed jobs")
    ap.add_argument("--min-jobs", type=int, default=None,
                    help="override the recurrence floor for this rebuild")
    ap.add_argument("--list", action="store_true", help="print the active vocabulary")
    ap.add_argument("--retire", metavar="TERM",
                    help="deactivate a term; a rebuild will not resurrect it")
    args = ap.parse_args(argv)

    if not (args.rebuild or args.list or args.retire):
        ap.error("nothing to do — pass --rebuild, --list or --retire")

    with session_scope() as session:
        if args.rebuild:
            count = rebuild(session, min_jobs=args.min_jobs)
            print(f"vocabulary rebuilt: {count} terms")

        if args.retire:
            result = session.execute(
                update(SkillVocabulary)
                .where(SkillVocabulary.term_key == args.retire.casefold())
                .values(is_active=False)
            )
            print(f"retired {args.retire!r}" if result.rowcount
                  else f"{args.retire!r} is not in the vocabulary")

        if args.list:
            rows = session.execute(
                select(SkillVocabulary.term, SkillVocabulary.job_count)
                .where(SkillVocabulary.is_active.is_(True))
                .order_by(SkillVocabulary.job_count.desc())
            ).all()
            print(f"\n{len(rows)} active terms (jobs the term was extracted from):\n")
            for term, count in rows:
                print(f"  {count:5}  {term}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
