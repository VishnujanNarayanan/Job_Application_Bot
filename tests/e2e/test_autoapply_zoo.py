"""The Easy Apply bot (src/autoapply/linkedin.py) on a LinkedIn-shaped zoo page.

The page pre-selects an old resume, as LinkedIn does: the bot must upload the
tailored PDF and confirm it is the selected one before going on. Made-up data;
the endpoint is answered in-process, nothing touches LinkedIn.
"""

from __future__ import annotations

import base64
import json

import pytest

pytest.importorskip("playwright.sync_api")

import structlog  # noqa: E402

from src.autoapply import linkedin  # noqa: E402
from src.autofill.matcher import ats_of, match_form  # noqa: E402
from tests.e2e.harness import FAKE_PDF, ZOO, Zoo  # noqa: E402
from tests.e2e.test_form_zoo import browser  # noqa: E402,F401 - the shared browser fixture

JOB_URL = "https://www.linkedin.com/jobs/view/1234567890/"


@pytest.fixture
def board(browser, tmp_path, monkeypatch):  # noqa: F811
    context = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 1600})
    zoo = Zoo(context, tmp_path)
    html = (ZOO / "linkedin_easy_apply.html").read_text()
    context.route("https://www.linkedin.com/**", lambda r, q: r.fulfill(status=200, content_type="text/html", body=html))

    def forward(req: dict) -> dict:
        """The bot's request path, answered by the real matcher over fake facts."""
        url = req["url"]
        if "/resume/" in url:
            return {"status": 200, "text": "", "b64": base64.b64encode(FAKE_PDF).decode(), "type": "application/pdf"}
        body = json.loads(req.get("data") or "{}")
        out: dict = {}
        if url.endswith("/autofill/match"):
            out = {"decisions": match_form(body["fields"], zoo.facts(), {}, site=ats_of(body["url"]),
                                           prefilled=body.get("prefilled")),
                   "resume": {"url": "http://localhost:8000/resume/j.pdf", "filename": linkedin.settings.resume_filename},
                   "job": {}}
        elif url.endswith("/autofill/site") or "/autofill/site?" in url:
            out = {"selectors": []}
        return {"status": 200, "text": json.dumps(out), "b64": None, "type": "application/json"}

    monkeypatch.setattr(linkedin, "_forward", forward)
    monkeypatch.setattr(linkedin, "_pause", lambda lo=0, hi=0: None)
    monkeypatch.setattr(linkedin, "SHOTS", tmp_path / "shots")
    monkeypatch.setattr(linkedin, "FAILURES", tmp_path / "failures")
    yield context
    context.close()


def test_dry_run_fills_every_step_and_stops_before_submit(board) -> None:
    page = board.new_page()
    out = linkedin.easy_apply(page, "linkedin-li-1234567890", JOB_URL, dry_run=True, log=structlog.get_logger())
    assert out.status == "dry_run", out
    assert out.screenshot.endswith("ready-to-submit.png")
    assert page.evaluate("() => window.__discarded") is True       # nothing sent
    assert page.evaluate("() => window.__submitted") is None
    assert page.evaluate("() => window.__phone") == "9000000000"
    resume = page.evaluate("() => window.__resume")
    assert linkedin._today_us() in resume, resume                  # the fresh upload, not the pre-selected 11/9 one
    # LinkedIn's boxes take whole numbers ("Invalid input" for 1.5): 1.5 years is sent as 2.
    assert page.evaluate("() => window.__azure") == "2"
    assert page.evaluate("() => window.__python") == "2"           # LinkedIn's remembered "5" replaced by the rule
    assert page.evaluate("() => window.__whole") == "2"            # "(Only Whole Numbers Allowed)": rounded up
    assert page.evaluate("() => window.__auth") == "Yes"           # radios labelled with the question itself
    assert page.evaluate("() => window.__sponsor") == "No"


def test_safety_reminder_then_the_form(board) -> None:
    page = board.new_page()
    out = linkedin.easy_apply(page, "linkedin-li-1234567890", JOB_URL + "?reminder=1", dry_run=True,
                              log=structlog.get_logger())
    assert out.status == "dry_run", out
    assert linkedin._today_us() in page.evaluate("() => window.__resume")


def test_live_run_submits(board) -> None:
    page = board.new_page()
    out = linkedin.easy_apply(page, "linkedin-li-1234567890", JOB_URL, dry_run=False, log=structlog.get_logger())
    assert out.status == "applied", out
    assert page.evaluate("() => window.__submitted") is not None


def _without_citizenship(monkeypatch) -> None:
    import src.autofill.facts as facts_mod
    real = facts_mod.load_facts

    def without(*a, **kw):
        f = real(*a, **kw)
        f.values.pop("citizenship", None)
        return f

    monkeypatch.setattr("tests.e2e.harness.load_facts", without)


def test_never_skip_answers_by_default_and_reports_it(board, monkeypatch) -> None:
    _without_citizenship(monkeypatch)  # the sponsorship question now has no stored answer
    page = board.new_page()
    out = linkedin.easy_apply(page, "linkedin-li-1234567890", JOB_URL, dry_run=False, log=structlog.get_logger())
    assert out.status == "applied", out
    assert page.evaluate("() => window.__sponsor") == "No"          # needing sponsorship: never a guessed Yes
    assert any("sponsorship" in d.lower() and d.endswith("No") for d in out.defaults), out.defaults


def test_with_never_skip_off_an_unanswered_question_skips_the_job(board, monkeypatch) -> None:
    _without_citizenship(monkeypatch)
    monkeypatch.setattr(linkedin, "never_skip", lambda: False)
    page = board.new_page()
    out = linkedin.easy_apply(page, "linkedin-li-1234567890", JOB_URL, dry_run=False, log=structlog.get_logger())
    assert out.status == "skipped", out
    assert any("sponsorship" in q.lower() for q in out.questions), out.questions
    assert page.evaluate("() => window.__submitted") is None
    assert page.evaluate("() => window.__discarded") is True
