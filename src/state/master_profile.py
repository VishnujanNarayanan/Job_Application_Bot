"""Master profile loader, validator, and DB rebuild logic (Layer 7 / §3).

`master_profile.yaml` is the single source of truth (the user edits it; code
never writes it). On a run, if its mtime changed, :func:`rebuild`:

    1. validates the YAML against :class:`MasterProfile` (fail loudly),
    2. writes the canonical `master_profile.json`,
    3. diffs against the DB — new/changed items embed + upsert, removed items
       are deactivated (``is_active=false``, never hard-deleted),
    4. records mtime + processed_at in ``master_meta``.

Embedding storage (architecture §3.3 / §7.3): bullets, summaries, title
aliases, and skills each get a stored ``vector(384)``. §7.3 defines no
``master_skills`` table, so skills live in ``master_bullets`` with
``parent_type='skill'``; project *names* (needed for §4.3 name-cosine) are
stored the same way (``parent_type='project_name'``) so they're pre-embedded
and diff-tracked — honoring "only the JD is embedded per run".

:func:`load_profile` is the adapter Layer 4 consumes: it reads structure
(company, dates, project name/link) from the canonical JSON and the matching
embeddings from the DB, joining by id, and returns a
:class:`src.scorer.selector.Profile`.

The diff brain (:func:`plan_sync`) and the validators are pure and unit-tested
without a DB.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.scorer.embeddings import Vector, embed
from src.scorer.selector import (
    BulletCand,
    ExperienceCand,
    Profile,
    ProjectCand,
    SkillCand,
    SummaryCand,
)
from src.state.models import (
    MasterBullets,
    MasterMeta,
    MasterSummaries,
    MasterTitleAliases,
)

_ROOT = Path(__file__).resolve().parents[2]
_YAML_PATH = _ROOT / "master_profile.yaml"
_JSON_PATH = _ROOT / "master_profile.json"

# Sentinel parent_id for skills (which have no work-experience/project parent).
SKILLS_PARENT = "__skills_pool__"

# master_meta keys.
_META_MTIME = "master_profile_mtime"
_META_PROCESSED = "master_profile_processed_at"


class MasterProfileError(RuntimeError):
    """Raised when the profile is invalid or the canonical JSON is missing."""


# ---------------------------------------------------------------------------
# Pydantic schema for master_profile.yaml
# ---------------------------------------------------------------------------


class PersonalInfo(BaseModel):
    name: str
    email: str
    phone: str | None = None
    location: str | None = None
    github: str | None = None
    linkedin: str | None = None
    certificates_link: str | None = None


class Bullet(BaseModel):
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)


class Summary(BaseModel):
    id: str
    text: str
    tags: list[str] = Field(default_factory=list)
    role_categories: list[str] = Field(default_factory=list)


class WorkExperience(BaseModel):
    id: str
    company: str
    actual_title: str
    safe_title_aliases: list[str]
    start_date: str
    end_date: str
    location: str | None = None
    bullet_pool: list[Bullet]

    @model_validator(mode="after")
    def _actual_title_in_aliases(self) -> WorkExperience:
        # Hard rule #6: the LLM may only display a title from this allow-list,
        # and the operator's real title must be one of the safe options.
        if self.actual_title not in self.safe_title_aliases:
            raise ValueError(
                f"work_experience '{self.id}': actual_title "
                f"'{self.actual_title}' must be in safe_title_aliases"
            )
        if not self.bullet_pool:
            raise ValueError(f"work_experience '{self.id}': bullet_pool is empty")
        return self


class Project(BaseModel):
    id: str
    name: str
    link: str
    tags: list[str] = Field(default_factory=list)
    bullet_pool: list[Bullet]

    @model_validator(mode="after")
    def _min_two_bullets(self) -> Project:
        # Projects show 2-3 bullets and are never hidden, so the pool needs >=2.
        if len(self.bullet_pool) < 2:
            raise ValueError(f"project '{self.id}': bullet_pool needs >= 2 bullets")
        return self


class Education(BaseModel):
    degree: str
    institution: str
    dates: str
    score: str | None = None


class Certification(BaseModel):
    id: str
    name: str
    verify_link: str
    bullets: list[str] = Field(default_factory=list)


class MasterProfile(BaseModel):
    personal: PersonalInfo
    summaries: list[Summary]
    work_experience: list[WorkExperience]
    projects: list[Project]
    skills_pool: list[str]
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> MasterProfile:
        _require_unique([e.id for e in self.work_experience], "work_experience id")
        _require_unique([p.id for p in self.projects], "project id")
        _require_unique([s.id for s in self.summaries], "summary id")
        # Bullet ids must be globally unique — they're the master_bullets PK.
        bullet_ids = [
            b.id
            for parent in (*self.work_experience, *self.projects)
            for b in parent.bullet_pool
        ]
        _require_unique(bullet_ids, "bullet id")
        if not self.skills_pool:
            raise ValueError("skills_pool is empty")
        _require_unique(self.skills_pool, "skill")
        return self


def _require_unique(values: list[str], label: str) -> None:
    seen: set[str] = set()
    for v in values:
        if v in seen:
            raise ValueError(f"duplicate {label}: '{v}'")
        seen.add(v)


# ---------------------------------------------------------------------------
# Desired-set builders (what the DB SHOULD contain, derived from the profile)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DesiredBullet:
    id: str
    parent_id: str
    parent_type: str  # experience | project | skill | project_name
    text: str
    tags: list[str]


def desired_bullets(profile: MasterProfile) -> list[_DesiredBullet]:
    """Every row master_bullets should hold (bullets + skills + project names)."""
    out: list[_DesiredBullet] = []
    for exp in profile.work_experience:
        for b in exp.bullet_pool:
            out.append(_DesiredBullet(b.id, exp.id, "experience", b.text, b.tags))
    for proj in profile.projects:
        for b in proj.bullet_pool:
            out.append(_DesiredBullet(b.id, proj.id, "project", b.text, b.tags))
        out.append(
            _DesiredBullet(f"projname::{proj.id}", proj.id, "project_name", proj.name, [])
        )
    for skill in profile.skills_pool:
        out.append(_DesiredBullet(f"skill::{skill}", SKILLS_PARENT, "skill", skill, []))
    return out


def desired_aliases(profile: MasterProfile) -> dict[str, tuple[str, str]]:
    """alias_id -> (parent_id, alias_text). The id embeds the alias text, so a
    changed alias is a remove+add (deactivate old, insert new)."""
    out: dict[str, tuple[str, str]] = {}
    for exp in profile.work_experience:
        for alias in exp.safe_title_aliases:
            out[f"{exp.id}::alias::{alias}"] = (exp.id, alias)
    return out


# ---------------------------------------------------------------------------
# Pure diff brain
# ---------------------------------------------------------------------------


@dataclass
class SyncPlan:
    to_insert: list[str] = field(default_factory=list)
    to_update: list[str] = field(default_factory=list)  # content changed → re-embed
    to_reactivate: list[str] = field(default_factory=list)  # same content, was off
    to_deactivate: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


def plan_sync(
    existing: dict[str, tuple[str, bool]], desired: dict[str, str]
) -> SyncPlan:
    """Diff DB state against desired state, keyed by id.

    ``existing``: id -> (content, is_active). ``desired``: id -> content.
    Items present in DB but not desired (and still active) are deactivated —
    never deleted (hard rule #17).
    """
    plan = SyncPlan()
    for id_, content in desired.items():
        if id_ not in existing:
            plan.to_insert.append(id_)
            continue
        old_content, active = existing[id_]
        if old_content != content:
            plan.to_update.append(id_)
        elif not active:
            plan.to_reactivate.append(id_)
        else:
            plan.unchanged.append(id_)
    for id_, (_content, active) in existing.items():
        if id_ not in desired and active:
            plan.to_deactivate.append(id_)
    return plan


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------


@dataclass
class RebuildReport:
    skipped: bool = False
    bullets_inserted: int = 0
    bullets_updated: int = 0
    bullets_reactivated: int = 0
    bullets_deactivated: int = 0
    summaries_inserted: int = 0
    summaries_updated: int = 0
    summaries_reactivated: int = 0
    summaries_deactivated: int = 0
    aliases_inserted: int = 0
    aliases_reactivated: int = 0
    aliases_deactivated: int = 0


def _get_meta(session: Session, key: str) -> str | None:
    row = session.get(MasterMeta, key)
    return row.value if row is not None else None


def _set_meta(session: Session, key: str, value: str) -> None:
    row = session.get(MasterMeta, key)
    if row is None:
        session.add(MasterMeta(key=key, value=value))
    else:
        row.value = value


def rebuild(
    session: Session,
    *,
    path: Path | None = None,
    embed_fn=None,
    force: bool = False,
) -> RebuildReport:
    """Validate the profile and reconcile the DB embeddings to it."""
    yaml_path = path or _YAML_PATH
    embed_fn = embed_fn or embed
    now = datetime.now(timezone.utc)

    mtime = str(os.path.getmtime(yaml_path))
    if not force and _get_meta(session, _META_MTIME) == mtime:
        return RebuildReport(skipped=True)

    with open(yaml_path) as f:
        raw = yaml.safe_load(f) or {}
    profile = MasterProfile.model_validate(raw)  # raises on schema violation

    # Canonical JSON: the structure source the loader reads (§3.2).
    json_path = _JSON_PATH if path is None else yaml_path.with_suffix(".json")
    json_path.write_text(json.dumps(profile.model_dump(), indent=2, sort_keys=True))

    report = RebuildReport()
    _sync_bullets(session, profile, embed_fn, now, report)
    _sync_summaries(session, profile, embed_fn, now, report)
    _sync_aliases(session, profile, embed_fn, report)

    _set_meta(session, _META_MTIME, mtime)
    _set_meta(session, _META_PROCESSED, now.isoformat())
    return report


def _sync_bullets(session, profile, embed_fn, now, report) -> None:
    rows = {r.bullet_id: r for r in session.scalars(select(MasterBullets)).all()}
    desired = {d.id: d for d in desired_bullets(profile)}
    plan = plan_sync(
        {bid: (r.text, r.is_active) for bid, r in rows.items()},
        {d.id: d.text for d in desired.values()},
    )
    for bid in plan.to_insert:
        d = desired[bid]
        session.add(
            MasterBullets(
                bullet_id=d.id, parent_id=d.parent_id, parent_type=d.parent_type,
                text=d.text, tags=d.tags, embedding=embed_fn(d.text), is_active=True,
            )
        )
        report.bullets_inserted += 1
    for bid in plan.to_update:
        d, r = desired[bid], rows[bid]
        r.text = d.text
        r.embedding = embed_fn(d.text)
        r.parent_id, r.parent_type, r.tags = d.parent_id, d.parent_type, d.tags
        r.is_active, r.deactivated_at, r.updated_at = True, None, now
        report.bullets_updated += 1
    for bid in plan.to_reactivate:
        r = rows[bid]
        r.is_active, r.deactivated_at, r.updated_at = True, None, now
        report.bullets_reactivated += 1
    for bid in plan.to_deactivate:
        r = rows[bid]
        r.is_active, r.deactivated_at = False, now
        report.bullets_deactivated += 1


def _sync_summaries(session, profile, embed_fn, now, report) -> None:
    rows = {r.summary_id: r for r in session.scalars(select(MasterSummaries)).all()}
    desired = {s.id: s for s in profile.summaries}
    plan = plan_sync(
        {sid: (r.text, r.is_active) for sid, r in rows.items()},
        {s.id: s.text for s in desired.values()},
    )
    for sid in plan.to_insert:
        s = desired[sid]
        session.add(
            MasterSummaries(
                summary_id=s.id, text=s.text, tags=s.tags,
                role_categories=s.role_categories, embedding=embed_fn(s.text),
                is_active=True,
            )
        )
        report.summaries_inserted += 1
    for sid in plan.to_update:
        s, r = desired[sid], rows[sid]
        r.text, r.embedding = s.text, embed_fn(s.text)
        r.tags, r.role_categories, r.is_active = s.tags, s.role_categories, True
        report.summaries_updated += 1
    for sid in plan.to_reactivate:
        rows[sid].is_active = True
        report.summaries_reactivated += 1
    for sid in plan.to_deactivate:
        rows[sid].is_active = False
        report.summaries_deactivated += 1


def _sync_aliases(session, profile, embed_fn, report) -> None:
    rows = {r.id: r for r in session.scalars(select(MasterTitleAliases)).all()}
    desired = desired_aliases(profile)
    plan = plan_sync(
        # Alias content == its id (alias text is baked into the id), so a
        # changed alias is insert+deactivate, never an in-place update.
        {aid: (aid, r.is_active) for aid, r in rows.items()},
        {aid: aid for aid in desired},
    )
    for aid in plan.to_insert:
        parent_id, alias = desired[aid]
        session.add(
            MasterTitleAliases(
                id=aid, parent_id=parent_id, alias=alias,
                embedding=embed_fn(alias), is_active=True,
            )
        )
        report.aliases_inserted += 1
    for aid in plan.to_reactivate:
        rows[aid].is_active = True
        report.aliases_reactivated += 1
    for aid in plan.to_deactivate:
        rows[aid].is_active = False
        report.aliases_deactivated += 1


# ---------------------------------------------------------------------------
# Candidate loader — JSON structure + DB embeddings → scorer.Profile
# ---------------------------------------------------------------------------


def _vec(value: Any) -> Vector:
    return list(value) if value is not None else []


def load_profile(session: Session, *, json_path: Path | None = None) -> Profile:
    """Build the Layer-4 :class:`Profile` from the canonical JSON + DB rows.

    Assumes :func:`rebuild` has run (the JSON exists and the DB reflects it).
    """
    path = json_path or _JSON_PATH
    if not path.exists():
        raise MasterProfileError(
            f"{path.name} not found — run `python -m src.cli.reparse` first"
        )
    profile = MasterProfile.model_validate(json.loads(path.read_text()))

    exp_bullets: dict[str, list[BulletCand]] = defaultdict(list)
    proj_bullets: dict[str, list[BulletCand]] = defaultdict(list)
    proj_name_emb: dict[str, Vector] = {}
    skills: list[SkillCand] = []
    for b in session.scalars(
        select(MasterBullets).where(MasterBullets.is_active.is_(True))
    ).all():
        if b.parent_type == "experience":
            exp_bullets[b.parent_id].append(BulletCand(b.bullet_id, b.text, _vec(b.embedding)))
        elif b.parent_type == "project":
            proj_bullets[b.parent_id].append(BulletCand(b.bullet_id, b.text, _vec(b.embedding)))
        elif b.parent_type == "skill":
            skills.append(SkillCand(b.text, _vec(b.embedding)))
        elif b.parent_type == "project_name":
            proj_name_emb[b.parent_id] = _vec(b.embedding)

    aliases_by_parent: dict[str, list[MasterTitleAliases]] = defaultdict(list)
    for a in session.scalars(
        select(MasterTitleAliases).where(MasterTitleAliases.is_active.is_(True))
    ).all():
        aliases_by_parent[a.parent_id].append(a)

    experiences: list[ExperienceCand] = []
    for exp in profile.work_experience:
        arows = aliases_by_parent.get(exp.id, [])
        if arows:
            safe_aliases = [a.alias for a in arows]
            alias_embs = [_vec(a.embedding) for a in arows]
        else:  # DB not synced yet — structure only, no alias embeddings
            safe_aliases, alias_embs = list(exp.safe_title_aliases), []
        experiences.append(
            ExperienceCand(
                id=exp.id, company=exp.company, actual_title=exp.actual_title,
                safe_title_aliases=safe_aliases, alias_embeddings=alias_embs,
                end_date=exp.end_date, bullets=exp_bullets.get(exp.id, []),
            )
        )

    projects = [
        ProjectCand(
            id=p.id, name=p.name, link=p.link,
            name_embedding=proj_name_emb.get(p.id, []),
            bullets=proj_bullets.get(p.id, []),
        )
        for p in profile.projects
    ]

    summaries = [
        SummaryCand(s.summary_id, s.text, s.role_categories or [], _vec(s.embedding))
        for s in session.scalars(
            select(MasterSummaries).where(MasterSummaries.is_active.is_(True))
        ).all()
    ]

    return Profile(
        experiences=experiences, projects=projects,
        summaries=summaries, skills=skills,
    )
