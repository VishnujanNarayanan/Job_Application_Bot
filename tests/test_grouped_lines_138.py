"""Grouped keyword lines (#138): extras' keywords, and keywords a skipped main
proves, joined from hand-written phrases into a few dense lines."""

from contextlib import contextmanager

import pytest

from src.config import settings
from src.scorer import grouped_lines as gl
from src.scorer.keywords import Keyword
from src.scorer.selector import Profile, SelectedBullet, SelectedEntry
from src.state.master_profile import KeywordLines, Project
from tests.test_iteration_2_scorer import _block, _bullet, _entry, _kw

ENDING = ", so the crawl kept to its monthly schedule."


def _lines(**over):
    data = {
        "phrases": [
            {"id": "prefect", "kw": "Prefect", "group": "orchestration",
             "text": "ran one task per month with Prefect", "source": "x1"},
            {"id": "airflow", "kw": "Airflow", "group": "orchestration",
             "text": "moved the nightly jobs to Airflow", "source": "b1"},
            {"id": "docker", "kw": "Docker", "group": "devops",
             "text": "packaged every collector in Docker", "source": "x2"},
            {"id": "sqs", "kw": "SQS", "group": "cloud",
             "text": "queued live news in SQS", "source": "x3"},
        ],
        "frames": [{"id": "orch", "text": "scheduled the crawl with {kws}", "ending": ENDING,
                    "allowed": ["prefect", "airflow"]}],
    }
    data.update(over)
    return KeywordLines.model_validate(data)


@contextmanager
def _on(**over):
    data = settings.selection.grouped_lines.as_dict()
    saved = dict(data)
    data.update({"enabled": True, **over})
    try:
        yield
    finally:
        data.clear()
        data.update(saved)


# --- composition ---------------------------------------------------------------


def test_a_frame_line_holds_one_group_and_ends_with_its_own_why():
    text = gl.compose(_lines(), [gl.Part(("prefect", "airflow"), "orch")])
    assert text == "Scheduled the crawl with Prefect and Airflow, so the crawl kept to its monthly schedule."


def test_a_phrase_line_is_a_capitalised_list_with_no_ending():
    parts = [gl.Part(("prefect",)), gl.Part(("docker",)), gl.Part(("sqs",))]
    assert gl.compose(_lines(), parts) == (
        "Ran one task per month with Prefect, packaged every collector in Docker, "
        "and queued live news in SQS."
    )
    two = gl.compose(_lines(), [gl.Part(("docker",)), gl.Part(("sqs",))])
    assert two == "Packaged every collector in Docker and queued live news in SQS."


def test_a_frame_holding_one_phrase_reads_as_that_phrase():
    assert gl.compose(_lines(), [gl.Part(("sqs",), "orch")]) == "Queued live news in SQS."


def test_a_line_id_round_trips_and_replays_the_same_text():
    parts = [gl.Part(("prefect", "airflow"), "orch")]
    bid = gl.line_id("market-data", parts)
    assert gl.is_line(bid) and not gl.is_line("market_data_x1")
    entry, back = gl.parse_line_id(bid)
    assert entry == "market-data" and back == parts
    assert gl.text_of(bid, {"market-data": _lines()}) == gl.compose(_lines(), parts)


def test_a_removed_phrase_fails_loudly_rather_than_rendering_something_else():
    bid = gl.line_id("e1", [gl.Part(("gone",))])
    with pytest.raises(KeyError):
        gl.text_of(bid, {"e1": _lines()})


# --- profile checks ------------------------------------------------------------


def _project(lines):
    return Project.model_validate({
        "id": "p1", "name": "Crawler", "keyword_lines": lines,
        "role_blocks": [{
            "role": "data", "entry_header": "Crawler", "title_aliases": ["Data Engineer"],
            "bullets": [{"id": "b0", "text": "Built a news crawler."},
                        {"id": "b1", "text": "Moved the nightly jobs to Airflow."}],
            "extra_bullets": [{"id": "x1", "text": "Chose Prefect to run one task per month."},
                              {"id": "x2", "text": "Packaged every collector in Docker."},
                              {"id": "x3", "text": "Queued live news in SQS."}],
        }],
    })


def test_a_valid_profile_section_loads():
    assert _project(_lines().model_dump()).keyword_lines.frames[0].ending == ENDING


@pytest.mark.parametrize("change, message", [
    (lambda d: d["phrases"][0].update(source="nope"), "is not a bullet"),
    (lambda d: d["phrases"][2].update(source="x3"), "does not say 'Docker'"),
    (lambda d: d["phrases"][3].update(text="queued live news"), "does not say its keyword"),
    (lambda d: d["phrases"][3].update(text="queued live news in SQS."), "no closing punctuation"),
    (lambda d: d["frames"][0].update(allowed=["prefect", "kafka"]), "unknown phrases"),
    (lambda d: d["frames"][0].update(text="scheduled the crawl"), "exactly one"),
    (lambda d: d["frames"][0].update(ending=", so it ran"), "full stop"),
    (lambda d: d["frames"][0].update(ending=", so it packaged every collector in Docker."),
     "repeats a bullet"),
    (lambda d: d["phrases"][1].update(id="Air-flow"), "lower-case"),
])
def test_the_profile_rejects_a_phrase_it_cannot_prove_or_join(change, message):
    data = _lines().model_dump()
    change(data)
    with pytest.raises(ValueError, match=message):
        _project(data)


