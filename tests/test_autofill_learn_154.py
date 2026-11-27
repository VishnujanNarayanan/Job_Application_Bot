"""Autofill (#154): answers worked out per form, preferences, and learning from submits.

Every value here is invented. The repo is public; real answers live only in the
operator's gitignored data/ats_answers.yaml and data/ats_learned.yaml.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from src.autofill.facts import Facts, load_facts
from src.autofill.learn import record_submission
from src.autofill.matcher import country_in, match_form

TODAY = date(2026, 11, 27)
AGES = ["17 or younger", "18-20", "21-29", "30-39", "40-49", "50-59", "60 or older"]

PROFILE = {"personal": {"name": "Asha Rao", "email": "asha@example.com", "phone": "+91-90000-00000",
                        "location": "Pune, Maharashtra, India"}}

ANSWERS = """\
# kept
salary:
  current_ctc_lpa: 4
  expected_min_lpa: 7
answers:
  notice_days: 10
  date_of_birth: 2000-03-04
  citizenship: India
  sponsorship_abroad: true
  preferred_name: Ash
  address: 1 Example Road, Pune, Maharashtra, India, 411001
  address_line1: 1 Example Road
  postal_code: "411001"
  how_heard: Company website
  pronouns:
    asks: ["pronouns", "preferred pronouns"]
    answer: They/them
    only_if_required: true
    choose: ["Prefer not to say", "Decline to state", "They/them"]
  work_mode:
    asks: ["work mode", "work arrangement", "working from office"]
    answer: Open to remote, hybrid or on-site
    choose: ["Any", "Flexible", "Yes", "Hybrid", "Remote", "On-site", "Onsite"]
  twelfth:
    asks: ["12th percentage", "class xii percentage"]
    answer: "85%"
  negotiable:
    asks: ["salary negotiable", "ctc negotiable", "negotiable"]
    answer: "Yes"
  travel:
    asks: ["willing to travel"]
    answer: "Yes"
  travel_abroad:
    asks: ["travel internationally"]
    answer: Within India for now
  relatives:
    asks: ["relatives working", "any relatives"]
    answer: "No"
  gender:
    asks: ["gender"]
    answer: null
  notice:
    asks: ["serving notice"]
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


def _one(facts: Facts, label: str, ftype: str = "text", job: dict | None = None, site: str = "",
         **kw) -> dict:
    field = {"ref": "f1", "label": label, "type": ftype, **kw}
    return match_form([field], facts, job, site=site, today=TODAY)[0]


def _fill(d: dict) -> tuple[str, object]:
    return d["action"], d.get("value")


# ---------------------------------------------------------------------------
# Worked out per form
# ---------------------------------------------------------------------------

def test_notice_period_in_any_shape(facts) -> None:
    assert _fill(_one(facts, "Notice period")) == ("fill", "10 days")
    assert _fill(_one(facts, "Notice period (in days)", "number")) == ("fill", "10")
    opts = ["Immediate", "0-15 days", "16-30 days", "More than 30 days"]
    assert _fill(_one(facts, "What is your notice period?", "radio", options=opts)) == ("fill", "0-15 days")


def test_start_date_is_notice_days_from_today(facts) -> None:
    assert _fill(_one(facts, "Earliest start date")) == ("fill", "7 December 2026")
    assert _fill(_one(facts, "Earliest start date", "date")) == ("fill", "2026-12-07")
    assert _one(facts, "Start date", placeholder="DD/MM/YYYY")["value"] == "07/12/2026"
    assert _one(facts, "Start date", placeholder="MM/DD/YYYY")["value"] == "12/07/2026"
    # A date in a work-history section is not the start date.
    assert _one(facts, "Employment start date")["action"] == "unknown"


def test_date_of_birth_and_age(facts) -> None:
    assert _one(facts, "Date of birth")["value"] == "4 March 2000"
    assert _one(facts, "Age", "number")["value"] == "26"
    assert _one(facts, "What is your age range?", "radio", options=AGES)["value"] == "21-29"
    assert _one(facts, "Are you at least 18 years of age?", "radio", options=["Yes", "No"])["value"] == "Yes"


@pytest.mark.parametrize("question,job,expected", [
    ("Are you legally authorized to work in India?", None, ("fill", "Yes")),
    ("Are you legally authorized to work in the United States?", None, ("fill", "No")),
    ("Are you authorised to work in the UK?", None, ("fill", "No")),
    ("Will you require visa sponsorship to work in the US?", None, ("fill", "Yes")),
    ("Will you now or in the future require sponsorship?", {"location": "Bengaluru, Karnataka, India"},
     ("fill", "No")),
    ("Will you now or in the future require sponsorship?", {"location": "Berlin, Germany"}, ("fill", "Yes")),
    # No country anywhere: home is assumed, and outlined for checking.
    ("Will you now or in the future require sponsorship?", None, ("review", "No")),
    ("Are you legally authorized to work in the country where this job is located?", None, ("review", "Yes")),
])
def test_work_authorization_follows_the_country(facts, question, job, expected) -> None:
    assert _fill(_one(facts, question, "radio", job=job, options=["Yes", "No"])) == expected


