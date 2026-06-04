"""Layer 4 — pure selection functions (experience, project, summary, skills).

All selection is deterministic sentence-transformers cosine math against the
JD embedding — NO LLM (the LLM only names skill categories and picks a title
alias later, in Layer 5 Call 1b). Every tunable comes from
``config.selection`` / ``config.scoring``; nothing is hardcoded.

Inputs are in-memory candidate dataclasses (the master_profile rebuild loads
these from the DB with embeddings already attached). Keeping the functions
pure means they unit-test exhaustively with synthetic profiles + JDs, with no
DB, model, or network.

Selection rules (CLAUDE.md "Selection rules — locked"):

    EXPERIENCE  score = alias*0.30 + top3_bullet_avg*0.70
                threshold 0.45, max 3, min 2 (force-include top-2)
                bullets: top 3 by score
    PROJECT     score = name*0.20 + topN_bullet_avg*0.80
                threshold 0.50, max 3, min 2 (force-include), never hidden
                bullets: >= floor, min 2, max 3, descending
    SUMMARY     role_category match then highest cosine (fallback: all)
    SKILLS      top-14 pool candidates by cosine (Layer 5 groups them)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.config import settings
from src.llm.schemas import JDParsed
from src.scorer.embeddings import Vector, add, cosine, embed_batch

# ---------------------------------------------------------------------------
# Input candidate structures (embeddings pre-computed by the rebuild)
# ---------------------------------------------------------------------------


@dataclass
class BulletCand:
    id: str
    text: str
    embedding: Vector


@dataclass
class ExperienceCand:
    id: str
    company: str
    actual_title: str
    safe_title_aliases: list[str]
    alias_embeddings: list[Vector]
    end_date: str  # "YYYY-MM" or "present"
    bullets: list[BulletCand]


@dataclass
class ProjectCand:
    id: str
    name: str
    link: str
    name_embedding: Vector
    bullets: list[BulletCand]


@dataclass
class SummaryCand:
    id: str
    text: str
    role_categories: list[str]
    embedding: Vector


@dataclass
class SkillCand:
    skill: str
    embedding: Vector


@dataclass
class Profile:
    """The whole candidate pool for one operator (from master_profile)."""

    experiences: list[ExperienceCand]
    projects: list[ProjectCand]
    summaries: list[SummaryCand]
    skills: list[SkillCand]


@dataclass(frozen=True)
class JDContext:
    """Everything Layer 4 needs about the job being scored.

    Three JD query vectors (architecture §4.1), each matched against a
    different candidate facet:
      * ``vec_role``   = embed(role_summary)            — titles, project
                          names, summary (role-level identity)
      * ``vec_skills`` = embed(required + nice_to_have) — skills_pool
      * ``vec_match``  = vec_skills + vec_resp          — bullets (concrete
                          work vs what the JD wants done)
    Build one with :func:`build_jd_context`.
    """

    vec_role: Vector
    vec_match: Vector
    vec_skills: Vector
    role_category: str | None = None
    role_level: str | None = None
    posted_at: datetime | None = None


# ---------------------------------------------------------------------------
# Output structures
# ---------------------------------------------------------------------------


@dataclass
class SelectedBullet:
    id: str
    text: str
    score: float


@dataclass
class SelectedExperience:
    id: str
    company: str
    actual_title: str
    safe_title_aliases: list[str]
    score: float
    alias_score: float
    end_date: str
    bullets: list[SelectedBullet]  # ordered, top-3 by score


@dataclass
class SelectedProject:
    id: str
    name: str
    link: str
    score: float
    bullets: list[SelectedBullet]  # ordered descending by score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scored_bullets(bullets: list[BulletCand], query: Vector) -> list[SelectedBullet]:
    """Score every bullet against a query vector, sorted best-first."""
    scored = [
        SelectedBullet(b.id, b.text, cosine(b.embedding, query)) for b in bullets
    ]
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def _force_min(passing: list, ranked: list, max_shown: int, min_shown: int) -> list:
    """Take up to ``max_shown`` that passed threshold; if fewer than
    ``min_shown`` passed, force-include the top ``min_shown`` overall."""
    selected = passing[:max_shown]
    if len(selected) < min_shown:
        selected = ranked[:min_shown]
    return selected


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------


def score_experience(
    exp: ExperienceCand, jd: JDContext
) -> tuple[float, float, list[SelectedBullet]]:
    """Return (experience_score, alias_score, top-N scored bullets)."""
    cfg = settings.selection.experience
    alias_score = max(
        (cosine(e, jd.vec_role) for e in exp.alias_embeddings), default=0.0
    )
    scored = _scored_bullets(exp.bullets, jd.vec_match)
    top = scored[: cfg.bullets_per_experience]
    top_avg = sum(s.score for s in top) / len(top) if top else 0.0
    exp_score = cfg.weight_alias * alias_score + cfg.weight_bullets * top_avg
    return exp_score, alias_score, top


def select_experiences(
    experiences: list[ExperienceCand], jd: JDContext
) -> list[SelectedExperience]:
    """Select 2-3 experiences by score (threshold 0.45, force-include top-2).

    Returns them score-ranked; display ordering (match-then-recency) is
    applied separately by :func:`src.scorer.ordering.order_experiences`.
    """
    cfg = settings.selection.experience
    ranked: list[SelectedExperience] = []
    for exp in experiences:
        score, alias_score, bullets = score_experience(exp, jd)
        ranked.append(
            SelectedExperience(
                id=exp.id,
                company=exp.company,
                actual_title=exp.actual_title,
                safe_title_aliases=list(exp.safe_title_aliases),
                score=score,
                alias_score=alias_score,
                end_date=exp.end_date,
                bullets=bullets,
            )
        )
    ranked.sort(key=lambda x: x.score, reverse=True)
    passing = [x for x in ranked if x.score >= cfg.threshold]
    return _force_min(passing, ranked, cfg.max_shown, cfg.min_shown)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


def _select_project_bullets(
    bullets: list[BulletCand], jd: JDContext
) -> list[SelectedBullet]:
    cfg = settings.selection.project
    scored = _scored_bullets(bullets, jd.vec_match)
    passing = [b for b in scored if b.score >= cfg.bullet_threshold]
    return _force_min(passing, scored, cfg.bullet_max, cfg.bullet_min)


def score_project(
    project: ProjectCand, jd: JDContext
) -> tuple[float, list[SelectedBullet]]:
    """Return (project_score, selected bullets) — N = bullets shown."""
    cfg = settings.selection.project
    name_score = cosine(project.name_embedding, jd.vec_role)
    chosen = _select_project_bullets(project.bullets, jd)
    bullet_avg = sum(b.score for b in chosen) / len(chosen) if chosen else 0.0
    score = cfg.weight_name * name_score + cfg.weight_bullets * bullet_avg
    return score, chosen


def select_projects(
    projects: list[ProjectCand], jd: JDContext
) -> list[SelectedProject]:
    """Select 2-3 projects by score (threshold 0.50, force-include top-2).

    The projects section is NEVER hidden (CLAUDE.md hard rule), so at least
    ``min_shown`` are always returned when any project exists.
    """
    cfg = settings.selection.project
    ranked: list[SelectedProject] = []
    for project in projects:
        score, bullets = score_project(project, jd)
        ranked.append(
            SelectedProject(
                id=project.id,
                name=project.name,
                link=project.link,
                score=score,
                bullets=bullets,
            )
        )
    ranked.sort(key=lambda x: x.score, reverse=True)
    passing = [x for x in ranked if x.score >= cfg.threshold]
    return _force_min(passing, ranked, cfg.max_shown, cfg.min_shown)


# ---------------------------------------------------------------------------
# Summary (deterministic — no LLM)
# ---------------------------------------------------------------------------


def select_summary(
    summaries: list[SummaryCand], jd: JDContext
) -> tuple[SummaryCand | None, float]:
    """Pick the best summary: among those whose role_categories include the
    JD's category, highest cosine; else (fallback) highest cosine overall."""
    if not summaries:
        return None, 0.0
    scored = [(s, cosine(s.embedding, jd.vec_role)) for s in summaries]
    if jd.role_category:
        matching = [t for t in scored if jd.role_category in t[0].role_categories]
        if matching:
            return max(matching, key=lambda t: t[1])
    if settings.selection.summary.fallback_to_all:
        return max(scored, key=lambda t: t[1])
    return None, 0.0


