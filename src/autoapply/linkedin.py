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

# LinkedIn's 2026 pages: obfuscated classes; the apply flow is a native <dialog>.
MODAL = "dialog[open], div.jobs-easy-apply-modal, div[role='dialog']"
SCOPE = "dialog[open], .jobs-easy-apply-modal, [role='dialog']"
EASY_BUTTON = re.compile(r"^\s*easy apply(\s+to\b.*)?\s*$", re.I)
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
    defaults: list[str] = field(default_factory=list)   # questions answered by the never-skip defaults


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
    if _easy_button(page) is not None:
        return "easy"
    buttons = page.evaluate("""() => [...document.querySelectorAll('button, a')].filter(b => b.offsetWidth || b.offsetHeight)
        .map(b => ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).trim()).slice(0, 400)""")
    if re.search(r"\bapplied\b.{0,40}\bago\b|application submitted|you applied", text, re.I):
        return "applied"
    if any(re.search(r"^apply\b|apply on company", b.strip(), re.I) for b in buttons):
        return "external"
    return "unknown"


def _easy_button(page):
    """The job's own Easy Apply button: a <button> reading exactly "Easy Apply"
    (aria-label "Easy Apply to this job"). Similar-job cards are links."""
    for b in page.query_selector_all("button"):
        try:
            if not b.is_visible():
                continue
            if EASY_BUTTON.match(b.get_attribute("aria-label") or "") or EASY_BUTTON.match((b.inner_text() or "").strip()):
                return b
        except Exception:  # noqa: BLE001 - detached while looking
            continue
    return None


def _through_interstitials(page) -> None:
    """LinkedIn sometimes shows a "Job search safety reminder" before the form:
    its "Continue applying" button opens the application."""
    for _ in range(3):
        cont = None
        for b in page.query_selector_all("dialog[open] button, [role=dialog] button"):
            try:
                if b.is_visible() and re.fullmatch(r"\s*continue( applying)?\s*", b.inner_text() or "", re.I):
                    cont = b
                    break
            except Exception:  # noqa: BLE001
                continue
        if cont is None:
            return
        cont.click()
        _pause(1.0, 2.0)


def _modal(page):
    for sel in MODAL.split(", "):
        el = page.query_selector(sel)
        if el and el.is_visible():
            return el
    return None


def never_skip() -> bool:
    return bool(settings.autoapply.get("never_skip", True))


def _inject(page, job_id: str) -> None:
    script = (SCRIPT.read_text(encoding="utf-8").replace("__JAB_ENDPOINT__", ENDPOINT)
              .replace("__JAB_HOST__", "localhost"))
    page.evaluate("(a) => { window.__jabAutoApply = a; window.__jabPasses = 0; }",
                  {"jobId": job_id, "scope": SCOPE, "overwrite": True, "noFiles": True,
                   "neverSkip": never_skip(),
                   "years": float(settings.operator.get("years_experience") or 1)})
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
        // LinkedIn's errors -- not its confirmations ("Resume uploaded successfully" uses the same alert box).
        for (const e of m.querySelectorAll('.artdeco-inline-feedback--error, [role=alert], [aria-live=assertive]')) {
            const t = (e.innerText || '').trim();
            if (!(e.offsetWidth || e.offsetHeight) || !t || /success|uploaded|saved|added/i.test(t)) continue;
            if (/please|required|invalid|enter a|select a|must|error|can.?t be|cannot be|too (long|short)|larger than|smaller than|between/i.test(t))
                out.push('LinkedIn: ' + t.slice(0, 140));
        }
        return out; }""")


def _today_us() -> str:
    """LinkedIn's card date, M/D/YYYY without leading zeros."""
    d = datetime.now()
    return f"{d.month}/{d.day}/{d.year}"


def _resume_step(modal) -> bool:
    """This step asks for the resume: it has an "Upload resume" control."""
    return bool(modal.evaluate("""(m) => [...m.querySelectorAll('button, label, [role=button]')]
        .some(b => (b.offsetWidth || b.offsetHeight) && /^\\s*upload resume\\s*$/i.test(b.innerText || ''))"""))


def _selected_resume(modal) -> str:
    """Text of the resume card whose radio is selected."""
    return modal.evaluate("""(m) => {
        const picked = [...m.querySelectorAll('input[type=radio]:checked, [role=radio][aria-checked=true], [aria-checked=true]')];
        for (const r of picked) {
            let card = r; for (let i = 0; card && i < 6 && !/\\.(pdf|docx?)/i.test(card.innerText || ''); i++) card = card.parentElement;
            if (card) return (card.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
        }
        return ''; }""")


