"""#97 — reading companies' ATS job boards directly (src/scraper/ats.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.scraper import ats

FIXTURES = Path(__file__).parent / "fixtures" / "ats"


def _body(name: str) -> str:
    return (FIXTURES / name).read_text()


def _parse(name: str, kind: str = "list") -> list[ats.Posting]:
    return ats.parse_list(name, "acme", _body(f"{name}_{kind}.json"))


def test_greenhouse_list_gives_title_location_and_first_published_date():
    jobs = _parse("greenhouse")
    assert [j.posting_id for j in jobs] == ["111", "112"]
    first = jobs[0]
    assert first.title == "Backend Engineer"
    assert first.location == "Bengaluru, India"
    assert first.company == "Acme"
    # first_published, not updated_at: an edited job must not look new.
    assert first.posted_at == datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    assert first.url == "https://job-boards.greenhouse.io/acme/jobs/111"
    assert first.description is None  # the light list has none


def test_greenhouse_description_comes_from_the_detail_and_is_unescaped():
    job = _parse("greenhouse")[0]
    assert ats.detail_url(job) == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/111"
    ats.parse_detail(job, _body("greenhouse_detail.json"))
    assert "**APIs**" in job.description
    assert "2+ years of experience" in job.description
    assert "&lt;" not in job.description


def test_lever_joins_description_lists_and_reads_workplace_type():
    data, sre = _parse("lever")
    assert data.title == "Data Engineer"
    assert data.location == "Pune / Remote - India"
    assert data.country == "IN"
    assert data.remote is False  # hybrid
    assert data.apply_url == "https://jobs.lever.co/acme/aaaa-1/apply"
    assert "We move data." in data.description and "Airflow" in data.description
    assert data.posted_at == datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc)  # ms timestamp
    assert sre.remote is True
    assert sre.apply_url == sre.url  # no applyUrl: the posting page


def test_ashby_skips_unlisted_jobs_and_finds_india_in_secondary_locations():
    jobs = _parse("ashby")
    assert [j.posting_id for j in jobs] == ["ash-1", "ash-2"]
    security, ml = jobs
    assert security.title == "Security Engineer, Cloud"  # stripped
    assert security.remote is True and security.country != "IN"
    assert ml.country == "IN"
    assert ml.description == "Train models."
    assert ml.apply_url == ml.url


def test_smartrecruiters_list_then_detail_gives_description_and_apply_link():
    [job] = _parse("smartrecruiters")
    assert job.country == "IN" and job.remote is False
    assert job.level == "Entry Level"
    assert job.company == "Acme Corp"
    assert ats.sr_total(_body("smartrecruiters_list.json")) == 250
    assert ats.detail_url(job) == "https://api.smartrecruiters.com/v1/companies/acme/postings/3743990016049646"
    ats.parse_detail(job, _body("smartrecruiters_detail.json"))
    assert job.description.startswith("## Job Description")
    assert "1-3 years of experience" in job.description
    assert job.apply_url.endswith("?oga=true")


def test_lists_that_carry_descriptions_need_no_detail_fetch():
    assert all(ats.detail_url(j) is None for j in _parse("ashby") + _parse("lever"))


def test_a_changed_api_fails_loudly_for_that_board():
    with pytest.raises(ValueError):
        ats.parse_list("greenhouse", "acme", "<html>maintenance</html>")
    with pytest.raises(ValueError):
        ats.parse_list("lever", "acme", '{"ok": false}')


def test_list_urls():
    assert ats.list_url("lever", "acme") == "https://api.lever.co/v0/postings/acme?mode=json"
    assert ats.list_url("smartrecruiters", "acme", country="in", offset=100) == (
        "https://api.smartrecruiters.com/v1/companies/acme/postings?limit=100&offset=100&country=in")


@pytest.mark.parametrize("location,country,remote,expected", [
    ("Bengaluru, India", None, None, "india"),
    ("Kochi", None, None, "india"),
    ("Remote", "IN", True, "india"),           # structured country wins
    ("Remote", None, True, "remote_open"),      # names no region
    ("Remote - APAC", None, True, "remote_open"),
    ("Remote, France", None, True, "remote_locked"),
    ("Seoul, South Korea", None, None, "elsewhere"),  # a foreign city is never remote
    ("Seoul, South Korea", None, False, "elsewhere"),
    ("Mohali, IND", None, None, "india"),
    ("Lucknow", None, None, "india"),
    ("Salem, Tamil Nadu", None, None, "india"),
    ("Makati, NCR, ph, Philippines", None, None, "elsewhere"),
    ("Parque Ind. Rio Bravo, Mexico", None, None, "elsewhere"),  # "Ind." is not India
    ("Hybrid", None, None, "unknown"),
    ("Multiple Locations", None, None, "unknown"),
    ("", None, None, "unknown"),
    ("Hybrid", None, True, "remote_open"),
])
def test_place(location, country, remote, expected):
    p = ats.Posting(ats="lever", board_id="acme", posting_id="1", title="x",
                    location=location, country=country, remote=remote)
    assert ats.place(p) == expected


def test_to_job_is_shaped_like_a_jobspy_row():
    [job] = _parse("smartrecruiters")
    ats.parse_detail(job, _body("smartrecruiters_detail.json"))
    row = ats.to_job(job, company="Acme")
    assert row.job_id == "smartrecruiters-acme-3743990016049646"
    assert row.site == "ats:smartrecruiters"
    assert row.company == "Acme" and row.role == "Software Engineer"
    assert row.job_url_direct.endswith("?oga=true")
    assert row.jd_text == job.description
    assert row.job_level == "Entry Level"


def test_an_unknown_location_is_decided_by_the_description():
    assert ats.mentions_india("This role sits in our Bengaluru office.")
    assert not ats.mentions_india("This role sits in our Berlin office.")


@pytest.mark.parametrize("raw,expected", [
    ("FullTime", "fulltime"), ("Full-time", "fulltime"), ("Permanent", "fulltime"),
    ("Contract", "contract"), ("Intern", "internship"), ("Part-time", "parttime"), (None, None),
])
def test_employment_types_are_mapped_to_jobspy_words(raw, expected):
    assert ats.employment_type(raw) == expected


@pytest.mark.parametrize("location,remote,excluded", [
    ("Gurugram", None, True),
    ("New Delhi, India", None, True),
    ("Pune / Gurugram", None, False),   # one allowed location is enough
    ("Gurugram", True, False),          # remote with a Gurugram office
    ("Bengaluru", None, False),
])
def test_excluded_regions_apply_only_when_every_location_is_excluded(location, remote, excluded):
    p = ats.Posting(ats="lever", board_id="acme", posting_id="1", title="x", location=location, remote=remote)
    assert ats.location_disallowed(p, ["Delhi", "Gurgaon", "Gurugram", "Noida"]) is excluded


@pytest.mark.parametrize("location,remote,expected", [
    ("bengaluru, , India", False, "Bengaluru, India"),
    ("Pune / Remote - India", True, "Pune / Remote - India"),
    ("Pune", True, "Pune (Remote)"),
    ("", True, "Remote"),
    ("Bengaluru, KA, in, 12.9628957, 77.57754, Bengaluru, KA, India", False, "Bengaluru, KA, In"),
])
def test_clean_location(location, remote, expected):
    p = ats.Posting(ats="smartrecruiters", board_id="acme", posting_id="1", title="x",
                    location=location, remote=remote)
    assert ats.clean_location(p) == expected


def test_greenhouse_offices_in_the_detail_can_place_a_vague_job_in_india():
    p = ats.Posting(ats="greenhouse", board_id="acme", posting_id="9", title="x", location="Hybrid")
    ats.parse_detail(p, json.dumps({"content": "&lt;p&gt;Hi&lt;/p&gt;",
                                    "offices": [{"name": "Bengaluru", "location": "Bengaluru, India"}]}))
    assert ats.place(p) == "india"
