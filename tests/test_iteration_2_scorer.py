"""Layer 4 — selection, ordering, and the final score.

The greedy set-cover tests are the heart of this file. Each one pins a rule that
is cheap to break silently: the covered-set resetting per entry, the summary
bullet staying pinned even when a denser bullet exists, the cap beating gain, and
the early stop never firing below the floor.
"""

from __future__ import annotations

from contextlib import contextmanager
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
    select_top,
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


def _bullet(bid, text, *, vec=None, summary=False, block="e1::data", role="data",
            extra=False):
    return BulletCand(bid, text, vec or V, block_id=block, role=role,
                      is_summary=summary, is_extra=extra)


@contextmanager
def _cfg(**overrides):
    """Temporarily override selection.bullets tunables.

    ``Section`` is a read-only view with ``__slots__``, so the override goes into
    its backing mapping rather than through setattr.
    """
    data = settings.selection.bullets._data
    saved = {k: data[k] for k in overrides}
    data.update(overrides)
    try:
        yield
    finally:
        data.update(saved)


_FILLERS = [
    "Documented the handover notes so the next reader started without a meeting.",
    "Sat with the operations desk each Friday to hear what broke that week.",
    "Rewrote the onboarding checklist after watching a new joiner stumble twice.",
    "Cut a weekly summary for the client so nobody chased status by email.",
    "Kept a decision log that let anyone see why a choice had been made.",
    "Ran a short retrospective after each release and acted on one item.",
    "Answered support questions in a shared channel rather than by direct message.",
]


def _filler(i: int) -> str:
    """Distinct, keyword-free prose.

    These must not read as rewords of each other. They are keyword-free, so what
    keeps them apart has to be the prose itself — seven variations on one sentence
    would make a test about cap or floor pass for the wrong reason.
    """
    return _FILLERS[(i - 1) % len(_FILLERS)]


def _no_qualification_fill():
    return _cfg(qualification_fill=False)


def _fake_canon(monkeypatch, mapping: dict[str, set[str]]) -> None:
    """Pin the qualification sheet for a test: bullet text -> canonical tokens.

    The real sheet is a 44 KB vendored file whose contents would make these tests
    assert on data rather than on logic. Both names are patched where the selector
    imported them, not at their source module.
    """
    monkeypatch.setattr(
        "src.scorer.selector.canonical_covered",
        lambda norm_text, checklist: frozenset(
            tok for text, toks in mapping.items() if text in norm_text for tok in toks
        ),
    )
    monkeypatch.setattr(
        "src.scorer.selector.canonical_overlap",
        lambda norm_text, checklist: len(
            {tok for text, toks in mapping.items() if text in norm_text for tok in toks}
        ),
    )


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


@pytest.mark.parametrize("kind,start,end", [
    ("work", "2026-02", "2026-06"),       # 4 months
    ("work", "2025-06", "2026-06"),       # 12 months
    ("work", "2023-12", "2026-06"),       # 30 months
    ("work", "2026-03", "present"),       # open-ended, 3 months as of NOW
    ("freelance", "2026-05", "2026-06"),  # 1 month
])
def test_tenure_never_changes_the_cap(kind, start, end) -> None:
    """A four-month job and a three-year job get the same ceiling.

    What an entry covers decides its length, not how long it lasted.
    """
    e = _entry("e1", kind, blocks=[_block(bullets=[])], start=start, end=end)
    assert bullet_cap(e, NOW) == int(settings.selection.bullets.max_cap) == 8


def test_projects_take_a_lower_cap_than_work() -> None:
    """Measured: with a flat 8, every project ran to 8 while jobs stopped at 4-6 —
    the cap was setting project length and the last slots filled with restatement."""
    proj = _entry("p1", "project", blocks=[_block(bullets=[])], start="", end="")
    assert bullet_cap(proj, NOW) == int(settings.selection.bullets.project_cap) == 5
    assert bullet_cap(proj, NOW) < bullet_cap(
        _entry("e1", "work", blocks=[_block(bullets=[])]), NOW
    )


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
    """When the lead's render set and another block's recovery pool say the same
    thing, only one of them reaches the page — the lead's wording, which is the
    sentence aimed at this JD."""
    twin = "Built ingestion jobs in Python for the trading desk."
    entry = _entry(blocks=[
        _block("e1::data", "data", bullets=[
            _bullet("d0", "S.", summary=True, block="e1::data"),
            _bullet("d1", twin, block="e1::data"),
        ]),
        _block("e1::backend", "backend", bullets=[
            _bullet("k0", "S.", summary=True, block="e1::backend", role="backend"),
            _bullet("x1", twin, block="e1::backend", role="backend", extra=True),
        ]),
    ])
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(entry, _jd(), _kw("Python"), now=NOW)
    assert out.block_id == "e1::data"
    assert [b.id for b in out.bullets] == ["d0", "d1"]


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
    """An entry stops at the cap no matter how much more it could still cover."""
    bullets = [_bullet("b0", "Summary.", summary=True)] + [
        _bullet(f"b{i}", f"Used Tool{i} in production.") for i in range(1, 20)
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)], start="2026-02", end="2026-06"),
        _jd(), _kw(*[f"Tool{i}" for i in range(1, 20)]), now=NOW,
    )
    assert len(out.bullets) == 8
    # 19 keywords were available and every remaining bullet still had gain — the
    # cap, not the early stop, is what ended this fill.
    assert out.coverage < 1.0


