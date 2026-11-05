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
    from src.scorer import keywords as _kw

    _kw.match_keys.cache_clear()
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


# --- #188 PR 2: the model's own skills, pending terms, the weekly review ---------------


def test_parse_keeps_the_models_own_skills_apart(monkeypatch):
    """The vocabulary must learn only from what the model extracted, not from
    the pool/vocabulary additions that follow it."""
    import src.parser as parser_module
    from src.llm.schemas import JDParsed
    from src.parser import apply_to_row, parse
    from src.state.models import AllJobs

    monkeypatch.setattr(parser_module, "_pool_terms", lambda: ("Docker",))
    monkeypatch.setattr(parser_module, "_vocabulary", lambda: (("Kafka", "kafka"),))
    monkeypatch.setattr(reg_mod, "enabled", lambda: False)
    job = AllJobs(job_id="j", company="c", role="r", site="s",
                  jd_text="We use Python and SQL daily, with Docker and Kafka.")
    raw = JDParsed(role_summary="x", role_category="data", role_level="junior", years_required=1,
                   required_skills=["Python"], nice_to_have=["SQL"])
    parsed = parse(job, complete=lambda *a, **k: raw)
    assert {"Docker", "Kafka"} <= set(parsed.required_skills)       # additions are used
    assert parsed._llm_skills == ["Python", "SQL"]                  # but kept apart
    apply_to_row(job, parsed)
    assert job.llm_skills_raw == ["Python", "SQL"]
    # Never part of what the model is asked for.
    assert not any("llm_skills" in f for f in JDParsed.model_json_schema()["properties"])


def test_a_dictionary_parse_does_not_write_the_column():
    from src.llm.schemas import JDParsed
    from src.parser import apply_to_row
    from src.state.models import AllJobs

    job = AllJobs(job_id="j", company="c", role="r", site="s", jd_text="x")
    apply_to_row(job, JDParsed(role_summary="x", role_category="data", role_level="junior",
                               years_required=1, required_skills=["Python"]))
    assert job.llm_skills_raw is None


def test_pending_terms_follow_the_setting(reg, monkeypatch):
    assert reg.admits("Snowpark")
    monkeypatch.setattr(reg_mod, "pending_admitted", lambda: False)
    assert not reg.admits("Snowpark")
    assert reg.admits("Python")


def test_review_apply_merges_and_keeps_the_operators_labels(tmp_path, monkeypatch):
    import json

    import yaml

    from tools import registry_review as rr

    pub, loc = tmp_path / "pub.yaml", tmp_path / "loc.yaml"
    pub.write_text("# header\nterms:\n  \"python\": {term: Python, label: approve, by: operator}\n")
    monkeypatch.setattr(reg_mod, "PUBLIC", pub)
    monkeypatch.setattr(reg_mod, "LOCAL", loc)
    monkeypatch.setattr(rr, "_pool_only", lambda t: t == "Fintech payments")
    dec = tmp_path / "d.json"
    dec.write_text(json.dumps([
        {"term": "Python", "label": "reject"},                    # operator's label kept
        {"term": "Snowpark", "label": "approve"},
        {"term": "Gemini", "label": "sense", "note": "Google Gemini"},
        {"term": "Fintech payments", "label": "domain"},          # pool-only -> local file
    ]))
    assert rr.main(["apply", str(dec)]) == 0
    p = yaml.safe_load(pub.read_text())["terms"]
    assert p["python"]["label"] == "approve" and p["snowpark"]["label"] == "approve"
    assert p["gemini"] == {"term": "Gemini", "label": "sense", "note": "Google Gemini"}
    assert pub.read_text().startswith("# header\n")
    assert yaml.safe_load(loc.read_text())["terms"]["fintech payments"]["label"] == "domain"
    dec.write_text(json.dumps([{"term": "X", "label": "nonsense"}]))
    assert rr.main(["apply", str(dec)]) == 1


def test_a_name_written_in_lower_case_is_not_the_name(reg):
    assert not reg.admits("Teams", "Collaborate with backend teams and Kafka engineers.")
    assert reg.admits("Teams", "Tools: Exchange, SharePoint, Teams.")


def test_a_skill_far_away_in_the_sentence_is_not_evidence(reg):
    long = "Go ahead and coordinate the whole incident review process with everyone involved in Python."
    assert not reg.admits("Go", long)


def test_terms_become_the_skills_written_for_them(monkeypatch):
    from src.scorer import keywords as kw

    def e(term, label="approve", means=()):
        return term.casefold(), (Entry(term=term, label=label, means=tuple(means)), False)

    r = build(dict([
        e("CI/CD"), e("CI", means=["CI/CD"]), e("CI/CD practices", means=["CI/CD"]),
        e("Agile"), e("Scrum"), e("Agile/Scrum methodologies", means=["Agile", "Scrum"]),
        e("Glue"), e("AWS Glue", means=["Glue"]), e("Amazon Glue", means=["Glue"]),
        e("responsible AI", "domain"), e("responsible AI practices", means=["responsible AI"]),
    ]))
    monkeypatch.setattr(reg_mod, "load", lambda: r)
    monkeypatch.setattr(reg_mod, "enabled", lambda: True)
    kw.match_keys.cache_clear()
    terms = ["CI/CD", "CI", "CI/CD practices", "Agile/Scrum methodologies", "AWS Glue", "responsible AI practices"]
    assert reg_mod.skills_of(terms) == ["CI/CD", "Agile", "Scrum", "Glue"]
    # A profile line in any spelling the registry maps to the skill shows it.
    assert kw.matches("Glue", kw.norm("Catalogued the lake in AWS Glue"))
    assert kw.matches("Agile", kw.norm("Worked to Agile/Scrum methodologies"))
    kw.match_keys.cache_clear()


def test_weekly_review_suggests_the_skill_a_new_term_names(monkeypatch):
    from tools import registry_review as rr

    def e(term, label="approve", means=()):
        return term.casefold(), (Entry(term=term, label=label, means=tuple(means)), False)

    r = build(dict([e("CI/CD"), e("CI/CD pipelines", means=["CI/CD"]), e("Kafka"), e("Error handling"), e("logging")]))
    approved = sorted((x.term for x in r.exact.values()), key=len, reverse=True)
    assert rr._suggest("GitLab CI/CD pipelines", r, approved) == ["CI/CD"]
    assert rr._suggest("Error handling and logging", r, approved) == ["Error handling", "logging"]
    assert rr._suggest("Kafka Streams", r, approved) == ["Kafka"]
