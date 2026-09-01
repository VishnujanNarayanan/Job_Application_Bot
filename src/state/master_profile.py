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
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.scorer.embeddings import Vector, embed
import structlog

from src.scorer.selector import (
    BulletCand,
    EntryCand,
    Profile,
    RoleBlockCand,
    SkillCand,
)
from src.state.models import (
    MasterBullets,
    MasterMeta,
    MasterTitleAliases,
)

log = structlog.get_logger(__name__)

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


class RoleBlock(BaseModel):
    """One role's worth of a job or project, as the bullet-extract skill writes it.

    A block is a *complete, liftable job entry* aimed at one title family: its own
    header, its own title aliases, its own ordered bullets. One entry may carry
    several blocks (a job that honestly serves `data`, `backend` and `quant`), and
    the bot renders that entry once, choosing the lead block per JD.

    ``bullets`` is the extractor's audited render set: ordered, density-checked, no
    repeated keyword within it, and ``bullets[0]`` is always the plain-language
    summary bullet. ``extra_bullets`` is the recovery pool -- true statements about
    real tools that this title's checklist happens not to name, plus the
    cross-cutting skills (Docker, Git, SQL, CI/CD) that every block needs available
    even when its own checklist omits them. Nothing in ``extra_bullets`` renders
    unless a JD asks for its keyword; without it, a keyword cut at extraction time
    is unrecoverable at selection time.
    """

    role: str
    role_fit: str = "primary"  # primary | adjacent
    entry_header: str
    entry_dates: str | None = None  # absent for projects (method: projects have no dates)
    checklist: list[str] = Field(default_factory=list)
    market: str | None = None
    target_titles: list[str] = Field(default_factory=list)
    title_aliases: list[str] = Field(..., min_length=1)
    bullets: list[Bullet] = Field(..., min_length=1)
    extra_bullets: list[Bullet] = Field(default_factory=list)
    covered: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    titles_dropped: list[dict[str, Any]] = Field(default_factory=list)
    lead_pct: int = 0

    @property
    def summary_bullet_id(self) -> str:
        """``bullets[0]`` is the summary bullet, by the extractor's contract."""
        return self.bullets[0].id

    @property
    def all_bullets(self) -> list[Bullet]:
        """Render set plus recovery pool -- what the selector may choose from."""
        return [*self.bullets, *self.extra_bullets]


class _Entry(BaseModel):
    """Shared shape. A work entry and a project entry differ only in identity
    fields and whether they carry dates; both render as one block of bullets under
    one header, so the selector treats them identically."""

    id: str
    role_blocks: list[RoleBlock] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique_roles(self) -> "_Entry":
        _require_unique([rb.role for rb in self.role_blocks], f"role in '{self.id}'")
        return self


class WorkExperience(_Entry):
    company: str
    actual_title: str
    safe_title_aliases: list[str]
    start_date: str  # YYYY-MM — drives the tenure bullet cap
    end_date: str  # YYYY-MM | present
    location: str | None = None
    #: ``employment`` entries are the operator's actual jobs and are
    #: force-included: a resume without them is not a resume. ``freelance``
    #: entries are separate engagements that compete on merit like projects do —
    #: any, all, or none may appear on a given resume — but they render under
    #: Work History rather than Projects, because that is what they are.
    employment_type: Literal["employment", "freelance"] = "employment"

    @model_validator(mode="after")
    def _check(self) -> WorkExperience:
        # Hard rule #6: a rendered title may only come from this allow-list, and the
        # operator's real title must be one of the safe options.
        if self.actual_title not in self.safe_title_aliases:
            raise ValueError(
                f"work_experience '{self.id}': actual_title "
                f"'{self.actual_title}' must be in safe_title_aliases"
            )
        # Every block alias is a title this entry may render under, so rule #6
        # requires safe_title_aliases to be their union -- otherwise a block could
        # put a title on the page that never passed the allow-list.
        union = {a for rb in self.role_blocks for a in rb.title_aliases}
        missing = sorted(union - set(self.safe_title_aliases))
        if missing:
            raise ValueError(
                f"work_experience '{self.id}': role_block title_aliases {missing} "
                "are not in safe_title_aliases (hard rule #6)"
            )
        for rb in self.role_blocks:
            if not rb.entry_dates:
                raise ValueError(
                    f"work_experience '{self.id}' block '{rb.role}': entry_dates "
                    "is required (the method: full-time work and internships must "
                    "show months AND years)"
                )
        return self


