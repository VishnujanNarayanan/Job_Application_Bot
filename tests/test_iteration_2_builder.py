"""Layer 5 — deterministic selection builder (Call 1b removed).

Title alias and skill grouping used to come from Gemini. They are now
computed from embeddings, so these tests hit real sentence-transformer maths
rather than a stub — and there is no LLM to mock. Nothing external is
contacted: the model runs locally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from src.builder.deterministic import assign_skill_categories, choose_title_alias
from src.builder.llm_call import build as build_selection
from src.config import settings
from src.llm.schemas import (
    SelectedExpEntry,
    SkillCategory,
    StoredSelection,
    StoredSkills,
)
from src.scorer.embeddings import embed
from src.scorer.apply_decision import SelectionResult
from src.scorer.selector import (
    BulletCand,
    ExperienceCand,
    Profile,
    ProjectCand,
    SelectedBullet,
    SelectedExperience,
    SelectedProject,
    SkillCand,
    SummaryCand,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(skills_before_projects: bool = True) -> SelectionResult:
    """Minimal SelectionResult with one experience, one project, one summary."""
    exp = SelectedExperience(
        id="exp1",
        company="ACME",
        actual_title="Software Engineer",
        safe_title_aliases=["Software Engineer", "Backend Engineer"],
        score=0.80,
        alias_score=0.75,
        end_date="present",
        bullets=[
            SelectedBullet("b1", "Did X", 0.9),
            SelectedBullet("b2", "Did Y", 0.8),
            SelectedBullet("b3", "Did Z", 0.7),
        ],
    )
    proj = SelectedProject(
        id="proj1",
        name="My Project",
        link="https://github.com/user/proj",
        score=0.70,
        bullets=[
            SelectedBullet("pb1", "Built A", 0.8),
            SelectedBullet("pb2", "Built B", 0.7),
        ],
    )
    summary = SummaryCand(
        id="sum1", text="Backend engineer with 1.5 years experience.",
        role_categories=["backend"], embedding=[0.0],
    )
    return SelectionResult(
        apply=True,
        final_score=0.72,
        fit=0.70,
        success_prob=0.80,
        recency=0.60,
        project_score=0.70,
        summary=summary,
        summary_score=0.65,
        experiences=[exp],
        projects=[proj],
        skill_candidates=[
            ("Python", 0.9), ("FastAPI", 0.85), ("PostgreSQL", 0.80),
            ("Docker", 0.75), ("Redis", 0.70), ("Kafka", 0.65),
            ("Celery", 0.60), ("Linux", 0.55), ("Git", 0.50),
            ("REST", 0.45), ("SQL", 0.43), ("pytest", 0.40),
            ("SQLAlchemy", 0.38), ("asyncio", 0.35),
        ],
        skills_before_projects=skills_before_projects,
    )


def _make_profile() -> Profile:
    return Profile(
        experiences=[
            ExperienceCand(
                id="exp1",
                company="ACME",
                actual_title="Software Engineer",
                safe_title_aliases=["Software Engineer", "Backend Engineer"],
                alias_embeddings=[[0.0]],
                end_date="present",
                bullets=[BulletCand("b1", "Did X", [0.0]),
                         BulletCand("b2", "Did Y", [0.0]),
                         BulletCand("b3", "Did Z", [0.0])],
            )
        ],
        projects=[
            ProjectCand(
                id="proj1", name="My Project",
                link="https://github.com/user/proj",
                name_embedding=[0.0],
                bullets=[BulletCand("pb1", "Built A", [0.0]),
                         BulletCand("pb2", "Built B", [0.0])],
            )
        ],
        summaries=[
            SummaryCand(
                id="sum1", text="Backend engineer.",
                role_categories=["backend"], embedding=[0.0],
            )
        ],
        skills=[SkillCand(s, [0.0]) for s in [
            "Python", "FastAPI", "PostgreSQL", "Docker", "Redis", "Kafka",
            "Celery", "Linux", "Git", "REST", "SQL", "pytest", "SQLAlchemy",
            "asyncio",
        ]],
    )


# ---------------------------------------------------------------------------
# choose_title_alias — replaces the LLM's title_choices
# ---------------------------------------------------------------------------

def test_title_alias_picks_the_closest_option_to_the_jd():
    """A backend-flavoured JD should pull the backend alias, not the generic one."""
    aliases = ["Data Scientist", "Backend Engineer", "Frontend Developer"]
    jd_vec = embed("Build REST APIs and backend services in Python and FastAPI.")

    assert choose_title_alias(aliases, jd_vec, fallback="X") == "Backend Engineer"


def test_title_alias_can_only_return_an_allowed_value():
    """Hard rule #6 is now structural, not validated after the fact.

    The old Call 1b could return any string and had to be constrained with
    Literal plus post-validation. An argmax over the list cannot produce an
    out-of-set title at all.
    """
    aliases = ["Software Engineer", "Backend Engineer"]
    for jd in ["quantum astrologer", "chief pizza officer", ""]:
        assert choose_title_alias(aliases, embed(jd or " "), fallback="F") in aliases


def test_title_alias_falls_back_when_no_aliases_exist():
    assert choose_title_alias([], embed("anything"), fallback="Actual Title") == "Actual Title"


def test_title_alias_is_deterministic():
    """Same inputs, same output — the LLM version varied between runs."""
    aliases = ["Software Engineer", "Backend Engineer", "Platform Engineer"]
    jd_vec = embed("Backend services, Python, Postgres.")

    picks = {choose_title_alias(aliases, jd_vec, fallback="X") for _ in range(3)}
    assert len(picks) == 1


# ---------------------------------------------------------------------------
# assign_skill_categories — replaces the LLM's skills_selection
# ---------------------------------------------------------------------------

CANDIDATES = [
    ("Python", 0.9), ("FastAPI", 0.85), ("PostgreSQL", 0.80),
    ("Docker", 0.75), ("Redis", 0.70), ("Kafka", 0.65),
    ("Celery", 0.60), ("Linux", 0.55), ("Git", 0.50),
    ("REST", 0.45), ("SQL", 0.43), ("pytest", 0.40),
    ("SQLAlchemy", 0.38), ("asyncio", 0.35),
]


def test_every_skill_comes_from_the_candidate_list():
    """Membership can no longer drift: nothing invents a skill."""
    out = assign_skill_categories(CANDIDATES, gap_skills=[])

    placed = [s for c in out.categories for s in c.skills]
    assert set(placed) <= {s for s, _ in CANDIDATES}


def test_category_names_come_from_the_taxonomy_only():
    """A heading outside the configured taxonomy is impossible."""
    out = assign_skill_categories(CANDIDATES, gap_skills=[])

    allowed = set(settings.selection.skills.taxonomy.as_dict())
    assert {c.name for c in out.categories} <= allowed


def test_no_skill_appears_in_two_categories():
    out = assign_skill_categories(CANDIDATES, gap_skills=[])

    placed = [s for c in out.categories for s in c.skills]
    assert len(placed) == len(set(placed))


def test_categories_respect_the_per_category_maximum():
    out = assign_skill_categories(CANDIDATES, gap_skills=[])

    cap = int(settings.selection.skills.skills_per_category_max)
    assert all(len(c.skills) <= cap for c in out.categories)


def test_categories_are_ordered_by_jd_relevance():
    """The group containing the strongest-matching skill must lead."""
    out = assign_skill_categories(CANDIDATES, gap_skills=[])
    scores = dict(CANDIDATES)

    bests = [max(scores[s] for s in c.skills) for c in out.categories]
    assert bests == sorted(bests, reverse=True)


def test_only_the_configured_number_of_categories_is_shown():
    out = assign_skill_categories(CANDIDATES, gap_skills=[])

    assert len(out.categories) <= int(settings.selection.skills.categories_count)


def test_real_pool_skills_land_in_their_taxonomy_category():
    """Grouping must be exact for skills the operator actually has.

    Embedding similarity got these wrong (pytest under "Cloud", Grafana under
    "Web & Frontend"), which is why the taxonomy exists.
    """
    from src.builder.deterministic import _taxonomy

    taxonomy = _taxonomy()
    cands = [
        ("Python", 0.92), ("FastAPI", 0.90), ("REST API Development", 0.88),
        ("PostgreSQL", 0.82), ("Redis", 0.78), ("JWT Authentication", 0.66),
    ]
    out = assign_skill_categories(cands, gap_skills=[])

    for category in out.categories:
        for skill in category.skills:
            assert taxonomy[skill.casefold()] == category.name


def test_headings_differ_between_a_backend_and_a_data_job():
    """The operator's actual objection: not the same three headings every time."""
    backend = assign_skill_categories([
        ("Python", .92), ("FastAPI", .90), ("Backend Development", .88),
        ("REST API Development", .86), ("PostgreSQL", .80), ("Redis", .76),
    ], gap_skills=[])
    data = assign_skill_categories([
        ("Python", .92), ("ETL Pipelines", .90), ("Data Engineering", .88),
        ("Data Ingestion", .86), ("pandas", .80), ("NumPy", .76),
    ], gap_skills=[])

    assert {c.name for c in backend.categories} != {c.name for c in data.categories}


