"""CLI — Re-parse master_profile.yaml and rebuild DB embeddings."""

import logging
import sys

import structlog

from src.state.db import session_scope
from src.state.master_profile import rebuild


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
    log = structlog.get_logger()

    with session_scope() as session:
        report = rebuild(session, force=True)
        session.commit()

    log.info(
        "reparse_complete",
        bullets_inserted=report.bullets_inserted,
        bullets_updated=report.bullets_updated,
        bullets_deactivated=report.bullets_deactivated,
        summaries_inserted=report.summaries_inserted,
        summaries_updated=report.summaries_updated,
        summaries_deactivated=report.summaries_deactivated,
        aliases_inserted=report.aliases_inserted,
        aliases_deactivated=report.aliases_deactivated,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
