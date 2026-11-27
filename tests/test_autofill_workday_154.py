"""Autofill (#154) on Workday-style forms: sections, dropdown alternatives, drop zones,
the resume copy to Downloads. Every value here is invented."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.autofill.facts import education_entries, load_facts, work_entries
from src.autofill.matcher import match_form
from src.endpoint import autofill

PROFILE = {
    "personal": {"name": "Asha Rao", "email": "asha@example.com", "phone": "+91-90000-00000",
                 "location": "Pune, Maharashtra, India"},
    "work_experience": [
        {"company": "Side Gig", "actual_title": "Freelancer", "employment_type": "freelance",
         "start_date": "2024-08", "end_date": "2025-06", "location": "Pune, India"},
        {"company": "Acme Analytics", "actual_title": "Data Engineer", "employment_type": "employment",
         "start_date": "2025-06", "end_date": "present", "location": "Remote"},
        {"company": "No Dates", "actual_title": "Hobby", "start_date": "", "end_date": ""},
    ],
    "education": [{"degree": "B.Tech in Computer Science (Big Data)", "institution": "Example University",
                   "dates": "Sep 2021 - May 2025", "score": "CGPA 8.1"}],
}
ANSWERS = """\
answers:
  prefix: Ms.
unanswered:
"""


@pytest.fixture
def facts(tmp_path: Path):
    (tmp_path / "p.json").write_text(json.dumps(PROFILE))
    (tmp_path / "a.yaml").write_text(ANSWERS)
    return load_facts(tmp_path / "p.json", tmp_path / "a.yaml")


def _one(facts, label, ftype="text", **kw):
    return match_form([{"ref": "f1", "label": label, "type": ftype, **kw}], facts, {}, today=date(2026, 11, 27))[0]


def test_entries_newest_first() -> None:
    rows = work_entries(PROFILE)
    assert [r["company"] for r in rows] == ["Acme Analytics", "Side Gig"]  # undated entries dropped
    assert rows[0]["current"] and rows[0]["start"] == (2025, 6)
    edu = education_entries(PROFILE)[0]
    assert (edu["degree"], edu["field"], edu["start"], edu["end"]) == ("B.Tech", "Computer Science (Big Data)",
                                                                       (2021, 9), (2025, 5))


@pytest.mark.parametrize("label,ftype,group,expected", [
    ("Job Title", "text", "Work Experience 1", "Data Engineer"),
    ("Company", "text", "Work Experience 1", "Acme Analytics"),
    ("Job Title", "text", "Work Experience 2", "Freelancer"),
    ("From month", "text", "Work Experience 1", "06"),
    ("From year", "text", "Work Experience 1", "2025"),
    ("To month", "text", "Work Experience 2", "06"),
    ("Location", "text", "Work Experience 2", "Pune, India"),
    ("From year", "text", "Education 1", "2021"),
    ("To (Actual or Expected) year", "text", "Education 1", "2025"),
    ("Overall Result (GPA)", "text", "Education 1", "8.1"),
])
def test_section_fields_come_from_that_entry(facts, label, ftype, group, expected) -> None:
    assert _one(facts, label, ftype, group=group)["value"] == expected


def test_current_job_ticks_and_skips_its_end_date(facts) -> None:
    assert _one(facts, "I currently work here", "checkbox", group="Work Experience 1")["value"] is True
    assert _one(facts, "I currently work here", "checkbox", group="Work Experience 2")["value"] is False
    assert _one(facts, "To month", group="Work Experience 1")["action"] == "skip"


def test_single_date_box_takes_its_format(facts) -> None:
    d = _one(facts, "From", placeholder="MM/YYYY", group="Work Experience 2")
    assert d["value"] == "08/2024"


def test_education_pick_lists_get_alternatives(facts) -> None:
    d = _one(facts, "Degree", "combobox", group="Education 1")
    assert d["value"] == "B.Tech"
    assert "Bachelor of Technology" in d["alts"] and "Bachelor's Degree" in d["alts"]
    d = _one(facts, "Field of Study", "prompt", group="Education 1")
    assert d["value"] == "Computer Science (Big Data)" and d["alts"] == ["Computer Science"]
    assert _one(facts, "School or University", "prompt", group="Education 1")["value"] == "Example University"


def test_a_section_beyond_the_profile_is_left(facts) -> None:
    assert _one(facts, "Job Title", group="Work Experience 3")["value"] == "Data Engineer"  # falls back to the table
    assert _one(facts, "From year", group="Work Experience 3")["action"] == "unknown"


def test_prefix_and_phone_device_type(facts) -> None:
    d = _one(facts, "Prefix", "combobox")
    assert d["value"] == "Ms." and "Ms" in d["alts"]
    d = _one(facts, "Phone Device Type", "combobox")
    assert d["value"] == "Mobile" and "Cell" in d["alts"]
    assert _one(facts, "Phone Number")["key"] == "phone"


def test_drop_zone_reads_the_heading(facts) -> None:
    d = _one(facts, "Drop files here or Select files", "file", context="Resume/CV Upload a file (5MB max) *")
    assert d["action"] == "resume"


def test_source_in_the_url_names_the_board(facts) -> None:
    job = autofill._with_source({}, "https://x.wd3.myworkdayjobs.com/job/apply?source=LinkedIn")
    assert job == {"site": "linkedin"}
    d = match_form([{"ref": "f", "label": "How Did You Hear About Us?", "type": "prompt"}], facts, job)[0]
    assert d["value"] == "LinkedIn" and "Company website" in d["alts"]
    assert autofill._with_source({"site": "naukri"}, "https://x/?source=LinkedIn") == {"site": "naukri"}


def test_resume_copy_replaces_the_last_one(tmp_path, monkeypatch) -> None:
    import src.endpoint.cache as cache
    import src.state.db as db

    monkeypatch.setattr(autofill, "downloads_dir", lambda: tmp_path)
    monkeypatch.setattr(cache, "get_or_build", lambda job_id, ext, session: (b"%PDF-" + job_id.encode(), "x"))

    class _S:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "session_scope", lambda: _S())
    name = autofill.settings.resume_filename
    (tmp_path / name).write_bytes(b"old")
    assert autofill.save_resume_copy("job-1") == tmp_path / name
    assert (tmp_path / name).read_bytes() == b"%PDF-job-1"
    assert sorted(p.name for p in tmp_path.iterdir()) == [name]  # no temp file left


def test_resume_copy_off(monkeypatch) -> None:
    monkeypatch.setattr(autofill, "downloads_dir", lambda: None)
    assert autofill.save_resume_copy("job-1") is None


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from src.endpoint.app import app

    monkeypatch.setattr(autofill, "downloads_dir", lambda: None)
    monkeypatch.setattr(autofill, "_job_facts", lambda job_id: {"found": True, "company": "Acme", "role": "DE"})
    monkeypatch.setattr(autofill, "_ROOT", tmp_path)
    return TestClient(app)


def test_opened_route(client) -> None:
    body = {"job_id": "j1"}
    assert client.post("/autofill/opened", json=body).status_code == 403
    r = client.post("/autofill/opened", json=body, headers={"X-JAB": "1"}).json()
    assert (r["company"], r["role"], r["saved_to"]) == ("Acme", "DE", None)


def test_debug_route_saves_locally(client, tmp_path) -> None:
    body = {"url": "https://acme.wd3.myworkdayjobs.com/x", "fields": [{"tag": "BUTTON", "label": "Prefix"}]}
    assert client.post("/autofill/debug", json=body).status_code == 403
    name = client.post("/autofill/debug", json=body, headers={"X-JAB": "1"}).json()["saved"]
    saved = json.loads((tmp_path / "data" / "logs" / "autofill_debug" / name).read_text())
    assert saved["fields"][0]["label"] == "Prefix"


def test_a_prefilled_phone_code_drops_the_code_from_the_number(facts) -> None:
    field = [{"ref": "f", "label": "Phone Number", "type": "text"}]
    assert match_form(field, facts, {})[0]["value"] == "+91-90000-00000"
    d = match_form(field, facts, {}, prefilled=["Country / Territory Phone Code"])[0]
    assert d["value"] == "9000000000"


def test_calling_code_and_someone_elses_title(facts) -> None:
    d = _one(facts, "International Calling code", "combobox")
    assert d["key"] == "phone_country_code" and "India (+91)" in d["alts"]
    for label in ("If you have any relative working with HSBC group, please provide name, relationship and job title",
                  "External Auditor Job Title"):
        assert _one(facts, label, "textarea")["key"] != "current_title", label


@pytest.mark.parametrize("label", [
    "If you have any relative working with HSBC group, please provide name, relationship and location",
    "If yes, please provide your Employee ID:", "If so, which city?", "Please provide details of your current role",
])
def test_follow_up_questions_take_no_profile_fact(facts, label) -> None:
    assert _one(facts, label, "textarea")["key"] is None


def test_site_recipe_learned_from_a_missed_field(client) -> None:
    body = {"url": "https://careers.example.com/apply", "fields": [],
            "missed": [{"selector": "div.fancy-select", "question": "Notice"},
                       {"selector": "<script>", "question": "bad"}]}
    assert client.post("/autofill/learn", json=body, headers={"X-JAB": "1"}).status_code == 200
    assert client.get("/autofill/site?host=careers.example.com").status_code == 403
    r = client.get("/autofill/site?host=careers.example.com", headers={"X-JAB": "1"}).json()
    assert r == {"selectors": ["div.fancy-select"]}
    assert client.get("/autofill/site?host=other.example.com", headers={"X-JAB": "1"}).json() == {"selectors": []}


def test_a_lone_checkbox_ticks_on_a_yes_choice(facts) -> None:
    facts.values["willing_to_relocate"] = "Yes"
    assert _one(facts, "Willing to relocate", "checkbox")["value"] is True


def test_years_and_months_split_and_currency(facts) -> None:
    facts.values["years_experience"] = 1.5
    assert _one(facts, "Total Work Experience (Years)", "combobox")["value"] == "1"
    assert _one(facts, "Total Work Experience (Months)", "combobox")["value"] == "6"
    assert _one(facts, "Total Relevant Experience(Years)", "combobox")["value"] == "1"
    assert _one(facts, "Years of experience", "number")["value"] == "1.5"
    d = _one(facts, "Paid in (Currency)", "combobox")
    assert d["value"] == "INR" and "Indian Rupee" in d["alts"]
    assert _one(facts, "Expected Gross Pay", "text")["key"] == "expected_ctc"
    assert _one(facts, "Country/Region Code", "select", options=["- Select -", "India (+91)", "USA (+1)"])["value"] == "India (+91)"
    assert _one(facts, "No Selection", "combobox")["action"] == "unknown"  # a placeholder is no question
