"""Application-form autofill (#154): facts, matching, the answer bank, the routes.

Every value here is invented. The repo is public; real answers live only in the
operator's gitignored data/ats_answers.yaml.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from src.autofill import bank
from src.autofill.facts import Facts, Salary, load_facts
from src.autofill.matcher import ats_of, match_form, pick_option, salary_text

PROFILE = {
    "personal": {
        "name": "Asha Rao", "email": "asha@example.com", "phone": "+91-90000-00000",
        "location": "Pune, Maharashtra, India", "github": "https://github.com/example",
        "linkedin": "https://linkedin.com/in/example", "certificates_link": "https://example.com/certs",
    },
    "work_experience": [
        {"company": "Old Freelance", "actual_title": "Freelancer", "employment_type": "freelance",
         "start_date": "2024-01", "end_date": "present"},
        {"company": "Acme Analytics", "actual_title": "Data Engineer", "employment_type": "employment",
         "start_date": "2025-01", "end_date": "present"},
    ],
    "education": [{"degree": "B.E. in Computer Engineering", "institution": "Example University",
                   "dates": "Aug 2020 - May 2024"}],
}

ANSWERS = """\
# my comment survives
salary:
  current_ctc_lpa: 4
  expected_min_lpa: 7
answers:
  notice_period:
    asks: ["notice period", "how soon can you join"]
    answer: "30 days"
  gender:
    asks: ["gender"]
    answer: null
