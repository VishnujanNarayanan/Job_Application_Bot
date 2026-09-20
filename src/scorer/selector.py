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

That is a set-cover, solved by a beam search over bullet sets.

The covered set resets for EVERY entry (PIVOT_V3.md D5a). Coverage is not rationed
across entries: the method grades the first entry on whether it clears the whole
checklist alone, so a keyword the first entry used must remain available to the
second. Repetition across entries is expected; only within one entry is it waste.

WHAT CHANGED IN v3.3
--------------------
Two changes, and the second is what the first paid for.

  1. The render set now comes from the LEAD BLOCK ALONE; only ``extra_bullets``
     pool across every block of the entry.
  2. Repetition inside an entry became a flat rule: a bullet that restates an
     already-covered keyword is never selected.

Every earlier version pooled render bullets across all blocks, which pulled the
extractor's three re-wordings of one accomplishment into a single entry and left
this module refereeing between them — with a squared repeat penalty, an
affordability ratio, two lexical near-duplicate tests, a cross-entry family
ceiling and a unique-source rule for extras. All of it is gone. The duplicates
came from the pooling, and the re-extract rewrote every ``extra`` as a
single-subject sentence carrying one or two keywords, so the flat ban costs no
coverage. ``max_keyword_renders`` survives as the one cross-entry ceiling.
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


def _render_set(block: RoleBlockCand) -> list[BulletCand]:
    """The block's AUDITED bullets — its recovery pool is not part of what it is."""
    return [b for b in block.bullets if not b.is_extra]


def block_coverage(
    block: RoleBlockCand, keywords: tuple[Keyword, ...]
) -> tuple[float, float, set[str]]:
    """What this block's render set covers of the JD checklist.

    Returns ``(total_weight, required_weight, tokens)``. Keywords only — the
    comparison is between the checklist and the tokens the block's sentences
    literally contain, never between embeddings of whole bullets. A bullet's
    embedding measures topical mood; a recruiter's checklist is answered by words
    that are either written down or are not.

    Render set only. ``extra_bullets`` are a recovery pool the ENTRY may reach into
    once a lead is chosen — they belong to no block's identity, and counting them
    would let a block win the lead on material it would not render.
    """
    hits: set[str] = set()
    for b in _render_set(block):
        hits |= covered_by(b.norm_text, keywords)
    required = {k.token for k in keywords if k.weight >= 1.0}
    return weight_of(hits, keywords), weight_of(hits & required, keywords), hits


def lead_block(
    entry: EntryCand, jd: JDContext, keywords: tuple[Keyword, ...] = ()
) -> RoleBlockCand:
    """The block that supplies the render set, the header, the dates and the title.

    Chosen on WHAT ITS BULLETS COVER of this JD's checklist — not on its title
    aliases (v3.3). An alias list is an arbitrary label the extractor attached to a
    block; two blocks of one project can carry near-identical alias lists, and a
    project needs no title at all, since the entry line shows the project's name.
    Picking the lead by alias cosine meant a label decided which bullets a
    recruiter reads, which is backwards: the block that can answer this advert is
    the one whose sentences contain the answers.

    ``lead = w_keywords * keyword_score + w_similarity * cosine``

    Keywords carry the weight (0.75 by default) because that is what a screen
    grades; cosine keeps a real minority share (0.25) because keywords alone cannot tell
    that a block is the same KIND of work — the "hot dog" failure — and two blocks
    of one entry routinely cover the same checklist tokens, where the embedding is
    the only thing left that can separate them.

    ``keyword_score`` is the mean of two ratios, both in [0, 1]:

      * what fraction of the whole checklist's weight the render set covers
      * what fraction of the REQUIRED half it covers — three required beats six
        nice-to-haves at the same total, because that is how a screen reads

    ``cosine`` is the render set's mean similarity to the JD. ``extra_bullets``
    enter neither term: a block cannot win the lead on material it would not
    render. ``primary`` breaks an exact tie — an adjacent block is, by the
    extractor's own admission, a stretch.

    With no checklist (a JD that parsed to nothing) both ratios are 0 for every
    block and the cosine share decides alone, which is the right degradation.
    """
    cfg = settings.selection.entry
    w_kw = float(getattr(cfg, "lead_weight_keywords", 0.75))
    w_sim = float(getattr(cfg, "lead_weight_similarity", 0.25))
    total_w = weight_of({k.token for k in keywords}, keywords)
    required_w = weight_of(
        {k.token for k in keywords if k.weight >= 1.0}, keywords
    )

    def key(rb: RoleBlockCand) -> tuple[float, bool]:
        total, required, _ = block_coverage(rb, keywords)
        ratios = [total / total_w if total_w else 0.0,
                  required / required_w if required_w else 0.0]
        bullets = _render_set(rb)
        sim = (
            sum(cosine(b.embedding, jd.vec_match) for b in bullets) / len(bullets)
            if bullets else 0.0
        )
        score = w_kw * (sum(ratios) / len(ratios)) + w_sim * sim
        return (round(score, 9), rb.role_fit == "primary")

    return max(entry.blocks, key=key)


