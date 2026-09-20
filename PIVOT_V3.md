# Pivot v3 — Headless template, keyword-coverage scoring

> **Read this before touching anything on the `v3-headless-template` branch.**
> It supersedes the "Selection rules — locked" block in `CLAUDE.md` and
> §4/§5 of `job_automation_architecture.md` for the duration of this pivot;
> those documents are reconciled in Stage 7. Where this file and CLAUDE.md
> disagree today, **this file wins** — CLAUDE.md still describes the old
> Skills/Summary template.
>
> Status: **Stages 1-5 landed on `v3-headless-template`; suite green (383
> passed). Stage 0 (the operator's personalised template) and Stage 6
> (threshold recalibration) are outstanding.** Update the Staging table in §10
> as stages land.

## Context

The bot builds resumes against `resumes/templates/Templete.docx` — a conventional
template with a **profile Summary paragraph**, a **Skills section** (3 categories
+ a "Familiar With" line), and a **Projects** section that swaps position with
Skills by match score.

The operator is switching to the **Headless Headhunter** template at
`resume guide/Headless+Resume+Template.docx`, which has **no Skills section and
no Summary section at all**. Its thesis (decoded in `resume guide/resume_method.md`
and `resume guide/video_qualification_method.md`) is that a skills list scores
zero with a recruiter — *"Where is LangGraph? I saw it in the skills section.
How did you use it? I don't know, you didn't tell me"* — and that every
qualification must be proven **inside a bullet** as WHAT (the keyword) + HOW it
was used + WHY, with WHERE coming from the entry header.

Three consequences drive the whole pivot:

1. **Keywords must live in bullet text, not in a list.** `skills_pool` survives
   only as machine input for scoring; nothing in it renders.
2. **The metric changes.** Today's fit score is 50% cosine against things that
   will not be on the page (summary 0.20 + skill-pool average 0.30). It is
   replaced by **literal keyword coverage of the bullets actually selected** —
   the same substring matching `resume guide/score_coverage.py` uses.
3. **Bullets are written differently.** The `bullet-extract` skill was built for
   this template: 6–8 bullets per entry, bullet 0 a plain-language summary
   bullet, ≤28 words, exactly one period, past tense, no performance numbers.
   The existing `master_profile.yaml` predates it and does **not** match — it is
   regenerated from fresh bullet-extract runs across every repo, not migrated.

Outcome: a resume that reads like the guide's template, scored on whether it
literally covers the JD's stated qualifications, built from bullets the extractor
has already audited for exactly that purpose.

## Decisions (locked — do not re-litigate)

| # | Decision |
|---|---|
| D1 | Two rendered sections: **Work History**, then **Projects**. Same entry format; projects carry no dates. |
| D2 | **Education & Certificates always at top.** No JD-dependent section ordering anywhere. |
| D3 | The resume **Summary section is dropped entirely** — no pool, no scoring, no `master_summaries` reads. (The per-entry *summary bullet* is a different thing and stays.) |
| D4 | Bullets per entry: **tenure-capped, coverage-filled.** `cap = 3 if months<6, 6 if months<18, else 8`; projects cap 5; floor 3. Bullet 0 (summary bullet) is pinned; remaining slots filled greedily. |
| D5 | Bullet selection is **pooled across all `role_blocks` of an entry**. Greedy set-cover: repeatedly take the bullet covering the most *currently-uncovered* JD keywords; stop when the best candidate adds zero new ones, or the cap is hit. Repetition is acceptable only as a side effect of a bullet that also brings something new — which the greedy produces without a separate rule. |
| D5a | **De-duplication is scoped per entry, not per resume.** The greedy **resets per entry**: every entry starts with an empty covered-set and independently tries to cover the whole JD checklist. This is what makes the guide's *"the first entry must tick every box by itself"* achievable. Resume coverage is the **union**; `lead_entry_coverage` is reported separately. |
| D5b | **Off-role bullets get a soft penalty, not a floor.** A pooled bullet's coverage gain is scaled by its own block's JD relevance, so an off-role bullet wins only when nothing on-role covers that keyword. Encodes "not having the keyword is more damaging than repeating" without letting one entry go stack-mixed. |
| D6 | `fit = 0.55·best_experience + 0.45·keyword_coverage`, coverage measured on the **selected bullet text** by literal substring. Selection runs first; coverage of the result feeds fit. |
| D7 | Fix the `bullet-extract` skill's internal drift **before** re-running it across the repos. |
| D8 | **Project links render as a short hyperlinked "Code →".** The full repo URL in the entry line's right slot measured **112 characters** against an ~89-character budget (Arial 10.5 across 6.5in) and overflowed; "Code →" is 59. ⚠ **See the hyperlink finding in §12 — the link is valid in the DOCX but NOT clickable in the LibreOffice-produced PDF.** This decision is unresolved pending that. `src/endpoint/hyperlinks.py` stays deleted; the link is minted with python-docx's `part.relate_to()`, and a test asserts no dangling relationship survives assembly. |
| D9 | **Education & Certificates is static.** The operator hand-writes those 2–3 lines into their template copy once; the assembler never touches the block. |
| D10 | **The 85 existing `applied` rows are left exactly as they are.** They are not real applications — historical data kept for later analysis. No backfill, no deletion. The endpoint 409s on a v1 row (their presigned links expired after 7 days anyway); analytics/dashboard keep a ~6-line version-aware title reader. |
| D11 | **The extractor emits a bullet POOL, not just the render set.** `bullets:` stays exactly as today — the audited, ordered, density-checked 6–8 with `bullets[0]` the summary. A new **`extra_bullets:`** list holds true-but-off-checklist bullets. The bot pools both. Rationale: today a "hot dog" is deleted at extraction time, so when a JD later asks for Redis the pool has nothing to select and the keyword is **unrecoverable**. Tier ZERO mechanism narration is still cut outright — it is not a keyword at all. The 3–8 cap becomes a **render** cap enforced by the bot, never an extraction cap. |
| D11a | **Cross-cutting skills go in EVERY role block, not only the block whose checklist names them.** If the repo genuinely used Docker, and Docker appears on the `quant` checklist but not the `data` one, the `data` block still gets a Docker bullet in its `extra_bullets` — because a data JD may well ask for Docker, and the pool must be able to answer. Concretely: a token whose **IDF across the 128 titles is low** (Git, Docker, SQL, CI/CD, Linux, cloud, testing, REST) is cross-cutting by definition — `suggest_titles.py` already computes that IDF. Every such token the repo has earns a bullet in every block it plausibly serves, **written fresh per role** so the WHY and framing suit that role, never copy-pasted across blocks. The bot renders one only when the JD names it. |
| D12 | **The canonical role checklist is a tie-break only.** JD keywords drive the greedy exactly as in D5. When two bullets have equal JD gain, the one covering more of that role's canonical tokens (from `job_qualifications.md`, via the block's `checklist:`) wins. A canonical token absent from the JD **never forces a bullet in** — zero JD gain still means not selected. |
| D13 | **`job_qualifications.md` is vendored into the repo** at `data/job_qualifications.md`, so the GitHub Actions runner has it without an S3 fetch. `resume guide/refresh_qualifications.py` regenerates it upstream; a `make refresh-quals` step copies it in and `--check` flags staleness. |