def test_an_unknown_skill_is_still_placed_and_flagged():
    """A skill not yet in the taxonomy must not vanish from the resume."""
    out = assign_skill_categories(
        [("Python", 0.9), ("SomeBrandNewTool", 0.85), ("PostgreSQL", 0.8)],
        gap_skills=[],
    )

    placed = [s for c in out.categories for s in c.skills]
    assert "SomeBrandNewTool" in placed


def test_skills_within_a_category_are_ordered_by_jd_match():
    out = assign_skill_categories(CANDIDATES, gap_skills=[])
    scores = dict(CANDIDATES)

    for category in out.categories:
        got = [scores[s] for s in category.skills]
        assert got == sorted(got, reverse=True)


def test_gap_skills_become_familiar_with_and_are_capped():
    gaps = ["Rust", "Kubernetes", "Terraform", "Scala", "Elixir", "Go"]
    out = assign_skill_categories(CANDIDATES, gap_skills=gaps)

    cap = int(settings.selection.skills.familiar_with_max)
    assert len(out.familiar_with) == cap
    assert set(out.familiar_with) <= set(gaps)


def test_empty_categories_are_dropped_not_padded():
    """Two skills cannot fill three groups; we ship fewer rather than filler."""
    out = assign_skill_categories([("Python", 0.9), ("FastAPI", 0.8)], gap_skills=[])

    assert all(c.skills for c in out.categories)
    assert sum(len(c.skills) for c in out.categories) == 2


