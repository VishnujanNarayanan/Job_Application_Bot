"""Iteration 2 — Phase B: master_profile schema, rebuild diff, and loader.

Offline. The schema validators and the diff brain (`plan_sync`,
`desired_bullets`) are pure. `rebuild` and `load_profile` are exercised with
an in-memory fake session (the master tables use pgvector/JSONB, so SQLite
can't host them) and an injected deterministic embed function.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest

from src.state import master_profile as mp
from src.state.master_profile import (
    MasterProfile,
    desired_bullets,
    load_profile,
    plan_sync,
    rebuild,
)
from src.state.models import (
    MasterBullets,
    MasterMeta,
    MasterSummaries,
    MasterTitleAliases,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _valid_profile() -> dict[str, Any]:
    """A role_block-shaped profile, as the bullet-extract skill now emits it."""
    return {
        "personal": {"name": "Jane Doe", "email": "jane@example.com"},
        "work_experience": [
            {
                "id": "exp1", "company": "Acme", "actual_title": "Data Engineer",
                "safe_title_aliases": ["Data Engineer", "Analytics Engineer"],
                "start_date": "2023-01", "end_date": "present",
                "role_blocks": [
                    {
                        "role": "data",
                        "entry_header": "Data Engineer at Acme, Pune",
                        "entry_dates": "January 2023 to current",
                        "checklist": ["Data Engineer"],
                        "title_aliases": ["Data Engineer", "Analytics Engineer"],
                        "bullets": [
                            {"id": "exp1_b1", "text": "Kept the warehouse current."},
                            {"id": "exp1_b2", "text": "Built pipelines in Python."},
                        ],
                        "extra_bullets": [
                            {"id": "exp1_x1", "text": "Ran jobs in Docker on Linux."},
                        ],
                    }
                ],
            }
        ],
        "projects": [
            {
                "id": "proj1", "name": "Pipeline Tool", "link": "http://x",
                "role_blocks": [
                    {
                        "role": "data",
                        "entry_header": "Pipeline Tool",
                        "checklist": ["Data Engineer"],
                        "title_aliases": ["Data Engineer"],
                        "bullets": [
                            {"id": "proj1_b1", "text": "Streaming ETL with Kafka."},
                            {"id": "proj1_b2", "text": "Dashboards in Superset."},
                        ],
                    }
                ],
            }
        ],
        "skills_pool": ["Python", "SQL", "Airflow"],
        "education": [{"degree": "BTech", "institution": "IIT", "dates": "2019-2023"}],
        "certifications": [],
    }


def _embed(text: str):
    # Deterministic, cheap, unique-per-length-ish stand-in for the model.
    return [float(len(text)), float(sum(map(ord, text)) % 97)]


class _Scalars:
    def __init__(self, data: list) -> None:
        self._data = data

    def all(self) -> list:
        return list(self._data)


class FakeSession:
    """Minimal Session for the calls rebuild/load make: add(_all), get,
    scalars(select(Model)[.where(...)]).all(). where clauses are ignored, so
    prime only the rows the test wants returned."""

    def __init__(self) -> None:
        self.store: dict[type, dict[Any, Any]] = defaultdict(dict)

    @staticmethod
    def _pk_name(model: type) -> str:
        return model.__mapper__.primary_key[0].name

    def add(self, obj: Any) -> None:
        model = type(obj)
        self.store[model][getattr(obj, self._pk_name(model))] = obj

    def add_all(self, objs) -> None:
        for o in objs:
            self.add(o)

    def get(self, model: type, pk: Any) -> Any:
        return self.store[model].get(pk)

    def scalars(self, stmt) -> _Scalars:
        entity = stmt.column_descriptions[0]["entity"]
        return _Scalars(list(self.store[entity].values()))

    def flush(self) -> None:  # rebuild/loader never rely on flush side effects
        pass


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_valid_profile_parses() -> None:
    p = MasterProfile.model_validate(_valid_profile())
    assert p.personal.name == "Jane Doe"
    assert len(p.work_experience) == 1


def test_actual_title_must_be_in_aliases() -> None:
    bad = _valid_profile()
    bad["work_experience"][0]["safe_title_aliases"] = ["Analytics Engineer"]
    with pytest.raises(ValueError, match="actual_title"):
        MasterProfile.model_validate(bad)


def test_a_project_may_not_carry_dates() -> None:
    """The method: projects don't need dates, and showing them invites the
    reader to date a side project like a job."""
    bad = _valid_profile()
    bad["projects"][0]["role_blocks"][0]["entry_dates"] = "Jan 2024 to Mar 2024"
    with pytest.raises(ValueError, match="projects carry no dates"):
        MasterProfile.model_validate(bad)


def test_a_work_block_must_carry_dates() -> None:
    bad = _valid_profile()
    del bad["work_experience"][0]["role_blocks"][0]["entry_dates"]
    with pytest.raises(ValueError, match="entry_dates"):
        MasterProfile.model_validate(bad)


def test_block_aliases_must_be_within_safe_title_aliases() -> None:
    """Hard rule #6 — a block alias is a title that can reach the page."""
    bad = _valid_profile()
    bad["work_experience"][0]["role_blocks"][0]["title_aliases"].append("CTO")
    with pytest.raises(ValueError, match="not in safe_title_aliases"):
        MasterProfile.model_validate(bad)


