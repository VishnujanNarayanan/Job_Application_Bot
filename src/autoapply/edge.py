"""The real Microsoft Edge on Windows, driven over its DevTools port.

The bot never launches a test browser: it starts the operator's installed Edge
with a profile of its own ("Job Bot", logged into LinkedIn once by hand) and a
DevTools port, then attaches Playwright to it. To LinkedIn that is an ordinary
Edge on this laptop, on this home connection: its own TLS handshake and
fingerprint, no automation flags. Edge keeps running between runs.

WSL reaches Windows' localhost with mirrored networking
(``networkingMode=mirrored`` in %USERPROFILE%\\.wslconfig), but Edge's own
DevTools socket never completes a handshake with WSL (it stays SYN_RECEIVED),
while other Windows programs do. So a tiny relay runs on Windows, as the
user, hidden: WSL connects to it (``cdp_relay_port``) and it passes the bytes
to Edge's port locally. The bot starts it when it isn't running.
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


RELAY_PS1 = r"""# Job Bot: lets WSL reach the Job Bot Edge's DevTools port (see src/autoapply/edge.py).
param([int]$Listen, [int]$Target)
$l = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Listen)
$l.Start()
while ($true) {
  $c = $l.AcceptTcpClient()
  try { $u = [System.Net.Sockets.TcpClient]::new('127.0.0.1', $Target) } catch { $c.Close(); continue }
  $a = $c.GetStream(); $b = $u.GetStream()
  $null = $a.CopyToAsync($b); $null = $b.CopyToAsync($a)
}
"""
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def _relay_port() -> int:
    return int(_cfg().get("cdp_relay_port", 9224))


def devtools_url(port: int | None = None) -> str:
    """Where the bot talks to Edge: the relay's port."""
    return f"http://127.0.0.1:{port or _relay_port()}"


def _relay_listening() -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", _relay_port())) == 0


def ensure_relay(wait_s: float = 15.0) -> None:
    """Start the Windows-side relay (hidden, as the user) if it isn't listening."""
    from pathlib import Path

    if _relay_listening():
        return
    script_win = str(PureWindowsPath(str(_cfg().edge_profile)).parent / "devtools-relay.ps1")
    script = Path(wsl_path(script_win))
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(RELAY_PS1.replace("\n", "\r\n"), encoding="utf-8")
    subprocess.Popen(
        [POWERSHELL, "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", script_win,
         "-Listen", str(_relay_port()), "-Target", str(int(_cfg().cdp_port))],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _relay_listening():
            return
        time.sleep(0.5)
    raise RuntimeError("the DevTools relay on Windows did not start")


def is_running(port: int | None = None) -> bool:
    try:
        with urllib.request.urlopen(devtools_url(port) + "/json/version", timeout=2) as r:
            return "Browser" in json.loads(r.read())
    except Exception:  # noqa: BLE001 - not up (or not reachable)
        return False


def ensure_edge(start_url: str = "about:blank", wait_s: float = 25.0) -> None:
    """Start the Job Bot Edge if its DevTools port isn't answering yet."""
    cfg = _cfg()
    ensure_relay()
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
        "Edge's DevTools port did not answer through the relay. Is another Edge already using the "
        "Job Bot profile without the port? Close the Job Bot Edge window and run again.")


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
