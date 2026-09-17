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
from src.scorer.qualifications import canonical_covered, canonical_overlap

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
    #: "employment" | "freelance" — see MasterProfile.WorkExperience. Freelance
    #: entries render under Work History but are selected on merit, like projects.
    employment_type: str = "employment"


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
    #: Which phase earned this slot — "jd" (this JD asked for it) or
    #: "qualification" (the title's standing checklist did). A phase-2 bullet can
    #: legitimately have an empty ``new_keywords``, so without this the audit trail
    #: cannot tell "covered nothing" from "covered nothing THIS JD named".
    via: str = "jd"
    #: Canonical tokens this bullet added, for phase-2 bullets. Empty for phase 1.
    new_canonical: list[str] = field(default_factory=list)


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
    #: URL for the right slot, or "". Separate from ``header_right`` because a
    #: freelance entry shows BOTH a label and a link in that slot.
    header_link: str = ""
    title_alias: str = ""
    link: str = ""
    end_date: str = ""
    #: Carried through from :class:`EntryCand` — "employment" | "freelance", and
    #: left at "employment" for projects, which have no such attribute. Ordering
    #: needs it: ``kind`` cannot tell a salaried job from a freelance gig, since
    #: both load as ``kind="work"``.
    employment_type: str = "employment"


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


def bullet_cap(entry: EntryCand, now: datetime) -> int:
    """How many bullets this entry may show.

    No tenure scaling: a four-month job and a three-year job get the same ceiling,
    because what an entry covers decides its length, not how long it lasted.

    Projects take a lower cap than work. Measured on a real run (v3.1): every
    project ran to the full 8 while the jobs stopped at 4-6, because a project's
    pooled blocks carry more near-equivalent material -- so the cap, not coverage,
    was setting project length, and the last slots filled with restatement. A job
    is what a recruiter reads; a project is supporting evidence, and 5 is where it
    stops earning its space.
    """
    cfg = settings.selection.bullets
    if entry.kind == "project":
        return int(cfg.project_cap)
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


def _header_right(entry: EntryCand, block: RoleBlockCand) -> tuple[str, str]:
    """What sits right of the tab, as ``(text, link)``.

    Three cases, deliberately different, because the slot answers a different
    question for each:

    * **Salaried employment** — dates, and nothing else. There is no public
      artifact to show: the work belongs to the employer. Dates are the thing a
      recruiter checks.
    * **Freelance** — the ``Freelance`` label and a link to the delivered site.
      No dates. A short engagement's value is that the result is live and can be
      clicked; its two-month span invites the wrong question.
    * **Project** — the link alone. Projects carry no dates by the method.

    The link prefers a live demo over a repo: a recruiter with twenty seconds
    opens a working site, not a source tree. ``entry.link`` holds the demo where
    one exists and falls back to the repo where it does not.
    """
    if entry.kind != "work":
        return "", entry.link
    if entry.employment_type == "freelance":
        return settings.selection.freelance.label, entry.link
    return block.entry_dates, ""


def _content_words(norm_text: str) -> list[str]:
    return [w for w in norm_text.split() if w]