def test_duplicate_bullet_id_rejected() -> None:
    bad = _valid_profile()
    bad["projects"][0]["role_blocks"][0]["bullets"][1]["id"] = "exp1_b1"  # collides
    with pytest.raises(ValueError, match="duplicate bullet id"):
        MasterProfile.model_validate(bad)


def test_an_empty_skills_pool_warns_but_loads() -> None:
    """It renders nothing — it only feeds JD-parse repair and the gap list — so a
    skill-less extractor run must not be a hard failure."""
    ok = _valid_profile()
    ok["skills_pool"] = []
    assert MasterProfile.model_validate(ok).skills_pool == []


# ---------------------------------------------------------------------------
# Desired-set builder
# ---------------------------------------------------------------------------


def test_desired_bullets_carries_block_provenance() -> None:
    p = MasterProfile.model_validate(_valid_profile())
    desired = {d.id: d for d in desired_bullets(p)}

    assert desired["exp1_b1"].block_id == "exp1::data"
    assert desired["exp1_b1"].is_summary is True, "bullets[0] is the summary bullet"
    assert desired["exp1_b1"].bullet_index == 0
    assert desired["exp1_b2"].is_summary is False

    extra = desired["exp1_x1"]
    assert extra.is_extra is True
    assert extra.is_summary is False, "the recovery pool never leads an entry"
    assert extra.bullet_index is None, "the recovery pool is unordered"

    assert {d.text for d in desired.values() if d.parent_type == "skill"} == {
        "Python", "SQL", "Airflow"
    }
    assert not any(d.parent_type == "project_name" for d in desired.values()), (
        "project_name rows fed the old name-cosine and are gone"
    )


# ---------------------------------------------------------------------------
# Pure diff brain
# ---------------------------------------------------------------------------


def test_plan_sync_all_branches() -> None:
    existing = {
        "keep": ("same", True),       # unchanged
        "edit": ("old", True),        # content changed -> update
        "wake": ("same", False),      # inactive, still desired -> reactivate
        "gone": ("x", True),          # not desired -> deactivate
        "gone_off": ("y", False),     # not desired, already inactive -> ignore
    }
    desired = {"keep": "same", "edit": "new", "wake": "same", "fresh": "z"}
    plan = plan_sync(existing, desired)
    assert plan.to_insert == ["fresh"]
    assert plan.to_update == ["edit"]
    assert plan.to_reactivate == ["wake"]
    assert plan.to_deactivate == ["gone"]
    assert plan.unchanged == ["keep"]


# ---------------------------------------------------------------------------
# rebuild (fake session + injected embed)
# ---------------------------------------------------------------------------


def test_rebuild_clean_db_inserts_everything(tmp_path) -> None:
    yaml_path = tmp_path / "master_profile.yaml"
    import yaml as _yaml
    yaml_path.write_text(_yaml.safe_dump(_valid_profile()))

    session = FakeSession()
    report = rebuild(session, path=yaml_path, embed_fn=_embed)

    assert report.skipped is False
    # 2 exp bullets + 1 exp extra + 2 proj bullets + 3 skills = 8
    assert report.bullets_inserted == 8
    # 2 aliases on the work block + 1 on the project block; projects have
    # aliases now, which they never did before the pivot.
    assert report.aliases_inserted == 3
    # Embeddings were attached.
    bullets = list(session.store[MasterBullets].values())
    assert all(b.embedding for b in bullets)
    # Canonical JSON was written next to the yaml.
    assert (tmp_path / "master_profile.json").exists()
    # Meta recorded so the next unchanged run can short-circuit.
    assert session.get(MasterMeta, "master_profile_mtime") is not None