def test_no_candidates_yields_an_empty_selection():
    out = assign_skill_categories([], gap_skills=["Rust"])

    assert out.categories == []


# ---------------------------------------------------------------------------
# build() — the whole selection, with no LLM
# ---------------------------------------------------------------------------

def test_build_produces_a_selection_without_any_llm_call():
    """The point of the change: a matched job costs zero Call-1b requests."""
    selection = build_selection(
        result=_make_result(),
        profile=_make_profile(),
        jd_role_summary="Backend Python engineer building REST APIs.",
        jd_required_skills=["Python", "FastAPI", "Kubernetes"],
    )

    assert isinstance(selection, StoredSelection)
    assert selection.experiences[0].exp_id == "exp1"
    assert selection.experiences[0].bullet_ids == ["b1", "b2", "b3"]
    assert selection.projects[0].link == "https://github.com/user/proj"
    assert selection.summary_id == "sum1"
    assert selection.skills.categories


def test_build_chooses_a_title_from_the_experience_allow_list():
    selection = build_selection(
        result=_make_result(),
        profile=_make_profile(),
        jd_role_summary="Backend services in Python.",
        jd_required_skills=["Python"],
    )

    assert selection.experiences[0].title_alias in [
        "Software Engineer", "Backend Engineer",
    ]


def test_build_surfaces_kubernetes_as_a_gap():
    """A required skill absent from the pool must reach Familiar With."""
    selection = build_selection(
        result=_make_result(),
        profile=_make_profile(),
        jd_role_summary="Backend engineer.",
        jd_required_skills=["Python", "Kubernetes"],
    )

    assert "Kubernetes" in selection.skills.familiar_with
    assert "Python" not in selection.skills.familiar_with


def test_build_leaves_cover_letter_empty():
    """The cover letter was generated then never read by anything; it's gone."""
    selection = build_selection(
        result=_make_result(),
        profile=_make_profile(),
        jd_role_summary="Backend engineer.",
        jd_required_skills=["Python"],
    )

    assert selection.cover_letter_text == ""


def test_build_ignores_a_passed_complete_fn():
    """Callers may still pass one; it must not be invoked."""
    def explode(*args, **kwargs):
        raise AssertionError("no LLM call should happen in Layer 5")

    selection = build_selection(
        result=_make_result(),
        profile=_make_profile(),
        jd_role_summary="Backend engineer.",
        jd_required_skills=["Python"],
        complete_fn=explode,
    )

    assert selection is not None


def test_build_section_order_follows_the_layer_4_decision():
    for flag, expected in [
        (True, ["Work", "Skills", "Projects"]),
        (False, ["Work", "Projects", "Skills"]),
    ]:
        selection = build_selection(
            result=_make_result(skills_before_projects=flag),
            profile=_make_profile(),
            jd_role_summary="Backend engineer.",
            jd_required_skills=["Python"],
        )
        assert selection.section_order == expected


def test_build_fails_when_no_experience_was_selected():
    result = _make_result()
    result.experiences = []

    assert build_selection(
        result=result,
        profile=_make_profile(),
        jd_role_summary="Backend engineer.",
        jd_required_skills=["Python"],
    ) is None


def test_build_is_deterministic_across_runs():
    """Two builds of the same job must produce identical selections."""
    kwargs = dict(
        profile=_make_profile(),
        jd_role_summary="Backend Python engineer building REST APIs.",
        jd_required_skills=["Python", "FastAPI", "Kubernetes"],
    )
    first = build_selection(result=_make_result(), **kwargs)
    second = build_selection(result=_make_result(), **kwargs)

    assert first.experiences == second.experiences
    assert first.skills == second.skills
    assert first.section_order == second.section_order