class Project(_Entry):
    name: str
    link: str = ""
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_dates(self) -> Project:
        for rb in self.role_blocks:
            if rb.entry_dates:
                raise ValueError(
                    f"project '{self.id}' block '{rb.role}': projects carry no dates"
                )
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
    """The whole profile.

    ``education`` and ``certifications`` are retained as the record of truth but
    render nowhere: the Headless template puts Education & Certificates in a static
    region the operator hand-writes into their template copy, capped at three lines
    (PIVOT_V3.md D9). ``skills_pool`` and ``gap_skills`` are likewise machine input
    only -- nothing in either appears on a resume; every keyword that matters must
    also live inside a bullet.
    """

    personal: PersonalInfo
    meta: dict[str, Any] = Field(default_factory=dict)
    work_experience: list[WorkExperience]
    projects: list[Project]
    skills_pool: list[str] = Field(default_factory=list)
    gap_skills: list[dict[str, Any]] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)

    @property
    def entries(self) -> list[_Entry]:
        return [*self.work_experience, *self.projects]

    @model_validator(mode="after")
    def _unique_ids(self) -> MasterProfile:
        _require_unique([e.id for e in self.work_experience], "work_experience id")
        _require_unique([p.id for p in self.projects], "project id")
        _require_unique(
            [f"{e.id}::{rb.role}" for e in self.entries for rb in e.role_blocks],
            "role_block id",
        )
        # Bullet ids are the master_bullets PK, so they must be globally unique
        # across render sets AND recovery pools.
        _require_unique(
            [b.id for e in self.entries for rb in e.role_blocks for b in rb.all_bullets],
            "bullet id",
        )
        if not self.skills_pool:
            # A warning, not a raise. skills_pool renders nothing; it only feeds JD
            # parse repair and the dashboard's gap list. A legitimate extractor run
            # for a skill-less repo must still load.
            log.warning("master_profile_empty_skills_pool")
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
    parent_id: str  # the ENTRY id — unchanged semantics
    parent_type: str  # experience | project | skill
    text: str
    tags: list[str]
    block_id: str | None  # "{entry_id}::{role}"
    role: str | None
    bullet_index: int | None  # position within its block's render set
    is_summary: bool  # bullets[0] of a block
    is_extra: bool  # from extra_bullets — the recovery pool


def desired_bullets(profile: MasterProfile) -> list[_DesiredBullet]:
    """Every row master_bullets should hold (block bullets + the skills pool).

    ``project_name`` rows are gone: project names fed the old name-cosine in
    ``score_project``, and projects are now scored through their role_block title
    aliases exactly like work entries.
    """
    out: list[_DesiredBullet] = []
    for entries, ptype in (
        (profile.work_experience, "experience"),
        (profile.projects, "project"),
    ):
        for entry in entries:
            for rb in entry.role_blocks:
                block_id = f"{entry.id}::{rb.role}"
                for i, b in enumerate(rb.bullets):
                    out.append(
                        _DesiredBullet(
                            b.id, entry.id, ptype, b.text, b.tags,
                            block_id, rb.role, i, i == 0, False,
                        )
                    )
                for b in rb.extra_bullets:
                    # No index and never a summary: the recovery pool is unordered
                    # and never leads an entry.
                    out.append(
                        _DesiredBullet(
                            b.id, entry.id, ptype, b.text, b.tags,
                            block_id, rb.role, None, False, True,
                        )
                    )
    for skill in profile.skills_pool:
        out.append(
            _DesiredBullet(
                f"skill::{skill}", SKILLS_PARENT, "skill", skill, [],
                None, None, None, False, False,
            )
        )
    return out


