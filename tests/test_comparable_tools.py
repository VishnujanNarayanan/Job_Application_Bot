"""#191: comparable tools give partial credit through the tool actually used."""

from __future__ import annotations

import pytest

from src.scorer import comparable
from src.scorer.keywords import Keyword, norm

EVIDENCE = [norm("Stored raw filings in AWS S3 and served the API on Render."),
            norm("Git / GitHub pull requests / code review"),
            norm("Kept sessions in MongoDB.")]


@pytest.fixture(autouse=True)
def _credit(monkeypatch):
    monkeypatch.setattr(comparable, "credit", lambda: 0.5)


def test_an_unproven_ask_is_split_with_the_comparable_tool_used():
    out = comparable.with_comparables((Keyword("GitLab", 1.25, True),), EVIDENCE)
    assert [(k.token, k.weight, k.stands_for) for k in out] == [
        ("GitLab", 0.625, None), ("GitHub", 0.625, "GitLab")]


def test_total_weight_never_changes():
    kws = (Keyword("Google Cloud Storage", 1.25, True), Keyword("Heroku", 1.0, True),
           Keyword("Cosmos DB", 1.0, True), Keyword("Kafka", 1.25, True))
    out = comparable.with_comparables(kws, EVIDENCE)
    assert sum(k.weight for k in out) == pytest.approx(sum(k.weight for k in kws))
    assert {k.stands_for for k in out if k.stands_for} == {"Google Cloud Storage", "Heroku", "Cosmos DB"}


def test_a_proven_ask_or_a_tool_already_asked_is_left_alone():
    kws = (Keyword("GitHub", 1.0, True), Keyword("Google Cloud Storage", 1.0, True), Keyword("S3", 1.0, True))
    assert comparable.with_comparables(kws, EVIDENCE) == kws


def test_proves_is_not_comparable():
    # MongoDB is comparable to Cosmos DB, never to SQL or PostgreSQL.
    assert "MongoDB" not in comparable.comparables("SQL")
    assert "MongoDB" not in comparable.comparables("PostgreSQL")
    assert "Cosmos DB" in comparable.comparables("MongoDB")


def test_off_when_credit_is_zero(monkeypatch):
    monkeypatch.setattr(comparable, "credit", lambda: 0.0)
    kws = (Keyword("GitLab", 1.0, True),)
    assert comparable.with_comparables(kws, EVIDENCE) == kws


def test_every_group_tool_is_unique_within_its_group():
    for name, tools in comparable._groups():
        assert len(set(tools)) == len(tools), name


def test_no_general_programming_language_is_comparable():
    langs = {"c", "c++", "c#", "java", "scala", "kotlin", "groovy", "javascript", "typescript",
             "python", "go", "golang", "rust", "ruby", "php", "swift", "r", "julia", "perl", "dart"}
    for name, tools in comparable._groups():
        assert not {t.casefold() for t in tools} & langs, name


def test_a_comparable_ask_is_still_a_gap():
    from src.tailor import compute_gap_skills

    assert compute_gap_skills(["GitLab"], [], tuple(EVIDENCE)) == ["GitLab"]
