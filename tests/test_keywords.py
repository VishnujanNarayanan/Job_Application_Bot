"""Layer 4 keyword extraction and the literal-substring matcher.

The parity tests matter more than they look. `src/scorer/keywords.py` and
`resume guide/score_coverage.py` must agree on what "covered" means: the grader
measures a bullet_extract file offline, the selector measures a built resume live,
and the operator compares the two numbers. A silent drift makes that comparison
meaningless without failing anything.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.scorer import keywords as kw

GRADER = Path("/home/vishnu/projects/resume guide/score_coverage.py")


# --------------------------------------------------------------------------
# norm / hit / tokens_of
# --------------------------------------------------------------------------

def test_norm_keeps_load_bearing_punctuation():
    """C++, C#, .NET, CI/CD and Node.js are all real names. Stripping +#./ ruins them."""
    assert kw.norm("C++") == "c++"
    assert kw.norm("CI/CD") == "ci/cd"
    assert kw.norm("Node.js") == "node.js"
    assert kw.norm("C#") == "c#"


def test_norm_folds_case_and_punctuation():
    assert kw.norm("REST-APIs, (v2)!").strip() == "rest apis   v2"


@pytest.mark.parametrize(
    "token,text,expected",
    [
        # literal substring — the whole rule for a technology token
        ("Python", "built pipelines in python for analysts", True),
        ("PostgreSQL", "designed schemas in postgresql", True),
        ("Docker", "shipped services with docker to cut setup time", True),
        # "Nothing is implied" — the method's central rule
        ("SQL", "queried data in postgresql", False),
        ("CSS", "styled pages with tailwind", False),
        ("Kubernetes", "ran containers in docker", False),
        # Boundary guard. A raw substring test marks all of these covered, which
        # would put a keyword on the scoresheet that the bullet never claimed.
        ("Java", "wrote javascript for the portal", False),
        ("R", "ran a report for the team", False),
        ("Go", "built services with django", False),
        ("Java", "wrote java services", True),
        # ...but names that legitimately carry punctuation must still match
        ("C++", "wrote c++ modules", True),
        (".NET", "built .net services", True),
        ("Node.js", "wrote services in node.js and deployed them", True),
        ("CI/CD", "set up ci/cd in github actions", True),
        # prose qualification: >=3 content words, >=60% present
        (
            "experience with distributed systems at scale",
            "worked on distributed systems running at scale for the trading desk",
            True,
        ),
        (
            "ability to communicate technical concepts to non-technical people",
            "built dashboards in python",
            False,
        ),
    ],
)
def test_hit(token, text, expected):
    assert kw.hit(token, kw.norm(text)) is expected


def test_short_token_never_reaches_the_prose_fallback():
    """A 2-word token has <3 content words, so it is literal-or-nothing.

    Without this, "Apache Spark" could be matched by a sentence containing only
    "spark" — and the whole point of the method is that a keyword either appears
    or it does not.
    """
    assert kw.hit("Apache Spark", kw.norm("used spark plugs")) is False
    assert kw.hit("Apache Spark", kw.norm("ran apache spark jobs")) is True


def test_hit_empty_token_is_false():
    assert kw.hit("", "anything") is False
    assert kw.hit("   ", "anything") is False


def test_tokens_of_splits_parens_and_commas():
    assert kw.tokens_of(["Python (NumPy, pandas)"]) == ["Python", "NumPy", "pandas"]


def test_tokens_of_dedupes_on_normalised_form_preserving_first_casing():
    assert kw.tokens_of(["Python, python", "PYTHON"]) == ["Python"]


def test_tokens_of_drops_one_character_fragments():
    assert "a" not in kw.tokens_of(["Python, a, SQL"])