def test_rebuild_skips_when_mtime_unchanged(tmp_path) -> None:
    yaml_path = tmp_path / "master_profile.yaml"
    import yaml as _yaml
    yaml_path.write_text(_yaml.safe_dump(_valid_profile()))

    session = FakeSession()
    rebuild(session, path=yaml_path, embed_fn=_embed)
    second = rebuild(session, path=yaml_path, embed_fn=_embed)
    assert second.skipped is True
    # force overrides the mtime short-circuit.
    forced = rebuild(session, path=yaml_path, embed_fn=_embed, force=True)
    assert forced.skipped is False


# ---------------------------------------------------------------------------
# load_profile (fake session + canonical json)
# ---------------------------------------------------------------------------


def test_load_profile_joins_json_and_db(tmp_path) -> None:
    json_path = tmp_path / "master_profile.json"
    json_path.write_text(json.dumps(_valid_profile()))

    session = FakeSession()
    # Prime active DB rows as the rebuild would have produced.
    for bid, text, emb in [
        ("exp1_b1", "Kept the warehouse current.", [1.0, 0.0]),
        ("exp1_b2", "Built pipelines in Python.", [0.9, 0.1]),
        ("exp1_x1", "Ran jobs in Docker on Linux.", [0.8, 0.2]),
    ]:
        session.add(MasterBullets(bullet_id=bid, parent_id="exp1",
                                  parent_type="experience", text=text,
                                  embedding=emb, is_active=True))
    session.add(MasterBullets(bullet_id="proj1_b1", parent_id="proj1",
                              parent_type="project", text="Kafka ETL.",
                              embedding=[0.0, 1.0], is_active=True))
    session.add(MasterBullets(bullet_id="skill::Python", parent_id="__skills_pool__",
                              parent_type="skill", text="Python",
                              embedding=[0.9, 0.1], is_active=True))
    session.add(MasterTitleAliases(id="exp1::data::alias::Data Engineer",
                                   parent_id="exp1", block_id="exp1::data",
                                   alias="Data Engineer", embedding=[1.0, 0.0],
                                   is_active=True))

    profile = load_profile(session, json_path=json_path)

    assert len(profile.work) == 1
    entry = profile.work[0]
    assert entry.label == "Acme"                       # structure from JSON
    assert entry.kind == "work"
    block = entry.blocks[0]
    assert block.block_id == "exp1::data"
    assert block.entry_header == "Data Engineer at Acme, Pune"
    assert block.alias_embeddings == [[1.0, 0.0]]      # embeddings from DB
    assert [b.id for b in block.bullets] == ["exp1_b1", "exp1_b2", "exp1_x1"]
    assert block.bullets[0].is_summary is True
    assert block.bullets[2].is_extra is True, "extra_bullets load into the pool"
    assert block.bullets[0].embedding == [1.0, 0.0]

    project = profile.projects[0]
    assert project.label == "Pipeline Tool"
    assert project.link == "http://x"
    assert [s.skill for s in profile.skills] == ["Python"]


def test_load_profile_skips_a_deactivated_bullet(tmp_path) -> None:
    """A bullet removed from the YAML is deactivated, never deleted — it must
    drop out of selection without a zero vector standing in for it."""
    json_path = tmp_path / "master_profile.json"
    json_path.write_text(json.dumps(_valid_profile()))

    session = FakeSession()
    session.add(MasterBullets(bullet_id="exp1_b1", parent_id="exp1",
                              parent_type="experience", text="Kept it current.",
                              embedding=[1.0, 0.0], is_active=True))

    profile = load_profile(session, json_path=json_path)
    ids = [b.id for b in profile.work[0].blocks[0].bullets]
    assert ids == ["exp1_b1"]


def test_load_profile_missing_json_raises(tmp_path) -> None:
    with pytest.raises(mp.MasterProfileError, match="not found"):
        load_profile(FakeSession(), json_path=tmp_path / "nope.json")