def test_country_in() -> None:
    assert country_in("Work in the U.S.") == "united states"
    assert country_in("Would you join us in Pune?") is None  # "us" is not the US
    assert country_in("India or the UK") is None  # two countries: not sure


def test_how_heard(facts) -> None:
    assert _one(facts, "How did you hear about us?")["value"] == "Company website"
    opts = ["LinkedIn", "Company Website", "Referral"]
    assert _one(facts, "How did you hear about this job?", "select", options=opts)["value"] == "Company Website"
    linked = _one(facts, "How did you hear about this job?", "select", options=opts, job={"site": "linkedin"})
    assert linked["value"] == "LinkedIn"


def test_address_fields(facts) -> None:
    assert _one(facts, "Address")["value"].startswith("1 Example Road, Pune")
    assert _one(facts, "Address line 1")["value"] == "1 Example Road"
    assert _one(facts, "PIN code")["value"] == "411001"
    assert _one(facts, "Zip / Postal code")["value"] == "411001"
    assert _one(facts, "Email address")["key"] == "email"


def test_preferred_name(facts) -> None:
    assert _one(facts, "Preferred first name")["value"] == "Ash"
    assert _one(facts, "First name")["value"] == "Asha"


def test_negotiable_is_a_yes_no_not_a_figure(facts) -> None:
    d = _one(facts, "Is your expected CTC negotiable?", "radio", options=["Yes", "No"])
    assert _fill(d) == ("fill", "Yes")


def test_the_closer_question_wins(facts) -> None:
    assert _one(facts, "Are you willing to travel?", "radio", options=["Yes", "No"])["value"] == "Yes"
    # "Within India for now" is no option: the Yes/No is left for the operator.
    d = _one(facts, "Are you willing to travel internationally?", "radio", options=["Yes", "No"])
    assert d["action"] == "unknown"
    assert _one(facts, "Are you willing to travel internationally?")["value"] == "Within India for now"


def test_a_fact_that_is_no_option_falls_back_to_answers(facts) -> None:
    facts.values["current_company"] = "Acme Analytics"
    q = "Do you have friends or family working at the company?"
    d = _one(facts, q, "radio", options=["Yes", "No"])
    assert d["action"] == "unknown"  # read as "company", and no stored answer fits
    d = _one(facts, "Do you have any relatives working at the company?", "radio", options=["Yes", "No"])
    assert _fill(d) == ("fill", "No")
    assert _one(facts, "Current company")["value"] == "Acme Analytics"


# ---------------------------------------------------------------------------
# Preferences for choices; only-if-required; numbers
# ---------------------------------------------------------------------------

def test_choose_picks_the_first_preference_present(facts) -> None:
    opts = ["He/him", "She/her", "They/them", "Prefer not to say"]
    assert _one(facts, "Pronouns", "radio", options=opts, required=True)["value"] == "Prefer not to say"
    assert _one(facts, "Pronouns", "radio", options=opts[:3], required=True)["value"] == "They/them"


def test_only_if_required_text(facts) -> None:
    assert _one(facts, "Pronouns")["action"] == "skip"
    assert _fill(_one(facts, "Pronouns", required=True)) == ("fill", "They/them")


def test_choose_ticks_every_preference_in_a_group(facts) -> None:
    d = _one(facts, "Work mode", "checkbox_group", options=["Remote", "Hybrid", "Onsite"])
    assert sorted(d["value"]) == ["Hybrid", "Onsite", "Remote"]
    yes = _one(facts, "Are you comfortable working from office?", "radio", options=["Yes", "No"])
    assert yes["value"] == "Yes"


def test_number_box_gets_the_number(facts) -> None:
    assert _one(facts, "12th percentage", "number")["value"] == "85"
    assert _one(facts, "12th percentage")["value"] == "85%"


# ---------------------------------------------------------------------------
# Learning from what is submitted
# ---------------------------------------------------------------------------

def _learn(files, fields, url="https://jobs.lever.co/acme/1/apply", job=None):
    profile, answers = files
    return record_submission(fields, url, job, path=answers.with_name("ats_learned.yaml"),
                             answers_path=answers, today=TODAY)


def _sub(question, value, source=None, filled=None, ftype="text", options=None):
    return {"question": question, "value": value, "source": source, "filled": filled, "type": ftype,
            "options": options or []}


def test_a_typed_answer_is_learned_then_trusted(files) -> None:
    q = "Do you hold a valid passport?"
    assert _learn(files, [_sub(q, "Yes", ftype="radio", options=["Yes", "No"])])["added"] == 1
    facts = load_facts(*files)
    d = _one(facts, "Do you have a valid passport?", "radio", options=["Yes", "No"])
    assert _fill(d) == ("review", "Yes")  # learned once: outlined
    src = d["source"]
    assert src.startswith("learned:")
    for _ in range(2):
        assert _learn(files, [_sub(q, "Yes", src, "Yes", "radio")])["confirmed"] == 1
    d = _one(load_facts(*files), "Do you hold a valid passport", "radio", options=["Yes", "No"])
    assert _fill(d) == ("fill", "Yes")  # submitted unchanged twice: trusted
    # A wording that is only probably the same stays outlined, however trusted.
    d = _one(load_facts(*files), "Do you have a valid passport?", "radio", options=["Yes", "No"])
    assert d["action"] == "review"


