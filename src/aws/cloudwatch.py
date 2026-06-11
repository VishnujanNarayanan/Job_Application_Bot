"""CloudWatch Logs handler factory (Layer 8 / §5.4).

Provides a single public function ``build_handler`` that returns a
watchtower ``CloudWatchLogHandler`` wired to the existing boto3 session,
or ``None`` if CloudWatch is not configured or unavailable.

The handler integrates with structlog transparently: structlog's
``JSONRenderer`` writes JSON strings into Python's root ``logging`` system;
adding this handler makes the root logger dispatch each record to both
stderr (existing) and CloudWatch. No re-serialisation occurs — CloudWatch
Logs Insights receives valid JSON directly.

IAM permissions required (hard rule #19):
  logs:CreateLogStream, logs:PutLogEvents  on the configured log group.
  logs:CreateLogGroup is intentionally NOT required — the group must
  pre-exist (created once via console/Terraform, verified by aws_check.py).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import structlog

from src.aws.iam_session import get_session

log = structlog.get_logger(__name__)


def build_handler() -> logging.Handler | None:
    """Return a CloudWatchLogHandler for the configured log group, or None.

    Returns None (never raises) when:
    - ``AWS_CLOUDWATCH_LOG_GROUP`` env var is absent or empty, or
    - any error occurs during handler construction (bad credentials,
      wrong region, missing log group, network issue).

    Stream name is ``YYYY-MM-DD`` (UTC) — one stream per day, easy to
    locate in the CloudWatch console.
    """
    log_group = os.environ.get("AWS_CLOUDWATCH_LOG_GROUP", "").strip()
    if not log_group:
        return None

    import watchtower

    stream_name = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        client = get_session().client("logs")
        handler = watchtower.CloudWatchLogHandler(
            log_group_name=log_group,
            log_stream_name=stream_name,
            boto3_client=client,
            create_log_group=False,
            create_log_stream=True,
        )
        handler.setLevel(logging.INFO)
        return handler
    except Exception as exc:
        log.warning("cloudwatch_handler_init_failed", error=str(exc))
        return None
