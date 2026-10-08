"""#30 — common bullets: written once, placed once per page, never blocking.

The property that matters most is the one the issue was held for: a common
bullet must never cost an entry one of its own keywords. Selection runs first;
common bullets are appended afterwards, so an entry's own picks are identical
with or without the pool. ``test_never_changes_what_an_entry_chose`` pins it.
"""

from __future__ import annotations

import dataclasses
import json
from contextlib import contextmanager

import pytest

from src.config import settings
from src.endpoint.assembler import _index_profile
from src.scorer.apply_decision import evaluate
from src.scorer.common import place_common_bullets
from src.scorer.selector import CommonCand, Profile, SelectedBullet, SelectedEntry
from src.state.master_profile import MasterProfile, load_profile
from tests.test_iteration_2_master_profile import FakeSession, _valid_profile
from tests.test_iteration_2_scorer import NOW, _block, _bullet, _entry, _jd, _kw

COMM = CommonCand(
    "common_comm", ("communication",),
    "Explained each finding to non-specialists in written reports, showing clear "
    "communication.",
    (("communication", "communication"),),
)
TEAM = CommonCand(
    "common_team", ("collaboration",),
    "Worked with analysts to agree requirements, showing teamwork.",
    (("collaboration", "teamwork"),),
)
BOTH = CommonCand(
    "common_comm_team", ("communication", "collaboration"),
    "Agreed requirements with analysts through clear communication and teamwork.",
    (("communication", "communication"), ("collaboration", "teamwork")),
)
AGILE = CommonCand(
    "common_agile", ("agile",),
    "Planned the work in Agile sprints with CI/CD checks.",
    (("agile", "Agile"),),
)
AI = CommonCand(
    "common_ai", ("ai_tools",),
    "Used AI coding tools such as Claude Code to draft and review changes.",
    (("ai_tools", "Claude Code"),), gated=True,
)


@contextmanager
def _common_cfg(**overrides):
    data = settings.selection.common_bullets._data
    saved = {k: data[k] for k in overrides}
    data.update(overrides)
    try:
        yield
    finally:
        data.update(saved)


def _mk(eid, kind, *texts, employment=True):
    bullets = [_bullet(f"{eid}_0", "Kept the data current.", summary=True,
                       block=f"{eid}::data")]
    bullets += [_bullet(f"{eid}_{i}", t, block=f"{eid}::data")
                for i, t in enumerate(texts, start=1)]
    e = _entry(eid, kind, blocks=[_block(f"{eid}::data", bullets=bullets)],
               link="http://x")
    if kind == "work" and not employment:
        e.employment_type = "freelance"
    return e


def _profile(common=()):
    return Profile(
        work=[_mk("e1", "work", "Built pipelines in Python.", "Shipped with CI/CD and Docker.")],
        projects=[_mk("p1", "project", "Analysed sales data with Python and SQL.")],
        skills=[],
        common=list(common),
    )


def _own_bullets(result):
    return {e.id: [b.id for b in e.bullets if b.via != "common"] for e in result.entries}


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


def test_an_asked_common_bullet_renders_once_on_the_job() -> None:
    kws = _kw("Python", "communication skills")
    result = evaluate(_profile([COMM]), _jd(posted_at=NOW), keywords=kws, now=NOW)

    hosts = [e for e in result.entries if any(b.id == "common_comm" for b in e.bullets)]
    assert [e.id for e in hosts] == ["e1"], "once per page, on the salaried job"
    placed = next(b for b in hosts[0].bullets if b.id == "common_comm")
    assert placed.via == "common"
    assert "communication skills" in hosts[0].covered
    assert result.keyword_coverage == 1.0


def test_an_unasked_common_bullet_renders_nowhere() -> None:
    result = evaluate(_profile([COMM]), _jd(posted_at=NOW), keywords=_kw("Python"), now=NOW)
    assert not any(b.id == "common_comm" for e in result.entries for b in e.bullets)


def test_never_changes_what_an_entry_chose() -> None:
    """The reason this module runs after selection: the Agile bullet also says
    CI/CD, which e1's own bullet carries alongside Docker. Seeded first, it would
    have barred that bullet and lost Docker. Appended last, it cannot."""
    kws = _kw("Python", "Agile", "CI/CD", "Docker")
    without = evaluate(_profile(), _jd(posted_at=NOW), keywords=kws, now=NOW)
    with_pool = evaluate(_profile([AGILE]), _jd(posted_at=NOW), keywords=kws, now=NOW)

    assert _own_bullets(with_pool) == _own_bullets(without)
    assert "Docker" in next(e for e in with_pool.entries if e.id == "e1").covered
    # e1 already says CI/CD, so the Agile line goes to the entry that doesn't.
    host = next(e for e in with_pool.entries if any(b.via == "common" for b in e.bullets))
    assert host.id == "p1"
    assert with_pool.keyword_coverage >= without.keyword_coverage