| D14 | **Freelance is a third category, not a flavour of employment.** `WorkExperience.employment_type` is `employment` or `freelance`. Employment is force-included — the operator's actual job always appears. Freelance engagements are *separate entries* that compete on merit exactly as projects do: any, all or none may appear on a given resume (`selection.freelance.min_shown: 0`). They render under **Work History**, because that is what they are, with `Freelance · <dates>` in the right-hand slot. |
| D14a | **Employment is not pinned to the top.** Once selected, jobs and freelance entries are ordered together by `order_entries` (match-then-recency). If a freelance engagement matches a JD better, it leads and the job moves down. Only *inclusion* differs between the two, not *position*. |
| D14b | **"Freelance" never appears in a job title.** It is stated once, in the dates slot. A title like "Freelance Full-Stack Developer" reads as a job called that, and repeats what the right-hand column already says. Long legal company names are shortened for the same reason the URL was (`Smart Centre for Perfect Legal Solutions & Research Pvt. Ltd.` → `SCPLS`). |
| D15 | **An entry is never stranded across a page break.** Word has no "keep 66% together" setting, so the rule is a `keepNext` chain: the entry header plus the first `ceil(keep_together_ratio × n)` bullets are bound into one unbreakable group, and the chain stops there so the remainder may still flow. `keepLines` on every bullet stops a single bullet's own wrapped lines splitting. Config: `endpoint.render.keep_together_ratio: 0.66`. |
| D16 | **Section headings are `Heading 2`, not bold Normal.** The template shipped them as merely-bolded Normal paragraphs, which carry no outline level: they do not collapse in Word, do not appear in the navigation pane, and give a parser no structure. Appearance is unchanged because every run carries Arial/10.5/bold/black as *direct* formatting, which beats the style — but colour had to be pinned explicitly, or the heading inherits Heading 2's `0f4761` blue. `_is_section_heading` accepts Heading 2 **or** bold-Normal, so a hand-made template still works. |

## Branch

```
git checkout main && git checkout -b v3-headless-template
```

