"""Layer 4 — selection, ordering, and the final score.

The greedy set-cover tests are the heart of this file. Each one pins a rule that
is cheap to break silently: the covered-set resetting per entry, the summary
bullet staying pinned even when a denser bullet exists, the cap beating gain, and
the early stop never firing below the floor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.config import settings
from src.scorer.apply_decision import evaluate, recency_score, seniority_score
from src.scorer.keywords import Keyword
from src.scorer.ordering import _recency_key, order_entries
from src.scorer.selector import (
    BulletCand,
    EntryCand,
    JDContext,
    Profile,
    RoleBlockCand,
    SkillCand,
    build_jd_context,
    bullet_cap,
    select_entries,
    select_entry_bullets,
)

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)
V = [1.0, 0.0, 0.0]
W = [0.0, 1.0, 0.0]


def _kw(*tokens: str) -> tuple[Keyword, ...]:
    return tuple(Keyword(t, 1.0) for t in tokens)


def _jd(vec_role=None, vec_match=None, **kw) -> JDContext:
    return JDContext(
        vec_role=vec_role or V,
        vec_match=vec_match or V,
        role_category=kw.get("role_category"),
        role_level=kw.get("role_level"),
        posted_at=kw.get("posted_at"),
        scraped_at=kw.get("scraped_at"),
        scrape_window_hours=kw.get("scrape_window_hours"),
    )


def _bullet(bid, text, *, vec=None, summary=False, block="e1::data", role="data"):
    return BulletCand(bid, text, vec or V, block_id=block, role=role, is_summary=summary)


def _block(block_id="e1::data", role="data", *, bullets, aliases=("Data Engineer",),
           alias_vecs=None, fit="primary", header="Data Engineer at Acme, Pune",
           dates="Jan 2024 to current", checklist=()):
    return RoleBlockCand(
        block_id=block_id, role=role, role_fit=fit, entry_header=header,
        entry_dates=dates, checklist=tuple(checklist), title_aliases=list(aliases),
        alias_embeddings=list(alias_vecs if alias_vecs is not None else [V]),
        bullets=bullets,
    )


def _entry(eid="e1", kind="work", *, blocks, start="2024-01", end="present", link=""):
    return EntryCand(
        id=eid, kind=kind, label="Acme", blocks=blocks,
        link=link, actual_title="Data Engineer",
        safe_title_aliases=["Data Engineer"], start_date=start, end_date=end,
    )


# ---------------------------------------------------------------------------
# Tenure cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start,end,expected", [
    ("2026-02", "2026-06", 3),    # 4 months  -> under 6
    ("2025-06", "2026-06", 6),    # 12 months -> under 18
    ("2023-12", "2026-06", 8),    # 30 months -> the max
])
def test_tenure_bands(start, end, expected) -> None:
    """The method: "less than six months? you need three, not five"."""
    e = _entry(blocks=[_block(bullets=[])], start=start, end=end)
    assert bullet_cap(e, NOW) == expected


def test_present_end_date_is_measured_against_now() -> None:
    e = _entry(blocks=[_block(bullets=[])], start="2026-03", end="present")
    assert bullet_cap(e, NOW) == 3  # 3 months as of NOW


def test_projects_take_a_flat_cap_having_no_dates() -> None:
    e = _entry("p1", "project", blocks=[_block(bullets=[])], start="", end="")
    assert bullet_cap(e, NOW) == int(settings.selection.bullets.project_cap)


# ---------------------------------------------------------------------------
# The greedy set-cover
# ---------------------------------------------------------------------------


def test_summary_bullet_is_pinned_first_even_when_a_denser_bullet_exists() -> None:
    """bullets[0] leads the entry regardless of coverage — the method requires an
    entry to open with the plain-language line."""
    bullets = [
        _bullet("b0", "Kept the data current for analysts.", summary=True),
        _bullet("b1", "Built pipelines in Python and SQL and Docker."),
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Python", "SQL", "Docker"),
        now=NOW,
    )
    assert out.bullets[0].id == "b0"
    assert out.bullets[0].is_summary


def test_greedy_picks_the_largest_NEW_keyword_gain_not_the_largest_total() -> None:
    """b2 names more keywords overall, but b3 is the only source of Docker."""
    bullets = [
        _bullet("b0", "Summary line.", summary=True),
        _bullet("b1", "Worked with Python and SQL."),
        _bullet("b2", "Used Python and SQL again."),        # 2 tokens, 0 new
        _bullet("b3", "Ran things in Docker."),             # 1 token, 1 new
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Python", "SQL", "Docker"),
        now=NOW,
    )
    ids = [b.id for b in out.bullets]
    assert "b3" in ids, "the uniquely-covering bullet must be selected"
    assert "b2" not in ids, "a bullet adding nothing new must not be selected"


def test_a_repeating_bullet_IS_selected_when_it_also_brings_something_new() -> None:
    """Not having a keyword is more damaging than saying one twice."""
    bullets = [
        _bullet("b0", "Summary line.", summary=True),
        _bullet("b1", "Worked with Python."),
        _bullet("b2", "Worked with Python and Docker."),   # repeats Python, adds Docker
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Python", "Docker"), now=NOW,
    )
    assert "b2" in [b.id for b in out.bullets]


def test_a_near_duplicate_bullet_is_never_selected() -> None:
    """Replaces the old bullet_groups machinery: a restatement has zero gain."""
    bullets = [
        _bullet("b0", "Summary line.", summary=True),
        _bullet("b1", "Built pipelines in Python."),
        _bullet("b2", "Constructed pipelines using Python."),  # same keyword, new words
        _bullet("b3", "Queried with SQL."),
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Python", "SQL"), now=NOW,
    )
    ids = [b.id for b in out.bullets]
    assert "b2" not in ids


def test_the_same_sentence_is_never_rendered_twice_in_one_entry() -> None:
    """Regression, found on a real ad rather than by a unit test.

    The extractor writes each accomplishment "re-worded in every block it
    honestly serves", so an entry legitimately holds near-identical bullets under
    different ids. The greedy alone does not catch them: once coverage is
    exhausted every candidate has gain 0, and the floor then fills the remaining
    slots by cosine — which picked the twin of a bullet already on the page.
    """
    twin = "Scraped signals with asyncio and Playwright for the desk."
    entry = _entry(blocks=[
        _block("e1::data", "data", bullets=[
            _bullet("d0", "Summary.", summary=True, block="e1::data"),
            _bullet("d1", twin, block="e1::data"),
        ]),
        _block("e1::quant", "quant", fit="adjacent", aliases=["Quant Researcher"],
               bullets=[_bullet("q1", twin, block="e1::quant", role="quant")]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw("Kubernetes"), now=NOW)
    texts = [b.text for b in out.bullets]
    assert len(texts) == len(set(texts)), f"a sentence repeated: {texts}"


def test_the_lead_blocks_wording_wins_a_text_collision() -> None:
    """When two blocks say the same thing, keep the one aimed at this JD."""
    twin = "Built ingestion jobs for the trading desk."
    entry = _entry(blocks=[
        _block("e1::data", "data", alias_vecs=[W], bullets=[
            _bullet("d0", "S.", summary=True, block="e1::data"),
            _bullet("d1", twin, block="e1::data"),
        ]),
        _block("e1::backend", "backend", alias_vecs=[V], bullets=[
            _bullet("k0", "S.", summary=True, block="e1::backend", role="backend"),
            _bullet("k1", twin, block="e1::backend", role="backend"),
        ]),
    ])
    out = select_entry_bullets(entry, _jd(vec_role=V), _kw(), now=NOW)
    assert out.block_id == "e1::backend"
    assert "d1" not in [b.id for b in out.bullets]


def test_the_covered_set_RESETS_for_every_entry() -> None:
    """The rule a global greedy would break.

    The method grades the first entry on whether it clears the checklist alone, so
    a keyword the first entry used must remain available to the second. Both
    entries here should independently select their Python bullet.
    """
    kws = _kw("Python")
    entries = [
        _entry("e1", blocks=[_block("e1::data", bullets=[
            _bullet("a0", "Summary.", summary=True, block="e1::data"),
            _bullet("a1", "Built things in Python.", block="e1::data"),
        ])]),
        _entry("e2", blocks=[_block("e2::data", bullets=[
            _bullet("c0", "Summary.", summary=True, block="e2::data"),
            _bullet("c1", "Wrote scripts in Python.", block="e2::data"),
        ])]),
    ]
    outs = [select_entry_bullets(e, _jd(), kws, now=NOW) for e in entries]
    assert "a1" in [b.id for b in outs[0].bullets]
    assert "c1" in [b.id for b in outs[1].bullets], "the second entry was starved"
    assert outs[0].coverage == outs[1].coverage == pytest.approx(1.0)


def test_cap_beats_gain() -> None:
    """A four-month job shows three bullets no matter how much it could cover."""
    bullets = [_bullet("b0", "Summary.", summary=True)] + [
        _bullet(f"b{i}", f"Used Tool{i} in production.") for i in range(1, 20)
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)], start="2026-02", end="2026-06"),
        _jd(), _kw(*[f"Tool{i}" for i in range(1, 20)]), now=NOW,
    )
    assert len(out.bullets) == 3


def test_zero_gain_stops_the_fill_above_the_floor() -> None:
    bullets = [_bullet("b0", "Summary with Python.", summary=True)] + [
        # Distinct wording: identical text is deduped out of the pool, which
        # would make this test pass for the wrong reason.
        _bullet(f"b{i}", f"Did unrelated thing number {i} for the team.")
        for i in range(1, 8)
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Python"), now=NOW,
    )
    # floor is 3: the summary plus two filler bullets, then it stops rather than
    # padding to the cap of 8.
    assert len(out.bullets) == int(settings.selection.bullets.min_per_entry)


def test_the_floor_overrides_the_early_stop() -> None:
    """An entry showing one bullet is not a valid entry."""
    bullets = [
        _bullet("b0", "Summary, nothing matching.", summary=True),
        _bullet("b1", "Also nothing matching."),
        _bullet("b2", "Still nothing."),
        _bullet("b3", "Nothing again."),
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Kubernetes"), now=NOW,
    )
    assert len(out.bullets) >= int(settings.selection.bullets.min_per_entry)


def test_an_empty_checklist_still_produces_a_valid_entry() -> None:
    """A JD parse can yield no skills; that must not divide by zero or return 1."""
    bullets = [_bullet("b0", "Summary.", summary=True)] + [
        _bullet(f"b{i}", f"Did thing {i}.") for i in range(1, 6)
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), (), now=NOW,
    )
    assert out.coverage == 0.0
    assert len(out.bullets) >= int(settings.selection.bullets.min_per_entry)


def test_new_keywords_are_recorded_per_bullet_for_audit() -> None:
    bullets = [
        _bullet("b0", "Summary.", summary=True),
        _bullet("b1", "Built in Python."),
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Python"), now=NOW,
    )
    picked = next(b for b in out.bullets if b.id == "b1")
    assert picked.new_keywords == ["Python"]


# ---------------------------------------------------------------------------
# Cross-block pooling and the off-role penalty
# ---------------------------------------------------------------------------


def test_bullets_are_pooled_across_all_blocks_of_an_entry() -> None:
    """A keyword only the quant block covers must still be reachable."""
    entry = _entry(blocks=[
        _block("e1::data", "data", bullets=[
            _bullet("d0", "Summary.", summary=True, block="e1::data"),
            _bullet("d1", "Built in Python.", block="e1::data"),
        ]),
        _block("e1::quant", "quant", fit="adjacent", aliases=["Quant Researcher"],
               bullets=[_bullet("q1", "Ran jobs in Docker.", block="e1::quant",
                                role="quant")]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw("Python", "Docker"), now=NOW)
    assert "q1" in [b.id for b in out.bullets]
    assert out.coverage == pytest.approx(1.0)


def test_an_on_role_bullet_wins_a_tie_against_an_off_role_one() -> None:
    """The soft penalty: equal JD gain, so the lead block's bullet takes the slot."""
    entry = _entry(blocks=[
        _block("e1::data", "data", alias_vecs=[V], bullets=[
            _bullet("d0", "Summary.", summary=True, block="e1::data"),
            _bullet("d1", "Shipped pipelines with Docker.", block="e1::data"),
        ]),
        # W is orthogonal to the JD role vector, so this block scores ~0.
        # Different wording, same keyword — identical text would be deduped.
        _block("e1::quant", "quant", fit="adjacent", alias_vecs=[W], bullets=[
            _bullet("q1", "Ran backtests inside Docker.", block="e1::quant",
                    role="quant"),
        ]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw("Docker"), now=NOW)
    ids = [b.id for b in out.bullets]
    assert ids.index("d1") < ids.index("q1")


def test_the_lead_block_supplies_the_header_and_dates() -> None:
    entry = _entry(blocks=[
        _block("e1::data", "data", alias_vecs=[W], header="Data Engineer at Acme, Pune",
               bullets=[_bullet("d0", "S.", summary=True, block="e1::data")]),
        _block("e1::backend", "backend", alias_vecs=[V],
               header="Backend Developer at Acme, Pune",
               bullets=[_bullet("k0", "S.", summary=True, block="e1::backend",
                                role="backend")]),
    ])
    out = select_entry_bullets(entry, _jd(vec_role=V), _kw(), now=NOW)
    assert out.header_left == "Backend Developer at Acme, Pune"
    assert out.block_id == "e1::backend"


def test_a_primary_block_wins_a_tie_against_an_adjacent_one() -> None:
    entry = _entry(blocks=[
        _block("e1::adj", "adj", fit="adjacent", alias_vecs=[V], header="ADJ",
               bullets=[_bullet("a0", "S.", summary=True, block="e1::adj")]),
        _block("e1::pri", "pri", fit="primary", alias_vecs=[V], header="PRI",
               bullets=[_bullet("p0", "S.", summary=True, block="e1::pri")]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw(), now=NOW)
    assert out.header_left == "PRI"


def test_a_project_puts_its_link_in_the_header_right_slot() -> None:
    entry = _entry("p1", "project", link="https://github.com/x/y", blocks=[
        _block("p1::backend", "backend", dates="",
               bullets=[_bullet("p0", "S.", summary=True, block="p1::backend")]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw(), now=NOW)
    assert out.header_right == "https://github.com/x/y"


# ---------------------------------------------------------------------------
# Entry selection and ordering
# ---------------------------------------------------------------------------


def _simple_entry(eid, text, *, end="present"):
    return _entry(eid, blocks=[_block(f"{eid}::data", bullets=[
        _bullet(f"{eid}_0", "Summary.", summary=True, block=f"{eid}::data"),
        _bullet(f"{eid}_1", text, block=f"{eid}::data"),
    ])], end=end)


def test_entries_force_include_the_minimum_below_threshold() -> None:
    """W is orthogonal to the entries' vectors, so similarity and coverage are
    both 0 and nothing clears the threshold — two are force-included anyway,
    because a resume with no work history is not a resume."""
    entries = [_simple_entry(f"e{i}", "Nothing relevant.") for i in range(4)]
    out = select_entries(
        entries, _jd(vec_role=W, vec_match=W), _kw("Kubernetes"), kind="work", now=NOW,
    )
    assert all(e.score < settings.selection.work.threshold for e in out)
    assert len(out) == int(settings.selection.work.min_shown)


def test_entries_cap_at_max_shown() -> None:
    entries = [_simple_entry(f"e{i}", "Built with Python.") for i in range(5)]
    out = select_entries(entries, _jd(), _kw("Python"), kind="work", now=NOW)
    assert len(out) <= int(settings.selection.work.max_shown)


def test_order_entries_best_match_first_when_gap_large() -> None:
    a, b = _simple_entry("e1", "x", end="2020-01"), _simple_entry("e2", "y")
    sa = select_entries([a], _jd(), _kw(), kind="work", now=NOW)[0]
    sb = select_entries([b], _jd(), _kw(), kind="work", now=NOW)[0]
    sa.score, sb.score = 0.9, 0.1   # a gap no calibration will ever exceed
    assert order_entries([sb, sa])[0] is sa


def test_order_entries_recency_when_gap_small() -> None:
    """Derived from the configured gap, not a literal — recalibration moves it.

    Stage 6 measured the real best-second gap at p50=0.015 and set the config to
    the p90 (0.047). A hardcoded 0.05 was "small" against the old inert 0.20 and
    is large against the measured value, so the literal tested the opposite of
    its own name."""
    gap = float(settings.selection.work.match_then_recency_gap)
    a, b = _simple_entry("e1", "x", end="2020-01"), _simple_entry("e2", "y")
    sa = select_entries([a], _jd(), _kw(), kind="work", now=NOW)[0]
    sb = select_entries([b], _jd(), _kw(), kind="work", now=NOW)[0]
    sa.score, sb.score = 0.50 + gap / 2, 0.50
    assert order_entries([sa, sb])[0] is sb  # "present" is newest


def test_recency_key_present_sorts_newest() -> None:
    assert _recency_key("present") > _recency_key("2026-01")
    assert _recency_key("") == (0, 0)


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
# evaluate() — end-to-end
# ---------------------------------------------------------------------------


def _full_profile() -> Profile:
    def mk(eid, kind, text):
        return _entry(eid, kind, blocks=[_block(f"{eid}::data", bullets=[
            _bullet(f"{eid}_0", "Kept the data current.", summary=True,
                    block=f"{eid}::data"),
            _bullet(f"{eid}_1", text, block=f"{eid}::data"),
            _bullet(f"{eid}_2", "Ran services in Docker.", block=f"{eid}::data"),
        ])], link="http://x")

    return Profile(
        work=[mk("e1", "work", "Built pipelines in Python and SQL."),
              mk("e2", "work", "Wrote ETL in Python.")],
        projects=[mk("p1", "project", "Analysed data with Python."),
                  mk("p2", "project", "Modelled with Python.")],
        skills=[SkillCand("Python", V)],
    )


def test_evaluate_returns_work_then_projects_in_render_order() -> None:
    result = evaluate(_full_profile(), _jd(posted_at=NOW), keywords=_kw("Python"), now=NOW)
    kinds = [e.kind for e in result.entries]
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "work" else 1)
    assert result.entries[0].kind == "work"


def test_coverage_is_the_union_and_lead_is_the_first_entry_alone() -> None:
    result = evaluate(
        _full_profile(), _jd(posted_at=NOW), keywords=_kw("Python", "Docker", "Kafka"),
        now=NOW,
    )
    assert result.lead_entry_coverage <= result.keyword_coverage
    assert 0.0 <= result.keyword_coverage <= 1.0
    # Kafka appears in no bullet, so full coverage is impossible.
    assert result.keyword_coverage < 1.0


def test_fit_is_experience_plus_coverage() -> None:
    result = evaluate(_full_profile(), _jd(posted_at=NOW), keywords=_kw("Python"), now=NOW)
    cfg = settings.scoring.fit
    best = max(e.score for e in result.work)
    expected = cfg.best_experience * best + cfg.keyword_coverage * result.keyword_coverage
    assert result.fit == pytest.approx(expected)


def test_evaluate_weak_match_skips_with_low_score() -> None:
    profile = Profile(
        work=[_simple_entry("e1", "Nothing relevant."),
              _simple_entry("e2", "Nothing relevant either.")],
        projects=[], skills=[],
    )
    result = evaluate(
        profile, _jd(vec_role=W, vec_match=W), keywords=_kw("Kubernetes"), now=NOW,
    )
    assert result.apply is False
    assert result.reason_category == "LOW_SCORE"


def test_build_jd_context_makes_three_embeds_not_three_plus_skills() -> None:
    """The per-skill vectors went with the Skills section; the batch is now flat 3."""
    from src.llm.schemas import JDParsed

    calls: list[list[str]] = []

    def fake_batch(texts):
        calls.append(texts)
        return [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]

    parsed = JDParsed(
        role_summary="data engineer", role_category="data", role_level="junior",
        required_skills=["Python", "SQL", "Docker", "Kafka"], nice_to_have=["Spark"],
        responsibilities=["build pipelines"], years_required=2,
    )
    ctx = build_jd_context(parsed, embed_batch_fn=fake_batch)
    assert len(calls[0]) == 3, "one embed per JD skill was removed with the Skills section"
    assert ctx.vec_role == [1.0, 1.0]
