"""CLI — Layer 9: write the monthly Gemini analytics report to a text file.

Run monthly (cron or manual):

    python -m src.cli.report

Writes ``data/reports/report-YYYY-MM.txt`` (path from config). Runs on the
laptop and reads Postgres, so the report covers every pipeline run regardless
of where it executed.

Best-effort: if the report is disabled in config or any step fails, it logs
and exits 0 (a failed report must not look like a pipeline failure).
"""

import logging
import sys

import structlog

from src.analytics import write_monthly_report


def main() -> int:
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,  # render exc_info → "exception" field
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )
    from src.aws.cloudwatch import build_handler as _cw_handler
    _handler = _cw_handler()
    if _handler:
        logging.getLogger().addHandler(_handler)

    write_monthly_report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