def _entry_pool(entry: EntryCand, lead_id: str = "") -> list[BulletCand]:
    """What this entry may choose from: the LEAD block's render set, plus every
    block's recovery pool.

    The two halves are pooled differently on purpose (v3.3).

    The render set is the extractor's authored entry — ordered, density-checked,
    written so those bullets read as one job aimed at one title family. Taking
    render bullets from several blocks at once mixed three such authored sets into
    one entry and produced the restatement this module spent four rules chasing:
    the extractor writes each accomplishment "re-worded in every block it honestly
    serves", so the same claim exists three times under three ids, and keyword
    arithmetic cannot see that they are the same sentence. Confining the render
    set to the lead block removes the duplicates at the source rather than
    detecting them afterwards.

    ``extra_bullets`` still pool across every block, because that is what the
    recovery pool is for: a keyword the lead block's checklist happens not to name
    (Docker under a `data` block, SQL under a `devops` one) must stay reachable,
    or it is unrecoverable at selection time. Since the re-extract, an extra is a
    single-subject sentence carrying one or two keywords, so pooling them adds
    coverage without adding restatement.

    The text dedup stays: the same wording can appear in two blocks' recovery
    pools under different ids, and the floor fills its last slots by cosine, which
    would otherwise pick the twin of a bullet already on the page.
    """
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    pool: list[BulletCand] = []
    blocks = sorted(entry.blocks, key=lambda rb: rb.block_id != lead_id)
    for rb in blocks:
        for b in rb.bullets:
            if b.id in seen_ids:
                continue
            # Off-lead blocks contribute their recovery pool only.
            if b.is_extra is False and lead_id and rb.block_id != lead_id:
                continue
            key = _text_key(b.norm_text)
            if key in seen_text:
                continue
            seen_ids.add(b.id)
            seen_text.add(key)
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