# --- building lines ------------------------------------------------------------


def _scene(page_ids, lines=None):
    bullets = [
        _bullet("b0", "Built a news crawler with Python.", summary=True),
        _bullet("b1", "Moved the nightly jobs to Airflow."),
        _bullet("x1", "Chose Prefect to run one task per month.", extra=True),
        _bullet("x2", "Packaged every collector in Docker.", extra=True),
        _bullet("x3", "Queued live news in SQS.", extra=True),
    ]
    cand = _entry(blocks=[_block(bullets=bullets)])
    cand.lines = lines if lines is not None else _lines()
    by_id = {b.id: b for b in bullets}
    se = SelectedEntry(
        id="e1", kind="work", block_id="e1::data", label="Acme", header_left="", header_right="",
        bullets=[SelectedBullet(i, by_id[i].text, 0.0, i == "b0") for i in page_ids],
        covered=set(), coverage=0.0, similarity=0.0, score=0.0, cap=5,
    )
    return se, Profile(work=[cand], projects=[], skills=[])


def test_three_one_keyword_extras_become_one_line():
    se, prof = _scene(["b0", "x1", "x2", "x3"])
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Docker", "SQS"))
    ids = [b.id for b in se.bullets]
    assert ids[0] == "b0" and len(ids) == 2 and gl.is_line(ids[1])
    assert {"Prefect", "Docker", "SQS"} <= set(se.bullets[1].new_keywords)


def test_lines_never_mix_a_frame_with_loose_phrases():
    se, prof = _scene(["b0", "x1", "x2", "x3"])
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Airflow", "Docker", "SQS"))
    for b in se.bullets:
        if gl.is_line(b.id):
            _, parts = gl.parse_line_id(b.id)
            assert len(parts) == 1 or not any(p.frame for p in parts)


def test_a_keyword_only_a_skipped_main_proves_joins_the_line():
    se, prof = _scene(["b0", "x1", "x2"])  # b1 (Airflow) was not chosen
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Docker", "Airflow"))
    text = " ".join(b.text for b in se.bullets if gl.is_line(b.id))
    assert "Airflow" in text and "Prefect" in text
    assert "Airflow" in se.covered


def test_the_entry_never_gets_longer():
    se, prof = _scene(["b0", "x1"])
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Docker", "SQS", "Airflow"))
    assert len(se.bullets) == 2


def test_no_phrase_repeats_a_keyword_the_entry_already_shows():
    se, prof = _scene(["b0", "b1", "x1", "x2", "x3"])  # b1 shows Airflow
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Airflow", "Prefect", "Docker", "SQS"))
    texts = " ".join(b.text for b in se.bullets)
    assert texts.count("Airflow") == 1


def test_a_keyword_no_line_can_hold_keeps_its_extra():
    se, prof = _scene(["b0", "x1", "x2", "x3"])
    with _on(max_chars=80, overflow_chars=0):  # room for two short clauses, not three
        gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Docker", "SQS"))
    text = " ".join(b.text for b in se.bullets)
    for kw in ("Prefect", "Docker", "SQS"):
        assert kw in text, "a keyword the entry showed is never lost"
    assert any(not gl.is_line(b.id) and b.id.startswith("x") for b in se.bullets)


def test_a_line_must_carry_at_least_two_keywords():
    se, prof = _scene(["b0", "x3"])
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "SQS"))
    assert [b.id for b in se.bullets] == ["b0", "x3"]


def test_off_changes_nothing():
    se, prof = _scene(["b0", "x1", "x2", "x3"])
    gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Docker", "SQS"))
    assert [b.id for b in se.bullets] == ["b0", "x1", "x2", "x3"]


def test_an_entry_without_phrases_is_left_alone():
    se, prof = _scene(["b0", "x1", "x2", "x3"])
    prof.work[0].lines = None
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Docker", "SQS"))
    assert [b.id for b in se.bullets] == ["b0", "x1", "x2", "x3"]


def test_two_unrelated_keywords_never_become_a_glued_list():
    se, prof = _scene(["b0", "x2", "x3"])  # Docker and SQS: two groups, no frame
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Docker", "SQS"))
    assert [b.id for b in se.bullets] == ["b0", "x2", "x3"], "two groups keep their extras"


def test_one_asked_keyword_makes_a_frame_line_completed_by_its_group():
    se, prof = _scene(["b0", "x1", "x2"])
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Docker"))
    text = " ".join(b.text for b in se.bullets if gl.is_line(b.id))
    assert "Prefect and Airflow" in text, "Prefect's frame filled with its unasked member"


