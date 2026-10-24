"""The job's city in the resume header (#134)."""

from datetime import date
from pathlib import Path

import pytest
from docx import Document

from src.config import settings
from src.endpoint import relocation as r

HOME = "Kochi, Kerala, India"


@pytest.mark.parametrize("location, city", [
    ("Bengaluru, Karnataka, India", "Bengaluru"),
    ("Bangalore Urban, Karnataka, India", "Bengaluru"),
    ("Bengaluru East, Karnataka, India", "Bengaluru"),
    ("Benagaluru, Karnataka, India", "Bengaluru"),
    ("Hyderbad, India", "Hyderabad"),
    ("Pune Division, Maharashtra, India", "Pune"),
    ("Gurgaon, Haryana, India", "Gurugram"),
    ("Trivandrum, Kerala, India", "Trivandrum"),
    ("Thiruvananthapuram, Kerala, India", "Trivandrum"),
    ("Navi Mumbai, MH", "Navi Mumbai"),
    ("New Delhi, Delhi, India", "New Delhi"),
    ("Kharadi, Maharashtra, India", "Pune"),
    ("Banjara Hills, Telangana, India", "Hyderabad"),
    ("Sahibzada Ajit Singh Nagar, PB, India", "Mohali"),
    ("Gujarat International Finance Tec-City, Gujarat, India", "GIFT City"),
    ("Bengaluru, //KA, India", "Bengaluru"),
    ("Remote - Pune", "Pune"),
])
def test_known_cities_and_spellings(location, city):
    assert r.job_city(location, None, HOME) == city


@pytest.mark.parametrize("location, place", [
    ("Faridkot, PB, India", "Faridkot"),
    ("kharabwadi, MH, India", "Kharabwadi"),
    ("RAIGAD, Maharashtra, India", "Raigad"),
    ("Dist. Raigad, Maharashtra, India", "Raigad"),
    ("Baruipur P, WB, India", "Baruipur"),
])
def test_another_indian_place_in_the_field_is_used_as_written(location, place):
    assert r.job_city(location, None, HOME) == place


def test_a_multi_location_field_takes_the_first_indian_city():
    assert r.job_city("Hyderabad / Chennai / Pune", None, HOME) == "Hyderabad"
    assert r.job_city("Austin, TX; Bengaluru, Karnataka, India; Cambridge, UK", None, HOME) == "Bengaluru"


def test_the_operators_own_city_among_the_jobs_cities_keeps_the_header():
    assert r.job_city("Bengaluru / Kochi", None, HOME) is None
    assert r.job_city("Cochin, Kerala, India", None, HOME) is None


@pytest.mark.parametrize("location", [
    "Remote", "Remote, IN", "Work from home", "Hybrid", "India", "KA, IN", "Multiple locations",
    "India (Remote-First)", "Remote-India (PEO)", "Any city in India, , India", "PAN India, , India",
    "San Francisco / India / Shenzhen", None, "",
])
def test_a_field_naming_no_place_reads_the_advert(location):
    assert r.job_city(location, "The team sits in Pune.", HOME) == "Pune"
    assert r.job_city(location, "Work from anywhere.", HOME) is None


@pytest.mark.parametrize("location", [
    "Remote", "Work from home", "Hybrid", "Remote (Work from Anywhere)", "India (Remote)",
    "IRIS India", "Open to Remote (India)", "All Cities, , India",
])
def test_a_work_mode_or_label_is_never_shown_as_a_place(location):
    assert r.job_city(location, "Fully remote role.", HOME) is None


@pytest.mark.parametrize("location", [
    "New York, NY", "Indianapolis, IN", "Columbus, IN / Indianapolis, IN",
    "Hybrid (San Francisco, CA)", "London, Dublin, UK-Remote",
])
def test_a_field_naming_only_places_abroad_keeps_the_profile_location(location):
    assert r.job_city(location, "Offices in Chennai and New York.", HOME) is None


def test_the_adverts_location_line_wins_over_other_cities_it_mentions():
    jd = "We have offices in Mumbai and Pune.\n**Job Location:** **Chennai**"
    assert r.job_city("India", jd, HOME) == "Chennai"


def test_an_unlisted_place_on_a_location_line_needs_an_indian_job():
    assert r.job_city("Remote, IN", "**Location:** Rajkot", HOME) == "Rajkot"
    assert r.job_city(None, "Location: Tampa, FL", HOME) is None


def test_a_state_field_prefers_the_adverts_city_in_that_state():
    jd = "Come into the Chennai office often. Our Bengaluru team leads."
    assert r.job_city("KA, IN", jd, HOME) == "Bengaluru"
    assert r.job_city("TN, IN", jd, HOME) == "Chennai"


def test_a_city_inside_a_longer_word_does_not_match():
    assert r.cities_in("Punekar Industries, Suratgarh") == []


@pytest.mark.parametrize("today, expected", [
    (date(2026, 1, 31), date(2026, 2, 28)),
    (date(2028, 1, 31), date(2028, 2, 29)),
    (date(2026, 3, 31), date(2026, 4, 30)),
    (date(2026, 12, 15), date(2027, 1, 15)),
    (date(2026, 10, 24), date(2026, 11, 24)),
])
def test_a_month_ahead_is_always_a_real_date(today, expected):
    assert r.add_months(today, 1) == expected


