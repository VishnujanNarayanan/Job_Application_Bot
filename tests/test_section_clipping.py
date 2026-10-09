"""#72 — cut an over-long advert by section, not by position.

The old cut kept the first 3,000 and last 1,500 characters. On 632 long stored
adverts that hid a third of the known skills the advert named, because
requirements usually sit in the middle. These pin what the section cut keeps,
in what priority, and that it never outgrows the cut it replaces.
"""

from __future__ import annotations

import pytest

from src.llm.prompts import _ELISION, clip_jd_text
from src.llm import sections
from src.llm.sections import clip_by_sections, heading_category, split_sections

#: A fixed skill list for the trimming. The real one includes the operator's
#: skills pool from the gitignored master_profile.json, which CI does not have,
#: so a test relying on it passed locally and failed in CI.
_TEST_SKILLS = ("Python", "SQL", "Postgres", "Kafka", "Spark", "Airflow", "AWS", "Go",
                "Kubernetes", "Terraform", "Bazel", "CMake", "Jenkins")


@pytest.fixture(autouse=True)
def _fixed_skill_list(monkeypatch):
    import re

    pattern = re.compile(
        r"(?<![\w+#.])(?:" + "|".join(re.escape(t) for t in _TEST_SKILLS) + r")(?![\w+#])", re.I
    )
    monkeypatch.setattr(sections, "_skill_pattern", lambda: pattern)


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


def test_no_headings_keeps_the_opening_and_the_middle() -> None:
    """Measured on the 12 long stored adverts with no usable headings: the two
    ends showed the parser 70% of their known skills, the opening plus the
    middle 96%. Unstructured adverts still front-load the company and
    back-load the benefits."""
    text = "Senior Data Engineer. " + "Company intro. " * 200 + "MIDDLE requirements Python SQL. " \
        + "Benefits text. " * 200
    assert clip_by_sections(text, 4500, _ELISION) is None
    clipped = clip_jd_text(text, head=3000, tail=1500)
    assert clipped.startswith("Senior Data Engineer.")
    assert "MIDDLE requirements Python SQL." in clipped
    assert len(clipped) <= 3000 + 1500 + len(_ELISION)


def test_no_headings_still_keeps_pay_at_the_end() -> None:
    text = "Role. " + "Intro words here. " * 400 + "Compensation: 18-24 LPA."
    assert "18-24 LPA" in clip_jd_text(text, head=3000, tail=1500)


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


# --- skill-aware trimming ---------------------------------------------------

from src.llm.sections import fit_lines  # noqa: E402


def test_trimming_keeps_skill_lines_over_prose_lines() -> None:
    """A section that must shrink keeps the lines that name skills, not its
    first lines: 624 missed skills sat in responsibilities cut at a fixed point."""
    body = "\n".join([
        "**Key Responsibilities**",
        "- Partner with stakeholders across the organisation to drive outcomes.",
        "- Foster a culture of excellence and continuous improvement in the team.",
        "- Champion our values and represent the team at company events.",
        "- Build streaming pipelines with Kafka, Spark and Airflow on AWS.",
        "- Contribute to a positive and inclusive working environment for all.",
        "- Ship services in Python and Go behind Kubernetes and Terraform.",
    ])
    out = fit_lines(body, 220)
    assert out.startswith("**Key Responsibilities**")
    assert "Kafka, Spark and Airflow" in out and "Kubernetes and Terraform" in out
    assert "culture of excellence" not in out
    assert len(out) <= 220


def test_trimmed_lines_keep_their_order_and_mark_gaps() -> None:
    body = "**Responsibilities**\nprose one here\n- Use Python daily\nprose two here\n- Run SQL on Postgres"
    out = fit_lines(body, 70)
    assert out.index("Python") < out.index("Postgres")
    assert "…" in out


def test_a_technical_word_counts_even_off_the_curated_list() -> None:
    """CamelCase, acronyms and C++-style tokens score, so a tool no list names
    yet is still preferred over plain prose."""
    body = "**Duties**\nWork closely with many teams every day.\nTune the ZorbleDB cluster.\n"
    assert "ZorbleDB" in fit_lines(body, 45)


def test_a_section_written_as_one_giant_line_is_split_into_sentences() -> None:
    """One stored advert had 4,794 characters on a single line; whole-line
    selection kept only its heading and the parser saw 0 of its 21 skills."""
    prose = "We value teamwork and a positive culture across every office. " * 30
    body = "**The Role**\n" + prose + "You will automate builds with Bazel, CMake and Jenkins. " + prose
    out = fit_lines(body, 400)
    assert "Bazel, CMake and Jenkins" in out
    assert len(out) <= 400


def test_nothing_fitting_whole_falls_back_to_a_plain_cut() -> None:
    body = "**The Role**\n" + "x" * 2000
    out = fit_lines(body, 300)
    assert out.startswith("**The Role**") and len(out) > 100
