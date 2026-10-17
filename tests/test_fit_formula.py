"""#101: the weighted_count fit formula.

Each skill weighs kind x type x emphasis, and each part of fit is half the
weighted share, half how many skills are matched (up to count_cap), so a long
advert mostly covered beats a short one fully covered. ``share`` stays the
default and is unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.config import settings
from src.scorer import apply_decision as ad
from src.scorer import keywords as kw


@pytest.fixture
def weighted(monkeypatch):
    monkeypatch.setitem(settings.scoring.fit.as_dict(), "formula", "weighted_count")
    # The short-advert scale has its own tests below.
    monkeypatch.setitem(settings.scoring.fit.weighted_count.as_dict(), "short_ad", {"below": 0})
    monkeypatch.setattr(kw, "_known_names", lambda: frozenset({"pandas", "numpy"}))
    monkeypatch.setattr(kw, "_usual_spelling", lambda: {"python": "Python", "analytics": "analytics",
                                                         "storage": "storage"})


def _jd(required=(), nice=()):
    return NS(required_skills=list(required), nice_to_have=list(nice), responsibilities=[])


# --- which skills count for more ---------------------------------------------------------

def test_a_word_adverts_write_in_lower_case_is_not_a_tool(weighted):
    assert kw.term_type("Storage") == "concept" and kw.term_type("pandas") == "tool"


@pytest.mark.parametrize("term, kind", [
    ("Python", "tool"), ("Kafka", "tool"), ("AWS", "tool"), ("Linux", "tool"), ("Git", "tool"),
    ("AWS S3", "tool"), ("Node.js", "tool"), ("REST APIs", "tool"), ("GitHub Actions", "tool"),
    ("Apache Spark", "tool"), ("pandas", "tool"),
    ("Machine Learning", "concept"), ("data pipelines", "concept"), ("Security", "concept"),
    ("networking", "concept"), ("queries", "concept"), ("best practices", "concept"),
    ("communication", "generic"), ("Strong Client Communication Skills", "generic"),
    ("teamwork", "generic"), ("problem-solving skills", "generic"), ("English", "generic"),
    ("skills", "generic"), ("data", "generic"),
    # Broad engineering words are concepts, not soft skills.
    ("Cloud", "concept"), ("APIs", "concept"), ("Testing", "concept"), ("monitoring", "concept"),
    # Fields written like names.
    ("AI", "concept"), ("DevOps", "concept"), ("RAG", "concept"), ("Agile", "concept"),
    ("analytics", "concept"), ("Performance Optimization", "concept"), ("Data Governance", "concept"),
    # A soft word inside a technical skill.
    ("Advanced SQL skills", "tool"), ("Strong programming skills in Python", "tool"),
    ("debugging skills", "concept"), ("Collaborative Filtering", "concept"),
    # Products the shape rules miss.
    ("Spring", "tool"), ("Confluence", "tool"), ("Azure Data Factory", "tool"),
])
def test_term_type(weighted, term, kind):
    assert kw.term_type(term) == kind


def test_emphasis_counts_the_title_a_must_have_line_and_repeats():
    text = "We use Python daily.\n* Must have strong Python skills\nPython, Python everywhere.\nSome Kafka."
    assert kw.emphasis_signals("Python", "Senior Python Engineer", text) == 3
    assert kw.emphasis_signals("Kafka", "Senior Python Engineer", text) == 0
    assert kw.emphasis_signals("Kafka", "Kafka Engineer", "Kafka.") == 1


def test_weight_is_kind_times_type_times_emphasis(weighted):
    ks = {k.token: k for k in kw.jd_keywords(
        _jd(["Python", "Machine Learning", "communication"], ["Kafka"]),
        title="Python Developer", jd_text="Python")}
    # type / concept: tool 3/2.4 = 1.25, concept 1, generic 1/2.4.
    assert ks["Python"].weight == pytest.approx(1.0 * 1.25 * 1.25)      # required, tool, in the title
    assert ks["Machine Learning"].weight == pytest.approx(1.0)
    assert ks["communication"].weight == pytest.approx(1 / 2.4)
    assert ks["Kafka"].weight == pytest.approx(0.5 * 1.25)              # nice-to-have tool
    assert ks["communication"].required and not ks["Kafka"].required  # kind kept apart from weight


def test_the_share_formula_is_unchanged(monkeypatch):
    monkeypatch.setitem(settings.scoring.fit.as_dict(), "formula", "share")
    ks = kw.jd_keywords(_jd(["Python", "communication"], ["Kafka"]), title="Python Developer", jd_text="Python")
    assert [(k.token, k.weight, k.required) for k in ks] == [
        ("Python", 1.0, True), ("communication", 1.0, True), ("Kafka", 0.5, False)]


def test_required_is_read_off_the_weight_when_not_given():
    assert kw.Keyword("x", 1.0).required and not kw.Keyword("y", 0.5).required
    assert kw.Keyword("z", 0.4, True).required


def test_an_unknown_formula_is_refused(monkeypatch):
    monkeypatch.setitem(settings.scoring.fit.as_dict(), "formula", "magic")
    with pytest.raises(ValueError):
        kw.fit_formula()


# --- how many skills count -----------------------------------------------------------------

def test_twelve_of_fifteen_beats_three_of_three():
    assert ad.count_blend(12 / 15, 12) > ad.count_blend(3 / 3, 3)
    assert ad.count_blend(1.0, 3) == pytest.approx(0.5 + 0.5 * 3 / 25)


def test_the_count_levels_off_at_the_cap():
    assert ad.count_blend(0.5, 25) == ad.count_blend(0.5, 40) == pytest.approx(0.75)


def _entry(*covered):
    return NS(covered=set(covered))


def test_weighted_repetition_weights_each_skill_and_counts_depth():
    ks = (kw.Keyword("python", 2.0, True), kw.Keyword("sql", 1.0, True), kw.Keyword("kafka", 1.0, False))
    entries = [_entry("python", "sql"), _entry("python"), _entry("python")]
    share, count = ad.weighted_repetition(entries, ks)
    # python in 3 entries (depth 1), sql in 1 (1/3); kafka is nice-to-have, left out.
    assert share == pytest.approx((2 * 1 + 1 * (1 / 3)) / 3)
    assert count == pytest.approx(1 + 1 / 3)


def test_the_lead_entry_counts_its_skills_only_under_weighted_count(weighted, monkeypatch):
    ks = tuple(kw.Keyword(t, 1.0, True) for t in ("a", "b", "c"))
    lead = NS(similarity=0.4, coverage=1.0, covered={"a", "b", "c"})
    weighted_lead, _ = ad.lead_entry_score(lead, ks)
    monkeypatch.setitem(settings.scoring.fit.as_dict(), "formula", "share")
    share_lead, _ = ad.lead_entry_score(lead, ks)
    assert weighted_lead < share_lead  # 3 of 3 is no longer full marks


# --- short adverts: a scale on type (#101) --------------------------------------------------

@pytest.fixture
def short_ad(weighted, monkeypatch):
    monkeypatch.setitem(settings.scoring.fit.weighted_count.as_dict(), "short_ad",
                        {"below": 12, "power": 1, "tool_boost": 3, "concept_boost": 1, "generic_cut": 0.6})


def test_in_a_short_advert_tools_count_more_and_generic_less(short_ad):
    assert kw.short_ad_scale("Python", 0) == pytest.approx(4.0)        # tool 3 -> 12
    assert kw.short_ad_scale("Machine Learning", 0) == pytest.approx(2.0)
    assert kw.short_ad_scale("communication", 0) == pytest.approx(0.4)
    assert kw.short_ad_scale("Python", 6) == pytest.approx(2.5)        # halfway
    assert kw.short_ad_scale("Python", 12) == kw.short_ad_scale("Python", 30) == 1.0


def test_the_scale_sets_weight_and_what_a_match_counts_for(short_ad):
    (py, comm) = kw.jd_keywords(_jd(["Python", "communication"]), title="Engineer", jd_text="")
    s = 1 - 2 / 12
    assert py.unit == pytest.approx(1 + 3 * s) and comm.unit == pytest.approx(1 - 0.6 * s)
    assert py.weight == pytest.approx(1.25 * (1 + 3 * s))
    assert ad.matched_units({"Python"}, (py, comm)) == pytest.approx(py.unit)


def test_without_the_scale_a_match_counts_one(weighted):
    (py,) = kw.jd_keywords(_jd(["Python"]), title="Engineer", jd_text="")
    assert py.unit == 1.0 and py.weight == pytest.approx(1.25)


# --- the junior boost ----------------------------------------------------------------------

@pytest.mark.parametrize("title, years, level, junior", [
    ("Data Engineer", 2, "mid", True),
    ("Data Engineer", 0, "mid", False),          # no years stated is not junior on its own
    ("Data Engineer", 5, "mid", False),
    ("Junior Python Developer", 0, None, True),
    ("Software Engineer I", 0, None, True),
    ("AI/ML Engineer", 0, "junior", True),
    ("Senior Data Engineer", 2, "junior", False),  # a senior title wins
    ("Software Engineer III", 2, None, False),
    ("Associate AWS Cloud Engineer", 2, None, True),
])
def test_is_junior_role(title, years, level, junior):
    assert ad.is_junior_role(title, years, level) is junior


def test_the_junior_boost_multiplies_final(monkeypatch):
    jd = NS(title="Data Analyst", years_required=2, role_level="mid")
    monkeypatch.setitem(settings.scoring.junior_boost.as_dict(), "multiplier", 1.0)
    assert ad.junior_multiplier(jd) == 1.0
    monkeypatch.setitem(settings.scoring.junior_boost.as_dict(), "multiplier", 1.1)
    assert ad.junior_multiplier(jd) == pytest.approx(1.1)
    assert ad.junior_multiplier(NS(title="Lead Data Analyst", years_required=2, role_level="mid")) == 1.0
