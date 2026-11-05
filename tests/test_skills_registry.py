"""#188: the skills registry decides which terms may become JD keywords."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.state import registry as reg_mod
from src.state.registry import Entry, build

REQ = "**Requirements**\n"
ABOUT = "**About us**\n"


@pytest.fixture
def reg(monkeypatch):
    """A small registry built in the test, so the tests don't depend on the
    reviewed labels in config/skills_registry.yaml."""
    def e(term, label):
        return term.casefold(), (Entry(term=term, label=label), False)

    r = build(dict([
        e("Python", "approve"), e("CI/CD", "approve"), e("Kafka", "approve"),
        e("application", "reject"), e("Teams", "sense"),
        e("Make", "sense"), e("Go", "sense"), e("Excel", "sense"),
        e("React", "sense"), e("React.js", "approve"),
        e("Testing", "sense"), e("AI", "sense"),
        e("n8n", "approve"), e("Zapier", "approve"), e("Rust", "approve"), e("TypeScript", "approve"),
        e("Exchange", "approve"), e("SharePoint", "approve"), e("Java", "approve"), e("Spring", "approve"),
        e("LLMs", "approve"), e("pytest", "approve"), e("Jenkins", "approve"), e("Harness", "sense"),
        e("Data Engineering", "domain"), e("communication", "soft"), e("Computer Science", "credential"),
    ]))
    monkeypatch.setattr(reg_mod, "load", lambda: r)
    # keywords.term_type reads the learned vocabulary from the database; the
    # tests stay offline.
    monkeypatch.setattr("src.parser._vocabulary", lambda: ())
    from src.scorer import keywords as kw

    kw._usual_spelling.cache_clear()
    monkeypatch.setattr(reg_mod, "enabled", lambda: True)
    return r


def test_labels_and_plural_fold(reg):
    assert reg.label("Python") == "approve"
    assert reg.label("team") == "sense"          # folds to "Teams"
    assert reg.label("applications") == "reject"  # folds to "application"
    assert reg.label("React.js") == "approve"     # its own entry wins over the fold
    assert reg.label("unknownthing") == "pending"


@pytest.mark.parametrize("term", ["application", "Data Engineering", "communication", "Computer Science"])
def test_blocked_labels_never_admit(reg, term):
    assert not reg.admits(term, f"We need {term} skills.")


def test_pending_terms_are_kept(reg):
    assert reg.admits("Snowpark", "")


@pytest.mark.parametrize("term,jd,want", [
    ("Make", "Experience with n8n, Make, or similar platforms.", True),
    ("Make", "Make sure you apply.\nWe make great products.", False),
    ("Make", "Build automations with Make and Zapier.", True),
    ("Go", "Background in Go, Python, Rust, or similar.", True),
    ("Go", "Go through the backlog.\nA go-to partner.", False),
    ("Excel", "* Advanced proficiency in MS Excel.", True),
    ("Excel", "Discover what you excel at.", False),
    ("React", "Build UIs in React and TypeScript.", True),
    ("React", "React quickly to incidents.", False),
    ("Go", "- Rust and Go\n- WebAssembly", True),
    ("Excel", "* Excel (pivot tables, macros)", True),
    ("Go", "Go: 3+ years building services with Rust", True),
    ("Make", "Make sure you apply.", False),
    ("Excel", "Excel at communication.", False),
    ("Teams", "Work with cross-functional teams.", False),
    ("Teams", "Administer Exchange, SharePoint and Teams.", True),
    ("Teams", "Experience with Teams administration and Exchange.", True),
    ("Harness", "We harness the power of AI.", False),
    ("Harness", "Experience with Harness and Jenkins.", True),
    ("Testing", REQ + "- Python, testing, CI/CD\n" + ABOUT + "We love testing ideas.", True),
    ("Testing", ABOUT + "Vehicle testing for our fleet.\n" + REQ + "- 3 years of Java", False),
    ("AI", ABOUT + "A leader in AI and ERP.\n" + REQ + "- Java, Spring", False),
    ("AI", ABOUT + "A leader in AI.\n" + REQ + "- Building AI features with LLMs", True),
])
def test_sense_terms_need_their_skill_sense(reg, term, jd, want):
    assert reg.admits(term, jd) is want


def test_sense_term_without_the_advert_is_not_admitted(reg):
    assert not reg.admits("Go", None)


def test_checklist_and_gaps_use_the_registry(reg):
    from src.scorer import keywords as kw
    from src.tailor import compute_gap_skills

    jd = REQ + "- Python, application development, communication, Kafka"
    parsed = NS(required_skills=["Python", "application", "communication", "Data Engineering"],
                nice_to_have=["Kafka"], responsibilities=[])
    tokens = [k.token for k in kw.jd_keywords(parsed, title="Engineer", jd_text=jd)]
    assert tokens == ["Python", "Kafka"]
    gaps = compute_gap_skills(["Python", "application", "communication", "Kafka"], ["Python"], (), jd_text=jd)
    assert gaps == ["Kafka"]


def test_disabled_registry_admits_everything(monkeypatch, reg):
    monkeypatch.setattr(reg_mod, "enabled", lambda: False)
    assert reg_mod.admitted(["application", "Python"]) == ["application", "Python"]


def test_the_committed_registry_loads():
    reg_mod.load.cache_clear()
    r = reg_mod.load()
    assert r.label("Python") == "approve"
    assert r.label("application") == "reject"
    reg_mod.load.cache_clear()