def test_zero_gain_stops_the_fill_above_the_floor() -> None:
    bullets = [_bullet("b0", "Summary with Python.", summary=True)] + [
        # Distinct wording: identical text is deduped out of the pool, which
        # would make this test pass for the wrong reason.
        _bullet(f"b{i}", _filler(i))
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
# Repetition — a rule, not a price (v3.3)
# ---------------------------------------------------------------------------


def test_a_denser_bullet_is_still_barred_when_it_restates_a_covered_keyword() -> None:
    """b1 is the densest bullet in the entry — Docker and CI/CD are both new — and
    it is still not selected, because it also says Git, which the pinned summary
    already said.

    This is the v3.3 reversal. Until now a repeat was priced: b1's two new keywords
    outweighed its one restatement, so it rendered and the entry said Git twice.
    The render set is now the lead block's alone and extras carry one or two
    keywords each, so barring b1 outright costs Docker and CI/CD only when nothing
    else in the entry can supply them — and then the floor, not the price, decides.
    """
    bullets = [
        _bullet("b0", "Summary using Git daily.", summary=True),
        _bullet("b1", "Shipped with Docker and Git under CI/CD."),
        _bullet("b2", "Wrote Terraform for the cluster."),
    ]
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(
            _entry(blocks=[_block(bullets=bullets)]), _jd(),
            _kw("Git", "Docker", "CI/CD", "Terraform"), now=NOW,
        )
    ids = [b.id for b in out.bullets]
    assert ids[0] == "b0", "the summary stays pinned"
    assert ids == ["b0", "b2"], "the repeating bullet does not render at any density"


def test_a_repeat_is_skipped_when_a_clean_bullet_covers_the_same_ground() -> None:
    """Both bullets bring Docker; only one of them also restates Git."""
    bullets = [
        _bullet("b0", "Summary using Git daily.", summary=True),
        _bullet("b1", "Shipped with Docker and Git again here."),
        _bullet("b2", "Shipped the service with Docker."),
    ]
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(
            _entry(blocks=[_block(bullets=bullets)]), _jd(),
            _kw("Git", "Docker"), now=NOW,
        )
    assert [b.id for b in out.bullets] == ["b0", "b2"]
    assert "b1" not in [b.id for b in out.bullets]


def test_no_trade_buys_a_repeat_however_favourable() -> None:
    """The old rule let a repeat through on an even trade and blocked it on a bad
    one. There is no trade any more: one new keyword bought with one repeat is
    refused exactly as three repeats were.

    The floor is the only route left onto the page for a restating bullet, so these
    run at min_per_entry=1 — at 3 the floor would take them whatever they cost.
    """
    kw = _kw("Python", "SQL", "Git", "Rust")

    # An EVEN trade: one new keyword for one repeat. Used to render; now does not.
    even = _entry(blocks=[_block(bullets=[
        _bullet("b0", "Summary with Python only.", summary=True),
        _bullet("b1", "Used Python and Rust in the core loop."),
    ])])
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(even, _jd(), kw, now=NOW)
    assert [b.id for b in out.bullets] == ["b0"], "1-for-1 is still a repeat"

    # A clean home for Rust renders instead, and the restating twin stays off.
    clean = _entry(blocks=[_block(bullets=[
        _bullet("b0", "Summary with Python and SQL and Git.", summary=True),
        _bullet("b1", "Used Python and SQL and Git and Rust here."),
        _bullet("b2", "Ported the core loop to Rust."),
    ])])
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(clean, _jd(), kw, now=NOW)
    assert [b.id for b in out.bullets] == ["b0", "b2"]


def test_the_floor_prefers_a_bullet_that_covers_nothing_to_one_that_repeats() -> None:
    """The one place the ban bends, and how far.

    The floor outranks the early stop: an entry showing one bullet is not an entry.
    When it fires, nothing left adds a keyword, so every candidate either repeats
    something or covers nothing at all. A bullet covering nothing repeats nothing,
    so it is taken first.
    """
    bullets = [
        _bullet("b0", "Summary with Python.", summary=True),
        _bullet("b1", "Used Python again on the second service."),
        _bullet("b2", "Sat with the operations desk each Friday to hear what broke."),
    ]
    with _cfg(min_per_entry=2):
        out = select_entry_bullets(
            _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Python"), now=NOW,
        )
    assert [b.id for b in out.bullets] == ["b0", "b2"]


def test_an_off_role_extra_cannot_buy_a_slot_with_a_repeat() -> None:
    """The recovery pool reaches across blocks; the repetition rule reaches with it.

    x2 is an extra from a barely-related block. It covers Rust, which the entry
    wants, but also restates Python from the pinned summary — and an off-role
    bullet is exactly where a cross-block duplicate comes from. The on-role bullet
    that covers Rust cleanly takes the slot instead.
    """
    lead = _block(bullets=[
        _bullet("b0", "Summary with Python.", summary=True),
        _bullet("b1", "Used Rust in the core loop."),
    ])
    off = _block("e1::ml", "ml", bullets=[
        _bullet("x2", "Used Python and Rust in the model.", block="e1::ml", role="ml",
                extra=True),
    ], aliases=("ML Engineer",), alias_vecs=[W])
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(
            _entry(blocks=[lead, off]), _jd(), _kw("Python", "Rust"), now=NOW,
        )
    assert [b.id for b in out.bullets] == ["b0", "b1"]


# ---------------------------------------------------------------------------
# Phase 2 — the title's own qualification checklist
# ---------------------------------------------------------------------------


def test_phase_2_selects_a_bullet_the_JD_never_asked_for(monkeypatch) -> None:
    """The headline change: a canonical token CAN now pull a bullet onto the page.

    b2 has zero JD gain. Before v3.1 the entry stopped at b1; now b2 earns the next
    slot because recruiters for this title screen for Kubernetes.
    """
    _fake_canon(monkeypatch, {"kubernetes": {"kubernetes"}})
    # Three JD-covering bullets, so the floor (3) is satisfied by phase 1 alone and
    # the Kubernetes bullet can only arrive through phase 2.
    bullets = [
        _bullet("b0", "Summary line.", summary=True),
        _bullet("b1", "Built the service in Python."),
        _bullet("b2", "Queried the warehouse in SQL."),
        _bullet("bk", "Ran the fleet on Kubernetes for the ops team."),
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets, checklist=("Data Engineer",))]),
        _jd(), _kw("Python", "SQL"), now=NOW,
    )
    picked = [b.id for b in out.bullets]
    assert picked == ["b0", "b1", "b2", "bk"]
    bk = out.bullets[-1]
    assert bk.via == "qualification"
    assert bk.new_keywords == []          # nothing this JD asked for
    assert bk.new_canonical == ["kubernetes"]
    assert all(b.via == "jd" for b in out.bullets[:3])


