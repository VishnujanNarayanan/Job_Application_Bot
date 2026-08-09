"""CLI — recompute stored JD embeddings.

    python -m src.cli.reembed            # only rows that need it
    python -m src.cli.reembed --all      # every row
    python -m src.cli.reembed --dry-run  # report, change nothing

Exists because how a JD is embedded changed on 2026-08-09. It used to be one
``encode()`` of the raw text, which all-MiniLM-L6-v2 silently truncates at 256
word-pieces — roughly the first 1,200 characters of a 4,400-character ad. It
is now a length-weighted mean over overlapping chunks, so the whole document
is represented.

The two are not comparable. Near-duplicate detection asks whether a new job's
vector is within 0.95 cosine of a stored one, and mixing an opening-only
vector with a whole-document vector makes that number meaningless — jobs would
be deduped, or not, for reasons unrelated to their content. Rewriting the
stored vectors keeps every comparison like-for-like.

Cost is time, not money: the model runs locally.
"""

from __future__ import annotations

import argparse
import logging
import sys

import structlog

log = structlog.get_logger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def reembed(batch_size: int = 32, dry_run: bool = False) -> dict[str, int]:
    """Recompute every stored JD embedding. Returns a summary."""
    from sqlalchemy import select

    from src.scorer.embeddings import embed_document
    from src.state.db import session_scope
    from src.state.models import AllJobs

    counts = {"scanned": 0, "rewritten": 0, "skipped_no_text": 0}

    with session_scope() as session:
        rows = list(
            session.execute(
                select(AllJobs).where(AllJobs.jd_text.isnot(None))
            ).scalars()
        )
        counts["scanned"] = len(rows)

        pending = []
        for row in rows:
            if not (row.jd_text or "").strip():
                counts["skipped_no_text"] += 1
                continue
            pending.append(row)

        for start in range(0, len(pending), batch_size):
            chunk = pending[start : start + batch_size]
            for row in chunk:
                if not dry_run:
                    row.jd_embedding = embed_document(row.jd_text)
                counts["rewritten"] += 1

            if not dry_run:
                session.add_all(chunk)
                session.flush()

            log.info(
                "reembed_progress",
                done=min(start + batch_size, len(pending)),
                total=len(pending),
            )

        if dry_run:
            session.rollback()
        else:
            session.commit()

    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.cli.reembed")
    parser.add_argument(
        "--dry-run", action="store_true", help="report without writing"
    )
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args(argv)

    _configure_logging()
    counts = reembed(batch_size=args.batch_size, dry_run=args.dry_run)

    print(
        f"{'would rewrite' if args.dry_run else 'rewrote'} "
        f"{counts['rewritten']} of {counts['scanned']} rows "
        f"({counts['skipped_no_text']} had no description text)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
