"""#72 — cut an over-long advert by section, not by position.

The old cut kept the first 3,000 and last 1,500 characters. On 632 long stored
adverts that hid a third of the known skills the advert named, because
requirements usually sit in the middle. These pin what the section cut keeps,
in what priority, and that it never outgrows the cut it replaces.
"""

from __future__ import annotations

import pytest

from src.llm.prompts import _ELISION, clip_jd_text
from src.llm.sections import clip_by_sections, heading_category, split_sections


@pytest.mark.parametrize("line,cat", [
    ("**Key Responsibilities**", "duty"),
    ("**Requirements:**", "req"),
    ("## Qualifications", "req"),
    ("**Preferred Qualifications**", "nice"),       # nice before req
    ("Nice to have:", "nice"),
    ("**What you'll do**", "duty"),
    ("**What you’ll bring**", "req"),
    ("**About Us**", "drop"),
    ("**Company Description**", "drop"),             # drop before duty's "description"
    ("**About the role**", "duty"),                  # "about" alone is not a drop
    ("**Benefits**", "drop"),
    ("**Equal Opportunity Employer**", "drop"),
    ("**Compensation**", "pay"),
    ("**Location**", "meta"),
    # NVIDIA-style adverts, which fell back to the head-and-tail cut
    ("**What You Will Be Doing**", "duty"),
    ("**What We Need To See**", "req"),
    ("**Ways To Stand Out From The Crowd**", "nice"),
    ("**Accountabilities**", "duty"),
    ("**What you need to bring**", "req"),
    ("**Recruitment Fraud Alert**", "drop"),
    # a real section that only looks like a legal notice
    ("**Fraud Analytics Skills**", "req"),
    ("**Drug Discovery Experience**", "req"),
])
def test_heading_categories_match_the_stored_adverts(line, cat) -> None:
    assert heading_category(line) == cat


@pytest.mark.parametrize("line", [
    "**Python**",                                     # a bold list item, not a section
    "We build data platforms for retail banks across Asia.",
    "- Strong SQL skills",
])
def test_ordinary_lines_are_not_headings(line) -> None:
    assert heading_category(line) is None


def _advert(intro=1500, duties=1500, reqs=800, benefits=1500, pay=""):
    return "\n".join([
        "Senior Data Engineer at Acme. " + "Acme intro. " * (intro // 12),
        "**Key Responsibilities**",
        "- Build pipelines. " * (duties // 19),
        "**Requirements**",
        "- Python, SQL, Airflow and Spark. " * (reqs // 34),
        "**Benefits**",
        "Free lunch and a gym. " * (benefits // 22),
        pay,
    ])


def test_requirements_survive_where_the_old_cut_dropped_them() -> None:
    text = _advert()
    assert "Airflow" not in text[:3000] + text[-1500:], "old cut loses them"
    clipped = clip_by_sections(text, 4500, _ELISION)
    assert "Airflow" in clipped
    assert "**Requirements**" in clipped


def test_requirements_outrank_the_company_intro_and_benefits() -> None:
    text = _advert(intro=4000, duties=3000, reqs=1200, benefits=4000)
    clipped = clip_by_sections(text, 4500, _ELISION)
    assert clipped.count("Python, SQL, Airflow and Spark.") == text.count("Python, SQL, Airflow and Spark.")
    assert clipped.count("Free lunch") < text.count("Free lunch")


def test_pay_under_benefits_is_kept() -> None:
    text = _advert(benefits=6000, pay="Salary: 18-24 LPA plus bonus.")
    clipped = clip_by_sections(text, 4500, _ELISION)
    assert "18-24 LPA" in clipped


def test_no_headings_falls_back_to_head_and_tail() -> None:
    text = "Plain prose with no structure at all. " * 300
    assert clip_by_sections(text, 4500, _ELISION) is None
    clipped = clip_jd_text(text, head=3000, tail=1500)
    assert clipped == text[:3000] + _ELISION + text[-1500:]


def test_never_longer_than_the_cut_it_replaces() -> None:
    text = _advert(intro=6000, duties=6000, reqs=6000, benefits=6000)
    assert len(clip_jd_text(text, head=3000, tail=1500)) <= 3000 + 1500 + len(_ELISION)


def test_left_out_parts_are_marked() -> None:
    text = _advert(intro=4000, duties=6000, reqs=1000, benefits=6000)
    assert _ELISION.strip() in clip_by_sections(text, 4500, _ELISION)


def test_split_keeps_each_heading_with_its_section() -> None:
    pre, secs = split_sections("Intro line\n**Requirements**\n- SQL\n**Benefits**\nGym")
    assert pre == "Intro line"
    assert secs == [("req", "**Requirements**\n- SQL"), ("drop", "**Benefits**\nGym")]


def test_markdown_escapes_do_not_hide_headings() -> None:
    """Stored adverts escape punctuation: "Required Skills \\& Qualifications"."""
    text = "Intro\n**Required Skills \\& Qualifications**\n- C\\+\\+\n" + "**About Us**\n" + "x " * 5000
    clipped = clip_jd_text(text, head=3000, tail=1500)
    assert "C++" in clipped and "**Required Skills & Qualifications**" in clipped


def test_plain_line_headings_followed_by_a_blank_line() -> None:
    """Some adverts write headings unstyled ("Job Summary"); a list item like
    "Experience with Python" has no blank line after it and stays content."""
    pre, secs = split_sections(
        "Acme is hiring.\nJob Summary\n\nBuild things.\n"
        "Requirements\n\n- Experience with Python\n- SQL\nWho We Are\n\nA company."
    )
    assert [c for c, _ in secs] == ["duty", "req", "drop"]
    assert "Experience with Python" in secs[1][1]


def test_a_plain_line_without_a_blank_after_is_not_a_heading() -> None:
    assert heading_category("Experience with Python") is None
    assert heading_category("Experience with Python", plain=True) == "req"  # only if a blank follows