def test_phase_2_stops_at_zero_canonical_gain_rather_than_padding(monkeypatch) -> None:
    """Phase 2 ends the same way phase 1 does. An entry may finish under the cap."""
    _fake_canon(monkeypatch, {"kubernetes": {"kubernetes"}})
    bullets = [_bullet("b0", "Summary line.", summary=True)] + [
        _bullet(f"b{i}", f"Did unrelated thing number {i} for the team.")
        for i in range(1, 9)
    ]
    bullets.append(_bullet("bk", "Ran the fleet on Kubernetes for the ops team."))
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets, checklist=("Data Engineer",))]),
        _jd(), _kw("Python"), now=NOW,
    )
    # Floor fills to 3, Kubernetes earns a 4th, then nothing has canonical gain
    # left — the entry stops well short of the cap of 8.
    assert len(out.bullets) < 8
    assert "bk" in [b.id for b in out.bullets]


def test_phase_2_does_not_inflate_the_entrys_JD_coverage(monkeypatch) -> None:
    """A canonical token is not a JD keyword; `coverage` must not move.

    The calibrated thresholds are all defined against JD coverage, so phase 2
    quietly widening it would invalidate every one of them.
    """
    _fake_canon(monkeypatch, {"kubernetes": {"kubernetes"}})
    bullets = [
        _bullet("b0", "Summary line.", summary=True),
        _bullet("b1", "Built the service in Python."),
        _bullet("b2", "Queried the warehouse in SQL."),
        _bullet("bk", "Ran the fleet on Kubernetes for the ops team."),
    ]
    entry = _entry(blocks=[_block(bullets=bullets, checklist=("Data Engineer",))])
    kw = _kw("Python", "SQL")
    out = select_entry_bullets(entry, _jd(), kw, now=NOW)

    with _no_qualification_fill():
        base = select_entry_bullets(entry, _jd(), kw, now=NOW)

    assert len(out.bullets) > len(base.bullets)     # phase 2 did add a bullet
    assert out.coverage == base.coverage            # and coverage did not move
    assert out.covered == base.covered