def test_skipped_when_the_page_already_shows_everything_it_adds() -> None:
    profile = _profile([COMM])
    profile.projects[0].blocks[0].bullets.append(
        _bullet("p1_9", "Wrote up results with clear communication.", block="p1::data")
    )
    result = evaluate(profile, _jd(posted_at=NOW), keywords=_kw("communication"), now=NOW)
    assert not any(b.via == "common" for e in result.entries for b in e.bullets)


def test_max_per_page_holds() -> None:
    kws = _kw("Python", "communication", "Agile")
    with _common_cfg(max_per_page=1):
        result = evaluate(_profile([COMM, AGILE]), _jd(posted_at=NOW), keywords=kws, now=NOW)
    placed = [b.id for e in result.entries for b in e.bullets if b.via == "common"]
    assert placed == ["common_comm"]


def test_host_less_bullets_spread_instead_of_stacking() -> None:
    """Both prefer the salaried job; with one per entry the second moves on."""
    job, proj = _entry_out("e1", "x"), _entry_out("p1", "y", kind="project")
    placed = place_common_bullets([job, proj], [COMM, AGILE],
                                  _kw("communication", "Agile"), _jd())
    assert placed == ["common_comm", "common_agile"]
    assert job.bullets[-1].id == "common_comm"
    assert proj.bullets[-1].id == "common_agile"


def test_disabled_places_nothing() -> None:
    with _common_cfg(enabled=False):
        result = evaluate(_profile([COMM]), _jd(posted_at=NOW),
                          keywords=_kw("communication"), now=NOW)
    assert not any(b.via == "common" for e in result.entries for b in e.bullets)


def _entry_out(eid, *texts, cap=8, covered=(), kind="work"):
    return SelectedEntry(
        id=eid, kind=kind, block_id=f"{eid}::data", label=eid, header_left="",
        header_right="", bullets=[SelectedBullet(f"{eid}_{i}", t, 0.0) for i, t in enumerate(texts)],
        covered=set(covered), coverage=0.0, similarity=0.0, score=0.0, cap=cap,
    )


def test_a_full_entry_passes_the_bullet_on() -> None:
    job = _entry_out("e1", "a", "b", "c", cap=3)
    proj = _entry_out("p1", "a", kind="project")
    placed = place_common_bullets([job, proj], [COMM], _kw("communication"), _jd())
    assert placed == ["common_comm"]
    assert len(job.bullets) == 3, "the cap still holds"
    assert proj.bullets[-1].id == "common_comm"


def test_the_ai_line_opens_on_the_generic_ask() -> None:
    jd = dataclasses.replace(_jd(), ai_tooling_asked=True)
    job = _entry_out("e1", "Kept the data current.")
    assert place_common_bullets([job], [AI], _kw("Python"), jd) == ["common_ai"]
    assert place_common_bullets([_entry_out("e1", "x")], [AI], _kw("Python"), _jd()) == []


def test_the_ai_line_is_credited_with_tool_names_only() -> None:
    """Every entry already says "AI" or "GitHub"; counting those incidental
    words made the line restate a keyword in every host, so it never placed."""
    ai = dataclasses.replace(
        AI, text="Used AI coding tools such as Claude Code and GitHub Copilot daily.",
        triggers=(("ai_tools", "Claude Code"), ("ai_tools", "GitHub Copilot")),
        norm_text="",  # recomputed from the new text
    )
    job = _entry_out("e1", "Built AI services on GitHub.", covered=("AI", "GitHub"))
    kws = _kw("AI", "GitHub", "Claude Code", "GitHub Copilot")
    assert place_common_bullets([job], [ai], kws, _jd()) == ["common_ai"]
    assert job.covered == {"AI", "GitHub", "Claude Code", "GitHub Copilot"}


def test_a_broad_keyword_does_not_ask_for_a_narrower_trigger() -> None:
    """"GitHub" sits inside the trigger "GitHub Copilot" but asks for no AI tool;
    "management" sits inside "stakeholder management" but asks for nothing."""
    ai = dataclasses.replace(AI, triggers=(("ai_tools", "GitHub Copilot"),))
    comm = dataclasses.replace(COMM, triggers=(("communication", "stakeholder management"),))
    job = _entry_out("e1", "x")
    assert place_common_bullets([job], [ai, comm], _kw("GitHub", "management"), _jd()) == []


def test_the_ai_line_never_doubles_a_page_line() -> None:
    jd = dataclasses.replace(_jd(), ai_tooling_asked=True)
    job = _entry_out("e1", "Used Claude Code to review every change.")
    assert place_common_bullets([job], [AI], _kw("Claude Code"), jd) == []


def test_a_hosted_bullet_renders_only_under_its_host() -> None:
    """The entry header is the claim's WHERE: a claim about the project must not
    print under the job, even though the job is the default host."""
    cb = dataclasses.replace(COMM, hosts=("p1",))
    job, proj = _entry_out("e1", "x"), _entry_out("p1", "y", kind="project")
    assert place_common_bullets([job, proj], [cb], _kw("communication"), _jd()) == ["common_comm"]
    assert [b.id for b in job.bullets] == ["e1_0"]
    assert proj.bullets[-1].id == "common_comm"


