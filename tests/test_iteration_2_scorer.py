"""Iteration 2 — Phase B Layer 4: deterministic selection + scoring.

Fully offline and synthetic — no DB, no model, no LLM. Embeddings are
hand-built unit vectors so cosine values are exact and the selection rules
(thresholds, force-include, match-then-recency, final-score formula) are
asserted precisely. Uses the real config thresholds from config.yaml.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import settings
from src.scorer.apply_decision import evaluate, recency_score, seniority_score
from src.scorer.ordering import _recency_key, order_experiences, skills_before_projects
from src.scorer.selector import (
    BulletCand,
    ExperienceCand,
    JDContext,
    Profile,
    ProjectCand,
    SelectedExperience,
    SkillCand,
    SummaryCand,
    select_experiences,
    select_projects,
    select_skill_candidates,
    select_summary,
)

# Two orthogonal "topics" in 2-D space. A candidate aligned with the JD
# vector [1, 0] scores cosine 1.0; an orthogonal one scores 0.0.
ALIGNED = [1.0, 0.0]
OFF = [0.0, 1.0]


def jd(vec=ALIGNED, *, role_category="data", role_level="junior", posted_at=None):
    """A JDContext with all query facets set to ``vec`` (the common case); a
    single JD skill vector. Tests that need to distinguish facets pass an
    explicit JDContext."""
    return JDContext(
        vec_role=vec, vec_match=vec, jd_skill_vecs=(vec,),
        role_category=role_category, role_level=role_level, posted_at=posted_at,
    )


JD = jd()


def _bullets(prefix: str, vecs: list[list[float]]) -> list[BulletCand]:
    return [BulletCand(f"{prefix}{i}", f"text {prefix}{i}", v) for i, v in enumerate(vecs)]


# ---------------------------------------------------------------------------
# Experience selection
# ---------------------------------------------------------------------------


def test_experience_score_blends_alias_and_bullets() -> None:
    from src.scorer.selector import score_experience

    exp = ExperienceCand(
        id="e1", company="Acme", actual_title="Data Engineer",
        safe_title_aliases=["Data Engineer"], alias_embeddings=[ALIGNED],
        end_date="present", bullets=_bullets("b", [ALIGNED, ALIGNED, ALIGNED]),
    )
    score, alias_score, top = score_experience(exp, JD)
    cfg = settings.selection.experience
    assert alias_score == pytest.approx(1.0)
    assert len(top) == cfg.bullets_per_experience
    assert score == pytest.approx(cfg.weight_alias + cfg.weight_bullets)  # 1.0


def test_experience_force_include_two_below_threshold() -> None:
    # Both experiences score 0 (everything orthogonal) — below the 0.45
    # threshold — but min_shown forces the top-2 to appear anyway.
    weak = [
        ExperienceCand(f"e{i}", f"Co{i}", "Eng", ["Eng"], [OFF], "2020-01",
                       _bullets(f"b{i}", [OFF, OFF, OFF]))
        for i in range(3)
    ]
    selected = select_experiences(weak, JD)
    assert len(selected) == settings.selection.experience.min_shown  # 2


def test_experience_caps_at_max_shown() -> None:
    strong = [
        ExperienceCand(f"e{i}", f"Co{i}", "Data Engineer", ["Data Engineer"],
                       [ALIGNED], "2021-01", _bullets(f"b{i}", [ALIGNED, ALIGNED, ALIGNED]))
        for i in range(5)
    ]
    selected = select_experiences(strong, JD)
    assert len(selected) == settings.selection.experience.max_shown  # 3


# ---------------------------------------------------------------------------
# Match-then-recency ordering
# ---------------------------------------------------------------------------


def _sel_exp(id_: str, score: float, end_date: str) -> SelectedExperience:
    return SelectedExperience(id_, "Co", "T", ["T"], score, score, end_date, [])


def test_order_experiences_best_match_first_when_gap_large() -> None:
    # gap 0.9 - 0.1 = 0.8 > 0.20 → best match leads even though it's older.
    a = _sel_exp("old_strong", 0.9, "2019-01")
    b = _sel_exp("new_weak", 0.1, "2024-01")
    assert [e.id for e in order_experiences([b, a])] == ["old_strong", "new_weak"]


def test_order_experiences_recency_when_gap_small() -> None:
    # gap 0.50 - 0.45 = 0.05 <= 0.20 → pure recency order.
    a = _sel_exp("strong_old", 0.50, "2019-01")
    b = _sel_exp("weak_new", 0.45, "2024-01")
    assert [e.id for e in order_experiences([a, b])] == ["weak_new", "strong_old"]


def test_recency_key_present_sorts_newest() -> None:
    assert _recency_key("present") > _recency_key("2025-12")
    assert _recency_key("2024-06") > _recency_key("2024-01")
    assert _recency_key("garbage") == (0, 0)


# ---------------------------------------------------------------------------
# Project selection (never hidden; force-include 2)
# ---------------------------------------------------------------------------


def test_projects_always_at_least_min_shown() -> None:
    weak = [
        ProjectCand(f"p{i}", f"Proj{i}", "http://x", OFF, _bullets(f"b{i}", [OFF, OFF]))
        for i in range(3)
    ]
    selected = select_projects(weak, JD)
    assert len(selected) >= settings.selection.project.min_shown  # never hidden


def test_project_bullets_min_two_descending() -> None:
    proj = ProjectCand(
        "p1", "Pipeline", "http://x", ALIGNED,
        _bullets("b", [ALIGNED, OFF, OFF]),  # only 1 strong bullet
    )
    selected = select_projects([proj], JD)[0]
    assert len(selected.bullets) >= settings.selection.project.bullet_min
    scores = [b.score for b in selected.bullets]
    assert scores == sorted(scores, reverse=True)  # descending


# ---------------------------------------------------------------------------
# Summary (deterministic, category-first)
# ---------------------------------------------------------------------------


def test_summary_prefers_category_match() -> None:
    # The off-category summary aligns better with the JD vector, but the
    # category gate picks the data-tagged one first.
    data_sum = SummaryCand("s_data", "data", ["data"], [0.7, 0.7])
    backend_sum = SummaryCand("s_be", "backend", ["backend"], ALIGNED)
    picked, score = select_summary([backend_sum, data_sum], JD)
    assert picked.id == "s_data"


def test_summary_fallback_to_all_when_no_category_match() -> None:
    s = SummaryCand("s1", "x", ["backend"], ALIGNED)
    picked, score = select_summary([s], JD)  # JD is "data" — no match
    assert picked.id == "s1"  # fallback returns the best overall
    assert score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Skills candidates
# ---------------------------------------------------------------------------


def test_skill_candidates_ranked_and_capped() -> None:
    skills = [SkillCand(f"s{i}", ALIGNED if i < 20 else OFF) for i in range(30)]
    cands = select_skill_candidates(skills, JD)
    assert len(cands) == settings.selection.skills.top_candidates  # 14
    assert all(score == pytest.approx(1.0) for _, score in cands)  # best first


# ---------------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------------


def test_seniority_score_from_config_and_unknown() -> None:
    assert seniority_score("junior") == 1.0
    assert seniority_score("lead") == 0.15
    assert seniority_score(None) == 0.80  # YAML null key → unknown == mid


def test_recency_score_bands() -> None:
    """Bands come from config (6h / 24h / 72h), first match wins."""
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    assert recency_score(now - timedelta(minutes=30), now) == 1.00
    assert recency_score(now - timedelta(hours=5), now) == 1.00
    assert recency_score(now - timedelta(hours=7), now) == 0.60
    assert recency_score(now - timedelta(hours=23), now) == 0.60
    assert recency_score(now - timedelta(hours=30), now) == 0.40
    assert recency_score(now - timedelta(days=5), now) == 0.30   # past every band


def test_an_undated_listing_is_dated_from_when_it_was_scraped() -> None:
    """No posted_at is the norm, not the exception — infer, don't punish.

    LinkedIn gave a posting date on 1 of 472 listings and is currently the only
    enabled source. Scoring those at `default` (0.30, "older than every band")
    charged almost every job the maximum age penalty for its portal's missing
    metadata: every job on the live run of 2026-08-09 reported recency 0.30,
    and two would otherwise have crossed the threshold.

    The scrape window makes the inference sound — JobSpy only returns postings
    younger than `hours_old` — so an undated listing is treated as half a
    window old at the moment it was seen.
    """
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)

    # Scraped just now through a 24h window → effective age 12h → the <24 band.
    assert recency_score(None, now, scraped_at=now, window_hours=24) == 0.60
    # A narrower window puts the same listing in the freshest band.
    assert recency_score(None, now, scraped_at=now, window_hours=6) == 1.00

    # Elapsed time still dominates: a row scraped six weeks ago is old, which
    # is why backfilling stale listings must NOT resurrect them as fresh.
    old = now - timedelta(days=42)
    assert recency_score(None, now, scraped_at=old, window_hours=24) == 0.30


def test_no_timestamp_at_all_scores_neutral_not_worst() -> None:
    """Absence of evidence is not evidence of staleness.

    With neither posted_at nor scraped_at there is nothing to infer, so the
    job must not inherit `default` — that is the score for a listing MEASURED
    to be older than every band, a verdict this job never earned.
    """
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    unknown = recency_score(None, now)
    assert unknown == 0.65
    assert unknown > 0.30, "an unmeasured job must not be scored as the oldest"
    assert unknown < 1.00, "nor as the freshest — it is neutral, not a bonus"


def test_recency_discriminates_across_the_scrape_window() -> None:
    """The bands must actually separate jobs inside `scraper.hours_old`.

    Regression for the live run of 2026-08-08: the old bands topped out at
    "over_12h", so once the lookback widened to 24h every scraped job landed in
    the final band and recency became a constant 0.20 instead of a signal —
    silently docking every job up to 0.08 of final score.
    """
    from src.config import settings

    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    window = float(settings.scraper.hours_old.peak)
    scores = {
        recency_score(now - timedelta(hours=h), now)
        for h in (0.5, window / 4, window / 2, window - 0.5)
    }

    assert len(scores) > 1, "recency does not vary within the scrape window"


def test_recency_bands_are_read_in_ascending_order() -> None:
    """A mis-ordered config must still band correctly."""
    from unittest.mock import patch

    from src.scorer import apply_decision as ad

    class FakeCfg:
        bands = [
            {"under_hours": 72, "score": 0.40},
            {"under_hours": 6, "score": 1.00},
            {"under_hours": 24, "score": 0.60},
        ]
        default = 0.30

    fake = type("S", (), {"scoring": type("SC", (), {"recency_score": FakeCfg})})
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    with patch.object(ad, "settings", fake):
        assert ad.recency_score(now - timedelta(hours=2), now) == 1.00
        assert ad.recency_score(now - timedelta(hours=10), now) == 0.60


# ---------------------------------------------------------------------------
# evaluate() — end-to-end scoring + decision
# ---------------------------------------------------------------------------


def _full_profile(vec: list[float]) -> Profile:
    return Profile(
        experiences=[
            ExperienceCand("e1", "Acme", "Data Engineer", ["Data Engineer"],
                           [vec], "present", _bullets("eb", [vec, vec, vec])),
            ExperienceCand("e2", "Beta", "Data Engineer", ["Data Engineer"],
                           [vec], "2022-01", _bullets("eb2", [vec, vec, vec])),
        ],
        projects=[
            ProjectCand("p1", "Pipeline", "http://x", vec, _bullets("pb", [vec, vec, vec])),
            ProjectCand("p2", "ETL", "http://y", vec, _bullets("pb2", [vec, vec])),
        ],
        summaries=[SummaryCand("s1", "data eng", ["data"], vec)],
        skills=[SkillCand(f"s{i}", vec) for i in range(14)],
    )


def test_evaluate_strong_match_applies() -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    ctx = jd(ALIGNED, role_level="junior", posted_at=now - timedelta(minutes=10))
    result = evaluate(_full_profile(ALIGNED), ctx, now=now)
    assert result.apply is True
    assert result.final_score >= settings.scoring.apply_threshold
    assert result.reason_category is None
    assert len(result.experiences) >= settings.selection.experience.min_shown
    assert len(result.skill_candidates) == settings.selection.skills.top_candidates


def test_evaluate_weak_match_skips_with_low_score() -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    # Everything orthogonal to the JD, posted long ago, senior role.
    ctx = jd(ALIGNED, role_level="senior", posted_at=now - timedelta(days=5))
    result = evaluate(_full_profile(OFF), ctx, now=now)
    assert result.apply is False
    assert result.reason_category == "LOW_SCORE"
    assert result.final_score < settings.scoring.apply_threshold


def test_evaluate_skills_before_projects_flag() -> None:
    now = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
    ctx = jd(ALIGNED, posted_at=now)
    # Skills aligned, projects orthogonal → skills section leads.
    profile = _full_profile(ALIGNED)
    profile.projects = [
        ProjectCand("p1", "Pipeline", "http://x", OFF, _bullets("pb", [OFF, OFF]))
    ]
    result = evaluate(profile, ctx, now=now)
    assert result.skills_before_projects is True


# ---------------------------------------------------------------------------
# build_jd_context — the three query vectors (architecture §4.1)
# ---------------------------------------------------------------------------


def test_build_jd_context_three_vectors() -> None:
    from src.llm.schemas import JDParsed
    from src.scorer.selector import build_jd_context

    parsed = JDParsed(
        role_summary="Backend role.", role_category="backend", role_level="mid",
        years_required=2, required_skills=["Python"], nice_to_have=["AWS"],
        responsibilities=["Build APIs"],
    )
    # Stub embed_batch returns one vector per input text, in call order:
    # [blended_skills, resp, role, Python, AWS].
    vectors = iter([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5], [0.9, 0.1], [0.2, 0.8]])
    seen = {}

    def _eb(texts):
        seen["texts"] = texts
        return [next(vectors) for _ in texts]

    ctx = build_jd_context(parsed, embed_batch_fn=_eb)
    assert ctx.vec_role == [0.5, 0.5]
    assert ctx.vec_match == [1.0, 1.0]   # blended_skills + vec_resp
    assert ctx.role_level == "mid"
    # Per-skill vectors kept individually (one per required + nice_to_have).
    assert ctx.jd_skill_vecs == ([0.9, 0.1], [0.2, 0.8])
    # Batch order: blended skills text first, then individual skills appended.
    assert "Python" in seen["texts"][0] and "AWS" in seen["texts"][0]
    assert seen["texts"][3] == "Python" and seen["texts"][4] == "AWS"