def test_phase_2_can_be_turned_off(monkeypatch) -> None:
    _fake_canon(monkeypatch, {"kubernetes": {"kubernetes"}})
    bullets = [
        _bullet("b0", "Summary line.", summary=True),
        _bullet("b1", "Built the service in Python."),
        _bullet("b2", "Ran the fleet on Kubernetes for the ops team."),
    ]
    entry = _entry(blocks=[_block(bullets=bullets, checklist=("Data Engineer",))])
    with _no_qualification_fill():
        out = select_entry_bullets(entry, _jd(), _kw("Python"), now=NOW)
    assert all(b.via == "jd" for b in out.bullets)


def test_the_summary_bullets_canonical_tokens_count_as_already_said(monkeypatch):
    """Phase 2 must not repeat what the entry opened with."""
    _fake_canon(monkeypatch, {"kubernetes": {"kubernetes"}})
    bullets = [
        _bullet("b0", "Summary running Kubernetes.", summary=True),
        _bullet("b1", "Also ran the fleet on Kubernetes for the ops team."),
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets, checklist=("Data Engineer",))]),
        _jd(), _kw("Python"), now=NOW,
    )
    # b1 is only reachable through the floor, never through phase 2 gain.
    assert all(b.via == "jd" for b in out.bullets)


# ---------------------------------------------------------------------------
# The recovery pool (extra_bullets)
# ---------------------------------------------------------------------------


def test_an_audited_bullet_beats_an_extra_at_equal_gain() -> None:
    bullets = [
        _bullet("b0", "Summary line.", summary=True),
        _bullet("x1", "Deployed Python services nightly.", extra=True),
        _bullet("b1", "Built Python services for the desk."),
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(), _kw("Python"), now=NOW,
    )
    assert [b.id for b in out.bullets][:2] == ["b0", "b1"]


def test_an_extra_still_wins_when_it_covers_more() -> None:
    """The recovery pool exists to recover keywords. Equal-gain ties are all it loses."""
    bullets = [
        _bullet("b0", "Summary line.", summary=True),
        _bullet("b1", "Built Python services for the desk."),
        _bullet("x1", "Built Python services on Kubernetes.", extra=True),
    ]
    out = select_entry_bullets(
        _entry(blocks=[_block(bullets=bullets)]), _jd(),
        _kw("Python", "Kubernetes"), now=NOW,
    )
    assert [b.id for b in out.bullets][:2] == ["b0", "x1"]


# ---------------------------------------------------------------------------
# What each block contributes (v3.3): render set from the lead, extras from all
# ---------------------------------------------------------------------------


def test_the_render_set_comes_from_the_lead_block_alone() -> None:
    """q1 covers a keyword nothing else covers, and still does not render.

    It is an AUDITED bullet of a non-lead block. The extractor writes each block as
    a complete entry aimed at one title family, re-wording the same accomplishment
    in every block it serves, so pooling render sets pulled three versions of one
    claim into one entry. The lead block's render set is the entry.
    """
    entry = _entry(blocks=[
        _block("e1::data", "data", alias_vecs=[V], bullets=[
            _bullet("d0", "Summary.", summary=True, block="e1::data"),
            _bullet("d1", "Built in Python.", block="e1::data"),
        ]),
        _block("e1::quant", "quant", fit="adjacent", alias_vecs=[W],
               aliases=["Quant Researcher"],
               bullets=[_bullet("q1", "Ran jobs in Docker.", block="e1::quant",
                                role="quant")]),
    ])
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(entry, _jd(), _kw("Python", "Docker"), now=NOW)
    assert [b.id for b in out.bullets] == ["d0", "d1"]