def upload_tailored(page, modal, job_id: str, filename: str) -> bool:
    """Upload this job's tailored PDF and make sure it is the selected resume.

    LinkedIn pre-selects the resume used last time. The "Upload resume" button
    opens the system file picker; Playwright intercepts it (no window opens) and
    the PDF is handed to its file input directly.
    """
    pdf = _forward({"url": f"{ENDPOINT}/resume/{job_id}.pdf", "blob": True})
    if pdf["status"] != 200:
        return False
    button = None
    for b in modal.query_selector_all("button, label, [role=button]"):
        if b.is_visible() and re.fullmatch(r"\s*upload resume\s*", b.inner_text() or "", re.I):
            button = b
            break
    if button is None:
        return False
    with page.expect_file_chooser(timeout=10000) as chooser:
        button.click()
    chooser.value.element.evaluate("""(el, a) => {
        const bin = atob(a.b64); const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const dt = new DataTransfer(); dt.items.add(new File([bytes], a.name, {type: 'application/pdf'}));
        el.files = dt.files; el.dispatchEvent(new Event('input', {bubbles: true})); el.dispatchEvent(new Event('change', {bubbles: true}));
    }""", {"b64": pdf["b64"], "name": filename})
    stem, today = filename.rsplit(".", 1)[0], _today_us()
    for _ in range(40):  # the upload takes a moment
        time.sleep(0.5)
        chosen = _selected_resume(modal)
        if stem in chosen and today in chosen:
            return True
        # Uploaded but not selected: pick the card dated today.
        card = modal.evaluate_handle("""(m, a) => [...m.querySelectorAll('input[type=radio], [role=radio]')].find(r => {
            let c = r; for (let i = 0; c && i < 6 && !(c.innerText || '').includes(a.stem); i++) c = c.parentElement;
            return c && (c.innerText || '').includes(a.today); }) || null""", {"stem": stem, "today": today})
        el = card.as_element() if card else None
        if el is not None:
            try:
                el.click(force=True)
            except Exception:  # noqa: BLE001
                pass
    return False


def _primary(modal):
    for b in modal.query_selector_all("button"):
        text = (b.inner_text() or "").strip()
        label = (b.get_attribute("aria-label") or "").strip()
        if b.is_visible() and b.is_enabled() and (PRIMARY.match(text) or PRIMARY.match(label)):
            return b, (text if PRIMARY.match(text) else label)
    return None, ""


def _discard(page) -> None:
    modal = _modal(page)
    if not modal:
        return
    close = modal.query_selector("button[aria-label='Dismiss'], button[aria-label*='Close'], button[data-test-modal-close-btn]")
    if close:
        close.click()
        _pause(0.6, 1.2)
    for b in page.query_selector_all("button"):
        try:
            if b.is_visible() and (b.inner_text() or "").strip().lower() == "discard":
                b.click()
                return
        except Exception:  # noqa: BLE001
            continue


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
    button = _easy_button(page)
    if button is None:
        return Outcome("not_easy_apply", "no Easy Apply button")
    button.click()
    page.wait_for_selector(MODAL, timeout=15000)
    _pause(0.8, 1.6)
    _through_interstitials(page)
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
        if _resume_step(modal) and not upload_tailored(page, modal, job_id, filename):
            shot = _shot(page, job_id, "resume")
            _discard(page)
            return Outcome("skipped", "could not upload and select the tailored resume", shot)
        problems = _problems(modal)
        if problems:
            shot = _shot(page, job_id, "unanswered")
            _discard(page)
            return Outcome("skipped", "a question has no stored answer", shot, problems)
        btn, label = _primary(modal)
        if btn is None:
            shot = _shot(page, job_id, "stuck")
            _discard(page)
            return Outcome("error", "no Next / Review / Submit button found", shot)
        if re.match(r"submit application", label, re.I):
            defaults = [f"{d['question'][:90]} → {d['answer']}" for d in page.evaluate("() => window.__jabFallbacks || []")]
            if dry_run:
                shot = _shot(page, job_id, "ready-to-submit")
                _discard(page)
                return Outcome("dry_run", "filled; stopped before Submit", shot, defaults=defaults)
            btn.click()
            for _ in range(30):
                time.sleep(0.5)
                done = page.evaluate("() => /application (was )?sent|application submitted|you applied|applied \\d/i.test(document.body.innerText.slice(0, 30000))")
                if done:
                    shot = _shot(page, job_id, "sent")
                    for sel in ("button[aria-label='Dismiss']", "button:has-text('Done')"):
                        b = page.query_selector(sel)
                        if b and b.is_visible():
                            b.click()
                            break
                    return Outcome("applied", "Easy Apply submitted", shot, defaults=defaults)
            return Outcome("error", "pressed Submit but saw no confirmation", _shot(page, job_id, "no-confirm"))
        log.info("autoapply_step", job_id=job_id, step=step, button=label)
        btn.click()
        _pause(1.2, 2.4)
    shot = _shot(page, job_id, "too-many-steps")
    _discard(page)
    return Outcome("error", "more than 15 steps", shot)