def desired_aliases(profile: MasterProfile) -> dict[str, tuple[str, str, str]]:
    """alias_id -> (parent_id, block_id, alias_text).

    Aliases hang off the BLOCK now, not the entry: each block targets its own title
    family, so the `data` block's aliases are not the `backend` block's. Projects
    gain aliases too, which they never had -- they are scored the same way as work
    entries now.

    The id embeds the alias text, so an edited alias is a remove+add (deactivate
    the old row, insert the new one) rather than an in-place update.
    """
    out: dict[str, tuple[str, str, str]] = {}
    for entry in profile.entries:
        for rb in entry.role_blocks:
            block_id = f"{entry.id}::{rb.role}"
            for alias in rb.title_aliases:
                out[f"{block_id}::alias::{alias}"] = (entry.id, block_id, alias)
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
                block_id=d.block_id, role=d.role, bullet_index=d.bullet_index,
                is_summary=d.is_summary, is_extra=d.is_extra,
            )
        )
        report.bullets_inserted += 1
    for bid in plan.to_update:
        d, r = desired[bid], rows[bid]
        r.text = d.text
        r.embedding = embed_fn(d.text)
        r.parent_id, r.parent_type, r.tags = d.parent_id, d.parent_type, d.tags
        r.block_id, r.role, r.bullet_index = d.block_id, d.role, d.bullet_index
        r.is_summary, r.is_extra = d.is_summary, d.is_extra
        r.is_active, r.deactivated_at, r.updated_at = True, None, now
        report.bullets_updated += 1
    for bid in plan.to_reactivate:
        # Same text, previously deactivated. Its block metadata may still have
        # moved (a bullet reassigned to another role_block keeps its id and its
        # wording), so refresh those alongside the flag.
        d, r = desired[bid], rows[bid]
        r.block_id, r.role, r.bullet_index = d.block_id, d.role, d.bullet_index
        r.is_summary, r.is_extra = d.is_summary, d.is_extra
        r.is_active, r.deactivated_at, r.updated_at = True, None, now
        report.bullets_reactivated += 1
    for bid in plan.to_deactivate:
        r = rows[bid]
        r.is_active, r.deactivated_at = False, now
        report.bullets_deactivated += 1


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
        parent_id, block_id, alias = desired[aid]
        session.add(
            MasterTitleAliases(
                id=aid, parent_id=parent_id, block_id=block_id, alias=alias,
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

    Structure (headers, dates, which bullet sits in which block) comes from the
    JSON; embeddings come from the DB, joined by id. Assumes :func:`rebuild` has
    run.

    Only ACTIVE bullets are loaded, so a bullet removed from the YAML disappears
    from selection without ever being deleted — and any older ``selection_json``
    that references it still resolves against the JSON structure.
    """
    path = json_path or _JSON_PATH
    if not path.exists():
        raise MasterProfileError(
            f"{path.name} not found — run `python -m src.cli.reparse` first"
        )
    profile = MasterProfile.model_validate(json.loads(path.read_text()))

    rows: dict[str, MasterBullets] = {}
    skills: list[SkillCand] = []
    for b in session.scalars(
        select(MasterBullets).where(MasterBullets.is_active.is_(True))
    ).all():
        if b.parent_type == "skill":
            skills.append(SkillCand(b.text, _vec(b.embedding)))
        else:
            rows[b.bullet_id] = b

    aliases_by_block: dict[str, list[MasterTitleAliases]] = defaultdict(list)
    for a in session.scalars(
        select(MasterTitleAliases).where(MasterTitleAliases.is_active.is_(True))
    ).all():
        aliases_by_block[a.block_id or a.parent_id].append(a)

    def _blocks(entry) -> list[RoleBlockCand]:
        out: list[RoleBlockCand] = []
        for rb in entry.role_blocks:
            block_id = f"{entry.id}::{rb.role}"
            bullets: list[BulletCand] = []
            for i, b in enumerate(rb.all_bullets):
                row = rows.get(b.id)
                if row is None:
                    # Deactivated, or the DB is not synced yet. Skipping keeps
                    # selection honest rather than scoring a zero vector as if it
                    # were a genuine non-match.
                    continue
                bullets.append(
                    BulletCand(
                        id=b.id,
                        text=b.text,
                        embedding=_vec(row.embedding),
                        block_id=block_id,
                        role=rb.role,
                        is_summary=(i == 0 and b.id == rb.summary_bullet_id),
                        is_extra=i >= len(rb.bullets),
                    )
                )
            arows = aliases_by_block.get(block_id, [])
            out.append(
                RoleBlockCand(
                    block_id=block_id,
                    role=rb.role,
                    role_fit=rb.role_fit,
                    entry_header=rb.entry_header,
                    entry_dates=rb.entry_dates or "",
                    checklist=tuple(rb.checklist),
                    title_aliases=[a.alias for a in arows] or list(rb.title_aliases),
                    alias_embeddings=[_vec(a.embedding) for a in arows],
                    bullets=bullets,
                )
            )
        return out

    work = [
        EntryCand(
            id=e.id,
            kind="work",
            label=e.company,
            blocks=_blocks(e),
            actual_title=e.actual_title,
            safe_title_aliases=list(e.safe_title_aliases),
            start_date=e.start_date,
            end_date=e.end_date,
            employment_type=e.employment_type,
        )
        for e in profile.work_experience
    ]
    projects = [
        EntryCand(id=p.id, kind="project", label=p.name, blocks=_blocks(p), link=p.link)
        for p in profile.projects
    ]
    return Profile(work=work, projects=projects, skills=skills)
