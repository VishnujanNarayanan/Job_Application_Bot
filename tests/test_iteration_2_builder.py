"""Layer 5 — the deterministic builder.

Title alias used to come from Gemini; it is now computed from embeddings, so
these tests hit real sentence-transformer maths (no mock). The
`assign_skill_categories` suite that used to live here went with the Skills
section — the Headless template has no skills list to group.
"""

from __future__ import annotations

import pytest

from src.builder.deterministic import choose_title_alias
from src.builder.llm_call import build as build_selection
from src.llm.schemas import StoredSelection
from src.scorer.apply_decision import SelectionResult
from src.scorer.embeddings import embed
from src.scorer.selector import (
    BulletCand,
    EntryCand,
    Profile,
    RoleBlockCand,
    SelectedBullet,
    SelectedEntry,
    SkillCand,
)

V = [1.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _selected(eid="exp1", kind="work", block=None, bullets=("b1", "b2", "b3")):
    return SelectedEntry(
        id=eid, kind=kind, block_id=block or f"{eid}::backend", label="ACME",
        header_left="Backend Engineer at ACME, Pune",
        header_right="Jan 2024 to current" if kind == "work" else "https://github.com/user/proj",
        bullets=[SelectedBullet(b, f"Did {b}", 0.9, is_summary=(i == 0))
                 for i, b in enumerate(bullets)],
        covered={"Python"}, coverage=0.5, similarity=0.8, score=0.80, cap=6,
        end_date="present",
    )


def _make_result() -> SelectionResult:
    work = _selected()
    proj = _selected("proj1", "project", "proj1::backend", ("pb1", "pb2"))
    return SelectionResult(
        apply=True, final_score=0.72, fit=0.70, success_prob=0.80,
        recency=0.60, project_score=0.70,
        entries=[work, proj], work=[work], projects=[proj],
        keyword_coverage=0.55, lead_entry_coverage=0.50,
    )


def _block(block_id, aliases):
    return RoleBlockCand(
        block_id=block_id, role="backend", role_fit="primary",
        entry_header="Backend Engineer at ACME, Pune",
        entry_dates="Jan 2024 to current", checklist=("Python SWE",),
        title_aliases=list(aliases),
        alias_embeddings=[embed(a) for a in aliases],
        bullets=[BulletCand("b1", "Did b1", V, block_id=block_id, is_summary=True)],
    )


def _make_profile() -> Profile:
    return Profile(
        work=[EntryCand(
            id="exp1", kind="work", label="ACME",
            blocks=[_block("exp1::backend", ["Software Engineer", "Backend Engineer"])],
            actual_title="Software Engineer",
            safe_title_aliases=["Software Engineer", "Backend Engineer"],
            start_date="2024-01", end_date="present",
        )],
        projects=[EntryCand(
            id="proj1", kind="project", label="My Project",
            blocks=[_block("proj1::backend", ["Backend Engineer"])],
            link="https://github.com/user/proj",
        )],
        skills=[SkillCand(s, embed(s)) for s in ("Python", "FastAPI", "PostgreSQL")],
    )


# ---------------------------------------------------------------------------
# choose_title_alias
# ---------------------------------------------------------------------------


def test_title_alias_picks_the_closest_option_to_the_jd():
    aliases = ["Data Engineer", "Backend Engineer", "Machine Learning Engineer"]
    jd_vec = embed("We need a backend engineer to build REST APIs in Python.")
    assert choose_title_alias(aliases, jd_vec, fallback="X") == "Backend Engineer"


def test_title_alias_can_only_return_an_allowed_value():
    """Hard rule #6 — the display title is never free text."""
    aliases = ["Data Engineer", "Backend Engineer"]
    jd_vec = embed("Senior Staff Principal Distinguished Architect of Everything")
    assert choose_title_alias(aliases, jd_vec, fallback="X") in aliases


def test_title_alias_falls_back_when_no_aliases_exist():
    assert choose_title_alias([], embed("anything"), fallback="Actual Title") == "Actual Title"


def test_title_alias_is_deterministic():
    aliases = ["Data Engineer", "Backend Engineer"]
    jd_vec = embed("Backend Python services.")
    assert len({choose_title_alias(aliases, jd_vec, fallback="X") for _ in range(3)}) == 1


# ---------------------------------------------------------------------------
# build() — the whole selection, with no LLM
# ---------------------------------------------------------------------------


def test_build_produces_a_selection_without_any_llm_call():
    """The point of the change: a matched job costs zero Call-1b requests."""
    selection = build_selection(
        result=_make_result(), profile=_make_profile(),
        jd_role_summary="Backend Python engineer building REST APIs.",
        jd_required_skills=["Python", "FastAPI", "Kubernetes"],
    )
    assert isinstance(selection, StoredSelection)
    assert selection.version == 2
    assert selection.entries[0].entry_id == "exp1"
    assert selection.entries[0].bullet_ids == ["b1", "b2", "b3"]
    assert [e.kind for e in selection.entries] == ["work", "project"]


def test_build_chooses_a_title_from_the_blocks_allow_list():
    selection = build_selection(
        result=_make_result(), profile=_make_profile(),
        jd_role_summary="Backend services in Python.",
        jd_required_skills=["Python"],
    )
    assert selection.entries[0].title_alias in ["Software Engineer", "Backend Engineer"]


def test_build_carries_the_coverage_measurements():
    selection = build_selection(
        result=_make_result(), profile=_make_profile(),
        jd_role_summary="Backend engineer.", jd_required_skills=["Python"],
    )
    assert selection.keyword_coverage == pytest.approx(0.55)
    assert selection.lead_entry_coverage == pytest.approx(0.50)


def test_build_puts_the_project_link_in_the_header_right_slot():
    selection = build_selection(
        result=_make_result(), profile=_make_profile(),
        jd_role_summary="Backend engineer.", jd_required_skills=["Python"],
    )
    project = selection.project_entries()[0]
    assert project.header_right == "https://github.com/user/proj"


def test_build_leaves_cover_letter_empty():
    """The cover letter was generated then never read by anything; it's gone."""
    selection = build_selection(
        result=_make_result(), profile=_make_profile(),
        jd_role_summary="Backend engineer.", jd_required_skills=["Python"],
    )
    assert selection.cover_letter_text == ""


def test_build_ignores_a_passed_complete_fn():
    """Callers may still pass one; it must not be invoked."""
    def explode(*args, **kwargs):
        raise AssertionError("no LLM call should happen in Layer 5")

    assert build_selection(
        result=_make_result(), profile=_make_profile(),
        jd_role_summary="Backend engineer.", jd_required_skills=["Python"],
        complete_fn=explode,
    ) is not None


def test_build_fails_when_no_work_entry_was_selected():
    result = _make_result()
    result.entries = [e for e in result.entries if e.kind != "work"]
    result.work = []

    assert build_selection(
        result=result, profile=_make_profile(),
        jd_role_summary="Backend engineer.", jd_required_skills=["Python"],
    ) is None


def test_build_is_deterministic_across_runs():
    kwargs = dict(
        profile=_make_profile(),
        jd_role_summary="Backend Python engineer building REST APIs.",
        jd_required_skills=["Python", "FastAPI", "Kubernetes"],
    )
    first = build_selection(result=_make_result(), **kwargs)
    second = build_selection(result=_make_result(), **kwargs)
    assert first.entries == second.entries