def test_a_correction_replaces_the_file_answer(files) -> None:
    facts = load_facts(*files)
    d = _one(facts, "12th percentage")
    assert d["source"] == "answers:twelfth"
    assert _learn(files, [_sub("12th percentage", "86%", d["source"], d["value"])])["added"] == 1
    again = _one(load_facts(*files), "What was your Class XII percentage?")
    assert _fill(again) == ("review", "86%")
    learned = yaml.safe_load(files[1].with_name("ats_learned.yaml").read_text())["learned"]
    assert learned[0]["replaces"] == "answers:twelfth"


def test_a_corrected_learned_answer_keeps_history(files) -> None:
    _learn(files, [_sub("Languages known", "English")])
    src = _one(load_facts(*files), "Languages known")["source"]
    assert _learn(files, [_sub("Languages known", "English, Hindi", src, "English")])["corrected"] == 1
    row = yaml.safe_load(files[1].with_name("ats_learned.yaml").read_text())["learned"][0]
    assert row["answer"] == "English, Hindi"
    assert row["confirms"] == 0
    assert row["history"] == [{"answer": "English", "until": "2026-11-27"}]


def test_an_answer_for_a_waiting_question_fills_it(files) -> None:
    # "gender" sits in the answers file with answer: null.
    _learn(files, [_sub("What is your gender?", "Female", ftype="radio", options=["Female", "Male"])])
    d = _one(load_facts(*files), "Gender", "select", options=["Male", "Female", "Prefer not to say"])
    assert _fill(d) == ("review", "Female")


@pytest.mark.parametrize("field", [
    _sub("Expected CTC", "9 LPA"),
    _sub("Why do you want to join us?", "Because."),
    _sub("Cover letter", "Dear team"),
    _sub("Tell us about a project", "word " * 40, ftype="textarea"),
    _sub("Email", "asha@example.com", "fact:email", "asha@example.com"),
    _sub("Resume", "cv.pdf", ftype="file"),
    _sub("I agree to the privacy notice", "Yes", ftype="checkbox"),
    _sub("Portfolio", ""),
])
def test_not_worth_keeping(files, field) -> None:
    assert _learn(files, [field]) == {"added": 0, "corrected": 0, "confirmed": 0}


def test_an_unchanged_file_answer_is_not_copied(files) -> None:
    assert _learn(files, [_sub("12th percentage", "85%", "answers:twelfth", "85%")])["added"] == 0


def test_company_questions_stay_with_the_company(files) -> None:
    _learn(files, [_sub("Have you previously worked at Acme?", "Yes", ftype="radio")],
           url="https://jobs.lever.co/acme/1/apply", job={"company": "Acme"})
    row = yaml.safe_load(files[1].with_name("ats_learned.yaml").read_text())["learned"][0]
    assert row["company"] == "lever/acme"
    facts = load_facts(*files)
    here = _one(facts, "Have you previously worked at Acme?", "radio", site="lever/acme", options=["Yes", "No"])
    assert here["value"] == "Yes"
    elsewhere = _one(facts, "Have you previously worked at Acme?", "radio", site="lever/other",
                     options=["Yes", "No"])
    assert elsewhere["action"] == "unknown"


def test_learned_file_is_valid_yaml_with_header(files) -> None:
    _learn(files, [_sub("Languages known", "English")])
    text = files[1].with_name("ats_learned.yaml").read_text()
    assert text.startswith("# Answers learned from forms you submitted")
    assert yaml.safe_load(text)["learned"][0]["ask"] == "Languages known"
    assert files[1].read_text() == ANSWERS  # the operator's own file is untouched


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, files):
    from fastapi.testclient import TestClient

    from src.endpoint import autofill
    from src.endpoint.app import app

    profile, answers = files
    monkeypatch.setattr(autofill, "_job_facts", lambda job_id: {"company": "Acme"} if job_id else {})
    monkeypatch.setattr(autofill, "record_submission",
                        lambda fields, url, job: record_submission(
                            fields, url, job, path=answers.with_name("ats_learned.yaml"), answers_path=answers))
    return TestClient(app)


def test_learn_route(client, files) -> None:
    body = {"url": "https://jobs.lever.co/acme/1/apply", "job_id": "j1",
            "fields": [{"question": "Languages known", "type": "text", "value": "English"}]}
    assert client.post("/autofill/learn", json=body).status_code == 403
    r = client.post("/autofill/learn", json=body, headers={"X-JAB": "1"})
    assert r.status_code == 200
    assert r.json()["added"] == 1


def test_userscript_reports_submits(client) -> None:
    js = client.get("/autofill/jab.user.js").text
    assert "/autofill/learn" in js
    assert "// @grant        GM_setValue" in js