def _relevance(block_scores: dict[str, float], lead_id: str, block_id: str) -> float:
    """How much an off-role EXTRA's coverage gain counts.

    1.0 for the lead block. For any other block, what its render set covers of this
    JD relative to what the lead's covers — so an extra from a barely-related block
    must cover something genuinely unclaimed to beat an on-role one, but is never
    excluded outright. Excluding it wholesale (a hard floor) would drop keywords the
    operator really has, which is the more expensive mistake: not having a keyword
    costs the match, repeating one costs a line.

    Measured on the same signal the lead is chosen with (v3.3). It used to be alias
    cosine, which made a label decide how much a sentence counted for.
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
    rendered_keywords: dict[str, int] | None = None,
) -> SelectedEntry:
    """Pin the summary bullet, then fill the entry in two phases.

    Phase 1 covers THIS JD. Phase 2, once JD gain is exhausted, covers what
    recruiters for this title screen for whether or not this JD named it. Both
    phases draw from the same pool, share the same ``chosen`` list and stop the
    same way — at zero gain — so neither can pad the entry.

    REPETITION IS A RULE, NOT A PRICE (v3.3)
    ----------------------------------------
    A bullet that restates a keyword the entry has already covered — the pinned
    summary included — is never selected. There is no penalty term, no
    affordability ratio and no lexical restatement test: a candidate either brings
    something uncovered or it does not render.

    That replaces four rules this module had accumulated (``repeat_penalty``,
    ``repeat_requires_ratio``, the prefix/jaccard duplicate tests and
    ``extras_must_be_unique_source``). All four existed because bullets were pooled
    across every block, which pulled three re-wordings of one accomplishment into
    one entry and forced the selector to referee between them. The render set is now
    the lead block's alone (see ``_entry_pool``) and the re-extract rewrote each
    ``extra`` as a single-subject sentence carrying one or two keywords, so the
    duplicates no longer reach the choice and a flat ban costs nothing in coverage.

    The one ceiling left is across entries: ``max_keyword_renders`` caps how many
    entries may claim the same keyword. The method permits cross-entry repetition,
    so this is a ceiling rather than a ban — it only stops "Agile" landing in four
    of six entries.

    Precedence, when the limits disagree: **cap > early-stop > floor.**

      * ``cap`` is a hard ceiling — it is what fits on the page.
      * a zero-gain best candidate stops a phase: a bullet that says nothing new
        is exactly what the method says to delete.
      * the floor overrides that early stop, because an entry showing one bullet is
        not a valid entry. Below the floor every gain is already 0, so those slots
        are filled by cosine, preferring whatever repeats least.
    """
    cfg = settings.selection.bullets
    cap = bullet_cap(entry, now)
    floor = min(int(cfg.min_per_entry), cap)
    kw_cap = int(getattr(cfg, "max_keyword_renders", 0) or 0)
    kw_seen = {} if rendered_keywords is None else rendered_keywords

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

    block = lead_block(entry, jd, keywords)
    block_scores = {
        rb.block_id: block_coverage(rb, keywords)[0] for rb in entry.blocks
    }
    pool = _entry_pool(entry, block.block_id)

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
    # leads to is better. The single hard constraint is the zero-repeat rule: a
    # candidate may extend a beam only with keywords that beam has not covered.
    # The search chooses among sets that were already legal.
    #
    # Objective, lexicographic: covered WEIGHT first, then the tie-breaks —
    # audited bullets before the recovery pool, on-role blocks before off-role,
    # then cosine. Coverage never loses to tidiness.
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
        extras: int
        rel_sum: float
        sim_sum: float
        picks: tuple  # (bullet, gained) in path order

        @property
        def rank(self) -> tuple:
            # Set-level objective. The per-bullet tie-breaks the greedy applied one
            # step at a time have to be expressed here instead, or they vanish: two
            # sets covering the same keywords are still not equally good — an
            # audited set beats one leaning on the recovery pool, and a set drawn
            # from blocks this JD is about beats one reaching across the entry.
            # Coverage first, always.
            return (
                round(weight_of(set(self.covered), keywords), 9),
                -self.extras,
                round(self.rel_sum, 9),
                round(self.sim_sum, 9),
            )

    seed = _Beam(
        ids=(), covered=frozenset(covered), covered_canon=frozenset(covered_canon),
        extras=0, rel_sum=0.0, sim_sum=0.0, picks=(),
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
                hits = hits_of[b.id]
                # The rule: nothing this beam has already said. Not a penalty, not
                # a ratio — a bullet restating a covered keyword does not render.
                if hits & st.covered:
                    continue
                gained = _spent(hits)
                if not gained:
                    continue
                grew = True
                nxt.append(_Beam(
                    ids=st.ids + (b.id,),
                    covered=st.covered | gained,
                    covered_canon=st.covered_canon | canon_of[b.id],
                    extras=st.extras + (1 if b.is_extra else 0),
                    rel_sum=st.rel_sum + rel_of[b.id],
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
    #
    # The pinned summary keeps position 1 regardless: `chosen` already holds it and
    # this sorts only what follows.
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
        for tok in hits_of[b.id]:
            kw_seen[tok] = kw_seen.get(tok, 0) + 1
        remaining.remove(b)

    # The floor still outranks the early stop: an entry showing one bullet is not a
    # valid entry, so if the beam ran dry below it, fill by cosine.
    #
    # This is the one place the zero-repeat rule bends, and it bends as little as
    # it can: the beam only runs dry when nothing left adds a keyword, so every
    # remaining candidate either repeats something or covers nothing at all. A
    # bullet covering nothing repeats nothing, so those are taken first and a
    # restatement only reaches the page when the entry would otherwise be invalid.
    while len(chosen) < floor and remaining:
        b = max(
            remaining,
            key=lambda x: (not (hits_of[x.id] & covered), sim_of[x.id]),
            default=None,
        )
        if b is None:
            break
        gained = _spent(hits_of[b.id] - covered)
        chosen.append(
            SelectedBullet(b.id, b.text, sim_of[b.id], False, sorted(gained), via="jd")
        )
        covered |= gained
        covered_canon |= canon_of[b.id]
        for tok in hits_of[b.id]:
            kw_seen[tok] = kw_seen.get(tok, 0) + 1
        remaining.remove(b)

    # --- 3. phase 2: fill the rest from the title's own qualification list --
    # The JD has nothing left to ask for. The remaining slots go to what a recruiter
    # screening this TITLE looks for — the JD is one lossy sample of that list, not
    # the list itself. Unweighted: a canonical token carries no JD weight, so a
    # second scale of weights here would be false precision.
    while cfg.qualification_fill and len(chosen) < cap and remaining:
        best = None
        for b in remaining:
            hits = canonical_covered(b.norm_text, block.checklist)
            gained_c = hits - covered_canon
            repeated_c = hits & covered_canon
            jd_repeat = weight_of(covered_by(b.norm_text, keywords) & covered, keywords)
            # Phase 2 obeys the same zero-repeat rule as phase 1, on both
            # currencies. It runs only after this JD has nothing left to ask for, so
            # a phase-2 bullet's entire claim on the line is a canonical token the
            # entry has not said; one that also restates something already on the
            # page is buying a repetition with the weakest currency there is —
            # measured: a bullet earning its slot on the single token "git" while
            # restating CI/CD from the entry's own DevOps bullet.
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
        # A phase-2 bullet CAN still carry an uncovered JD keyword — phase 1 stops
        # when the cross-entry ceiling has spent every token a candidate would add,
        # which leaves bullets whose JD keywords are uncovered but unclaimable. If
        # such a bullet renders here, its keywords are genuinely on the page, so they
        # join `covered`; excluding them would understate coverage. What never joins
        # `covered` is a canonical token — the
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
    the "hot dog" failure the method warns about — and it is also why the lead block
    is now picked on coverage while the ENTRY is still ranked on both.

    The alias term applies to work and freelance only (v3.3). A job has a real
    title, and how close it sits to the advertised one is information. A project
    does not: the entry line shows the project's NAME, its alias list is a label the
    extractor attached for machine matching, and letting an arbitrary label carry
    30% of a project's similarity is the same mistake the lead-block choice just
    stopped making. For a project the bullets carry the whole similarity.
    """
    cfg = settings.selection.entry
    selected = select_entry_bullets(entry, jd, keywords, now=now)
    block = next(b for b in entry.blocks if b.block_id == selected.block_id)

    bullet_avg = (
        sum(b.score for b in selected.bullets) / len(selected.bullets)
        if selected.bullets
        else 0.0
    )
    if entry.kind == "project":
        selected.similarity = bullet_avg
    else:
        alias = _alias_score(block, jd)
        selected.similarity = cfg.weight_alias * alias + cfg.weight_bullets * bullet_avg
    selected.score = (
        cfg.weight_similarity * selected.similarity
        + cfg.weight_coverage * selected.coverage
    )
    return selected