unanswered:
"""


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / "master_profile.json"
    profile.write_text(json.dumps(PROFILE))
    answers = tmp_path / "ats_answers.yaml"
    answers.write_text(ANSWERS)
    return profile, answers


@pytest.fixture
def facts(files) -> Facts:
    return load_facts(*files)


def _field(label: str, ftype: str = "text", **kw) -> dict:
    return {"ref": kw.pop("ref", "f1"), "label": label, "type": ftype, **kw}


def _one(facts: Facts, label: str, ftype: str = "text", job: dict | None = None, **kw) -> dict:
    return match_form([_field(label, ftype, **kw)], facts, job)[0]


# ---------------------------------------------------------------------------
# Facts come from the profile and config, not typed twice
# ---------------------------------------------------------------------------

def test_profile_facts(facts: Facts) -> None:
    v = facts.values
    assert (v["first_name"], v["last_name"]) == ("Asha", "Rao")
    assert (v["phone_country_code"], v["phone"]) == ("+91", "9000000000")
    assert (v["city"], v["state"], v["country"]) == ("Pune", "Maharashtra", "India")
    # The salaried job still running, not a freelance engagement.
    assert (v["current_company"], v["current_title"]) == ("Acme Analytics", "Data Engineer")
    assert (v["degree"], v["field_of_study"], v["graduation_year"]) == ("B.E.", "Computer Engineering", "2024")
    # From config.yaml: portfolio link, years of experience, relocation.
    assert v["portfolio"].startswith("https://")
    assert "years_experience" in v
    assert v["willing_to_relocate"] == "Yes"


def test_answers_file_shorthand_overrides_a_fact(files) -> None:
    profile, answers = files
    answers.write_text(ANSWERS.replace("answers:\n", "answers:\n  city: Bengaluru\n"))
    assert load_facts(profile, answers).values["city"] == "Bengaluru"


# ---------------------------------------------------------------------------
# Salary: middle of the advert's range, never below the floor
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lo,hi,want", [
    (8, 12, 10),      # a range: its middle
    (3, 5, 7),        # middle below the floor: the floor
    (None, None, 7),  # nothing stated: the floor
    (9, None, 9),     # "9+ LPA": the stated minimum
    (None, 20, 7),    # "up to 20": no middle, the floor stands
])
def test_expected_ctc(lo, hi, want) -> None:
    assert Salary(current=4, expected_min=7).expected(lo, hi) == want


@pytest.mark.parametrize("label,ftype,want", [
    ("Expected CTC (in LPA)", "text", "7"),
    ("Expected CTC in lakhs", "number", "7"),
    ("Expected salary per month (INR)", "number", "58333"),
    ("Expected CTC (INR)", "number", "700000"),
    ("Expected CTC", "number", "700000"),
    ("Expected CTC", "text", "7 LPA"),
    ("Desired salary (USD)", "text", None),
    ("Desired salary in $", "text", None),
])
def test_salary_units_follow_the_label(label, ftype, want) -> None:
    assert salary_text(7, label, ftype)[0] == want


def test_salary_fields_are_filled_for_review(facts: Facts) -> None:
    d = _one(facts, "What is your desired Salary?*", job={"salary_min_lpa": 10, "salary_max_lpa": 14})
    assert (d["key"], d["action"], d["value"]) == ("expected_ctc", "review", "12 LPA")
    d = _one(facts, "What is your Current CTC and Expected CTC? Please provide variable details as well.")
    assert d["key"] == "ctc_both" and d["action"] == "review"
    assert d["value"] == "Current CTC: 4 LPA; Expected CTC: 7 LPA"
    assert _one(facts, "Current CTC (in LPA)")["value"] == "4"
    assert _one(facts, "Desired salary (USD)")["action"] == "unknown"


# ---------------------------------------------------------------------------
# Labels -> keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,key", [
    ("Full name ✱", "full_name"),
    ("Name", "full_name"),
    ("What is your Legal First Name?", "first_name"),
    ("Last Name*", "last_name"),
    ("Surname", "last_name"),
    ("Email address", "email"),
    ("Current location", "location"),
    ("City of Residence", "city"),
    ("Current company", "current_company"),
    ("Company name", "current_company"),
    ("LinkedIn Profile", "linkedin"),
    ("GitHub URL", "github"),
    ("Other website", "portfolio"),
    ("Total years of experience", "years_experience"),
    ("Highest qualification", "degree"),
    ("Year of passing", "graduation_year"),
    ("Are you willing to relocate to Bangalore?", "willing_to_relocate"),
])
def test_label_to_key(facts: Facts, label: str, key: str) -> None:
    assert _one(facts, label)["key"] == key


@pytest.mark.parametrize("label", [
    "Hiring manager name", "Referrer name", "Name of your university", "Why do you want to join our company?",
    "Twitter URL", "Emergency contact phone",
])
def test_labels_that_are_not_the_applicant(facts: Facts, label: str) -> None:
    d = _one(facts, label)
    assert d["key"] not in ("full_name", "current_company", "phone"), d


def test_generic_label_uses_the_surrounding_text(facts: Facts) -> None:
    d = _one(facts, "Attach", "file", context="Resume/CV")
    assert d["action"] == "resume"
    assert _one(facts, "Attach", "file", context="Cover Letter")["action"] == "skip"
    assert _one(facts, "Attach", "file", context="Cover Letter", required=True)["action"] == "unknown"


def test_attribute_fallback(facts: Facts) -> None:
    # Lever names its company field "org" and labels it plainly; Workday uses ids.
    assert _one(facts, "", name="org")["key"] == "current_company"
    assert _one(facts, "", automation_id="legalNameSection_firstName")["key"] == "first_name"
    assert _one(facts, "Whatever", autocomplete="family-name")["key"] == "last_name"


def test_phone_national_only_beside_a_country_code_field(facts: Facts) -> None:
    alone = match_form([_field("Phone")], facts)[0]
    assert alone["value"] == "+91-90000-00000"
    pair = match_form([_field("Country Phone Code", "select", ref="a", options=["India (+91)", "Japan (+81)"]),
                       _field("Phone Number", ref="b")], facts)
    assert pair[0]["value"] == "India (+91)"
    assert pair[1]["value"] == "9000000000"


# ---------------------------------------------------------------------------
# Choices: the form's own option, never a near guess
# ---------------------------------------------------------------------------

def test_pick_option() -> None:
    assert pick_option(["Select...", "Yes", "No"], ["Yes"]) == "Yes"
    assert pick_option(["Yes, I am willing", "No"], ["Yes"]) == "Yes, I am willing"
    assert pick_option(["0-1 years", "1-3 years", "3+ years"], [1.5]) == "1-3 years"
    assert pick_option(["Less than 2 years", "2+ years"], ["1.5"]) == "Less than 2 years"
    assert pick_option(["Afghanistan", "India", "Indonesia"], ["India"]) == "India"
    assert pick_option(["Red", "Blue"], ["Yes"]) is None


def test_choice_without_a_matching_option_is_unknown(facts: Facts) -> None:
    d = _one(facts, "Are you willing to relocate?", "radio", options=["Maybe later", "Never"])
    assert d["action"] == "unknown"


def test_consent_box_is_never_ticked_unasked(facts: Facts) -> None:
    d = _one(facts, "I consent to the processing of my personal data.", "checkbox")
    assert d["action"] == "unknown"


def test_page_machinery_is_skipped(facts: Facts) -> None:
    assert _one(facts, "", "textarea", name="g-recaptcha-response")["action"] == "skip"


# ---------------------------------------------------------------------------
# The operator's own answers, matched on wording
# ---------------------------------------------------------------------------

def test_operator_answer_any_wording(facts: Facts) -> None:
    for label in ("Notice period", "What is your notice period?",
                  "What is your notice period? If you are already serving notice, mention your last day."):
        d = _one(facts, label)
        assert (d["action"], d["value"]) == ("fill", "30 days"), label


def test_null_answer_is_unknown(facts: Facts) -> None:
    assert _one(facts, "Gender", "radio", options=["Male", "Female"])["action"] == "unknown"


# ---------------------------------------------------------------------------
# The bank grows, and keeps the operator's file intact
# ---------------------------------------------------------------------------

def test_unknown_questions_are_appended_once(files) -> None:
    profile, answers = files
    q = [{"question": "Expected CTC in USD?", "type": "text", "options": []},
         {"question": "Do you hold a valid passport?", "type": "radio", "options": ["Yes", "No"]}]
    assert bank.record_unanswered(q, "lever/acme", answers) == 2
    # Same questions, other wording: not added again.
    again = [{"question": "Do you hold a valid passport", "type": "radio"},
             {"question": "Gender", "type": "radio"}]  # already in answers (null)
    assert bank.record_unanswered(again, "lever/other", answers) == 0
    text = answers.read_text()
    assert text.startswith("# my comment survives")
    data = yaml.safe_load(text)
    assert [e["ask"] for e in data["unanswered"]] == ["Expected CTC in USD?", "Do you hold a valid passport?"]
    assert data["unanswered"][1]["options"] == ["Yes", "No"]
    assert all(e["answer"] is None for e in data["unanswered"])


def test_a_filled_in_unanswered_entry_is_used(files) -> None:
    profile, answers = files
    bank.record_unanswered([{"question": "Do you hold a valid passport?", "type": "radio"}], "x", answers)
    answers.write_text(answers.read_text().replace("answer: null", "answer: 'Yes'"))
    facts = load_facts(profile, answers)
    d = _one(facts, "Do you hold a valid passport", "radio", options=["Yes", "No"])
    assert (d["action"], d["value"]) == ("fill", "Yes")


def test_bank_creates_the_file_and_handles_flow_empty_list(tmp_path: Path) -> None:
    fresh = tmp_path / "new.yaml"
    assert bank.record_unanswered([{"question": "Pronouns?", "type": "text"}], "x", fresh) == 1
    assert yaml.safe_load(fresh.read_text())["unanswered"][0]["ask"] == "Pronouns?"
    flow = tmp_path / "flow.yaml"
    flow.write_text("answers: {}\nunanswered: []\n")
    assert bank.record_unanswered([{"question": "Pronouns?", "type": "text"}], "x", flow) == 1
    assert len(yaml.safe_load(flow.read_text())["unanswered"]) == 1


def test_seen_log_has_questions_never_values(tmp_path: Path, facts: Facts) -> None:
    decisions = match_form([_field("Email"), _field("Notice period", ref="f2")], facts)
    seen = tmp_path / "seen.jsonl"
    bank.record_seen([{**d, "type": "text"} for d in decisions], "lever/acme", seen)
    body = seen.read_text()
    assert "Email" in body and "Notice period" in body
    assert "asha@example.com" not in body and "30 days" not in body


def test_ats_of() -> None:
    assert ats_of("https://jobs.lever.co/acme/123/apply") == "lever/acme"
    assert ats_of("https://job-boards.eu.greenhouse.io/roboyo/jobs/1") == "greenhouse/roboyo"
    assert ats_of("https://3m.wd1.myworkdayjobs.com/Search/job/x") == "workday/3m"
    assert ats_of("https://jobs.ashbyhq.com/Kong/abc/application") == "ashby/Kong"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, files, tmp_path):
    from fastapi.testclient import TestClient

    from src.endpoint import autofill
    from src.endpoint.app import app

    profile, answers = files
    monkeypatch.setattr(autofill, "load_facts", lambda: load_facts(profile, answers))
    monkeypatch.setattr(autofill, "_job_facts",
                        lambda job_id: {"found": True, "salary_min_lpa": 10, "salary_max_lpa": 14} if job_id else {})
    real_unanswered = bank.record_unanswered
    monkeypatch.setattr(bank, "record_unanswered", lambda q, s: real_unanswered(q, s, answers))
    monkeypatch.setattr(bank, "SEEN_PATH", tmp_path / "seen.jsonl")
    monkeypatch.setattr(bank, "OPENS_PATH", tmp_path / "opens.jsonl")
    real_seen, real_open = bank.record_seen, bank.record_open
    monkeypatch.setattr(bank, "record_seen", lambda f, s: real_seen(f, s, tmp_path / "seen.jsonl"))
    monkeypatch.setattr(bank, "record_open", lambda j, u, s: real_open(j, u, s, tmp_path / "opens.jsonl"))
    return TestClient(app)


def test_match_route_needs_the_header(client) -> None:
    r = client.post("/autofill/match", json={"url": "https://jobs.lever.co/acme/1", "fields": []})
    assert r.status_code == 403


def test_match_route(client, tmp_path: Path) -> None:
    body = {"url": "https://jobs.lever.co/acme/1/apply", "job_id": "lever-acme-1", "first": True,
            "fields": [_field("Full name", ref="a"), _field("Desired salary", ref="b"),
                       _field("Resume/CV", "file", ref="c"), _field("Do you have a car?", "radio", ref="d",
                                                                 options=["Yes", "No"])]}
    r = client.post("/autofill/match", json=body, headers={"X-JAB": "1"})
    assert r.status_code == 200
    out = r.json()
    by = {d["ref"]: d for d in out["decisions"]}
    assert by["a"]["value"] == "Asha Rao"
    assert by["b"]["value"] == "12 LPA"
    assert by["c"]["action"] == "resume"
    assert by["d"]["action"] == "unknown"
    assert out["resume"]["url"].endswith("/resume/lever-acme-1.pdf")
    assert out["new_questions"] == 1
    assert json.loads((tmp_path / "opens.jsonl").read_text())["job_id"] == "lever-acme-1"


def test_userscript_route_has_this_endpoint(client) -> None:
    r = client.get("/autofill/jab.user.js")
    assert r.status_code == 200
    assert "__JAB_" not in r.text
    assert "// ==UserScript==" in r.text
    assert "/autofill/jab.user.js" in r.text


def test_userscript_from_localhost_calls_localhost(client) -> None:
    r = client.get("http://localhost:8000/autofill/jab.user.js")
    assert "const ENDPOINT = 'http://localhost:8000';" in r.text
    assert "// @connect      localhost" in r.text
    other = client.get("http://evil.example/autofill/jab.user.js")
    assert "evil.example" not in other.text


# ---------------------------------------------------------------------------
# The Telegram Apply link
# ---------------------------------------------------------------------------

def test_tag_apply_url(monkeypatch) -> None:
    from src.notifications import tag_apply_url

    assert tag_apply_url("https://jobs.lever.co/a/1", "lever-a 1") == "https://jobs.lever.co/a/1#jab=lever-a%201"
    # A fragment-routed page keeps its route.
    assert tag_apply_url("https://x.example/#/job/1", "id") == "https://x.example/#/job/1"
    assert tag_apply_url("", "id") == ""


# ---------------------------------------------------------------------------
# The public catalog grows from what was seen
# ---------------------------------------------------------------------------

def test_catalog_sync_adds_new_questions_once(tmp_path: Path) -> None:
    from tools.ats_catalog_sync import new_entries

    catalog = tmp_path / "cat.yaml"
    catalog.write_text("questions:\n  - id: email\n    text: \"Email\"\n    type: text\n")
    seen = tmp_path / "seen.jsonl"
    rows = [{"seen_at": "greenhouse/a", "text": "What is your notice period?", "type": "text"},
            {"seen_at": "lever/b", "text": "What is your notice period", "type": "text"},
            {"seen_at": "lever/b", "text": "Email", "type": "text"}]
    seen.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = new_entries(seen, catalog)
    assert [(e["text"], e["seen_at"], e["kind"]) for e in out] == [
        ("What is your notice period?", ["greenhouse/a", "lever/b"], "unsorted")]