`ci-video` (PR #12) is unrelated cosmetic footage — close it separately, don't
branch from it.

---

## 1. Stage 0 — the template asset (manual, blocks everything)

The shipped template has **placeholder** header text (`"First Name Last Name"`,
`"Phone Number ⎪ Email ⎪ LinkedIn/Portfolio"`) and placeholder Education lines.
Hard rule #10 ("never modify the header") applied to that file would ship every
resume as "First Name Last Name".

The operator makes a one-time personalised copy at
**`resumes/templates/headless_v1.docx`**:

- paras 0–2: real name, contact, location — hyperlinks applied **in Word**, so
  Word writes correct rels and no code touches them
- paras 5–7: real Education & Certificates, hand-trimmed to the method's
  **3-line cap** (realistically one degree line + one line naming 2–3 certs)
- body entries stay as placeholders; the assembler overwrites them

`resumes/templates/` is already gitignored ("Operator's resume/cover templates
carry personal info + real hyperlinks"), so this is the **existing** pattern —
hard rules #10 and #21 both stay literally true, and no operator identity enters
source.

## 2. New `master_profile.yaml` schema

Mirrors the bullet-extract Step 11 output so merging is mechanical.

```yaml
personal:        # unchanged shape; no longer rendered — filename/Telegram/dashboard only
meta:            # verbatim _meta from the extractor (provenance)
work_experience:
  - id: citesert
    company: CiteSert
    location: "Mumbai, India"
    actual_title: Quantitative Researcher
    safe_title_aliases: [...]     # union of every block's title_aliases (hard rule #6)
    start_date: "2025-06"         # YYYY-MM — drives the tenure cap
    end_date: "present"
    role_blocks:
      - role: data                # data|finance_market|quant|fullstack|ml|backend|devops
        role_fit: primary         # primary|adjacent
        entry_header: "Data Engineer at CiteSert, Mumbai"
        entry_dates: "June 2025 to current"
        checklist: ["Data Engineer"]
        market: "US"
        target_titles: [...]      # may be []
        title_aliases: [...]      # NEVER empty
        bullets:                  # ORDERED; index 0 IS the summary bullet
          - {id: cite_data_b0, text: "..."}
        covered: [...]            # extractor self-report, advisory only
        missing: [...]
        titles_dropped: [{title, reason}]
        lead_pct: 0
projects:
  - id: jab
    name: Job Application Bot
    link: "https://github.com/..."   # rendered as plain text (D8)
    role_blocks: [...]               # entry_dates OMITTED — projects have no dates
skills_pool: [...]    # machine input. Renders NOWHERE.
gap_skills: [...]     # max 4. Renders NOWHERE. Dashboard only.
education: [...]      # record of truth. Renders nowhere (D9).
certifications: [...] # record of truth. Renders nowhere (D9).
```

**Disposition of the old blocks:**

| Block | Fate |
|---|---|
| `summaries:` | **Deleted from the YAML.** The `master_summaries` *table* survives (hard rule #17 — v1 rows reference `summary_id`), rows flipped `is_active=false`. |
| `skills_pool:` | **Kept.** Two live consumers: `with_pool_skills` (`src/parser.py:56`) repairs under-reported JD parses; `_gap_skills_for_jd` (`src/builder/llm_call.py:57`) feeds the dashboard. The empty-pool **raise** at `src/state/master_profile.py:177` softens to a warning. |
| `bullet_groups:` / `selection_contract:` | **Deleted.** They encoded "these bullets restate one claim, show at most one". Greedy set-cover subsumes this exactly: a restatement adds zero new keywords, so its gain is 0 and it is never picked while a non-restating bullet exists. |
| `master_bullets.parent_type` | **Values unchanged** (`experience`\|`project`\|`skill`). `plan_sync` keys on id and compares text only, so renaming the enum would leave stale values on unchanged rows. `project_name` rows stop being desired and auto-deactivate. |
| `master_title_aliases.parent_id` | Becomes the **block id** `"{entry_id}::{role}"` — aliases are per-block now. Old rows stop being desired and deactivate; new rows insert. Self-healing. Projects gain aliases, which they never had. |

**Pydantic** (`src/state/master_profile.py`, replacing L90–180): `Bullet`,
`RoleBlock` (with `summary_bullet_id` → `bullets[0].id`), a shared `Entry` base,
`WorkExperience` and `ProjectEntry`. Validators enforce: `actual_title` ∈
`safe_title_aliases` (rule #6); every block's `title_aliases` ⊆
`safe_title_aliases`; work blocks **require** `entry_dates`; project blocks
**forbid** them; unique entry / block / bullet ids.

`desired_bullets` (L205) gains `block_id`, `role`, `bullet_index`, `is_summary`.
`desired_aliases` (L222) walks role_blocks for both work and projects.
`rebuild` (L307) drops the `_sync_summaries` call.

### Migration `0009_role_blocks.py`

`down_revision = "0008_skill_vocabulary"`.

```python
op.add_column("master_bullets", sa.Column("block_id", sa.Text(), nullable=True))
op.add_column("master_bullets", sa.Column("role", sa.Text(), nullable=True))
op.add_column("master_bullets", sa.Column("bullet_index", sa.Integer(), nullable=True))
op.add_column("master_bullets", sa.Column("is_summary", sa.Boolean(),
              nullable=False, server_default=sa.text("false")))
op.create_index("idx_bullets_block", "master_bullets", ["block_id"])
op.add_column("master_title_aliases", sa.Column("block_id", sa.Text(), nullable=True))
op.create_index("idx_aliases_block", "master_title_aliases", ["block_id"])
op.execute("UPDATE master_summaries SET is_active = false")
op.execute("UPDATE master_bullets SET is_active = false WHERE parent_type = 'project_name'")
```

Never drops `master_summaries` (rule #17). Mirror the columns on
`src/state/models.py:180` and `:218`.

## 3. New module `src/scorer/keywords.py`

`hit()` and `tokens_of()` are **ports of `resume guide/score_coverage.py`**, kept
semantically identical so the offline grader and the live selector agree on what
"covered" means. If they drift, every measured coverage number in the repo
becomes uncomparable.

```python
def norm(s) -> str                  # NFKD, lowercase, strip to [a-z0-9+#./ ]
def hit(tok, text) -> bool          # literal substring; >=60% content-word
                                    # fallback only for >=3-word prose tokens,
                                    # so `Python` needs the literal string
def tokens_of(lines) -> list[str]   # split on , and () ; dedupe on norm
def jd_keywords(parsed) -> tuple[Keyword, ...]
def covered_by(norm_text, keywords) -> set[str]
def coverage_of(covered, keywords) -> float     # weighted fraction
```

**What counts as a JD keyword:** `required_skills` at weight 1.0,
`nice_to_have` at 0.5, **`responsibilities` excluded**. `resume_method.md` is
explicit: *"In a JD, ignore everything except the Qualifications section. Job
title, salary, day-to-day duties, EEOC statements — none of it matters."*
`responsibilities` is exactly that duty prose; including it would flood the token
set with unmatchable sentences and turn `keyword_coverage` into a measure of
prose overlap. It keeps its existing job inside `vec_match` as the embedding
tie-break. `nice_to_have` gets half weight because `score_coverage.py` excludes
"nice to have:" lines from required tokens entirely — half is the softest honest
version of that. All three are config keys, so the choice stays auditable.

## 4. Selection algorithm — `src/scorer/selector.py`

New candidate dataclasses: `BulletCand` (+ `block_id`, `role`, `is_summary`,
`norm_text` computed once), `RoleBlockCand`, `EntryCand` (unified work/project),
`SelectedBullet` (+ `new_keywords`, making the greedy auditable),
`SelectedEntry`, `Profile`.

**Tenure cap** — `bullet_cap(entry, now)` reads `selection.bullets.tenure_bands`;
projects return `project_cap`.

**Lead block** — `_lead_block()` picks max alias cosine to `jd.vec_role`, with
`primary` preferred on a tie. It supplies the header, dates and title alias.

**The greedy**, per entry, covered-set starting empty:

```python
1. Pin bullets[0] of the lead block (the summary bullet). Add its coverage.
2. Loop while len(chosen) < cap and candidates remain:
     for each remaining bullet b:
         gained = covered_by(b.norm_text, keywords) - covered
         gain   = sum(k.weight for k in keywords if k.token in gained)
         gain  *= relevance(b.block)        # D5b soft penalty:
                  # 1.0 if b's block is the lead block,
                  # else alias_cos(b.block, jd) / alias_cos(lead, jd)
     take the highest (gain, cosine) pair
     if gain <= 0 and len(chosen) >= floor: break     # nothing new to say
     append; covered |= gained
```

Precedence is **cap > early-stop > floor**: the cap is a hard ceiling; a
zero-gain bullet stops the fill; but the floor (3) overrides the early stop,
because an entry with one bullet is not a valid entry — below the floor we take
best-by-cosine since all gains are 0 there.

Other blocks' `b0` summary bullets stay in the pool: they describe the same work
from a different angle, and if one carries a keyword the pinned summary lacks it
earns its slot like any other bullet.

**Entry scoring** — selection runs first, then the entry is scored on what it
selected:

```
similarity = 0.30·best_alias_cosine + 0.70·mean(selected bullet cosines)
entry.score = 0.50·similarity + 0.50·coverage
```

Two signals deliberately, not one: `coverage` is what the recruiter grades,
`similarity` is the calibrated embedding score with a year of measured thresholds
behind it. Coverage alone would rank a keyword-dense but off-topic entry top.

**`evaluate()`** (`src/scorer/apply_decision.py:154`):

```
work     = order_entries(select_entries(profile.work,     kind="work"))
projects =        sorted(select_entries(profile.projects, kind="project"))
entries  = [*work, *projects]                    # fixed order, D1

keyword_coverage    = coverage_of(union of per-entry covered sets, keywords)
lead_entry_coverage = entries[0].coverage        # the headline number

fit = 0.55·best_experience_score + 0.45·keyword_coverage
# success_prob / recency / final_score UNCHANGED
```

**Reused vs deleted:**

| Function | Fate |
|---|---|
| `_force_min` (`selector.py:169`) | **reused verbatim** |
| `deterministic.choose_title_alias` (`:52`) | **kept**, now called with the lead block's aliases |
| `deterministic.jd_role_vector` (`:178`), `llm_call._gap_skills_for_jd` (`:57`), `_template_version` (`:47`) | kept |
| `ordering.order_experiences` (`:35`) | kept, renamed `order_entries`, retyped |
| `build_jd_context` (`:334`) | **kept, trimmed** — `jd_skill_vecs` had one consumer, so the batch drops from `[skills, resp, role, *each_skill]` to `[skills, resp, role]`; saves one embed per JD skill per job |
| `select_summary` (`:284`), `select_skill_candidates` (`:306`), `_scored_bullets` (`:160`), `score_experience` (`:183`), `score_project` (`:241`), `_select_project_bullets` (`:232`), `ordering.skills_before_projects` (`:51`) | **deleted** |
| `deterministic._taxonomy` (`:75`), `_category_of` (`:84`), `assign_skill_categories` (`:108`) | **deleted** |
| `endpoint/hyperlinks.py` (whole module) | **deleted** (D8) |

## 5. Assembler rewrite — `src/endpoint/assembler.py`

Every structural assumption in the current file is wrong for this template. The
rewrite replaces text-matched `Heading 1` lookup with three local predicates:

```python
_is_section_heading(p)  # Normal, no numPr, non-empty, ALL non-empty runs bold.
                        # The method bolds ONLY section headings, so boldness is
                        # unambiguous. Text is NOT matched — the template ships
                        # the placeholder "Work History OR Projects".
_is_entry_line(p)       # Heading 3 WITHOUT numPr (Education lines HAVE numPr)
_is_entry_bullet(p)     # Normal with numId == 1 (numId 2 is Education)
```

`_find_heading` (`:498`) and the `_WORK`/`_SKILLS`/`_PROJECTS`/`_EDUCATION`
constants (`:50-54`) are deleted, as is the `doc.paragraphs[2]` summary write
(`:94`).

**Region split:** the frozen prefix is `body[0 : i]` where `i` is the index of
the **second** `_is_section_heading` (the first is "Education & Certificates").
Everything from `i` to `sectPr` is rebuilt from scratch.

**Prototypes** drop from six to four — `section_heading`, `entry_line`,
`entry_bullet`, `spacer`. The `skill` prototype and its `AssemblerError`
(`:281-292`) are gone, as is `proj_name` (found by `<w:hyperlink>` presence,
which no longer exists anywhere in the file).

**Build:** the template ships only **one** section heading, so the assembler
**mints the second** by cloning it — "Work History", then "Projects". Per entry:
clone `entry_line` and set left/right of the tab (right = dates for work, repo
URL or empty for projects), then clone `entry_bullet` per bullet id, then a
spacer; drop the trailing spacer so there is no blank page 2.

**Hard rules #9 / #10 collapse into one property**, which is a genuine
simplification:

- **#10 (new):** the assembler never touches any body element before the second
  bold-Normal section heading — paras 0–8: name, contact, citizenship, spacers,
  Education & Certificates and its lines. Previously the frozen zone was paras
  0–1 only.
- **#9 (new):** one `_frozen_canonical()` lxml-c14n comparison before and after
  assembly replaces both `_assert_header_unchanged` (`:427`) and
  `_static_region_canonical` (`:437`). The static region flips from a **suffix**
  (EDUCATION→EOF) to a **prefix** (BOF→Work History) because the sections swapped
  ends of the document.

Plus `_assert_education_within_cap()` — the region is static so it cannot drift
at runtime, but it *can* drift when the operator edits the template, which is
exactly when nobody is looking. Raises if more than 3 `numId=2` lines exist.

Deleted with the rest: `_set_skill` (`:236`), `_build_skills` (`:341`),
`_build_projects`' hyperlink minting (`:364-372`), `_apply_order` (`:462`),
`_collect_section_elems` (`:480`).

## 6. `StoredSelection` v2 — `src/llm/schemas.py`

```python
class SelectedEntryOut(BaseModel):
    kind: Literal["work", "project"]
    entry_id: str
    block_id: str          # "{entry_id}::{role}" — which block led
    title_alias: str       # from that block's aliases (rule #6)
    header_left: str       # "Data Engineer at CiteSert, Mumbai"
    header_right: str      # dates; repo URL or "" for projects
    bullet_ids: list[str]  # ordered; [0] is the summary bullet
    covered: list[str]
    coverage: float
    score: float
    cap: int               # the tenure cap that applied — makes 3-vs-8 auditable

class StoredSelection(BaseModel):
    version: Literal[2] = 2
    job_id: str
    entries: list[SelectedEntryOut]     # work first, then projects, render order
    jd_keywords: list[str]              # the checklist this was graded against
    keyword_coverage: float             # union over entries
    lead_entry_coverage: float          # first entry alone
    template_version: str
    built_at: str
    cover_letter_text: str = ""
```

Gone: `summary_id`, `experiences`, `projects`, `skills`, `section_order`.
`SkillCategory` (`:424`), `StoredSkills` (`:440`), `SelectedExpEntry` (`:464`),
`SelectedProjEntry` (`:472`) deleted.

**Consumers (D10 — v1 rows stay untouched):** `cache.py:71` dispatches on
`selection_json["version"]`; v1 → `StaleSelectionError` → `app.py` maps to **409**.
`src/state/selection_compat.py` gets one small `title_alias_of(d)` reading both
shapes, imported by `dashboard.py:60` and `analytics.py:129,142` so the 85 rows
stay readable. `main.py:351,429` switches to `selection.entries[0]`.

## 7. Config diff — `config/config.yaml`

**Delete:** `selection.experience.*` (L167-186) · `selection.project` weights and
bullet bounds (L188-201) · `selection.summary` (L203-207) · **the entire
`selection.skills` block including the 130-line taxonomy** (L209-328) ·
`scoring.fit.selected_summary` and `avg_skill_pool_match` (L343-345).

**Add:**

```yaml
selection:
  keywords:
    weight_required: 1.00
    weight_nice_to_have: 0.50
    include_responsibilities: false   # method: qualifications section only
  bullets:
    min_per_entry: 3
    max_cap: 8
    project_cap: 5
    tenure_bands:
      - {under_months: 6,  cap: 3}
      - {under_months: 18, cap: 6}
  entry:
    weight_alias: 0.30
    weight_bullets: 0.70
    weight_similarity: 0.50
    weight_coverage: 0.50
    off_role_scaling: true            # D5b soft penalty
  work:    {threshold: 0.332, max_shown: 3, min_shown: 2, match_then_recency_gap: 0.20}
  project: {threshold: 0.344, max_shown: 3, min_shown: 2}

scoring:
  fit: {best_experience: 0.55, keyword_coverage: 0.45}

builder:  {template_path: "resumes/templates/headless_v1.docx"}
endpoint:
  template_path: "resumes/templates/headless_v1.docx"
  render:
    education_max_lines: 3
    bullet_num_id: "1"
    education_num_id: "2"
    project_link_as_text: true
```

Both `template_path` keys must move together (`builder` L413, `endpoint` L767),
and `python -m src.cli.assets push` must re-push, or GitHub Actions pulls a stale
template.

## 8. bullet-extract skill fixes (D7) — before re-running on the repos

In `/home/vishnu/.claude/skills/bullet-extract/`:

- **Step 12's self-check demands ~8 fields Step 11 explicitly bans** —
  `_meta.role_index`, `has_why`, `group`/`bullet_groups`, `quantifiables_found`,
  `coverage.checklist_source`, per-block `covered_pct`, `_meta.resume_format`,
  `summary_section`. It also asks for "big-picture lines each carry a concrete
  quantifiable", contradicting both the removal of `big_picture` as a kind and
  R9's ban on numbers. Rewrite Step 12 against the Step 11 schema.
- **Stale "the sheet is a 46-title sample"** in the Tier A section — it is 128
  titles since the 2026-08-09 live-source pass.
- **`skill_update_backlog.md`'s `[KEPT] titles / role_index / coverage` line** is
  the likely origin of the drift — correct it so the next pass doesn't reintroduce.

In `/home/vishnu/projects/resume guide/score_coverage.py`:

- `score()` walks `projects[].role_blocks[]` only and **never
  `work_experience[]`** — your two work entries are silently unscored.
- It hard-requires `bullet_extract_1.yaml` and crashes without it, and always
  diffs against `_1` specifically rather than the previous archive.
- It scores the merged checklist pool only; SKILL.md requires **per-title**
  reporting (a real run measured 18% merged while its three titles were
  20%/0%/29%).

Keep `hit()`/`tokens_of()` here and in `src/scorer/keywords.py` semantically
identical — that parity is asserted in Stage 1's tests.

## 9. Test plan

**Delete:** `test_iteration_2_builder.py` L173-308 (all `assign_skill_categories`
tests) · `test_iteration_2_scorer.py` L155-185 (summary + skill-candidate tests),
L331-340, L141 · `test_iteration_2_master_profile.py` L146, L158 (project_name
half) · the hyperlink/`zipfile` rels tests in `test_iteration_2_endpoint.py`.

**Rewrite:** endpoint fixtures to `headless_v1.docx` + role_blocks + v2 selection.
The `skipif(not TEMPLATE_PATH.exists())` guards **stay** (CI must not fail on a
gitignored template) — but add one *non-skipped* test running against the pristine
`resume guide/Headless+Resume+Template.docx`, so structure detection is covered
even without the operator's file. `test_..._header_unchanged` →
`test_frozen_prefix_unchanged` covering Education too. Assert cloned bullets carry
**`numId == 1`** specifically, not merely "some numPr", and entry lines keep three
tab stops.

**Add:** `hit()` parity table vs `score_coverage.py` incl. the 60%-fallback branch
and that short tokens never reach it · `tokens_of("Python (NumPy, pandas)")` → 3
deduped tokens · greedy picks largest **new**-keyword gain, not largest total ·
stops at zero gain **above** the floor, not below · cap beats gain (20 bullets +
4 months → exactly 3) · tenure bands 4mo→3, 12mo→6, 30mo→8, `present` vs `now` ·
`bullets[0]` is always the pinned summary even when another bullet has higher
coverage · **per-entry reset (D5a): two entries sharing a keyword both select the
bullet carrying it** — the test that catches a global greedy · a repeating bullet
IS selected when it also brings an uncovered keyword · a near-duplicate bullet is
never selected (the `bullet_groups` replacement) · off-role bullet loses a tie to
an on-role one but wins when uniquely covering (D5b) · `lead <= total` coverage ·
empty `jd_keywords` → 0.0, no divide-by-zero, still ≥3 bullets · two section
headings minted from one prototype · project right tab slot holds the URL or is
empty · education cap raises on a 4-line template · v1 selection → 409, row
uncorrupted · `title_alias_of` handles both shapes.

## 10. Staging

| Stage | Work | Ends green |
|---|---|---|
| **0** | ⏳ Operator personalises `headless_v1.docx` (§1). A pristine copy is staged there so the code runs; it still has placeholder name/contact/education. | n/a — blocks a real render |
| **1** ✅ | `src/scorer/keywords.py` + tests, incl. parity against `score_coverage.py`. Nothing imports it yet. | ✅ trivially |
| **2+3** ✅ | Profile schema + migration 0009 + `rebuild`/`load_profile`, **merged with** the selection algorithm — `Profile` changes shape, so splitting them needs a throwaway alias. Regenerate `master_profile.yaml` from bullet-extract runs. Config `selection:`/`scoring.fit` diff. Rewrite scorer tests. | ✅ |
| **4** ✅ | `StoredSelection` v2, `llm_call.build`, `deterministic.py` trio deleted, `selection_compat.py`, `main`/`analytics`/`dashboard` updates, `cache.py` version dispatch + 409. | ✅ |
| **5** ✅ | Assembler rewrite, `hyperlinks.py` deleted, both `template_path` keys moved, `assets push`, endpoint tests rewritten. | ✅ |
| **6** ✅ | **Recalibration.** Done 2026-09-01 over all 767 parsed rows; see TODO 8. **Provisional** — measured against the mechanically converted profile, so re-run after the bullet-extract re-run. |
| **7** | 🔶 Partly done (CHANGELOG written). Docs: CLAUDE.md "Selection rules — locked" + hard rules #1/#7/#9/#10/#11, PRD, architecture §3/§4/§5/§6, and `resume_method.md`'s "Where this conflicts with the bot" table (4 of 5 rows become resolved). CHANGELOG `[Unreleased]`. | ✅ |

**Riskiest: Stage 5.** Every other stage is pure functions with cheap unit tests.
Stage 5 is the only one whose failure mode is *silent* — a wrong `numId` on a
cloned bullet, a lost tab stop, or a `w:t` written without
`xml:space="preserve"` produces a DOCX that opens fine, passes every structural
assertion, and looks wrong to a recruiter. Mitigations: assert `numId` **values**
not just presence; c14n-compare a refilled bullet's `pPr` against the prototype's
(text differs, formatting must not); render one real PDF by hand before enabling
`prerender`.

**Second-riskiest: Stage 6**, the only stage with no failing-test signal. A bad
threshold either floods you with matches or silently applies to nothing — and
the last time this went wrong (`config.yaml:171-180`) the threshold sat inert for
85 selections before anyone measured it. **Do not ship Stage 3's thresholds as
final.**

## 11. Known problems, accepted

1. **`fit` is partly self-referential.** Selection maximises coverage, then
   coverage feeds fit, so a keyword-dense profile scores well on every JD.
   Normalising by the JD's own keyword count limits it, but `apply_threshold:
   0.50` is meaningless until Stage 6.
2. **D2 (Education always at top) contradicts `resume_method.md`** for roles that
   don't require a degree — the guide says move it to the bottom then. With a
   static frozen region that needs a second template file
   (`headless_v1_no_degree.docx`). Deferred, not forgotten.
3. **`hit()`'s 60%-content-word fallback was written for prose checklist lines**,
   not Gemini-parsed `required_skills`. A long parsed phrase could register as
   covered at 60% word overlap when it isn't. Low frequency — log when the
   fallback branch fires.
4. **`projects[].link` renders only as plain text** (D8), never as a clickable
   link. Note this in the YAML comment so it doesn't read as a bug.

## Verification

```bash
# Stage 1
pytest tests/test_keywords.py -v
python "resume guide/score_coverage.py"          # parity target

# Stage 2+3
alembic upgrade head
python -m src.cli.reparse                        # rebuild profile from new YAML
pytest tests/test_iteration_2_scorer.py tests/test_iteration_2_master_profile.py -v
python -m src.cli.inspect --job-id=<known>       # per-job selection + coverage

# Stage 5
pytest tests/test_iteration_2_endpoint.py -v
python -m src.cli.render --job-id=<known>        # then OPEN the PDF and read it
python -m src.cli.assets push

# Stage 6
python -m src.cli.dryrun                         # test chat, real links
pytest                                           # whole suite

# end-to-end, several days before trusting selections
python -m src.main --dry-run
```

The non-negotiable manual check: **open a rendered PDF and read it**. No
structural assertion catches a resume that is valid XML and wrong to a human.


---

## Appendix — defects found while implementing

Two were found by the Stage 1 tests and are worth recording, because both were
silently wrong in code that had been shipping.

**1. Substring matching counted keywords the bullet never claimed.**
`hit()` tested `token in text`, so `Java` matched "javascript", `SQL` matched
"postgresql", `R` matched almost any sentence containing "ran", and `Go` matched
"django". A JD requiring Java would have scored as covered by a JavaScript
bullet — the exact fabrication the method exists to prevent, arriving through
the measurement rather than through the writing. Fixed by matching at
alphanumeric boundaries, applied only at the token's own alphanumeric edges so
`C++`, `.NET`, `Node.js` and `CI/CD` still match. The same bug was in
`resume guide/score_coverage.py` and is fixed there too, holding the parity.

**2. Variant titles never resolved.** `score_coverage.py` stripped
` (variant N)` from a block's `checklist` entry before looking it up, but the
sheet's own headings *carry* that suffix (`### DevOps Engineer (variant 1)`), so
the lookup missed and the block scored against an empty token set — silently, as
0 of 0. `canonical_tokens` now tries the exact title first and falls back to
unioning a bare title's variants, which SKILL.md 0.3 explicitly permits.

Also worth knowing: running the rewritten grader against the existing
`bullet_extract_latest.yaml` measures **39% merged coverage** against a ≥75%
target, with `extra: 0` everywhere. That file predates both the `extra_bullets`
pool and the boundary fix, so re-running the skill on this repo is the first
real test of the new extraction rules.


---

## 12. Session findings — read before continuing

### The hyperlink problem — SOLVED (2026-09-01)

**The earlier diagnosis in this section was wrong and is kept below only so the
dead end is not walked twice.** It read the evidence as "LibreOffice only
exports a link Word originally wrote". Provenance has nothing to do with it.

**The actual rule: LibreOffice emits a `/Subtype /Link` annotation only when the
run inside `<w:hyperlink>` carries a `<w:rStyle>`.** Isolated by A/B against both
templates:

| minted `<w:hyperlink>` + rels entry | PDF annotations |
|---|---|
| bare run, no `rPr` | ❌ 0 |
| `w:history="1"`, bare run | ❌ 0 |
| empty `rPr` | ❌ 0 |
| `rPr` holding only `rFonts` | ❌ 0 |
| **`rPr` holding only `<w:rStyle>`** | ✅ all |

`w:history` is irrelevant. So is python-docx vs zip surgery. The v2 clone
approach (`git show main:src/endpoint/hyperlinks.py`) worked purely because its
donor run happened to carry `rStyle="IntenseQuoteChar"`.

This also explains why `headless_v1.docx` shipped five valid header links that
rendered zero annotations: its runs use direct `w:color` + `w:u` with no
character style, which is what a Google Docs export writes.

**Fixed** in `src/endpoint/assembler.py::apply_link_style`, called from
`_set_hyperlink` and from `tools/build_headless_template.py`. It defines the
`Hyperlink` character style when the operator's template lacks it, so this holds
for any template (hard rule #21). Measured end to end on a real render:
**0 link annotations before, 6 after** — five header links plus the project
`Code →`. Guarded by `test_every_hyperlink_run_carries_a_character_style` and a
LibreOffice-gated PDF readback test.

Verify with:

```bash
python -c "import re; d=open('out.pdf','rb').read(); print(len(re.findall(rb'/Subtype\s*/Link', d)))"
```

<details>
<summary>Superseded diagnosis (kept as a record of the dead end)</summary>

**LibreOffice renders the text but silently drops the hyperlink for any link that
was not in the file when Word or Google Docs originally wrote it.** Isolated five
ways, each reproducible:

| test | link exported to PDF? |
|---|---|
| The old `Templete.docx`, 15 Word-authored links | ✅ 15 annotations |
| Same file re-saved through python-docx, unchanged | ✅ still 15 |
| One link added via `part.relate_to()` | ❌ 0 |
| Same, plus `w:history` / `rStyle` / three placements | ❌ 0 |
| **Raw zip surgery — hand-written XML + rels, no python-docx** | ❌ 0 |

The last row rules out python-docx. The XML is valid, the `Relationship` entry is
byte-identical in form to the working ones, and the text renders — LibreOffice
just ignores the reference. Verify with:

```bash
python -c "import re; d=open('out.pdf','rb').read(); print(len(re.findall(rb'/Subtype\s*/Link', d)))"
```

**Consequences.** The header links (Portfolio, LinkedIn, Certificates) and the
project `Code →` links are valid in the DOCX — they work in Word and on portals
that render DOCX — and are dead in the PDF. `Code →` is currently *worse* than
the plain URL it replaced: a reader can neither click it nor read the address.

**Three ways out, undecided:**
1. The operator adds the five header links **in Word** once (the original Stage 0
   plan). Word-authored links demonstrably export. Does not help project links,
   which are minted per render.
2. Revert projects to a readable plain-text URL — not clickable either, but at
   least copyable. Costs the overflow fix unless shortened.
3. Post-process the PDF to inject `/Annots` link annotations after conversion.
   Deterministic, and the only option that fixes both. Real work.

</details>

### Bullet glyph and line height

The template defines the bullet as `U+25CF BLACK CIRCLE` in **Noto Sans Symbols**
at the *inherited* body size. Word lacks that font and substitutes a small dot;
LibreOffice on Linux has it and draws the true heavy circle — so the DOCX and the
PDF disagreed. Current setting: `U+2022 BULLET` in Arial at **16pt**.

**Measured, and worth knowing before changing it:** any glyph above 10.5pt
inflates the line box, because `lineRule="auto"` grows a line to fit its tallest
content. Uniform 18.1pt spacing became an uneven 19–23pt. The fix is
`lineRule="exact"` at 360 twips, pinned on every numbered paragraph, which
restores an 18.0pt dominant gap. Both are applied in
`tools/build_headless_template.py`.

A **cleaner** route exists and was rendered but not chosen: a naturally larger
*character* at the inherited 10.5pt needs neither the size override nor the pin,
because the line box never grows. `U+2022` in the **Symbol** font is Word's own
default bullet and is the obvious candidate.

### Spacing — the thing that was being checked wrongly

Verifying that paragraph *properties* match the template says nothing about
rendered *gaps*. That assumption hid a doubled blank paragraph before
"Education & Certificates" — **36pt against 18pt at the same kind of boundary** —
through several rounds of review. Use `tools/measure_pdf_spacing.py`, which reads
line positions out of the PDF itself.

Blank paragraphs quantise spacing to whole 18pt lines. Real control is
`space_before` / `space_after` on the heading. A ladder at 6/10/14/18pt was
rendered for the operator to choose from; **no value has been chosen yet**, and
the template still uses collapsed blank paragraphs.

### The reference resume cannot be measured

`resume guide/Example+Resume+.pdf` is a Google Docs export that writes one `Tm`
per glyph with three distinct Y values, so per-line positions are not recoverable.
`skill_update_backlog.md` records the same conclusion from an earlier attempt.
Spacing decisions against it have to be made by eye.

---

## 13. TODO for the next agent

**Blocking / decide first**
1. ~~Resolve the hyperlink problem (§12).~~ **Done 2026-09-01** — the run inside
   `<w:hyperlink>` needs an `<w:rStyle>`; see §12. Header and `Code →` links are
   live in the PDF.
2. **Choose the header spacing value** — 6/10/14/18pt — and switch the template
   from blank paragraphs to `space_before` on the section headings.
3. **Consider the Symbol-font bullet**, which would let both the 16pt size
   override and the `lineRule="exact"` pin be removed.

**Stage 0 — the operator's own work**
4. Re-run the `bullet-extract` skill across every repo. The current
   `master_profile.yaml` is the OLD flat shape and will not validate against the
   new `role_blocks` schema. Bullets must also be re-written to the method: ≤28
   words, one period, past tense, **no performance numbers** (the current pool is
   full of them).
5. Mark `employment_type: freelance` on DekhLaw, SCPLS and the MQL5/Yaagi
   entries. ~~shorten the SCPLS company name; strip "Freelance" from every
   title.~~ **Both done 2026-09-01** — company is `SCPLS`, and "Freelance" is
   gone from `actual_title` and from four `safe_title_aliases`. The
   `employment_type` marking still needs doing, and until it is, the operator's
   actual job is not force-included: measured, CiteSert drops off the page
   entirely when all four entries claim to be employment.
6. `alembic upgrade head` — the database is still at `0008`; `0009_role_blocks`
   has never been applied.
7. `python -m src.cli.assets push` — `resumes/templates/` is gitignored, so the
   GitHub Actions runner has no template until this is run.

**Stage 6 — recalibration, and it matters more than it looks**
8. ~~Every threshold is the v2 value measured against the OLD scoring formula.~~
   **Done 2026-09-01**, over all 767 parsed `all_jobs` rows. Every threshold was
   inert, and in both directions:

   | knob | was | observed | now |
   |---|---|---|---|
   | `scoring.apply_threshold` | 0.50 | final_score p50 0.254, p95 0.372, **max 0.523** | **0.372** (p95) |
   | `selection.work.threshold` | 0.332 | employment entry p50 0.172, p90 0.224, max 0.341 | **0.199** (p75) |
   | `selection.project.threshold` | 0.344 | project entry p50 0.085, p90 0.153, p99 0.241 | **0.153** (p90) |
   | `selection.freelance.threshold` | 0.150 | freelance entry p50 0.158 — the old value was the **p22** | **0.210** (p88) |
   | `match_then_recency_gap` | 0.20 | best-second gap p50 0.015, p99 0.098 | **0.047** (p90) |

   Effect: 40 of 767 ads notify (5.2%); freelance appears on 31% of resumes
   instead of effectively all; the third project slot is earned in 18%.

   **Provisional.** The profile behind these numbers is the OLD flat bullet pool
   mechanically converted to `role_blocks`, not a bullet-extract re-run. Coverage
   rises once bullets are rewritten to the method, which lifts `final_score` —
   **re-run the calibration after Stage 0 or this floods.**
9. ~~`match_then_recency_gap: 0.20` is likewise too wide.~~ **Done** — folded
   into 8 above. Measured p50 gap 0.015, p99 0.098; set to the p90, 0.047.

**Verification that is not optional**
10. Render a real PDF and *read it*. 389 passing tests did not catch: a sentence
    rendered twice, a 112-character line overflow, a blue name, a doubled blank
    line, or dead hyperlinks. Every one of those was found by looking at the page.

**Scratch work worth knowing about**
The session's throwaway harnesses live in the session scratchpad and will be
lost: `mkprofile.py` (converts the OLD profile to `role_blocks` for testing
before the real re-extraction), `e2e.py` (real JD from the DB → selection →
DOCX → PDF), and the glyph/spacing ladder generators. Only
`tools/build_headless_template.py` and `tools/measure_pdf_spacing.py` were kept.
