"""Layer 4 — pure selection functions (entries and their bullets).

Selection is deterministic: no LLM anywhere in this module. Every tunable comes
from ``config.selection``; nothing is hardcoded. Inputs are in-memory candidate
dataclasses with embeddings already attached by the master-profile rebuild, so
these functions unit-test exhaustively against synthetic profiles with no DB, no
model and no network.

WHAT CHANGED IN v3, and why
---------------------------
The old template had a Skills section and a profile Summary, so this module also
picked a summary from a pool and ranked the skills pool. The Headless template has
neither. A qualification now counts only when it is written *inside a bullet*, so
bullet selection stopped being "top 3 by cosine" and became a coverage problem:

    Pick the bullets that, together, cover the most of what this JD asks for.

That is a set-cover, and it is solved greedily — repeatedly take the bullet adding
the most currently-uncovered JD keyword weight. Two properties fall out of the
greedy rather than needing rules of their own:

  * A bullet that repeats only keywords already covered has zero gain and is never
    picked, which is the old "no repeated keyword within an entry" rule.
  * A bullet that repeats a covered keyword BUT also carries an uncovered one has
    positive gain and IS picked — because not having a keyword is more damaging
    than saying one twice.

The covered set resets for EVERY entry (PIVOT_V3.md D5a). Coverage is not rationed
across entries: the method grades the first entry on whether it clears the whole
checklist alone, so a keyword the first entry used must remain available to the
second. Repetition across entries is expected; only within one entry is it waste.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from src.config import settings
from src.llm.schemas import JDParsed
from src.scorer.embeddings import Vector, add, cosine, embed_batch
from src.scorer.keywords import Keyword, covered_by, coverage_of, norm, weight_of
from src.scorer.qualifications import canonical_overlap

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Input candidates (embeddings pre-computed by the rebuild)
# ---------------------------------------------------------------------------


@dataclass
class BulletCand:
    id: str
    text: str
    embedding: Vector
    block_id: str = ""
    role: str = ""
    is_summary: bool = False
    is_extra: bool = False
    #: ``norm(text)``, computed once at load. The greedy tests every remaining
    #: bullet against every keyword on every iteration, so re-normalising inside
    #: the loop would dominate the cost.
    norm_text: str = ""

    def __post_init__(self) -> None:
        if not self.norm_text:
            self.norm_text = norm(self.text)


@dataclass
class RoleBlockCand:
    block_id: str
    role: str
    role_fit: str
    entry_header: str
    entry_dates: str
    checklist: tuple[str, ...]
    title_aliases: list[str]
    alias_embeddings: list[Vector]
    bullets: list[BulletCand]


@dataclass
class EntryCand:
    """A work entry or a project. They render identically, so they select
    identically — the only differences are the tenure cap and the header's right
    slot (dates for work, a repo URL for projects)."""

    id: str
    kind: str  # "work" | "project"
    label: str  # company, or project name
    blocks: list[RoleBlockCand]
    link: str = ""
    actual_title: str = ""
    safe_title_aliases: list[str] = field(default_factory=list)
    start_date: str = ""
    end_date: str = ""


@dataclass
class SkillCand:
    skill: str
    embedding: Vector


@dataclass
class Profile:
    work: list[EntryCand]
    projects: list[EntryCand]
    skills: list[SkillCand]


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass
class SelectedBullet:
    id: str
    text: str
    score: float  # cosine vs vec_match — ordering and logging only
    is_summary: bool = False
    #: What THIS bullet added to the entry's covered set. Makes a greedy run
    #: auditable after the fact: `python -m src.cli.inspect` can show why each
    #: bullet earned its slot.
    new_keywords: list[str] = field(default_factory=list)


@dataclass
class SelectedEntry:
    id: str
    kind: str
    block_id: str
    label: str
    header_left: str
    header_right: str
    bullets: list[SelectedBullet]
    covered: set[str]
    coverage: float
    similarity: float
    score: float
    cap: int
    title_alias: str = ""
    link: str = ""
    end_date: str = ""


@dataclass(frozen=True)
class JDContext:
    vec_role: Vector
    vec_match: Vector
    role_category: str | None
    role_level: str | None
    posted_at: datetime | None
    scraped_at: datetime | None
    scrape_window_hours: float | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _force_min(passing: list, ranked: list, max_shown: int, min_shown: int) -> list:
    """Take up to ``max_shown`` that passed threshold; if fewer than
    ``min_shown`` passed, force-include the top ``min_shown`` overall."""
    selected = passing[:max_shown]
    if len(selected) < min_shown:
        selected = ranked[:min_shown]
    return selected


def _months_between(start: str, end: str, now: datetime) -> int:
    """Whole months between two ``YYYY-MM`` strings; ``present`` means ``now``."""

    def parse(value: str) -> tuple[int, int] | None:
        v = (value or "").strip().lower()
        if v in ("", "present", "current"):
            return None
        year, _, month = v.partition("-")
        try:
            return int(year), int(month or 1)
        except ValueError:
            return None

    s = parse(start) or (now.year, now.month)
    e = parse(end) or (now.year, now.month)
    return max(0, (e[0] - s[0]) * 12 + (e[1] - s[1]))


def bullet_cap(entry: EntryCand, now: datetime) -> int:
    """How many bullets this entry may show.

    The method: minimum 3, maximum 8, "scaled to how long you were there — if
    you've been here less than six months you need three, not five". Projects have
    no dates, so they take a flat cap.
    """
    cfg = settings.selection.bullets
    if entry.kind == "project":
        return int(cfg.project_cap)
    months = _months_between(entry.start_date, entry.end_date, now)
    for band in sorted(cfg.tenure_bands, key=lambda b: float(b["under_months"])):
        if months < float(band["under_months"]):
            return int(band["cap"])
    return int(cfg.max_cap)


def _alias_score(block: RoleBlockCand, jd: JDContext) -> float:
    return max((cosine(e, jd.vec_role) for e in block.alias_embeddings), default=0.0)


def lead_block(entry: EntryCand, jd: JDContext) -> RoleBlockCand:
    """The block that supplies the header, dates and title alias.

    Best title-alias cosine to the JD role, preferring a ``primary`` block on a
    tie: an ``adjacent`` block is, by the extractor's own admission, a stretch, so
    it should not get to name the entry when a primary block matches as well.
    """
    return max(
        entry.blocks,
        key=lambda rb: (_alias_score(rb, jd), rb.role_fit == "primary"),
    )


def _entry_pool(entry: EntryCand, lead_id: str = "") -> list[BulletCand]:
    """Every bullet of every block, deduped by id AND by text.

    Pooled across blocks deliberately: a `data` bullet and a `backend` bullet from
    the same job are both true of that job, and confining the choice to one block
    throws away coverage the entry actually has. The off-role scaling keeps that
    from turning the entry into a stack-mixed mess.

    The text dedup is not belt-and-braces — it is load-bearing. The extractor
    writes each accomplishment "re-worded in every block it honestly serves", so
    an entry legitimately holds several near-identical bullets under different
    ids. The greedy alone does not catch them: once every keyword is covered,
    every remaining candidate has gain 0, and the floor then fills the last slots
    by cosine — which picks the twin of a bullet already on the page. Observed on
    a real ad, where one entry rendered the same sentence twice.

    The lead block's copy wins, since that is the wording aimed at this JD.
    """
    seen_ids: set[str] = set()
    seen_text: dict[str, int] = {}
    pool: list[BulletCand] = []
    blocks = sorted(entry.blocks, key=lambda rb: rb.block_id != lead_id)
    for rb in blocks:
        for b in rb.bullets:
            if b.id in seen_ids:
                continue
            key = _text_key(b.norm_text)
            if key in seen_text:
                continue
            seen_ids.add(b.id)
            seen_text[key] = 1
            pool.append(b)
    return pool


def _text_key(norm_text: str) -> str:
    """Collapse whitespace so trivially-reflowed duplicates compare equal."""
    return " ".join(norm_text.split())


def _relevance(block_scores: dict[str, float], lead_id: str, block_id: str) -> float:
    """How much an off-role bullet's coverage gain counts.

    1.0 for the lead block. For any other block, its alias cosine relative to the
    lead's — so a bullet from a barely-related block must cover something genuinely
    unclaimed to beat an on-role bullet, but is never excluded outright. Excluding
    it wholesale (a hard floor) would drop keywords the operator really has, which
    is the more expensive mistake: not having a keyword costs the match, repeating
    one costs a line.
    """
    if block_id == lead_id:
        return 1.0
    cfg = settings.selection.entry
    if not cfg.off_role_scaling:
        return 1.0
    lead = block_scores.get(lead_id, 0.0)
    if lead <= 0:
        return 1.0
    return max(float(cfg.off_role_floor), min(1.0, block_scores.get(block_id, 0.0) / lead))


# ---------------------------------------------------------------------------
# Bullet selection — the greedy set-cover
# ---------------------------------------------------------------------------


def select_entry_bullets(
    entry: EntryCand,
    jd: JDContext,
    keywords: tuple[Keyword, ...],
    *,
    now: datetime,
) -> SelectedEntry:
    """Pin the summary bullet, then greedily cover this JD's checklist.

    Precedence, when the three limits disagree: **cap > early-stop > floor.**

      * ``cap`` is a hard ceiling — it is what fits on the page.
      * a zero-gain best candidate stops the fill early: a bullet that says nothing
        new is exactly what the method says to delete.
      * the floor overrides that early stop, because an entry showing one bullet is
        not a valid entry. Below the floor every gain is already 0, so those slots
        are filled by cosine.
    """
    cfg = settings.selection.bullets
    cap = bullet_cap(entry, now)
    floor = min(int(cfg.min_per_entry), cap)

    block = lead_block(entry, jd)
    block_scores = {rb.block_id: _alias_score(rb, jd) for rb in entry.blocks}
    pool = _entry_pool(entry, block.block_id)

    covered: set[str] = set()
    chosen: list[SelectedBullet] = []

    # --- 1. pin the summary bullet ----------------------------------------
    # The lead block's bullets[0]: the one an eight-year-old can follow, and the
    # method requires an entry to open with it.
    summary = next((b for b in block.bullets if b.is_summary), None)
    if summary is None:  # malformed block — fall back to any block's summary
        summary = next((b for b in pool if b.is_summary), None)
    if summary is not None:
        gained = covered_by(summary.norm_text, keywords)
        chosen.append(
            SelectedBullet(
                summary.id, summary.text, cosine(summary.embedding, jd.vec_match),
                True, sorted(gained),
            )
        )
        covered |= gained

    # --- 2. greedy set-cover over the rest --------------------------------
    # Other blocks' summary bullets stay in the pool. They describe the same work
    # from another angle, and if one carries a keyword the pinned summary lacks it
    # earns a slot like any other bullet.
    remaining = [b for b in pool if summary is None or b.id != summary.id]
    while len(chosen) < cap and remaining:
        best = None
        for b in remaining:
            gained = covered_by(b.norm_text, keywords) - covered
            gain = weight_of(gained, keywords) * _relevance(
                block_scores, block.block_id, b.block_id
            )
            sim = cosine(b.embedding, jd.vec_match)
            # Tie-break on the role's canonical checklist (PIVOT_V3.md D12): when
            # JD gain is equal, prefer the bullet that also covers more of what
            # recruiters for this title screen for. It can only ever reorder
            # candidates that already have equal, positive JD gain — a canonical
            # token the JD never mentions cannot pull a bullet in, because the
            # zero-gain stop below fires first.
            canon = canonical_overlap(b.norm_text, block.checklist)
            key = (round(gain, 9), canon, sim)
            if best is None or key > best[0]:
                best = (key, b, gained, gain, sim)

        _, cand, gained, gain, sim = best
        if gain <= 0.0 and len(chosen) >= floor:
            break
        chosen.append(
            SelectedBullet(cand.id, cand.text, sim, False, sorted(gained))
        )
        covered |= gained
        remaining.remove(cand)

    return SelectedEntry(
        id=entry.id,
        kind=entry.kind,
        block_id=block.block_id,
        label=entry.label,
        header_left=block.entry_header,
        header_right=block.entry_dates if entry.kind == "work" else entry.link,
        bullets=chosen,
        covered=covered,
        coverage=coverage_of(covered, keywords),
        similarity=0.0,
        score=0.0,
        cap=cap,
        link=entry.link,
        end_date=entry.end_date,
    )


# ---------------------------------------------------------------------------
# Entry scoring
# ---------------------------------------------------------------------------


def score_entry(
    entry: EntryCand,
    jd: JDContext,
    keywords: tuple[Keyword, ...],
    *,
    now: datetime,
) -> SelectedEntry:
    """Select first, then score the entry on what it actually selected.

    Two signals, deliberately not one. ``coverage`` is what a recruiter grades in
    twenty seconds; ``similarity`` is the calibrated embedding score with a year of
    measured thresholds behind it. Scoring on coverage alone would rank a
    keyword-dense but off-topic entry above a well-matched one — which is precisely
    the "hot dog" failure the method warns about.
    """
    cfg = settings.selection.entry
    selected = select_entry_bullets(entry, jd, keywords, now=now)
    block = next(b for b in entry.blocks if b.block_id == selected.block_id)

    alias = _alias_score(block, jd)
    bullet_avg = (
        sum(b.score for b in selected.bullets) / len(selected.bullets)
        if selected.bullets
        else 0.0
    )
    selected.similarity = cfg.weight_alias * alias + cfg.weight_bullets * bullet_avg
    selected.score = (
        cfg.weight_similarity * selected.similarity
        + cfg.weight_coverage * selected.coverage
    )
    return selected


def select_entries(
    entries: list[EntryCand],
    jd: JDContext,
    keywords: tuple[Keyword, ...],
    *,
    kind: str,
    now: datetime | None = None,
) -> list[SelectedEntry]:
    """Rank entries, keep ``max_shown`` above threshold, force-include ``min_shown``."""
    now = now or datetime.now(timezone.utc)
    cfg = settings.selection.work if kind == "work" else settings.selection.project
    ranked = [score_entry(e, jd, keywords, now=now) for e in entries]
    ranked.sort(key=lambda s: s.score, reverse=True)
    passing = [s for s in ranked if s.score >= cfg.threshold]
    return _force_min(passing, ranked, cfg.max_shown, cfg.min_shown)


# ---------------------------------------------------------------------------
# JD context builder — the only per-run embedding (architecture §4.1)
# ---------------------------------------------------------------------------


def build_jd_context(
    parsed: JDParsed,
    *,
    posted_at: datetime | None = None,
    scraped_at: datetime | None = None,
    scrape_window_hours: float | None = None,
    embed_batch_fn=None,
) -> JDContext:
    """Embed a parsed JD into the query facets Layer 4 scores against.

    One batched embed call per job: ``[blended_skills, responsibilities+summary,
    role_summary]``. The first two sum into ``vec_match`` (holistic "does this
    bullet describe the work they want done"); the third is ``vec_role``, matched
    against title aliases.

    The old per-skill ``jd_skill_vecs`` are gone. Their only consumer was
    ``select_skill_candidates``, which ranked the skills pool for a Skills section
    that no longer exists — so the batch drops from ``3 + len(skills)`` embeds per
    job to a flat 3.

    ``responsibilities`` feeds ``vec_match`` but is deliberately NOT a keyword (see
    ``keywords.jd_keywords``): it is the right signal for "is this the same kind of
    work" and the wrong one for "does the resume state this qualification".
    """
    embed_batch_fn = embed_batch_fn or embed_batch
    skills_text = " ".join([*parsed.required_skills, *parsed.nice_to_have])
    resp_text = " ".join([*parsed.responsibilities, parsed.role_summary])
    base = embed_batch_fn([skills_text, resp_text, parsed.role_summary])
    return JDContext(
        vec_role=base[2],
        vec_match=add(base[0], base[1]),
        role_category=parsed.role_category,
        role_level=parsed.role_level,
        posted_at=posted_at,
        scraped_at=scraped_at,
        scrape_window_hours=scrape_window_hours,
    )