def _reads_as_repeat(
    norm_text: str, chosen_norm: list[str], *, jaccard: float | None = None
) -> bool:
    """Would this bullet read as a restatement of one already chosen?

    Keyword arithmetic cannot see this. Two bullets can cover different keywords
    and still open with the same six words -- measured on a real run, one entry
    rendered "Worked to an Agile practice of small pull requests in Git..." and
    "Worked to an Agile practice, reviewing each change..." because the second
    brought one new token and paid only one repeat. On the page that is one claim
    made twice.

    Two cheap lexical tests, no embeddings: a shared opening (what the eye catches
    scanning a bullet list) or heavy word overlap (a genuine reword). Both run on
    text already normalised for keyword matching.
    """
    cfg = settings.selection.bullets
    lead_n = int(getattr(cfg, "duplicate_prefix_words", 0) or 0)
    jaccard_max = (
        float(getattr(cfg, "duplicate_jaccard", 1.0)) if jaccard is None else jaccard
    )
    min_words = int(getattr(cfg, "duplicate_min_words", 12))
    words = _content_words(norm_text)
    if not words:
        return False
    head = words[:lead_n]
    bag = set(words)
    for other in chosen_norm:
        o_words = _content_words(other)
        if lead_n and len(head) == lead_n and o_words[:lead_n] == head:
            return True
        o_bag = set(o_words)
        # Word overlap only means something once there are enough words for the
        # ratio to be informative. Two five-word sentences differing in one noun
        # score 0.6 while saying entirely different things; a real bullet is
        # 20-28 words by the method, where 0.55 is a genuine reword.
        if min(len(words), len(o_words)) < min_words:
            continue
        union = bag | o_bag
        if union and len(bag & o_bag) / len(union) >= jaccard_max:
            return True
    return False


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
    rendered_norm: list[str] | None = None,
    rendered_keywords: dict[str, int] | None = None,
) -> SelectedEntry:
    """Pin the summary bullet, then fill the entry in two greedy phases.

    Phase 1 covers THIS JD. Phase 2, once JD gain is exhausted, covers what
    recruiters for this title screen for whether or not this JD named it. Both
    phases draw from the same cross-block pool, share the same ``chosen`` list and
    stop the same way — at zero net gain — so neither can pad the entry.

    Both phases score a candidate as::

        net = new_weight * relevance - repeat_penalty * repeated_weight

    The penalty is what keeps a keyword from rendering twice in one entry (Git in a
    `data` bullet and again in a `devops` bullet, both true, both pooled). It sits
    inside the gain rather than in the tie-break because a tie-break only fires on
    exactly equal gain, which is never the case that produces the duplicate: a
    denser bullet that happens to repeat one keyword would win outright.

    Relevance scales the REWARD only, never the penalty. Scaling both would make an
    off-role bullet (relevance as low as ``off_role_floor``) nearly exempt from the
    penalty — and off-role bullets, pooled from other blocks, are exactly where the
    duplicates come from.

    Precedence, when the limits disagree: **cap > early-stop > floor.**

      * ``cap`` is a hard ceiling — it is what fits on the page.
      * a zero-net-gain best candidate stops a phase: a bullet that says nothing new
        is exactly what the method says to delete.
      * the floor overrides that early stop, because an entry showing one bullet is
        not a valid entry. Below the floor every gain is already 0, so those slots
        are filled by cosine.
    """
    cfg = settings.selection.bullets
    cap = bullet_cap(entry, now)
    floor = min(int(cfg.min_per_entry), cap)
    lam = float(getattr(cfg, "repeat_penalty", 0.0))
    across_cap = int(getattr(cfg, "max_repeats_across_entries", 0) or 0)
    require_unique_extras = bool(getattr(cfg, "extras_must_be_unique_source", True))
    across_jaccard = float(getattr(cfg, "across_entry_jaccard", 0.33))
    repeat_ratio = float(getattr(cfg, "repeat_requires_ratio", 1.0))
    kw_cap = int(getattr(cfg, "max_keyword_renders", 0) or 0)
    kw_seen = {} if rendered_keywords is None else rendered_keywords
    rendered = [] if rendered_norm is None else rendered_norm

    def _spent(tokens: set[str]) -> set[str]:
        """Drop tokens already claimed their maximum number of times on this page.

        The method permits a keyword to repeat across entries, and that stays true
        -- this is a ceiling, not a ban. Measured: "Agile" rendered in four of six
        entries, each time in a genuinely different sentence, so no text-similarity
        rule could see it. The repetition lives in the keyword, so the ceiling has
        to live there too.
        """
        if not kw_cap:
            return tokens
        return {t for t in tokens if kw_seen.get(t, 0) < kw_cap}

    def _blocked(b: BulletCand, chosen_norm: list[str]) -> bool:
        """Barred within this entry, or already at its cross-entry ceiling."""
        if _reads_as_repeat(b.norm_text, chosen_norm):
            return True
        # A LOOSER ratio than the within-entry bar. Within an entry a match is a
        # ban, so it must be precise; across entries it only caps at N, so it can
        # afford to group a family more generously. Measured on the AI-tooling
        # family: 0.66 / 0.44 / 0.36 between its three renderings, so the strict
        # 0.55 bar recognised only one of the three pairs.
        if across_cap and sum(
            1 for t in rendered
            if _reads_as_repeat(b.norm_text, [t], jaccard=across_jaccard)
        ) >= across_cap:
            return True
        # The recovery pool is for RECOVERY. An extra may render only when it is the
        # only bullet in this entry that can claim something the JD asked for --
        # which is the contract RoleBlock.extra_bullets already states: "nothing in
        # extra_bullets renders unless a JD asks for its keyword". Until now that
        # was enforced only incidentally, and an extra whose subject the JD never
        # mentions (the AI-tooling bullets) could win a slot on one incidental word
        # it happened to share with the checklist.
        if b.is_extra and require_unique_extras:
            jd_hits = covered_by(b.norm_text, keywords)
            mine = jd_hits | canonical_covered(b.norm_text, block.checklist)
            unique = bool(mine - coverable_by_render_set)
            # A zero-repeat extra is admitted even when it is not the unique source.
            # Measured: the unique-source test was blocking the CLEANER route to a
            # keyword — "Regression" was reachable both from an audited bullet that
            # also restates SQL and from an extra that repeats nothing. Blocking the
            # extra forced the repeat, so a rule meant to protect the render set was
            # manufacturing the repetition it exists alongside.
            clean_here = not (jd_hits & covered)
            if not unique and not clean_here:
                return True
        return False

    block = lead_block(entry, jd)
    block_scores = {rb.block_id: _alias_score(rb, jd) for rb in entry.blocks}
    pool = _entry_pool(entry, block.block_id)

    # Everything the AUDITED bullets of this entry could claim. An extra that adds
    # nothing outside this set is not recovering anything.
    coverable_by_render_set: set[str] = set()
    for b in pool:
        if not b.is_extra:
            coverable_by_render_set |= covered_by(b.norm_text, keywords)
            coverable_by_render_set |= canonical_covered(b.norm_text, block.checklist)

    covered: set[str] = set()
    covered_canon: set[str] = set()
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
        # The summary's canonical tokens count as said, so phase 2 does not repeat
        # what the entry already opened with.
        covered_canon |= canonical_covered(summary.norm_text, block.checklist)

    # --- 2. phase 1: beam search over this JD's checklist -----------------
    # Greedy took the single best bullet at each step and never reconsidered, which
    # is myopic on a set-cover: an early pick can consume a common keyword that
    # another bullet would have supplied ALONGSIDE a rare one, so the rare keyword
    # then needs a worse bullet. Measured on one entry, that cost a whole repeat —
    # a set existed with identical coverage and half the repetition, and greedy
    # could not reach it.
    #
    # The beam keeps `beam_width` partial sets alive and re-ranks after each
    # expansion, so a bullet that looks worse now can be taken when the set it
    # leads to is better. Every rule stays a HARD CONSTRAINT: a candidate must pass
    # `_blocked` (restatement, cross-entry family ceiling, extras) and the
    # affordability gate before it may extend any beam. The search only chooses
    # better among sets that were already legal.
    #
    # Objective, lexicographic: covered WEIGHT first, then fewest repeats, then the
    # existing per-bullet tie-breaks. Coverage never loses to tidiness; tidiness
    # decides between sets that cover the same thing.
    chosen_norm = [summary.norm_text] if summary is not None else []
    remaining = [b for b in pool if summary is None or b.id != summary.id]

    # Hit sets are computed ONCE here rather than per candidate per iteration, which
    # is also why the beam runs faster than the greedy it replaces.
    hits_of = {b.id: covered_by(b.norm_text, keywords) for b in remaining}
    canon_of = {
        b.id: canonical_covered(b.norm_text, block.checklist) for b in remaining
    }
    sim_of = {b.id: cosine(b.embedding, jd.vec_match) for b in remaining}
    rel_of = {
        b.id: _relevance(block_scores, block.block_id, b.block_id) for b in remaining
    }

    @dataclass
    class _Beam:
        ids: tuple[str, ...]
        covered: frozenset
        covered_canon: frozenset
        norms: tuple[str, ...]
        repeats: int
        extras: int
        sim_sum: float
        picks: tuple  # (bullet, gained) in path order

        @property
        def rank(self) -> tuple:
            # Set-level objective. The per-bullet tie-breaks the greedy applied one
            # step at a time have to be expressed here instead, or they vanish: two
            # sets covering the same keywords with the same repetition are still not
            # equally good, and an audited set beats one leaning on the recovery
            # pool. Coverage first, always.
            return (
                round(weight_of(set(self.covered), keywords), 9),
                -self.repeats,
                -self.extras,
                round(self.sim_sum, 9),
            )

    seed = _Beam(
        ids=(), covered=frozenset(covered), covered_canon=frozenset(covered_canon),
        norms=tuple(chosen_norm), repeats=0, extras=0, sim_sum=0.0, picks=(),
    )
    beams = [seed]
    finished: list[_Beam] = []
    width = max(1, int(getattr(cfg, "beam_width", 20)))
    slots = cap - len(chosen)

    for _ in range(max(0, slots)):
        nxt: list[_Beam] = []
        for st in beams:
            grew = False
            for b in remaining:
                if b.id in st.ids:
                    continue
                if _blocked(b, list(st.norms)):
                    continue
                hits = hits_of[b.id]
                gained = _spent(hits - st.covered)
                if not gained:
                    continue
                repeated = hits & st.covered
                raw_w = weight_of(gained, keywords)
                rep_w = weight_of(repeated, keywords)
                # The affordability gate, unchanged: a repeating bullet's new
                # coverage must be worth at least `repeat_ratio` times what it
                # restates. Clean bullets skip the test entirely.
                if rep_w > 0.0 and raw_w < repeat_ratio * rep_w:
                    continue
                grew = True
                nxt.append(_Beam(
                    ids=st.ids + (b.id,),
                    covered=st.covered | gained,
                    covered_canon=st.covered_canon | canon_of[b.id],
                    norms=st.norms + (b.norm_text,),
                    repeats=st.repeats + (1 if repeated else 0),
                    extras=st.extras + (1 if b.is_extra else 0),
                    sim_sum=st.sim_sum + sim_of[b.id],
                    picks=st.picks + ((b, gained),),
                ))
            if not grew:
                finished.append(st)
        if not nxt:
            break
        # Deduplicate on the SET, not the path: two orders of the same bullets are
        # the same resume entry and must not both occupy the beam.
        seen_sets: set = set()
        ranked = sorted(nxt, key=lambda st: st.rank, reverse=True)
        beams = []
        for st in ranked:
            key = frozenset(st.ids)
            if key in seen_sets:
                continue
            seen_sets.add(key)
            beams.append(st)
            if len(beams) >= width:
                break

    best = max([*beams, *finished], key=lambda st: st.rank, default=seed)

    # The beam chose a SET; the order it happened to build that set in is an
    # artifact of the search, not a reading order. Sort for the page instead —
    # densest first, audited before recovery pool, then cosine — so the strongest
    # sentence sits directly under the pinned summary where the twenty-second scan
    # lands. Attribution is then replayed in THIS order, so `new_keywords` says what
    # each bullet adds as the reader meets it rather than as the search found it.
    ordered = sorted(
        (b for b, _ in best.picks),
        key=lambda b: (
            round(weight_of(hits_of[b.id], keywords), 9),
            not b.is_extra,
            sim_of[b.id],
        ),
        reverse=True,
    )
    for b in ordered:
        gained = hits_of[b.id] - covered
        chosen.append(
            SelectedBullet(
                b.id, b.text, sim_of[b.id], False, sorted(gained), via="jd"
            )
        )
        covered |= gained
        covered_canon |= canon_of[b.id]
        chosen_norm.append(b.norm_text)
        for tok in hits_of[b.id]:
            kw_seen[tok] = kw_seen.get(tok, 0) + 1
        remaining.remove(b)

    # The floor still outranks the early stop: an entry showing one bullet is not a
    # valid entry, so if the beam ran dry below it, fill by cosine.
    while len(chosen) < floor and remaining:
        b = max(
            (x for x in remaining if not _blocked(x, chosen_norm)),
            key=lambda x: sim_of[x.id], default=None,
        )
        if b is None:
            break
        gained = _spent(hits_of[b.id] - covered)
        chosen.append(
            SelectedBullet(b.id, b.text, sim_of[b.id], False, sorted(gained), via="jd")
        )
        covered |= gained
        covered_canon |= canon_of[b.id]
        chosen_norm.append(b.norm_text)
        remaining.remove(b)

    # --- 3. phase 2: fill the rest from the title's own qualification list --
    # The JD has nothing left to ask for. The remaining slots go to what a recruiter
    # screening this TITLE looks for — the JD is one lossy sample of that list, not
    # the list itself. Unweighted: a canonical token carries no JD weight, so a
    # second scale of weights here would be false precision.
    while cfg.qualification_fill and len(chosen) < cap and remaining:
        best = None
        for b in remaining:
            if _blocked(b, chosen_norm):
                continue
            hits = canonical_covered(b.norm_text, block.checklist)
            gained_c = hits - covered_canon
            repeated_c = hits & covered_canon
            jd_repeat = weight_of(covered_by(b.norm_text, keywords) & covered, keywords)
            # Phase 2 may not repeat AT ALL. It runs only after this JD has nothing
            # left to ask for, so a phase-2 bullet's entire claim on the line is a
            # canonical token the entry has not said. One that also restates
            # something already on the page is buying a repetition with the weakest
            # currency there is — measured: a bullet earning its slot on the single
            # token "git" while restating CI/CD from the entry's own DevOps bullet.
            if repeated_c or jd_repeat > 0.0:
                continue
            gain_c = len(gained_c) * _relevance(
                block_scores, block.block_id, b.block_id
            )
            sim = cosine(b.embedding, jd.vec_match)
            key = (round(gain_c, 9), not b.is_extra, sim)
            if best is None or key > best[0]:
                best = (key, b, gained_c, gain_c, sim)

        if best is None:
            break
        _, cand, gained_c, gain_c, sim = best
        if gain_c <= 0.0:
            break
        # A phase-2 bullet CAN still carry an uncovered JD keyword — phase 1 stops on
        # NET gain, so a bullet with real new coverage and heavier repeats ends that
        # phase without being taken. If such a bullet renders here, its keywords are
        # genuinely on the page, so they join `covered`; excluding them would
        # understate coverage. What never joins `covered` is a canonical token — the
        # JD set stays the JD set, so `coverage_of` and the calibrated thresholds
        # keep meaning exactly what they meant before.
        chosen.append(
            SelectedBullet(
                cand.id, cand.text, sim, False,
                sorted(covered_by(cand.norm_text, keywords) - covered),
                via="qualification", new_canonical=sorted(gained_c),
            )
        )
        covered |= covered_by(cand.norm_text, keywords) - covered
        covered_canon |= gained_c
        chosen_norm.append(cand.norm_text)
        for tok in covered_by(cand.norm_text, keywords):
            kw_seen[tok] = kw_seen.get(tok, 0) + 1
        remaining.remove(cand)

    right_text, right_link = _header_right(entry, block)
    return SelectedEntry(
        id=entry.id,
        kind=entry.kind,
        block_id=block.block_id,
        label=entry.label,
        header_left=block.entry_header,
        header_right=right_text,
        header_link=right_link,
        bullets=chosen,
        covered=covered,
        coverage=coverage_of(covered, keywords),
        similarity=0.0,
        score=0.0,
        cap=cap,
        link=entry.link,
        end_date=entry.end_date,
        employment_type=entry.employment_type,
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
    cfg = getattr(settings.selection, kind)
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
