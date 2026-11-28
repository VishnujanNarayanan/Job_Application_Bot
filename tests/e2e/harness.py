"""The form zoo's harness: the real userscript against the real matcher, in Chromium.

Every page in ``zoo/`` is a made-up form built to behave like a design met on a
live site (Workday, SuccessFactors, IBM, upload pickers...). Nothing here
touches a real site: pages are served from memory, the endpoint is answered
in-process by ``match_form`` / ``record_submission`` over fake facts, and the
resume is a few bytes of fake PDF.

Violentmonkey's GM_* functions are shimmed: requests go through ``fetch`` to
the in-process endpoint, and GM storage persists across pages of one ``Zoo``
(as it does across tabs in the browser).
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.autofill.facts import load_facts
from src.autofill.learn import record_submission
from src.autofill.matcher import ats_of, match_form

ROOT = Path(__file__).resolve().parents[2]
ZOO = Path(__file__).resolve().parent / "zoo"
SCRIPT = ROOT / "src" / "endpoint" / "static" / "jab.user.js"
ENDPOINT = "https://jab.test"
FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
RESUME_NAME = "Test_Person_Resume.pdf"

PROFILE = {
    "personal": {"name": "Test Person", "email": "test@example.com", "phone": "+91-90000-00000",
                 "location": "Pune, Maharashtra, India"},
    "work_experience": [
        {"company": "Acme Analytics", "actual_title": "Data Engineer", "employment_type": "employment",
         "start_date": "2025-06", "end_date": "present", "location": "Remote"},
        {"company": "Side Gig", "actual_title": "Freelancer", "employment_type": "freelance",
         "start_date": "2024-08", "end_date": "2025-06", "location": "Pune, India"},
    ],
    "education": [{"degree": "B.Tech in Computer Science (Big Data)", "institution": "Example Institute of Technology",
                   "dates": "Aug 2021 - Apr 2025", "score": "CGPA 8.1"}],
}

ANSWERS = """\
salary:
  current_ctc_lpa: 4
  expected_min_lpa: 7
answers:
  years_experience: 1.5
  notice_days: 15
  date_of_birth: 2000-03-04
  citizenship: India
  sponsorship_abroad: true
  prefix: Mr.
  how_heard: Company website
  willing_to_relocate: "Yes"
  gender:
    asks: ["gender"]
    answer: Female
  work_mode:
    asks: ["work mode", "type of work", "work arrangement"]
    answer: Open to remote, hybrid or on-site
    choose: ["Any", "Hybrid", "Remote", "On-site", "Onsite"]
  languages:
    asks: ["languages known"]
    answer: "English, Hindi"
  relatives:
    asks: ["relatives working", "any relatives"]
    answer: "No"
    choose: ["No"]