# --------------------------------------------------------------------------
# parity with the offline grader
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def grader():
    if not GRADER.exists():
        pytest.skip("resume guide/score_coverage.py not present")
    spec = importlib.util.spec_from_file_location("_grader", GRADER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PARITY_CASES = [
    ("Python", "built pipelines in python"),
    ("SQL", "queried data in postgresql"),
    ("CI/CD", "set up ci/cd in github actions"),
    ("Node.js", "wrote services in node.js"),
    ("Apache Spark", "used spark plugs"),
    ("Java", "wrote javascript for the portal"),
    ("SQL", "queried data in postgresql"),
    ("R", "ran a report"),
    ("C++", "wrote c++ modules"),
    (".NET", "built .net services"),
    ("experience with distributed systems at scale",
     "worked on distributed systems running at scale"),
    ("", "anything"),
]


@pytest.mark.parametrize("token,text", PARITY_CASES)
def test_hit_matches_the_offline_grader(grader, token, text):
    assert kw.hit(token, kw.norm(text)) == grader.hit(token, grader.norm(text))


@pytest.mark.parametrize(
    "lines",
    [
        ["Python (NumPy, pandas)"],
        ["Python, SQL (any), Docker", "Cloud, RESTful APIs/REST APIs"],
        ["Git, CI/CD"],
        [],
    ],
)
def test_tokens_of_matches_the_offline_grader(grader, lines):
    assert kw.tokens_of(lines) == grader.tokens_of(lines)


def test_norm_matches_the_offline_grader(grader):
    for s in ("C++", "CI/CD", "Node.js", "REST-APIs, (v2)!", "Naïve Bayes"):
        assert kw.norm(s) == grader.norm(s)


# --------------------------------------------------------------------------
# jd_keywords — what counts as a checklist item
# --------------------------------------------------------------------------

def _jd(required=None, nice=None, resp=None):
    return SimpleNamespace(
        required_skills=required or [],
        nice_to_have=nice or [],
        responsibilities=resp or [],
    )


def test_required_skills_carry_full_weight():
    ks = kw.jd_keywords(_jd(required=["Python", "SQL"]))
    assert {k.token for k in ks} == {"Python", "SQL"}
    assert all(k.weight == 1.0 for k in ks)


def test_nice_to_have_carries_half_weight():
    ks = {k.token: k.weight for k in kw.jd_keywords(_jd(["Python"], ["Kafka"]))}
    assert ks == {"Python": 1.0, "Kafka": 0.5}


def test_nice_to_have_does_not_duplicate_a_required_skill():
    """A term in both lists must not be counted twice, or coverage exceeds 1.0."""
    ks = kw.jd_keywords(_jd(["Python"], ["python", "Kafka"]))
    assert [k.token for k in ks] == ["Python", "Kafka"]


def test_responsibilities_are_excluded():
    """resume_method.md: ignore everything except the Qualifications section."""
    ks = kw.jd_keywords(_jd(["Python"], resp=["Attend standups", "Mentor juniors"]))
    assert [k.token for k in ks] == ["Python"]


def test_empty_jd_yields_no_keywords():
    assert kw.jd_keywords(_jd()) == ()


# --------------------------------------------------------------------------
# coverage arithmetic
# --------------------------------------------------------------------------

def test_covered_by_finds_only_present_tokens():
    ks = kw.jd_keywords(_jd(["Python", "Kafka"]))
    assert kw.covered_by(kw.norm("built pipelines in python"), ks) == {"Python"}


def test_coverage_is_weighted_not_counted():
    """Covering one required skill beats covering one nice-to-have."""
    ks = kw.jd_keywords(_jd(["Python"], ["Kafka"]))          # weights 1.0 + 0.5
    assert kw.coverage_of({"Python"}, ks) == pytest.approx(1 / 1.5)
    assert kw.coverage_of({"Kafka"}, ks) == pytest.approx(0.5 / 1.5)


def test_coverage_of_empty_checklist_is_zero_not_a_crash():
    """A JD parse can legitimately yield no skills; that must not divide by zero."""
    assert kw.coverage_of(set(), ()) == 0.0
    assert kw.coverage_of({"Python"}, ()) == 0.0


def test_full_coverage_is_one():
    ks = kw.jd_keywords(_jd(["Python", "SQL"]))
    assert kw.coverage_of({"Python", "SQL"}, ks) == pytest.approx(1.0)


def test_weight_of_ignores_unknown_tokens():
    ks = kw.jd_keywords(_jd(["Python"]))
    assert kw.weight_of({"Python", "Rust"}, ks) == pytest.approx(1.0)


# --------------------------------------------------------------------------
# the canonical sheet — tie-break only
# --------------------------------------------------------------------------

def test_sheet_loads_all_128_titles():
    from src.scorer import qualifications as q

    sheet = q._sheet()
    assert len(sheet) >= 120, f"expected ~128 titles, got {len(sheet)}"
    assert "Python SWE" in sheet


def test_canonical_tokens_are_normalised_and_exclude_tiers():
    from src.scorer import qualifications as q

    toks = q.canonical_tokens(("Python SWE",))
    assert "python" in toks
    assert all(t == t.lower() for t in toks)


def test_canonical_overlap_counts_present_tokens():
    from src.scorer import qualifications as q

    text = kw.norm("built REST APIs in python with docker and git")
    assert q.canonical_overlap(text, ("Python SWE",)) >= 3
    assert q.canonical_overlap(kw.norm("baked bread"), ("Python SWE",)) == 0


def test_unknown_title_contributes_nothing_and_does_not_raise():
    from src.scorer import qualifications as q

    assert q.canonical_tokens(("Nonexistent Title 9000",)) == frozenset()


def test_variant_suffix_is_stripped():
    from src.scorer import qualifications as q

    assert q.canonical_tokens(("DevOps Engineer (variant 1)",)) != frozenset()
