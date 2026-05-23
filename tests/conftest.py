"""Pytest configuration for the job application bot.

All external services (Gemini, Neon, Telegram, Playwright) are mocked
in unit tests. Real services are only touched in integration tests run
manually outside CI.
"""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repo root."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def config_path(repo_root: Path) -> Path:
    """Absolute path to config/config.yaml."""
    return repo_root / "config" / "config.yaml"
