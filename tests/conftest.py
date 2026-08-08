"""Pytest configuration for the job application bot.

All external services (Gemini, Neon, Telegram, AWS) are mocked
in unit tests. Real services are only touched in integration tests run
manually outside CI.
"""

import logging
from pathlib import Path

import pytest
import structlog


REPO_ROOT = Path(__file__).resolve().parent.parent


def _configure_test_logging() -> None:
    """Mirror src/main.py's logging config for the whole test session.

    Left unconfigured, structlog falls back to its *development* renderer,
    whose rich-powered exception formatter pretty-prints every frame local.
    Tests pass MagicMock clients into code that logs ``exc_info``, and rich
    recurses into a MagicMock's infinitely auto-generated attributes — which
    hung tests/test_v2_llm_retry.py indefinitely rather than failing.

    Production never had the bug (src/main.py installs JSONRenderer), so the
    fix belongs here: give tests the same non-rich rendering the real run uses.
    """
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )


_configure_test_logging()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repo root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_path(repo_root: Path) -> Path:
    """Absolute path to config/config.yaml."""
    return repo_root / "config" / "config.yaml"