def select_top(
    profile: Profile,
    jd: JDContext,
    keywords: tuple[Keyword, ...],
    *,
    now: datetime | None = None,
) -> list[SelectedEntry]:
    """Score every entry the operator has and keep the best ``top_n``.

    WHY THERE IS NO THRESHOLD ANY MORE (v3.4)
    -----------------------------------------
    Until now each kind had its own cutoff — work 0.199, freelance 0.210, project
    0.153 — plus a ``max_shown`` and a ``min_shown`` to catch the cases the cutoff
    got wrong. Every one of those numbers was a percentile measured by running
    selection over the job corpus once, which means they describe a distribution
    that stops existing the moment the scoring formula changes. It did change, and
    the failure was not subtle: on a real full-stack advert exactly ONE entry of
    nineteen cleared its threshold, and the page was filled out by ``min_shown``
    backfill rather than by merit.

    A count needs no calibration. "The best five" is a ranking, not a cutoff, so it
    cannot drift when a weight moves: the page is always full, always of the five
    entries that answer this JD best, and a formula change reorders them instead of
    emptying the page.

    Kind stops gating anything too. Work, freelance and projects are scored the
    same way and compete in one pool, which is what the merged section already
    renders — a project that answers the advert better than a gig should outrank
    it, and now does.

    THE ONE GUARANTEE. The salaried employment entry is always on the page, even
    when five others outscore it: a resume without the operator's actual job is not
    a resume. If it did not earn a place it takes the last one, displacing the
    weakest entry. Where it then SITS is ``order_entries``' job — it holds position
    1 or 2 (``selection.entry.job_within_top``), so the page never opens without
    the job in view.
    """
    now = now or datetime.now(timezone.utc)
    top_n = max(1, int(getattr(settings.selection, "top_n", 5)))

    candidates = [*profile.work, *profile.projects]
    ranked = sorted(
        (score_entry(e, jd, keywords, now=now) for e in candidates),
        key=lambda s: s.score,
        reverse=True,
    )
    selected = ranked[:top_n]

    if not any(_is_employment(s) for s in selected):
        job = next((s for s in ranked if _is_employment(s)), None)
        if job is not None:
            # Displace the weakest, not the nearest miss: the entry that earned its
            # place least is the one that gives it up.
            selected = [*selected[: top_n - 1], job]
    return selected


def _is_employment(entry: SelectedEntry) -> bool:
    """The operator's salaried job. A freelance engagement loads as ``kind="work"``
    too, so ``kind`` alone cannot answer this -- see ``ordering._is_salaried``."""
    return entry.kind == "work" and entry.employment_type == "employment"


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
