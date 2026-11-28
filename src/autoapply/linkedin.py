"""Easy Apply on LinkedIn, in the attached Edge.

The form inside the Easy Apply dialog is filled by the same userscript the
operator uses by hand (src/endpoint/static/jab.user.js), injected in its
auto-apply mode: it reads only the dialog and fills it from the endpoint's
answers, including the tailored resume. This module only moves through the
dialog: it waits for each step to be filled, refuses to go on when a field is
left unanswered (red) or LinkedIn shows an error, checks that the tailored PDF
is the selected resume, and presses Next / Review / Submit.

Nothing is guessed: an unanswered question discards the application and the
job is reported, so the answer can be added and the job retried by hand.
"""

from __future__ import annotations

import base64
import json
import random
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.config import settings

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "src" / "endpoint" / "static" / "jab.user.js"
ENDPOINT = "http://localhost:8000"
SHOTS = ROOT / "data" / "autoapply" / "screens"

MODAL = "div.jobs-easy-apply-modal, div[data-test-modal].jobs-easy-apply-modal, div[role='dialog'][aria-labelledby*='easy-apply'], div[role='dialog']"
PRIMARY = re.compile(r"^(submit application|review( your application)?|continue to next step|next)$", re.I)

GM_SHIM = r"""(() => {
  window.GM_registerMenuCommand = () => {};
  window.__gm = window.__gm || {};
  window.GM_getValue = (k, d) => (k in window.__gm ? window.__gm[k] : d);
  window.GM_setValue = (k, v) => { window.__gm[k] = v; };
  window.GM_xmlhttpRequest = (o) => {
    window.__jabRequest({method: o.method, url: o.url, headers: o.headers || {}, data: o.data || null,
                         blob: o.responseType === 'blob'}).then((r) => {
      let response = r.text;
      if (r.b64 !== null) {
        const bin = atob(r.b64); const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        response = new Blob([bytes], {type: r.type || 'application/octet-stream'});
      }
      o.onload({status: r.status, responseText: r.text, response});
    }).catch((e) => o.onerror && o.onerror(e));
  };
})()"""


@dataclass
class Outcome:
    status: str            # applied | dry_run | not_easy_apply | already_applied | closed | skipped | login | error
    detail: str = ""
    screenshot: str = ""
    questions: list[str] = field(default_factory=list)


def _pause(lo: float = 0.7, hi: float = 1.8) -> None:
    """A person's pace between actions."""
    time.sleep(random.uniform(lo, hi))


def _forward(page_request: dict) -> dict:
    """The userscript's GM_xmlhttpRequest, done here (no CORS, no browser cookies)."""
    req = urllib.request.Request(page_request["url"], data=(page_request.get("data") or "").encode() or None,
                                 method=page_request.get("method") or "GET",
                                 headers=page_request.get("headers") or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body, status, ctype = r.read(), r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        body, status, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
    if page_request.get("blob"):
        return {"status": status, "text": "", "b64": base64.b64encode(body).decode(), "type": ctype}
    return {"status": status, "text": body.decode("utf-8", "replace"), "b64": None, "type": ctype}


def page_state(page) -> str:
    """What the job page offers: easy | external | applied | closed | login | unknown."""
    url = page.url
    if re.search(r"/(login|authwall|checkpoint|uas/login)", url):
        return "login"
    text = page.evaluate("() => (document.querySelector('main') || document.body).innerText.slice(0, 6000)")
    if re.search(r"no longer accepting applications", text, re.I):
        return "closed"
    buttons = page.evaluate("""() => [...document.querySelectorAll('button, a')].filter(b => b.offsetWidth || b.offsetHeight)
        .map(b => ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).trim()).slice(0, 400)""")
    if any(re.search(r"\beasy apply\b", b, re.I) for b in buttons):
        return "easy"
    if re.search(r"\bapplied\b.{0,40}\bago\b|application submitted|you applied", text, re.I):
        return "applied"
    if any(re.search(r"^apply\b|apply on company", b.strip(), re.I) for b in buttons):
        return "external"
    return "unknown"


def _modal(page):
    for sel in MODAL.split(", "):
        el = page.query_selector(sel)
        if el and el.is_visible():
            return el
    return None


def _inject(page, job_id: str) -> None:
    script = (SCRIPT.read_text(encoding="utf-8").replace("__JAB_ENDPOINT__", ENDPOINT)
              .replace("__JAB_HOST__", "localhost"))
    page.evaluate("(a) => { window.__jabAutoApply = a; window.__jabPasses = 0; }",
                  {"jobId": job_id, "scope": ".jobs-easy-apply-modal, [role='dialog']"})
    page.evaluate(GM_SHIM)
    page.evaluate(script)


def _wait_filled(page, before: int, limit_s: float = 30.0) -> None:
    """Until the userscript has finished at least one more pass and gone quiet."""
    deadline = time.monotonic() + limit_s
    seen = before
    quiet_since = None
    while time.monotonic() < deadline:
        passes = page.evaluate("() => window.__jabPasses || 0")
        if passes > seen:
            seen, quiet_since = passes, time.monotonic()
        elif quiet_since and time.monotonic() - quiet_since > 1.5:
            return
        time.sleep(0.25)


def _problems(modal) -> list[str]:
    """Questions left unanswered (red) and LinkedIn's own error messages."""
    return modal.evaluate("""(m) => {
        const out = [];
        for (const e of m.querySelectorAll('.jab-missing')) out.push((e.title || '').replace(/^Job Bot:\\s*/, '') + ' | ' +
            ((e.closest('[data-test-form-element], .jobs-easy-apply-form-element, fieldset, div') || e).innerText || '').trim().slice(0, 140));
        for (const e of m.querySelectorAll('.artdeco-inline-feedback--error, [role=alert]'))
            if ((e.offsetWidth || e.offsetHeight) && e.innerText.trim()) out.push('LinkedIn: ' + e.innerText.trim().slice(0, 140));
        return out; }""")


def _resume_ok(modal, filename: str) -> bool | None:
    """On a step with a resume section: is a freshly uploaded tailored PDF the selected one?
    None when this step has no resume section."""
    return modal.evaluate("""(m, name) => {
        const section = [...m.querySelectorAll('h3, h2, span, label')].find(e => /^\\s*resume\\s*$/i.test(e.innerText || ''));
        const cards = [...m.querySelectorAll('[class*="document-upload"][class*="card"], [class*="jobs-resume-picker__resume"], [data-test-document-upload-card]')];
        if (!section && !cards.length) return null;
        const selected = cards.filter(c => /selected|is-checked|--active/.test(c.className)
            || c.querySelector('input[type=radio]:checked, [aria-checked=true], [aria-selected=true]'));
        if (!selected.length) return false;
        const text = selected.map(c => c.innerText).join(' | ');
        const stem = name.replace(/\\.pdf$/i, '');
        // The tailored PDF, uploaded just now (not an older one with the same name).
        return text.includes(stem) && /(uploaded on|last used).{0,20}(\\d|today|just now)/i.test(text)
            && (/just now|today/i.test(text) || text.includes(new Date().toLocaleDateString('en-US')));
    }""", filename)


def _primary(modal):
    for b in modal.query_selector_all("button"):
        label = ((b.get_attribute("aria-label") or "") or (b.inner_text() or "")).strip()
        text = (b.inner_text() or "").strip()
        if b.is_visible() and b.is_enabled() and (PRIMARY.match(label) or PRIMARY.match(text)):
            return b, (label or text)
    return None, ""


def _discard(page) -> None:
    modal = _modal(page)
    if not modal:
        return
    close = modal.query_selector("button[aria-label='Dismiss'], button[data-test-modal-close-btn]")
    if close:
        close.click()
        _pause(0.6, 1.2)
    for sel in ("button[data-control-name='discard_application_confirm_btn']",
                "button[data-test-dialog-secondary-btn]", "button:has-text('Discard')"):
        b = page.query_selector(sel)
        if b and b.is_visible():
            b.click()
            break


def _shot(page, job_id: str, tag: str) -> str:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9-]+', '-', job_id.lower())}-{tag}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception:  # noqa: BLE001
        return ""