# ---------------------------------------------------------------------------
# Skills — Layer 4 produces ranked candidates; Layer 5's LLM groups them
# ---------------------------------------------------------------------------


def select_skill_candidates(
    skills: list[SkillCand], jd: JDContext
) -> list[tuple[str, float]]:
    """Top-N skills_pool candidates by JD cosine (N = config top_candidates)."""
    cfg = settings.selection.skills
    scored = [(s.skill, cosine(s.embedding, jd.vec_skills)) for s in skills]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[: cfg.top_candidates]


# ---------------------------------------------------------------------------
# JD context builder — the only per-run embedding (architecture §4.1)
# ---------------------------------------------------------------------------


def build_jd_context(
    parsed: JDParsed,
    *,
    posted_at: datetime | None = None,
    embed_batch_fn=None,
) -> JDContext:
    """Embed a parsed JD into the three query vectors Layer 4 scores against.

    One batched embed call per job (skills / responsibilities+summary / role
    summary); ``vec_match`` is the element-wise sum of the first two. The embed
    function is injectable so scoring tests run without the model.
    """
    embed_batch_fn = embed_batch_fn or embed_batch
    skills_text = " ".join([*parsed.required_skills, *parsed.nice_to_have])
    resp_text = " ".join([*parsed.responsibilities, parsed.role_summary])
    role_text = parsed.role_summary
    vec_skills, vec_resp, vec_role = embed_batch_fn([skills_text, resp_text, role_text])
    return JDContext(
        vec_role=vec_role,
        vec_match=add(vec_skills, vec_resp),
        vec_skills=vec_skills,
        role_category=parsed.role_category,
        role_level=parsed.role_level,
        posted_at=posted_at,
    )
