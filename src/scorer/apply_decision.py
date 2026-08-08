"""Layer 4 — final scoring + apply/skip decision.

Combines the deterministic selections (selector.py) into the final score and
the build-or-skip decision. Pure: ``evaluate(profile, jd, now=...)`` takes the
candidate pool + JD context and returns a :class:`SelectionResult` — no DB,
LLM, model, or network. The orchestrator loads the profile, calls this, and
(for every match >= threshold) hands the result to Layer 5; there are NO
quotas and NO top-N picking (CLAUDE.md hard rule #14).

Formulas (CLAUDE.md "FINAL" + config.scoring):

    fit          = best_experience*0.50 + selected_summary*0.20
                   + avg_skill_pool_match*0.30
    success_prob = seniority*0.60 + recency*0.40
    recency      = banded on hours since posted
    final        = fit*0.55 + success_prob*0.30 + recency*0.10 + project*0.05
    apply        = final >= 0.50
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import settings
from src.reasons import LOW_SCORE
from src.scorer.ordering import order_experiences, skills_before_projects
from src.scorer.selector import (
    JDContext,
    Profile,
    SelectedExperience,
    SelectedProject,
    SummaryCand,
    select_experiences,
    select_projects,
    select_skill_candidates,
    select_summary,
)


@dataclass
class SelectionResult:
    """Everything Layer 5 needs to build a selection_json, plus the scores."""

    apply: bool
    final_score: float
    fit: float
    success_prob: float
    recency: float
    project_score: float
    summary: SummaryCand | None
    summary_score: float
    experiences: list[SelectedExperience]
    projects: list[SelectedProject]
    skill_candidates: list[tuple[str, float]]
    skills_before_projects: bool
    reason_category: str | None = None


def seniority_score(role_level: str | None) -> float:
    """Map a Gemini-parsed role_level to its seniority weight (config)."""
    scores = settings.scoring.success_prob.seniority_scores.as_dict()
    # YAML `null:` parses to a None key — used when role_level is unknown.
    return float(scores.get(role_level, scores.get(None, 0.80)))


def recency_score(posted_at: datetime | None, now: datetime) -> float:
    """Band the hours since posting into a recency score.

    Both the band edges and their scores come from
    ``scoring.recency_score.bands`` — they used to be hardcoded at 1/3/6/12
    hours here, which meant the bands could not be resized when the scrape
    window changed. A listing older than every band (or with no timestamp at
    all) gets ``default``.
    """
    cfg = settings.scoring.recency_score
    if posted_at is None:
        return float(cfg.default)

    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    hours = (now - posted_at).total_seconds() / 3600.0

    # Ascending by edge, so a mis-ordered config still behaves sanely.
    for band in sorted(cfg.bands, key=lambda b: float(b["under_hours"])):
        if hours < float(band["under_hours"]):
            return float(band["score"])
    return float(cfg.default)


def evaluate(
    profile: Profile, jd: JDContext, *, now: datetime | None = None
) -> SelectionResult:
    """Score one job against the profile and decide build-or-skip."""
    now = now or datetime.now(timezone.utc)

    experiences = order_experiences(select_experiences(profile.experiences, jd))
    projects = select_projects(profile.projects, jd)
    summary, summary_score = select_summary(profile.summaries, jd)
    skill_candidates = select_skill_candidates(profile.skills, jd)

    best_experience = max((e.score for e in experiences), default=0.0)
    best_project = max((p.score for p in projects), default=0.0)
    avg_skill = (
        sum(s for _, s in skill_candidates) / len(skill_candidates)
        if skill_candidates
        else 0.0
    )

    fit_cfg = settings.scoring.fit
    fit = (
        fit_cfg.best_experience * best_experience
        + fit_cfg.selected_summary * summary_score
        + fit_cfg.avg_skill_pool_match * avg_skill
    )

    sp_cfg = settings.scoring.success_prob
    recency = recency_score(jd.posted_at, now)
    success_prob = (
        sp_cfg.weight_seniority * seniority_score(jd.role_level)
        + sp_cfg.weight_recency * recency
    )

    final_cfg = settings.scoring.final
    final_score = (
        final_cfg.fit * fit
        + final_cfg.success_prob * success_prob
        + final_cfg.recency * recency
        + final_cfg.project * best_project
    )

    apply = final_score >= settings.scoring.apply_threshold
    return SelectionResult(
        apply=apply,
        final_score=final_score,
        fit=fit,
        success_prob=success_prob,
        recency=recency,
        project_score=best_project,
        summary=summary,
        summary_score=summary_score,
        experiences=experiences,
        projects=projects,
        skill_candidates=skill_candidates,
        skills_before_projects=skills_before_projects(skill_candidates, projects),
        reason_category=None if apply else LOW_SCORE,
    )