def test_extras_are_still_pooled_across_every_block() -> None:
    """The counterpart: a keyword only another block's RECOVERY pool covers stays
    reachable. That is what the recovery pool is for — Docker under a `data` block
    whose checklist never names it is otherwise unrecoverable at selection time."""
    entry = _entry(blocks=[
        _block("e1::data", "data", alias_vecs=[V], bullets=[
            _bullet("d0", "Summary.", summary=True, block="e1::data"),
            _bullet("d1", "Built in Python.", block="e1::data"),
        ]),
        _block("e1::quant", "quant", fit="adjacent", alias_vecs=[W],
               aliases=["Quant Researcher"],
               bullets=[_bullet("x1", "Ran jobs in Docker.", block="e1::quant",
                                role="quant", extra=True)]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw("Python", "Docker"), now=NOW)
    assert "x1" in [b.id for b in out.bullets]
    assert out.coverage == pytest.approx(1.0)


def test_an_on_role_extra_wins_a_tie_against_an_off_role_one() -> None:
    """Equal JD gain, so relevance decides: the lead block's extra takes the slot
    and the off-role twin is left with nothing new to add."""
    entry = _entry(blocks=[
        _block("e1::data", "data", alias_vecs=[V], bullets=[
            _bullet("d0", "Summary.", summary=True, block="e1::data"),
            _bullet("x1", "Shipped pipelines with Docker.", block="e1::data",
                    extra=True),
        ]),
        # W is orthogonal to the JD role vector, so this block scores ~0.
        # Different wording, same keyword — identical text would be deduped.
        _block("e1::quant", "quant", fit="adjacent", alias_vecs=[W], bullets=[
            _bullet("q1", "Ran backtests inside Docker.", block="e1::quant",
                    role="quant", extra=True),
        ]),
    ])
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(entry, _jd(), _kw("Docker"), now=NOW)
    ids = [b.id for b in out.bullets]
    assert ids == ["d0", "x1"]


def test_the_lead_block_supplies_the_header_and_dates() -> None:
    entry = _entry(blocks=[
        _block("e1::data", "data", header="Data Engineer at Acme, Pune",
               bullets=[_bullet("d0", "Modelled the warehouse.", summary=True,
                                block="e1::data")]),
        _block("e1::backend", "backend",
               header="Backend Developer at Acme, Pune",
               bullets=[_bullet("k0", "Built the service in Python.", summary=True,
                                block="e1::backend", role="backend")]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw("Python"), now=NOW)
    assert out.header_left == "Backend Developer at Acme, Pune"
    assert out.block_id == "e1::backend"


def test_the_lead_block_is_chosen_on_coverage_not_on_title_aliases() -> None:
    """v3.3, and it reverses the old behaviour outright.

    The `data` block carries the alias list that matches this JD's role text
    exactly; the `backend` block's aliases are orthogonal to it. On alias cosine
    `data` led every time. But `backend` is the block whose BULLETS answer the
    advert — Python and Docker, both on the checklist, neither one said by `data`.
    An alias is a label the extractor attached; the bullets are the evidence.
    """
    entry = _entry(blocks=[
        _block("e1::data", "data", alias_vecs=[V], header="DATA",
               bullets=[
                   _bullet("d0", "Modelled the warehouse tables.", summary=True,
                           block="e1::data"),
                   _bullet("d1", "Wrote the nightly load.", block="e1::data"),
               ]),
        _block("e1::backend", "backend", fit="adjacent", alias_vecs=[W],
               header="BACKEND",
               bullets=[
                   _bullet("k0", "Built the service in Python.", summary=True,
                           block="e1::backend", role="backend"),
                   _bullet("k1", "Shipped it in Docker.", block="e1::backend",
                           role="backend"),
               ]),
    ])
    out = select_entry_bullets(entry, _jd(vec_role=V), _kw("Python", "Docker"), now=NOW)
    assert out.block_id == "e1::backend", "coverage decides, not the alias list"
    assert out.header_left == "BACKEND"
    assert out.coverage == pytest.approx(1.0)


def test_the_required_half_of_the_checklist_breaks_a_lead_block_tie() -> None:
    """Equal total weight, unequal seriousness.

    `data` covers two nice-to-haves (0.5 each); `backend` covers one required
    (1.0). Both total 1.0, and the block that answers the REQUIRED line leads —
    that is the half a screen rejects on.
    """
    kws = (Keyword("Python", 1.0), Keyword("Airflow", 0.5), Keyword("dbt", 0.5))
    entry = _entry(blocks=[
        _block("e1::data", "data", header="DATA",
               bullets=[_bullet("d0", "Scheduled loads in Airflow and dbt.",
                                summary=True, block="e1::data")]),
        _block("e1::backend", "backend", header="BACKEND",
               bullets=[_bullet("k0", "Built the service in Python.", summary=True,
                                block="e1::backend", role="backend")]),
    ])
    out = select_entry_bullets(entry, _jd(), kws, now=NOW)
    assert out.block_id == "e1::backend"


def test_extras_do_not_count_toward_which_block_leads() -> None:
    """A block cannot win the lead on material it would not render. The recovery
    pool belongs to the entry once a lead is picked, not to a block's identity."""
    entry = _entry(blocks=[
        _block("e1::data", "data", header="DATA",
               bullets=[
                   _bullet("d0", "Built the service in Python.", summary=True,
                           block="e1::data"),
               ]),
        _block("e1::backend", "backend", header="BACKEND",
               bullets=[
                   _bullet("k0", "Modelled the warehouse tables.", summary=True,
                           block="e1::backend", role="backend"),
                   _bullet("x1", "Shipped Python services in Docker.",
                           block="e1::backend", role="backend", extra=True),
               ]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw("Python", "Docker"), now=NOW)
    assert out.block_id == "e1::data", "the backend block's two keywords are extras"


def test_a_primary_block_wins_a_tie_against_an_adjacent_one() -> None:
    entry = _entry(blocks=[
        _block("e1::adj", "adj", fit="adjacent", alias_vecs=[V], header="ADJ",
               bullets=[_bullet("a0", "S.", summary=True, block="e1::adj")]),
        _block("e1::pri", "pri", fit="primary", alias_vecs=[V], header="PRI",
               bullets=[_bullet("p0", "S.", summary=True, block="e1::pri")]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw(), now=NOW)
    assert out.header_left == "PRI"


def test_a_project_puts_its_link_in_the_header_link_slot() -> None:
    entry = _entry("p1", "project", link="https://github.com/x/y", blocks=[
        _block("p1::backend", "backend", dates="",
               bullets=[_bullet("p0", "S.", summary=True, block="p1::backend")]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw(), now=NOW)
    # v3.2 split the slot: text and link are separate, because a freelance entry
    # shows BOTH. A project has no label text, only the link.
    assert out.header_link == "https://github.com/x/y"
    assert out.header_right == ""


def test_salaried_work_shows_dates_and_no_link() -> None:
    """There is no public artifact for salaried work — it belongs to the employer."""
    entry = _entry("e1", "work", link="https://example.com/ignored", blocks=[
        _block(dates="Jan 2024 to current",
               bullets=[_bullet("b0", "S.", summary=True)]),
    ])
    out = select_entry_bullets(entry, _jd(), _kw(), now=NOW)
    assert out.header_right == "Jan 2024 to current"
    assert out.header_link == ""


def test_freelance_shows_its_label_and_link_but_no_dates() -> None:
    """A short engagement's value is that the result is live and clickable; its
    two-month span invites the wrong question."""
    entry = _entry("e1", "work", link="https://client.example/site", blocks=[
        _block(dates="Dec 2025 to Jan 2026",
               bullets=[_bullet("b0", "S.", summary=True)]),
    ])
    entry.employment_type = "freelance"
    out = select_entry_bullets(entry, _jd(), _kw(), now=NOW)
    assert out.header_right == settings.selection.freelance.label
    assert out.header_link == "https://client.example/site"
    assert "2025" not in out.header_right


# ---------------------------------------------------------------------------
# Entry selection and ordering
# ---------------------------------------------------------------------------


def _profile(work=(), projects=()):
    """A Profile for select_top. `skills` is vestigial — the Skills section is
    gone — but the dataclass still carries it."""
    return Profile(work=list(work), projects=list(projects), skills=[])


def _scored(eid, **kw):
    """One entry, scored, for tests that only care about ordering."""
    return select_top(
        _profile(work=[_simple_entry(eid, "x")]), _jd(), _kw(), now=NOW,
    )[0]


def _simple_entry(eid, text, *, end="present", kind="work"):
    return _entry(eid, kind, blocks=[_block(f"{eid}::data", bullets=[
        _bullet(f"{eid}_0", "Summary.", summary=True, block=f"{eid}::data"),
        _bullet(f"{eid}_1", text, block=f"{eid}::data"),
    ])], end=end)


def _simple_project(eid, text):
    """kind matters: a `work`-kind entry satisfies the salaried guarantee, so a
    test about that guarantee must build real projects."""
    return _simple_entry(eid, text, kind="project")


def test_the_page_is_always_full_even_when_nothing_matches() -> None:
    """W is orthogonal to every entry's vector, so similarity and coverage are both
    0 across the board. Under the old per-kind thresholds that emptied the page and
    min_shown backfilled it; a count cannot empty — the best five of a bad field are
    still the best five."""
    projects = [_simple_project(f"p{i}", "Nothing relevant.") for i in range(8)]
    out = select_top(
        _profile(work=[_simple_entry("job", "Nothing either.")], projects=projects),
        _jd(vec_role=W, vec_match=W), _kw("Kubernetes"), now=NOW,
    )
    assert len(out) == int(settings.selection.top_n)
    assert all(e.score == 0.0 for e in out)


def test_only_the_best_top_n_reach_the_page() -> None:
    """More candidates than slots: the page takes the best `top_n` and no more."""
    projects = [_simple_project(f"p{i}", "Built with Python.") for i in range(9)]
    out = select_top(
        _profile(work=[_simple_entry("job", "Built with Python.")], projects=projects),
        _jd(), _kw("Python"), now=NOW,
    )
    assert len(out) == int(settings.selection.top_n)


def test_work_freelance_and_projects_compete_in_one_pool() -> None:
    """Kind gates nothing. A project that answers the JD outranks a gig that does
    not, and takes the slot — which is what the merged section renders."""
    job = _simple_entry("job", "Nothing relevant.")
    gig = _simple_entry("gig", "Nothing relevant either.")
    gig.employment_type = "freelance"
    winner = _simple_project("proj", "Built the service in Python.")
    out = select_top(
        _profile(work=[job, gig], projects=[winner]), _jd(), _kw("Python"), now=NOW,
    )
    assert out[0].id == "proj", "the best match leads whatever kind it is"
    assert {e.id for e in out} == {"job", "gig", "proj"}


def test_the_salaried_job_is_always_on_the_page_even_when_outscored() -> None:
    """The one guarantee. Five projects all beat the job; the job still renders,
    displacing the WEAKEST of them — a resume without the operator's actual job is
    not a resume."""
    job = _simple_entry("job", "Nothing relevant.")
    projects = [
        _simple_project(f"p{i}", "Built the service in Python.") for i in range(6)
    ]
    out = select_top(
        _profile(work=[job], projects=projects), _jd(), _kw("Python"), now=NOW,
    )
    ids = [e.id for e in out]
    assert "job" in ids
    assert len(out) == int(settings.selection.top_n)
    assert out[-1].id == "job", "it takes the last slot, not a better one"


def test_a_freelance_gig_does_not_satisfy_the_salaried_guarantee() -> None:
    """A gig reads to a recruiter as a project with an invoice, so it cannot stand
    in for the job — the job is pulled in alongside it."""
    gig = _simple_entry("gig", "Built the service in Python.")
    gig.employment_type = "freelance"
    job = _simple_entry("job", "Nothing relevant.")
    projects = [
        _simple_project(f"p{i}", "Built the service in Python.") for i in range(5)
    ]
    out = select_top(
        _profile(work=[job, gig], projects=projects), _jd(), _kw("Python"), now=NOW,
    )
    assert "job" in [e.id for e in out]


def test_order_entries_best_match_first_when_gap_large() -> None:
    sa, sb = _scored("e1"), _scored("e2")
    sa.score, sb.score = 0.9, 0.1
    assert order_entries([sb, sa])[0] is sa


def test_order_is_by_match_not_recency() -> None:
    """v3.2: one merged section ordered purely on match. Recency decides nothing.

    An older entry that fits this JD better now leads an entry that merely ended
    more recently — the page is arranged for the reader's twenty seconds, not
    chronologically.
    """
    sa, sb = _scored("e1"), _scored("e2")
    sa.score, sb.score = 0.60, 0.50
    assert order_entries([sa, sb])[0] is sa  # better match, despite being older


def test_a_project_may_lead_the_page() -> None:
    sp, sw = _scored("p1"), _scored("e1")
    sp.kind, sw.kind = "project", "work"
    sp.score, sw.score = 0.70, 0.40
    assert [e.id for e in order_entries([sw, sp])] == ["p1", "e1"]


def _ordering_entry(eid, *, kind, score, employment_type="employment"):
    """A scored entry shaped only for ordering: kind, employment_type, score."""
    selected = _scored(eid)
    selected.kind, selected.employment_type, selected.score = (
        kind, employment_type, score,
    )
    return selected


def test_the_salaried_job_is_pulled_into_the_top_two_when_projects_sweep_them() -> None:
    """The one guard on pure match order: the salaried job stays in view."""
    sp1 = _ordering_entry("p1", kind="project", score=0.90)
    sp2 = _ordering_entry("p2", kind="project", score=0.80)
    sw = _ordering_entry("e1", kind="work", score=0.20)
    out = order_entries([sp1, sp2, sw])
    assert [e.id for e in out] == ["p1", "e1", "p2"]
    assert out[0].kind == "project", "the best match still leads"


def test_a_freelance_entry_does_not_satisfy_the_guard() -> None:
    """The bug this rule was written to fix.

    Freelance loads as ``kind="work"``, so the old ``kind != "project"`` test read
    a gig as employment and left the salaried job below the fold. Only
    ``employment_type == "employment"`` counts now.
    """
    sf = _ordering_entry("f1", kind="work", score=0.90, employment_type="freelance")
    sp = _ordering_entry("p1", kind="project", score=0.80)
    sw = _ordering_entry("e1", kind="work", score=0.20)
    out = order_entries([sf, sp, sw])
    assert [e.id for e in out] == ["f1", "e1", "p1"]


def test_the_salaried_job_keeps_slot_one_when_it_matches_best() -> None:
    sw = _ordering_entry("e1", kind="work", score=0.90)
    sf = _ordering_entry("f1", kind="work", score=0.50, employment_type="freelance")
    sp = _ordering_entry("p1", kind="project", score=0.30)
    assert [e.id for e in order_entries([sp, sf, sw])] == ["e1", "f1", "p1"]


def test_the_salaried_job_drops_to_slot_two_when_something_matches_better() -> None:
    sp = _ordering_entry("p1", kind="project", score=0.95)
    sw = _ordering_entry("e1", kind="work", score=0.60)
    sf = _ordering_entry("f1", kind="work", score=0.70, employment_type="freelance")
    # Match order alone would read p1, f1, e1; the guard lifts the job to slot 2.
    assert [e.id for e in order_entries([sp, sf, sw])] == ["p1", "e1", "f1"]


def test_no_salaried_entry_leaves_pure_match_order() -> None:
    sf = _ordering_entry("f1", kind="work", score=0.40, employment_type="freelance")
    sp1 = _ordering_entry("p1", kind="project", score=0.90)
    sp2 = _ordering_entry("p2", kind="project", score=0.80)
    assert [e.id for e in order_entries([sf, sp1, sp2])] == ["p1", "p2", "f1"]


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


def test_a_clean_extra_is_preferred_to_a_repeating_audited_bullet() -> None:
    """"Regression" is reachable two ways: from an audited bullet that also restates
    SQL, and from an extra that repeats nothing. v3.2 required the extra to be the
    ONLY source, which blocked the clean route and forced the repeat. With
    repetition barred outright, the clean extra is simply the only legal route.
    """
    bullets = [
        _bullet("b0", "Summary with Python and SQL.", summary=True),
        _bullet("b1", "Ran SQL checks and Regression tests on the warehouse."),
        _bullet("x1", "Kept a Regression suite on every branch.", extra=True),
    ]
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(
            _entry(blocks=[_block(bullets=bullets)]), _jd(),
            _kw("Python", "SQL", "Regression"), now=NOW,
        )
    ids = [b.id for b in out.bullets]
    assert "x1" in ids, "the clean extra supplies Regression without repeating SQL"
    assert "b1" not in ids, "the repeating audited bullet is no longer needed"


def test_a_repeating_extra_is_barred_like_any_other_bullet() -> None:
    """The recovery pool buys no exemption: x1 covers Regression and restates both
    Python and SQL, so the clean audited bullet takes the slot instead."""
    bullets = [
        _bullet("b0", "Summary with Python and SQL.", summary=True),
        _bullet("b1", "Built Regression checks in the pipeline."),
        _bullet("x1", "Ran SQL and Python and Regression in one go.", extra=True),
    ]
    with _cfg(min_per_entry=1):
        out = select_entry_bullets(
            _entry(blocks=[_block(bullets=bullets)]), _jd(),
            _kw("Python", "SQL", "Regression"), now=NOW,
        )
    assert "x1" not in [b.id for b in out.bullets]
