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
    return {
        "personal": {"name": "Jane Doe", "email": "jane@example.com"},
        "summaries": [
            {"id": "sum1", "text": "Data engineer summary.", "role_categories": ["data"]},
        ],
        "work_experience": [
            {
                "id": "exp1", "company": "Acme", "actual_title": "Data Engineer",
                "safe_title_aliases": ["Data Engineer", "Analytics Engineer"],
                "start_date": "2023-01", "end_date": "present",
                "bullet_pool": [
                    {"id": "exp1_b1", "text": "Built pipelines in Python."},
                    {"id": "exp1_b2", "text": "Owned the SQL warehouse."},
                ],
            }
        ],
        "projects": [
            {
                "id": "proj1", "name": "Pipeline Tool", "link": "http://x",
                "bullet_pool": [
                    {"id": "proj1_b1", "text": "Streaming ETL with Kafka."},
                    {"id": "proj1_b2", "text": "Dashboards in Superset."},
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


def test_project_needs_two_bullets() -> None:
    bad = _valid_profile()
    bad["projects"][0]["bullet_pool"] = [{"id": "proj1_b1", "text": "only one"}]
    with pytest.raises(ValueError, match="bullet_pool needs >= 2"):
        MasterProfile.model_validate(bad)


def test_duplicate_bullet_id_rejected() -> None:
    bad = _valid_profile()
    bad["projects"][0]["bullet_pool"][1]["id"] = "exp1_b1"  # collides
    with pytest.raises(ValueError, match="duplicate bullet id"):
        MasterProfile.model_validate(bad)


def test_empty_skills_pool_rejected() -> None:
    bad = _valid_profile()
    bad["skills_pool"] = []
    with pytest.raises(ValueError, match="skills_pool is empty"):
        MasterProfile.model_validate(bad)


# ---------------------------------------------------------------------------
# Desired-set builder
# ---------------------------------------------------------------------------


def test_desired_bullets_includes_skills_and_project_names() -> None:
    p = MasterProfile.model_validate(_valid_profile())
    desired = desired_bullets(p)
    by_type = defaultdict(list)
    for d in desired:
        by_type[d.parent_type].append(d)
    assert {b.id for b in by_type["experience"]} == {"exp1_b1", "exp1_b2"}
    assert {b.id for b in by_type["project"]} == {"proj1_b1", "proj1_b2"}
    assert {b.text for b in by_type["skill"]} == {"Python", "SQL", "Airflow"}
    assert by_type["project_name"][0].text == "Pipeline Tool"
    assert by_type["project_name"][0].id == "projname::proj1"


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
    # 2 exp bullets + 2 proj bullets + 1 project_name + 3 skills = 8
    assert report.bullets_inserted == 8
    assert report.summaries_inserted == 1
    assert report.aliases_inserted == 2  # two safe_title_aliases
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
    session.add(MasterBullets(bullet_id="exp1_b1", parent_id="exp1",
                              parent_type="experience", text="Built pipelines.",
                              embedding=[1.0, 0.0], is_active=True))
    session.add(MasterBullets(bullet_id="proj1_b1", parent_id="proj1",
                              parent_type="project", text="Kafka ETL.",
                              embedding=[0.0, 1.0], is_active=True))
    session.add(MasterBullets(bullet_id="projname::proj1", parent_id="proj1",
                              parent_type="project_name", text="Pipeline Tool",
                              embedding=[0.5, 0.5], is_active=True))
    session.add(MasterBullets(bullet_id="skill::Python", parent_id="__skills_pool__",
                              parent_type="skill", text="Python",
                              embedding=[0.9, 0.1], is_active=True))
    session.add(MasterTitleAliases(id="exp1::alias::Data Engineer", parent_id="exp1",
                                   alias="Data Engineer", embedding=[1.0, 0.0],
                                   is_active=True))
    session.add(MasterSummaries(summary_id="sum1", text="Data engineer summary.",
                                role_categories=["data"], embedding=[0.7, 0.7],
                                is_active=True))

    profile = load_profile(session, json_path=json_path)

    assert len(profile.experiences) == 1
    exp = profile.experiences[0]
    assert exp.company == "Acme"                      # structure from JSON
    assert exp.bullets[0].embedding == [1.0, 0.0]     # embedding from DB
    assert exp.safe_title_aliases == ["Data Engineer"]
    assert exp.alias_embeddings == [[1.0, 0.0]]
    proj = profile.projects[0]
    assert proj.name == "Pipeline Tool"
    assert proj.name_embedding == [0.5, 0.5]          # project_name row
    assert [s.skill for s in profile.skills] == ["Python"]
    assert profile.summaries[0].role_categories == ["data"]


def test_load_profile_missing_json_raises(tmp_path) -> None:
    with pytest.raises(mp.MasterProfileError, match="not found"):
        load_profile(FakeSession(), json_path=tmp_path / "nope.json")
