"""The form zoo: every form design met on live sites, rebuilt with made-up data and
filled by the real userscript in Chromium. Runs in CI before every merge (job
``form-zoo``); skipped where Playwright isn't installed.

    pip install -r requirements-e2e.txt && python -m playwright install chromium --only-shell
    pytest tests/e2e -v

A failure here means a change broke a design that used to work.
"""

from __future__ import annotations

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.e2e.harness import RESUME_NAME, Zoo, chromium_executable  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except PlaywrightError:
            exe = chromium_executable()
            if not exe:
                pytest.skip("no Chromium for Playwright: python -m playwright install chromium --only-shell")
            b = p.chromium.launch(executable_path=exe)
        yield b
        b.close()


@pytest.fixture
def zoo(browser, tmp_path):
    context = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 2400})
    yield Zoo(context, tmp_path)
    context.close()


def no_errors(page) -> None:
    assert page.errors == [], page.errors


# ---------------------------------------------------------------------------
# Workday
# ---------------------------------------------------------------------------

def test_workday(zoo) -> None:
    page = zoo.open("https://acme.wd3.myworkdayjobs.com/en-US/careers/job/x/apply/applyManually?source=LinkedIn",
                    "workday.html")
    v = lambda sel: zoo.value(page, sel)  # noqa: E731
    pills = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('[data-automation-id="multiselectInputContainer"]')]
        .map(b => [b.querySelector('input').id, [...b.querySelectorAll('[data-automation-id="selectedItem"]')].map(x => x.textContent).join(', ')]))""")
    assert pills["src"] == "LinkedIn"                           # how did you hear, from ?source=
    assert pills["pc"] == "India (+91)"                         # pre-selected phone code left alone
    assert pills["sc"] == "Example Institute of Technology"     # school search box
    assert pills["fs"] == "Computer Science"                    # field of study, the plain name
    assert (v("#pre"), v("#reg"), v("#pt"), v("#dg")) == ("Mr.", "Maharashtra", "Mobile", "Bachelor of Technology")
    assert v("#ph") == "9000000000"                             # no +91: the form has a code box
    assert (v("#jt"), v("#co"), v("#cw")) == ("Data Engineer", "Acme Analytics", True)
    assert (v("#sm"), v("#sy"), v("#em"), v("#ey")) == ("06", "2025", "", "")  # current job: no end
    assert (v("#gpa"), v("#efy"), v("#ely")) == ("8.1", "2021", "2025")
    assert v("#rf") == RESUME_NAME
    no_errors(page)


# ---------------------------------------------------------------------------
# SuccessFactors (HSBC, Yash)
# ---------------------------------------------------------------------------

def test_successfactors(zoo) -> None:
    page = zoo.open("https://career10.successfactors.com/careers?company=acme", "successfactors.html")
    v = lambda sel: zoo.value(page, sel)  # noqa: E731
    assert v("#fname") == RESUME_NAME            # the "+" dialog's file input
    assert v("#cover") == ""                     # the cover-letter tile is left alone
    assert (v("#cc"), v("#ph")) == ("India (+91)", "9000000000")
    assert (v("#c1"), v("#c2")) == ("India", "Maharashtra")
    assert (v("#c3"), v("#c4")) == ("2", "0")    # whole years, rounded up: 2 years, 0 months
    assert (v("#c5"), v("#c6"), v("#c7")) == ("15 Days", "INR", "")
    no_errors(page)


# ---------------------------------------------------------------------------
# IBM
# ---------------------------------------------------------------------------

def test_ibm(zoo) -> None:
    page = zoo.open("https://careers.ibm.com/apply", "ibm.html")
    v = lambda sel: zoo.value(page, sel)  # noqa: E731
    assert v("#cvout") == "Uploaded " + RESUME_NAME          # "From Device", never "Without Resume"
    assert v(".ph") == "India"                               # Select2 list loading from a server
    assert v("#cc") == "Acme Analytics"
    assert v("#pw") == "Yes"                                 # past experience: opens the section
    assert (v("#co"), v("#sd"), v("#cur"), v("#pt")) == ("Acme Analytics", "2025-06-01", "Yes", "Data Engineer")
    assert (v("#un"), v("#esd"), v("#eed")) == ("Example Institute of Technology", "2021-08-01", "2025-04-30")
    assert (v("#dm"), v("#dd"), v("#dy")) == ("March", "4", "2000")   # date of birth in three boxes
    assert page.evaluate("() => document.querySelector('input[name=g]:checked').value") == "f"
    assert v("#mr") == "Yes"                                 # hybrid requirement: open to any mode
    assert page.evaluate("() => document.querySelector('input[name=au]:checked').value") == "y"
    no_errors(page)


def test_ibm_availability_date_is_not_an_entry_date(zoo) -> None:
    from datetime import date, timedelta

    page = zoo.open("https://careers.ibm.com/apply", "ibm.html")
    assert zoo.value(page, "#es") == (date.today() + timedelta(days=15)).isoformat()


# ---------------------------------------------------------------------------
# Awkward designs, coverage, learning a missed field
# ---------------------------------------------------------------------------

def test_gallery_of_awkward_designs(zoo) -> None:
    page = zoo.open("https://careers.example.org/apply", "gallery.html")
    v = lambda sel: zoo.value(page, sel)  # noqa: E731
    assert page.evaluate("() => document.querySelector('job-field').shadowRoot.getElementById('fn').value") == "Test"
    assert v("#co") == "Acme Analytics"                       # label above, linked to nothing
    assert page.evaluate("() => document.querySelector('#rg [aria-checked=true]').textContent") == "Yes"
    assert page.evaluate("() => document.getElementById('swi').getAttribute('aria-checked')") == "true"
    assert v("#ed") == "English, Hindi"                       # rich-text editor
    assert v("#np") == "15 days"                              # read-only picker
    assert (v("#cty"), v("#cw")) == ("India", "India")        # hidden select and its widget
    assert v("#ph").replace(" ", "").endswith("9000000000")   # reformatting box keeps the digits
    assert v("#em") == "test@example.com"                     # only takes real typing
    assert zoo.mark(page, ".fancy-select") == "unseen"        # nothing missed silently
    assert zoo.sent("/autofill/debug"), "a snapshot is sent for the unrecognised field"

    page.click("button[type=submit]")
    page.wait_for_timeout(2200)
    assert "the site says: This field is required" in page.evaluate("() => document.querySelector('.fancy-select').title")
    page.evaluate("() => { document.querySelector('.fancy-select').textContent = 'B.Tech'; }")
    page.click("button[type=submit]")
    page.wait_for_timeout(1500)
    missed = [m for b in zoo.sent("/autofill/learn") for m in b.get("missed", [])]
    assert {"selector": "div.fancy-select", "question": "Highest qualification *"} in missed
    no_errors(page)


def test_a_learned_selector_finds_the_missed_field_next_time(zoo) -> None:
    zoo.site_selectors = ["div.fancy-select"]
    page = zoo.open("https://careers.example.org/apply", "gallery.html")
    assert zoo.mark(page, ".fancy-select") in ("review", "optional", "missing")   # seen now, not grey


def test_learning_from_a_submit(zoo) -> None:
    page = zoo.open("https://careers.example.org/apply", "learn.html")
    assert zoo.value(page, "#n") == "Test Person"
    page.check("input[name=pp][value=y]")
    page.fill("#why", "Because.")
    page.click("button[type=submit]")
    page.wait_for_timeout(1500)
    text = zoo.learned_path.read_text()
    assert "Do you hold a valid passport?" in text and "Because." not in text   # essays are never learned
    again = zoo.open("https://careers.example.org/apply", "learn.html")
    assert again.evaluate("() => document.querySelector('input[name=pp]:checked').value") == "y"
    assert zoo.mark(again, "input[name=pp]") == "review"        # learned once: outlined until confirmed


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("page_file,shows", [
    ("upload_drop_only.html", "#out"),        # no input at all: a drop
    ("upload_hidden_input.html", "#out"),     # hidden input inside the area
    ("upload_drop_listener.html", "#out"),    # input ignored: falls back to a drop
    ("upload_plain.html", "#out"),            # plain input; the caption is no second upload
])
def test_resume_uploads(zoo, page_file, shows) -> None:
    page = zoo.open(f"https://{page_file.split('.')[0].replace('_', '-')}.example.org/apply", page_file)
    assert RESUME_NAME in zoo.value(page, shows)
    assert "resume attached" in zoo.panel(page)
    no_errors(page)


# ---------------------------------------------------------------------------
# Chips and toggle buttons
# ---------------------------------------------------------------------------

def test_chips_and_toggles(zoo) -> None:
    page = zoo.open("https://chips.example.org/apply", "chips.html")
    chips = page.evaluate("() => [...document.querySelectorAll('[role=checkbox]')].map(c => c.getAttribute('aria-checked'))")
    assert chips == ["true", "true", "true"]
    pressed = page.evaluate("() => [...document.querySelectorAll('[aria-pressed]')].map(c => c.textContent + '=' + c.getAttribute('aria-pressed'))")
    assert pressed == ["Yes=true", "No=false"]


# ---------------------------------------------------------------------------
# A job opened on a board, applied for on the company site
# ---------------------------------------------------------------------------

def test_board_then_company_site(zoo) -> None:
    zoo.job = {"found": True, "company": "Acme", "role": "Data Engineer"}
    zoo.serve("https://acme.wd3.myworkdayjobs.com/apply", "company_form.html")
    zoo.serve("https://other.example-ats.com/apply", "company_form.html")
    board = zoo.open("https://www.linkedin.com/jobs/view/123/", "board.html", tag="job-9", wait_ms=500)
    assert "remembered this job: Acme" in zoo.panel(board)
    assert zoo.store["jab-pending"]["jobId"] == "job-9"
    site = zoo.open("https://acme.wd3.myworkdayjobs.com/apply", tag=None)
    assert zoo.value(site, "#r") == RESUME_NAME                 # the company's own site: filled for that job
    other = zoo.context.new_page()
    other.goto("https://other.example-ats.com/apply")
    other.evaluate("(s) => { window.__gm = s; }", zoo.store)
    from tests.e2e.harness import GM_SHIM
    other.evaluate(GM_SHIM)
    other.evaluate(zoo.script)
    other.wait_for_timeout(2500)
    assert "Autofill for Acme — Data Engineer" in zoo.panel(other)   # an unrelated site only offers


# ---------------------------------------------------------------------------
# Timing: picked the moment it shows, never more than 8 s
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("delay_ms,expect_pick", [(0, True), (1500, True), (10000, False)])
def test_dropdown_timing(zoo, delay_ms, expect_pick) -> None:
    import time

    zoo.serve("https://slow.example.org/apply", "slow_list.html")
    start = time.monotonic()
    page = zoo.open(f"https://slow.example.org/apply?delay={delay_ms}")
    picked = zoo.value(page, ".ph") == "India"
    elapsed = time.monotonic() - start
    assert picked is expect_pick
    if expect_pick:
        assert elapsed < delay_ms / 1000 + 4.5, elapsed   # soon after the list shows (settle adds ~1.5 s)
    else:
        assert elapsed < 12, elapsed                      # gave up within the 8 s budget
        assert zoo.mark(page, ".select2-selection") == "review"
