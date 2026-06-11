"""Tests for src/aws/cloudwatch.py — CloudWatch handler factory.

All tests use moto to mock AWS; no real CloudWatch calls are made.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from src.aws.iam_session import get_session


LOG_GROUP = "/job-bot/test"


@pytest.fixture(autouse=True)
def _isolate_session(monkeypatch):
    """Clear the lru_cache before and after each test so moto's fake
    credentials are picked up rather than a cached real session."""
    get_session.cache_clear()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    yield
    get_session.cache_clear()


# ---------------------------------------------------------------------------
# 1. Absent env var → None
# ---------------------------------------------------------------------------

def test_returns_none_when_env_var_absent(monkeypatch):
    monkeypatch.delenv("AWS_CLOUDWATCH_LOG_GROUP", raising=False)

    from src.aws.cloudwatch import build_handler
    assert build_handler() is None


# ---------------------------------------------------------------------------
# 2. Configured correctly → returns CloudWatchLogHandler
# ---------------------------------------------------------------------------

@mock_aws
def test_returns_handler_when_configured(monkeypatch):
    monkeypatch.setenv("AWS_CLOUDWATCH_LOG_GROUP", LOG_GROUP)

    # Pre-create log group (mirrors production — the group must already exist)
    get_session().client("logs").create_log_group(logGroupName=LOG_GROUP)

    import watchtower
    from src.aws.cloudwatch import build_handler

    handler = build_handler()

    assert handler is not None
    assert isinstance(handler, watchtower.CloudWatchLogHandler)
    assert handler.log_group_name == LOG_GROUP
    expected_stream = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert handler.log_stream_name == expected_stream


# ---------------------------------------------------------------------------
# 3. Session error → None, no exception raised
# ---------------------------------------------------------------------------

def test_skips_gracefully_on_session_error(monkeypatch):
    monkeypatch.setenv("AWS_CLOUDWATCH_LOG_GROUP", LOG_GROUP)

    with patch("src.aws.cloudwatch.get_session", side_effect=RuntimeError("no creds")):
        from src.aws.cloudwatch import build_handler
        result = build_handler()

    assert result is None


# ---------------------------------------------------------------------------
# 4. Round-trip: a JSON log record appears in the CloudWatch stream
# ---------------------------------------------------------------------------

@mock_aws
def test_handler_ships_structlog_json(monkeypatch):
    """A JSON record emitted via Python logging appears in the CW stream.

    Uses use_queues=False so the send is synchronous and the assertion
    can run immediately. This mirrors how structlog ships JSON strings
    through the Python logging system to watchtower.
    """
    import watchtower

    monkeypatch.setenv("AWS_CLOUDWATCH_LOG_GROUP", LOG_GROUP)

    client = get_session().client("logs")
    client.create_log_group(logGroupName=LOG_GROUP)

    stream_name = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    handler = watchtower.CloudWatchLogHandler(
        log_group_name=LOG_GROUP,
        log_stream_name=stream_name,
        boto3_client=client,
        create_log_group=False,
        create_log_stream=True,
        use_queues=False,  # synchronous — avoids background-thread timing issues
    )
    handler.setLevel(logging.INFO)

    root_logger = logging.getLogger()
    original_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    try:
        # Emit the same JSON string that structlog's JSONRenderer produces
        payload = json.dumps({
            "event": "cloudwatch_test_event",
            "level": "info",
            "timestamp": "2026-01-01T00:00:00Z",
        })
        logging.getLogger("test_cw").info(payload)

        resp = client.get_log_events(
            logGroupName=LOG_GROUP,
            logStreamName=stream_name,
        )
        events = resp.get("events", [])
        assert events, "No log events found in CloudWatch stream"

        messages = [e["message"] for e in events]
        matching = [m for m in messages if "cloudwatch_test_event" in m]
        assert matching, f"Test event not found. Messages: {messages}"

        parsed = json.loads(matching[0])
        assert parsed.get("event") == "cloudwatch_test_event"
        assert parsed.get("level") == "info"
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(original_level)
        handler.close()