unanswered:
"""

GM_SHIM = r"""(() => {
  window.GM_registerMenuCommand = () => {};
  window.GM_getValue = (k, d) => (k in window.__gm ? window.__gm[k] : d);
  window.GM_setValue = (k, v) => { window.__gm[k] = v; window.__gmSave(k, JSON.stringify(v)); };
  window.GM_xmlhttpRequest = (o) => {
    fetch(o.url, {method: o.method, headers: o.headers, body: o.data}).then(async (r) => {
      const blob = await r.blob();
      const text = await blob.text();
      o.onload({status: r.status, responseText: text, response: o.responseType === 'blob' ? blob : text});
    }).catch((e) => o.onerror(e));
  };
})()"""


def chromium_executable() -> str | None:
    """Playwright's own browser when installed; else a cached headless shell."""
    found = glob.glob(str(Path.home() / ".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    return sorted(found)[-1] if found else None


@dataclass
class Zoo:
    """One browser context: pages, the fake endpoint, and what it was sent."""

    context: Any
    tmp: Path
    calls: list[tuple[str, dict]] = field(default_factory=list)
    store: dict[str, Any] = field(default_factory=dict)
    site_selectors: list[str] = field(default_factory=list)
    give_resume: bool = True
    job: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        (self.tmp / "profile.json").write_text(json.dumps(PROFILE))
        (self.tmp / "ats_answers.yaml").write_text(ANSWERS)
        self.script = (SCRIPT.read_text(encoding="utf-8")
                       .replace("__JAB_ENDPOINT__", ENDPOINT).replace("__JAB_HOST__", "jab.test"))
        self.context.expose_function("__gmSave", lambda k, v: self.store.__setitem__(k, json.loads(v)))
        self.context.route(ENDPOINT + "/**", self._endpoint)

    # --- the endpoint, in-process ----------------------------------------

    @property
    def learned_path(self) -> Path:
        return self.tmp / "ats_learned.yaml"

    def facts(self):
        return load_facts(self.tmp / "profile.json", self.tmp / "ats_answers.yaml", self.learned_path)

    def _endpoint(self, route, request) -> None:
        name = request.url.split(ENDPOINT, 1)[1]
        body = json.loads(request.post_data or "{}") if request.method == "POST" else {}
        self.calls.append((name.split("?")[0], body))
        if name.startswith("/resume/"):
            return route.fulfill(status=200, body=FAKE_PDF, content_type="application/pdf",
                                 headers={"Access-Control-Allow-Origin": "*"})
        out: dict[str, Any] = {}
        if name == "/autofill/match":
            from src.endpoint.autofill import _with_source  # same source rule as the real route

            job = _with_source(dict(self.job), body.get("url", ""))
            decisions = match_form(body["fields"], self.facts(), job, site=ats_of(body["url"]),
                                   prefilled=body.get("prefilled"))
            resume = ({"url": ENDPOINT + "/resume/job.pdf", "filename": RESUME_NAME}
                      if body.get("job_id") and self.give_resume else None)
            out = {"decisions": decisions, "resume": resume, "job": self.job, "new_questions": 0}
        elif name == "/autofill/learn":
            out = record_submission(body.get("fields", []), body["url"], self.job, path=self.learned_path,
                                    answers_path=self.tmp / "ats_answers.yaml")
        elif name == "/autofill/opened":
            out = {"found": True, "company": self.job.get("company"), "role": self.job.get("role"),
                   "saved_to": RESUME_NAME}
        elif name.startswith("/autofill/site"):
            out = {"selectors": self.site_selectors}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(out),
                      headers={"Access-Control-Allow-Origin": "*"})

    def sent(self, name: str) -> list[dict]:
        return [b for n, b in self.calls if n == name]

    # --- pages -------------------------------------------------------------

    def serve(self, url: str, page_file: str) -> None:
        html = (ZOO / page_file).read_text(encoding="utf-8")
        self.context.route(url.split("#")[0].split("?")[0] + "*",
                           lambda r, q: r.fulfill(status=200, content_type="text/html", body=html))

    def open(self, url: str, page_file: str | None = None, tag: str | None = "job-1", wait_ms: int = 0):
        """Load a page, then run the userscript on it as Violentmonkey would."""
        if page_file:
            self.serve(url, page_file)
        page = self.context.new_page()
        page.errors = []  # type: ignore[attr-defined]
        page.on("pageerror", lambda e: page.errors.append(str(e)))
        page.goto(url + (f"#jab={tag}" if tag else ""))
        page.evaluate("(s) => { window.__gm = s; }", self.store)
        page.evaluate(GM_SHIM)
        page.evaluate(self.script)
        self.settle(page, wait_ms)
        return page

    def settle(self, page, extra_ms: int = 0, quiet_ms: int = 1500, limit_ms: int = 30000) -> None:
        """Wait for the panel, then until no request has gone out for ``quiet_ms``."""
        page.wait_for_function("() => document.getElementById('jab-panel')", timeout=limit_ms)
        seen, quiet = len(self.calls), 0
        while quiet < quiet_ms:
            page.wait_for_timeout(250)
            quiet = quiet + 250 if len(self.calls) == seen else 0
            seen = len(self.calls)
        if extra_ms:
            page.wait_for_timeout(extra_ms)

    @staticmethod
    def panel(page) -> str:
        return page.evaluate("() => (document.getElementById('jab-panel') || {}).innerText || ''")

    @staticmethod
    def value(page, selector: str) -> str:
        return page.evaluate("""(sel) => { const e = document.querySelector(sel);
            if (!e) return null;
            if (e.type === 'checkbox' || e.type === 'radio') return e.checked;
            if (e.type === 'file') return [...e.files].map(f => f.name).join(',');
            if (e.tagName === 'SELECT') return e.options[e.selectedIndex] ? e.options[e.selectedIndex].text : '';
            if ('value' in e && e.tagName !== 'BUTTON' && e.tagName !== 'DIV' && e.tagName !== 'SPAN') return e.value;
            return e.textContent.trim(); }""", selector)

    @staticmethod
    def mark(page, selector: str) -> str:
        """The outline the script put on a field (or its group): review / missing / optional / unseen, or ''."""
        return page.evaluate("""(sel) => { const e = document.querySelector(sel); if (!e) return null;
            const x = e.closest('.jab-review, .jab-missing, .jab-optional, .jab-unseen') || e.querySelector('.jab-review, .jab-missing, .jab-optional, .jab-unseen');
            return x ? [...x.classList].find(c => c.startsWith('jab-')).replace('jab-', '') : ''; }""", selector)