def test_the_header_line_reads_the_date_unambiguously():
    assert r.header_line("Bengaluru", date(2026, 10, 21)) == \
        "Relocating to Bengaluru by 21 Nov 2026, willing to relocate sooner if required."
    assert r.header_line("Pune", date(2026, 1, 31)).startswith("Relocating to Pune by 28 Feb 2026")


def test_switched_off_the_header_never_changes(monkeypatch):
    monkeypatch.setitem(settings._data, "relocation", {"enabled": False, "months": 1})
    assert r.header_location_for("Pune, MH, India", None) is None
    assert r.printed_location("Pune, MH, India", None) is None


def test_switched_on_a_job_in_another_city_gets_the_line(monkeypatch):
    monkeypatch.setitem(settings._data, "relocation", {"enabled": True, "months": 2})
    monkeypatch.setattr(r, "_home_location", lambda: HOME)
    line = r.header_location_for("Pune, MH, India", None, today=date(2026, 10, 21))
    assert line == "Relocating to Pune by 21 Dec 2026, willing to relocate sooner if required."
    assert r.header_location_for("Kochi, Kerala, India", None) is None


def test_the_match_views_show_the_printed_line(monkeypatch):
    monkeypatch.setitem(settings._data, "relocation", {"enabled": True, "months": 1})
    monkeypatch.setattr(r, "_home_location", lambda: HOME)
    assert r.printed_location("Pune, MH, India", None, today=date(2026, 10, 24)).startswith(
        "Relocating to Pune by 24 Nov 2026")
    assert r.printed_location("Remote", "Work from anywhere.") == HOME


def test_the_relocation_line_is_part_of_the_render_cache_key():
    from src.endpoint.cache import render_version
    from src.llm.schemas import StoredSelection

    sel = StoredSelection(job_id="j", template_version="abc12345", built_at="2026-10-24T00:00:00Z",
                          jd_keywords=[], keyword_coverage=0.0, lead_entry_coverage=0.0, entries=[])
    plain = render_version(sel)
    pune = render_version(sel, "Relocating to Pune by 31 Oct 2026, willing to relocate sooner if required.")
    chennai = render_version(sel, "Relocating to Chennai by 31 Oct 2026, willing to relocate sooner if required.")
    assert len({plain, pune, chennai}) == 3
    assert "_" not in pune, "the cache reads the version back as the key's last _ part"


# --- assembler ---------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]


def _template() -> Path:
    for p in (REPO / "resumes/templates/headless_v1.docx", REPO / "resumes/templates/example_template.docx"):
        if p.exists():
            return p
    pytest.skip("no Headless template available")


def test_the_assembler_swaps_only_the_location_line(tmp_path):
    import json

    from src.endpoint.assembler import _frozen_canonical, _tailored_start, assemble_docx
    from src.llm.schemas import SelectedEntryOut, StoredSelection

    profile = tmp_path / "p.json"
    profile.write_text(json.dumps({"work_experience": [{"id": "e", "role_blocks": [{
        "role": "data", "entry_header": "Engineer", "bullets": [{"id": "b", "text": "Did a thing."}]}]}],
        "projects": []}))
    sel = StoredSelection(
        job_id="j", template_version="abc12345", built_at="2026-10-24T00:00:00Z",
        jd_keywords=[], keyword_coverage=0.0, lead_entry_coverage=0.0,
        entries=[SelectedEntryOut(kind="work", entry_id="e", block_id="e::data", title_alias="Engineer",
                                  header_left="Engineer", header_right="2025", bullet_ids=["b"], cap=3)],
    )
    line = "Relocating to Pune by 31 Oct 2026, willing to relocate sooner if required."
    plain, moved = tmp_path / "plain.docx", tmp_path / "moved.docx"
    assemble_docx(sel, profile, _template(), plain)
    assemble_docx(sel, profile, _template(), moved, header_location=line)

    a, b = Document(str(plain)), Document(str(moved))
    assert b.paragraphs[2].text == line
    assert a.paragraphs[2].text != line
    for i in (0, 1):
        assert a.paragraphs[i].text == b.paragraphs[i].text
    # Everything else in the frozen prefix is untouched.
    pa = [p for i, p in enumerate(a.element.body) if i != 2]
    pb = [p for i, p in enumerate(b.element.body) if i != 2]
    sa, sb = _tailored_start(a.element.body), _tailored_start(b.element.body)
    assert sa == sb
    from lxml import etree
    assert [etree.tostring(x, method="c14n") for x in pa[:sa - 1]] == \
           [etree.tostring(x, method="c14n") for x in pb[:sb - 1]]
    assert _frozen_canonical(a.element.body, sa) != _frozen_canonical(b.element.body, sb)


def test_every_known_city_fits_the_header_on_one_line():
    from datetime import timedelta

    for city in r.CITIES:
        for d in range(0, 366, 3):
            assert r.fits_one_line(r.header_line(city, date(2026, 1, 1) + timedelta(days=d))), city


def test_a_line_too_long_for_one_row_keeps_the_profile_location(monkeypatch):
    monkeypatch.setitem(settings._data, "relocation", {"enabled": True, "months": 1})
    monkeypatch.setattr(r, "_home_location", lambda: HOME)
    monkeypatch.setattr(r, "job_city", lambda *a: "Wwwwwwwwwwwwwww")
    assert r.header_location_for("anything", None) is None


def test_larger_towns_in_the_advert_are_found():
    assert r.job_city("UP, IN", "Our Ghaziabad plant is hiring.", HOME) == "Ghaziabad"
    assert r.job_city("Chhatrapati Sambhajinagar, MH, India", None, HOME) == "Aurangabad"