def easy_apply(page, job_id: str, job_url: str, *, dry_run: bool, log) -> Outcome:
    """Open the job and apply with Easy Apply. Never guesses; discards on doubt."""
    page.goto(job_url, wait_until="domcontentloaded", timeout=60000)
    _pause(2.0, 4.0)
    state = page_state(page)
    if state == "applied":
        return Outcome("already_applied")
    if state != "easy":
        return Outcome(state if state in ("login", "closed") else "not_easy_apply",
                       "apply happens on the company site" if state == "external" else "")
    try:
        page.expose_function("__jabRequest", _forward)
    except Exception:  # noqa: BLE001 - already exposed on this page
        pass
    button = page.locator("button:has-text('Easy Apply'), button[aria-label*='Easy Apply']").first
    button.click()
    page.wait_for_selector(".jobs-easy-apply-modal, [role='dialog']", timeout=15000)
    _pause()
    _inject(page, job_id)
    filename = settings.resume_filename
    for step in range(15):
        modal = _modal(page)
        if modal is None:
            return Outcome("error", "the Easy Apply dialog closed unexpectedly", _shot(page, job_id, "closed"))
        before = page.evaluate("() => window.__jabPasses || 0")
        _wait_filled(page, before if step else -1)
        _pause(0.5, 1.2)
        problems = _problems(modal)
        if problems:
            shot = _shot(page, job_id, "unanswered")
            _discard(page)
            return Outcome("skipped", "a question has no stored answer", shot, problems)
        resume = _resume_ok(modal, filename)
        if resume is False:
            shot = _shot(page, job_id, "resume")
            _discard(page)
            return Outcome("skipped", "could not confirm the tailored resume is the selected one", shot)
        btn, label = _primary(modal)
        if btn is None:
            shot = _shot(page, job_id, "stuck")
            _discard(page)
            return Outcome("error", "no Next / Review / Submit button found", shot)
        if re.match(r"submit application", label, re.I):
            if dry_run:
                shot = _shot(page, job_id, "ready-to-submit")
                _discard(page)
                return Outcome("dry_run", "filled; stopped before Submit", shot)
            btn.click()
            for _ in range(30):
                time.sleep(0.5)
                done = page.evaluate("() => /application (was )?sent|applied/i.test(document.body.innerText.slice(0, 20000))")
                if done:
                    shot = _shot(page, job_id, "sent")
                    for sel in ("button[aria-label='Dismiss']", "button:has-text('Done')"):
                        b = page.query_selector(sel)
                        if b and b.is_visible():
                            b.click()
                            break
                    return Outcome("applied", "Easy Apply submitted", shot)
            return Outcome("error", "pressed Submit but saw no confirmation", _shot(page, job_id, "no-confirm"))
        log.info("autoapply_step", job_id=job_id, step=step, button=label)
        btn.click()
        _pause(1.2, 2.4)
    shot = _shot(page, job_id, "too-many-steps")
    _discard(page)
    return Outcome("error", "more than 15 steps", shot)
