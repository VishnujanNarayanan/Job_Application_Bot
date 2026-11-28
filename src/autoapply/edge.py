"""The real Microsoft Edge on Windows, driven over its DevTools port.

The bot never launches a test browser: it starts the operator's installed Edge
with a profile of its own ("Job Bot", logged into LinkedIn once by hand) and a
DevTools port, then attaches Playwright to it. To LinkedIn that is an ordinary
Edge on this laptop, on this home connection: its own TLS handshake and
fingerprint, no automation flags. Edge keeps running between runs.

WSL reaches Edge's port on Windows' localhost only with mirrored networking
(``networkingMode=mirrored`` in %USERPROFILE%\\.wslconfig).
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from contextlib import contextmanager
from pathlib import PureWindowsPath
from typing import Iterator

from src.config import settings


def _cfg():
    return settings.autoapply


def wsl_path(windows_path: str) -> str:
    """C:\\Program Files\\x.exe -> /mnt/c/Program Files/x.exe"""
    p = PureWindowsPath(windows_path)
    drive = p.drive.rstrip(":").lower()
    return "/mnt/" + drive + "/" + "/".join(p.parts[1:])


def devtools_url(port: int | None = None) -> str:
    return f"http://localhost:{port or int(_cfg().cdp_port)}"


def is_running(port: int | None = None) -> bool:
    try:
        with urllib.request.urlopen(devtools_url(port) + "/json/version", timeout=2) as r:
            return "Browser" in json.loads(r.read())
    except Exception:  # noqa: BLE001 - not up (or not reachable)
        return False


def ensure_edge(start_url: str = "about:blank", wait_s: float = 25.0) -> None:
    """Start the Job Bot Edge if its DevTools port isn't answering yet."""
    cfg = _cfg()
    if is_running():
        return
    subprocess.Popen(
        [wsl_path(str(cfg.edge_exe)), f"--remote-debugging-port={int(cfg.cdp_port)}",
         f"--user-data-dir={cfg.edge_profile}", "--no-first-run", "--no-default-browser-check", start_url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if is_running():
            return
        time.sleep(0.5)
    raise RuntimeError(
        "Edge's DevTools port did not answer. Is mirrored networking on (wsl --shutdown after "
        "editing .wslconfig)? Is another Edge already using the Job Bot profile without the port?")


@contextmanager
def attached() -> Iterator[object]:
    """Playwright attached to the running Job Bot Edge: yields its browser context.

    Only detaches at the end: Edge, its windows and the LinkedIn login stay.
    """
    from playwright.sync_api import sync_playwright

    ensure_edge()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(devtools_url())
        try:
            yield browser.contexts[0] if browser.contexts else browser.new_context()
        finally:
            browser.close()  # over CDP this disconnects; the Edge process keeps running