def test_phrase_lines_read_in_the_order_the_work_happened():
    kl = _lines(phrases=[
        {"id": "docs", "kw": "documentation", "group": "process", "text": "wrote the documentation", "source": "x1"},
        {"id": "deploy", "kw": "Docker", "group": "devops", "text": "packaged it in Docker", "source": "x2"},
        {"id": "scrape", "kw": "SQS", "group": "collection", "text": "queued it in SQS", "source": "x3"},
    ], frames=[])
    cands = [gl._Cand(ph, {ph.kw}, {ph.kw}, 1.0) for ph in kl.phrases]
    lines = gl.build_lines("e1", kl, set(), cands, weights={}, max_lines=1, max_chars=200)
    ids = [p.phrases[0] for p in lines[0][0]]
    assert ids == ["scrape", "deploy", "docs"], "collection first, documentation last"


def test_a_phrase_line_that_cannot_be_completed_keeps_the_extras():
    se, prof = _scene(["b0", "x2", "x3"])
    with _on(max_chars=70, overflow_chars=0):  # room for the two asked phrases, not a third
        gl.apply_grouped_lines([se], prof, _kw("Python", "Docker", "SQS"))
    assert [b.id for b in se.bullets] == ["b0", "x2", "x3"]


def test_a_filler_never_names_a_tool_the_entry_already_shows():
    se, prof = _scene(["b0", "b1", "x2", "x3"])  # b1 names Airflow
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Docker", "SQS"))
    text = " ".join(b.text for b in se.bullets)
    assert text.count("Airflow") == 1


# --- the no-keyword-missed gate -------------------------------------------------


class _P:
    def __init__(self, *entries):
        self.entries = list(entries)


def _gaps(lines, extra_text=None):
    from src.state import keyword_lines

    proj = _project(lines)
    if extra_text:
        from src.state.master_profile import Bullet

        proj.role_blocks[0].extra_bullets.append(Bullet(id="x9", text=extra_text))
    return keyword_lines.check(_P(proj), {"Prefect", "Airflow", "Docker", "SQS", "Kafka"})


def test_every_proven_keyword_with_a_phrase_passes():
    assert _gaps(_lines().model_dump()).missing == 0


def test_a_new_keyword_in_a_bullet_without_a_phrase_fails_the_gate():
    report = _gaps(_lines().model_dump(), "Streamed live news through Kafka.")
    assert report.missing == 1
    assert report.entries[0].missing == [("Kafka", "x9")]
    assert "MISSING   Kafka" in report.render()


def test_a_deliberate_skip_passes_and_a_stale_one_is_reported():
    data = _lines().model_dump()
    data["skip"] = ["Kafka", "Hadoop"]
    report = _gaps(data, "Streamed live news through Kafka.")
    assert report.missing == 0
    assert report.entries[0].stale_skip == ["Hadoop"]


def test_phrases_outside_every_frame_are_listed_for_review():
    assert set(_gaps(_lines().model_dump()).entries[0].loose) == {"docker", "sqs"}


def test_an_asked_keyword_may_run_a_line_past_the_target_but_a_filler_may_not():
    se, prof = _scene(["b0", "x1", "x2", "x3"])
    with _on(max_chars=80, overflow_chars=60):
        gl.apply_grouped_lines([se], prof, _kw("Python", "Prefect", "Docker", "SQS"))
    line = next(b for b in se.bullets if gl.is_line(b.id))
    assert all(k in line.text for k in ("Prefect", "Docker", "SQS")), "asked keywords overflow the target"
    assert len(line.text) > 80


def test_a_free_slot_carries_required_skills_the_page_would_lose():
    se, prof = _scene(["b0", "b1"])  # no extras on the page, cap 5
    with _on():
        gl.apply_grouped_lines([se], prof, _kw("Python", "Airflow", "Prefect", "Docker", "SQS"))
    line = next((b for b in se.bullets if gl.is_line(b.id)), None)
    assert line is not None and all(k in line.text for k in ("Prefect", "Docker", "SQS"))
    assert len(se.bullets) == 3


def test_a_free_slot_is_never_used_for_nice_to_have_skills():
    se, prof = _scene(["b0", "b1"])
    kws = (*_kw("Python", "Airflow"), Keyword("Prefect", 0.5), Keyword("Docker", 0.5), Keyword("SQS", 0.5))
    with _on():
        gl.apply_grouped_lines([se], prof, kws)
    assert [b.id for b in se.bullets] == ["b0", "b1"]


def test_a_line_that_saves_a_missing_required_skill_beats_a_fuller_one():
    kl = _lines(frames=[
        {"id": "orch", "text": "scheduled the crawl with {kws}", "ending": ENDING, "allowed": ["prefect", "airflow"]},
        {"id": "ship", "text": "shipped it with {kws}", "ending": ", so releases were reviewed.", "allowed": ["docker", "sqs"]},
    ])
    ph = {p.id: p for p in kl.phrases}
    cands = [gl._Cand(ph[i], {ph[i].kw}, {ph[i].kw}, 1.0) for i in ("prefect", "airflow", "docker")]
    lines = gl.build_lines("e1", kl, set(), cands, weights={}, max_lines=1, max_chars=200, priority={"Docker"},
                           fillers=[ph["sqs"]])
    assert lines[0][0][0].frame == "ship", "the line carrying the missing required skill wins"
