"""Pipeline trigger for the dashboard — Layer 1, operator-facing.

Two ways to start a run, because they solve different problems:

  * **local** — ``python -m src.main`` as a subprocess, streaming its log
    lines back to the browser. Fast, no network, and you watch it work. Only
    possible while this process (and therefore the laptop) is running.
  * **github** — dispatch the ``pipeline`` workflow. Runs on GitHub's
    infrastructure, so it also works when the laptop is off; the same
    workflow the GitHub mobile app triggers. No live logs here — GitHub owns
    them.

Cross-process safety for local runs comes free from the ``fcntl`` lock on
``data/run.lock`` in ``src/main.py``: a second run exits immediately rather
than double-scraping. The in-process guard below is only so the UI can say
"already running" instead of silently spawning a process that gives up.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.config import settings

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]
_MAX_LOG_LINES = 500


@dataclass
class RunState:
    """Everything the dashboard needs to render the run panel."""

    running: bool = False
    target: str = "local"
    dry_run: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = None
    error: str | None = None
    log_lines: deque[str] = field(
        default_factory=lambda: deque(maxlen=_MAX_LOG_LINES)
    )

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "target": self.target,
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "returncode": self.returncode,
            "error": self.error,
            "log_lines": list(self.log_lines),
        }


_state = RunState()
_lock = threading.Lock()


def get_state() -> dict:
    with _lock:
        return _state.as_dict()


def _drain(process: subprocess.Popen) -> None:
    """Copy the child's output into the ring buffer until it exits.

    Runs on a daemon thread. The buffer is bounded, so a run that logs
    tens of thousands of lines can't grow memory without limit — the
    dashboard only ever shows the tail anyway.
    """
    try:
        assert process.stdout is not None
        for line in process.stdout:
            with _lock:
                _state.log_lines.append(line.rstrip("\n"))
        process.wait()
    except Exception as exc:  # pragma: no cover - defensive
        with _lock:
            _state.error = str(exc)
        log.error("run_drain_failed", error=str(exc), exc_info=True)
    finally:
        with _lock:
            _state.running = False
            _state.finished_at = datetime.now(timezone.utc)
            _state.returncode = process.returncode
        log.info("dashboard_run_finished", returncode=process.returncode)


def start_local_run(dry_run: bool = False) -> tuple[bool, str]:
    """Spawn the pipeline as a subprocess. Returns ``(started, message)``."""
    with _lock:
        if _state.running:
            return False, "A run is already in progress."

        _state.running = True
        _state.target = "local"
        _state.dry_run = dry_run
        _state.started_at = datetime.now(timezone.utc)
        _state.finished_at = None
        _state.returncode = None
        _state.error = None
        _state.log_lines.clear()

    cmd = [sys.executable, "-m", "src.main"] + (["--dry-run"] if dry_run else [])
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        with _lock:
            _state.running = False
            _state.error = str(exc)
        log.error("dashboard_run_failed", error=str(exc))
        return False, f"Could not start the pipeline: {exc}"

    threading.Thread(target=_drain, args=(process,), daemon=True).start()
    log.info("dashboard_run_started", dry_run=dry_run, pid=process.pid)
    return True, "Run started."


def dispatch_github_run(dry_run: bool = False) -> tuple[bool, str]:
    """Trigger the pipeline workflow on GitHub. Returns ``(dispatched, message)``.

    Used when the operator wants the run to survive closing the laptop. The
    token needs only ``actions: write`` on this repository.
    """
    import httpx

    repo = os.environ.get("GITHUB_REPO", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repo or not token:
        return False, (
            "Set GITHUB_REPO and GITHUB_TOKEN in .env to dispatch runs to "
            "GitHub Actions."
        )

    gh = settings.endpoint.dashboard.github
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{gh.workflow_file}/dispatches"
    )
    try:
        response = httpx.post(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "ref": str(gh.ref),
                "inputs": {"dry_run": "true" if dry_run else "false"},
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        log.error("github_dispatch_failed", error=str(exc))
        return False, f"Could not reach GitHub: {exc}"

    if response.status_code == 204:
        with _lock:
            _state.target = "github"
            _state.dry_run = dry_run
            _state.started_at = datetime.now(timezone.utc)
        log.info("github_dispatch_sent", repo=repo, dry_run=dry_run)
        return True, "Dispatched to GitHub Actions."

    log.error(
        "github_dispatch_rejected",
        status=response.status_code,
        body=response.text[:200],
    )
    return False, f"GitHub rejected the dispatch ({response.status_code})."
