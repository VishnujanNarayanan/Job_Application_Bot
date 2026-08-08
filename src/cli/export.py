"""CLI — Layer 9: regenerate the local CSV index from the database.

    python -m src.cli.export

The index files are a projection of Postgres, not an append-log, so this is
safe to run any number of times — two consecutive exports produce identical
files. Run it after a pipeline run that happened elsewhere (a GitHub Actions
run triggered from the phone) to pull those results into the local CSVs; the
dashboard also calls it on load, so this is mainly for terminal use.

Best-effort: a failed export logs and exits 0 (it must not look like a
pipeline failure).
"""

import logging
import sys

import structlog

from src.analytics import export_index


def main() -> int:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )

    from src.state.db import session_scope

    with session_scope() as session:
        counts = export_index(session)

    for name, count in sorted(counts.items()):
        print(f"{name}: {count} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