def test_a_hosted_bullet_whose_host_is_off_the_page_renders_nowhere() -> None:
    cb = dataclasses.replace(COMM, hosts=("p9",))
    job = _entry_out("e1", "x")
    assert place_common_bullets([job], [cb], _kw("communication"), _jd()) == []


def test_one_bullet_per_family_per_page() -> None:
    """Communication at the job and at a freelance gig: both hosts on the page,
    only the first renders."""
    at_job = dataclasses.replace(COMM, id="comm_job", hosts=("e1",))
    at_gig = dataclasses.replace(COMM, id="comm_gig", hosts=("p1",), norm_text="",
                                 text="Wrote weekly updates with clear communication.")
    job, proj = _entry_out("e1", "x"), _entry_out("p1", "y", kind="project")
    placed = place_common_bullets([job, proj], [at_job, at_gig], _kw("communication"), _jd())
    assert placed == ["comm_job"]


def test_a_combined_line_beats_two_singles_when_both_are_asked() -> None:
    job, proj = _entry_out("e1", "x"), _entry_out("p1", "y", kind="project")
    kws = _kw("communication", "teamwork")
    assert place_common_bullets([job, proj], [COMM, TEAM, BOTH], kws, _jd()) == ["common_comm_team"]


def test_a_combined_line_waits_until_the_advert_asks_for_both() -> None:
    job = _entry_out("e1", "x")
    assert place_common_bullets([job], [BOTH, COMM], _kw("communication"), _jd()) == ["common_comm"]


def test_a_role_variant_goes_only_where_that_role_leads() -> None:
    data_only = dataclasses.replace(COMM, roles=("data",))
    job = _entry_out("e1", "x")                       # block "e1::data"
    assert place_common_bullets([job], [data_only], _kw("communication"), _jd()) == ["common_comm"]
    job.block_id = "e1::backend"
    job.bullets, job.covered = job.bullets[:1], set()
    assert place_common_bullets([job], [data_only], _kw("communication"), _jd()) == []


# ---------------------------------------------------------------------------
# Schema, loader, renderer
# ---------------------------------------------------------------------------


FAMILIES = {
    "communication": ["communication"],
    "collaboration": ["teamwork"],
    "agile": ["Agile"],
    "review": ["code review"],
    "ai_tools": ["Claude Code"],
}


def _with_common(*common, families=None):
    raw = _valid_profile()
    raw["common_families"] = families or FAMILIES
    raw["common_bullets"] = list(common)
    return raw


def _cb(id_, families, text, **kw):
    return {"id": id_, "families": families, "text": text, **kw}


def test_common_bullets_parse() -> None:
    p = MasterProfile.model_validate(_with_common(
        _cb("c1", ["communication"], "Explained results with clear communication."),
        _cb("c2", ["communication", "collaboration"],
            "Agreed scope through clear communication and teamwork.", hosts=["exp1"]),
    ))
    assert [c.families for c in p.common_bullets] == [
        ["communication"], ["communication", "collaboration"]
    ]


@pytest.mark.parametrize("common,families,match", [
    ([_cb("exp1_b1", ["communication"], "Clear communication.")], None, "duplicate bullet id"),
    ([], {"a": ["Agile"], "b": ["agile"]}, "trigger"),
    ([_cb("c1", ["nope"], "t.")], None, "unknown family"),
    ([_cb("c1", ["communication"], "Clear communication.", hosts=["zzz"])], None, "unknown host"),
    ([_cb("c1", ["agile"], "Ran Agile sprints with code review.")], None,
     "must not share a keyword"),
    ([_cb("c1", ["communication", "collaboration"], "Clear communication.")], None,
     "states none of their triggers"),
])
def test_common_bullets_validation(common, families, match) -> None:
    with pytest.raises(ValueError, match=match):
        MasterProfile.model_validate(_with_common(*common, families=families))


def test_load_profile_carries_common_bullets(tmp_path) -> None:
    json_path = tmp_path / "master_profile.json"
    json_path.write_text(json.dumps(_with_common(
        _cb("c1", ["ai_tools"], "Used Claude Code daily.", gated=True, hosts=["exp1"],
            roles=["data"]),
    )))
    profile = load_profile(FakeSession(), json_path=json_path)
    c = profile.common[0]
    assert (c.id, c.gated, c.hosts, c.roles) == ("c1", True, ("exp1",), ("data",))
    assert c.triggers == (("ai_tools", "Claude Code"),)


def test_the_renderer_resolves_common_bullet_text(tmp_path) -> None:
    path = tmp_path / "master_profile.json"
    path.write_text(json.dumps(_with_common(
        _cb("c1", ["communication"], "Explained results with clear communication."),
    )))
    index = _index_profile(path)
    assert index["c1"] == "Explained results with clear communication."
    assert index["exp1_b1"] == "Kept the warehouse current."
