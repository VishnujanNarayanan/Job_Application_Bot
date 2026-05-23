# CLAUDE.md — Instructions for AI Assistants Working on This Project

This file is the canonical context for any AI assistant collaborating on this codebase. Read this FIRST before suggesting or writing any code.

---

## What this project is

A fully autonomous job application bot for **Vishnujan Narayanan**. It scrapes Indian job portals (Indeed, Glassdoor), scores listings, builds a custom-tailored resume per JD from a master profile, and submits applications automatically — all on $0/month free infrastructure.

**Read these in order before doing anything:**

1. `PRD.md` — what this product is and isn't
2. `job_automation_architecture.md` — full 9-layer architecture (source of truth)
3. This file — practical conventions when writing code

---

## Hard rules — never violate these

### 1. The LLM never writes or selects bullet content
Every bullet on every resume comes verbatim from `master_profile.yaml`. The LLM's only jobs are: pick a title alias from an allow-list, name 3 skill categories and assign skills from pre-scored candidates, pick Familiar With gap skills, write cover letter text, and answer form questions. Bullet selection is done by sentence-transformers scoring (pure math), not the LLM. Skill candidates are picked by deterministic scoring before the LLM ever sees them.

### 2. master_profile.yaml is the single source of truth
All work, projects, bullets, summaries, skills, education, and certifications live in `master_profile.yaml` (gitignored). The system reads it, never writes to it. The user edits it manually.

### 3. No LinkedIn — ever
LinkedIn scraping or auto-apply is forbidden. The user's main account uses LinkedIn and ban risk is unacceptable. Don't add LinkedIn code paths even "just in case."

### 4. No fabrication anywhere
LLM-generated content must reference only what exists in master profile text. Post-generation validation checks tech names and numbers against master profile. Regenerate up to 2x; if still failing, raise BUILD_FAILURE.

### 5. Job titles come only from safe_title_aliases
Each experience has a `safe_title_aliases` allow-list. The LLM picks the best match per JD, enforced at API level via `Literal[tuple(safe_title_aliases)]`. The LLM physically cannot return an unlisted title.

### 6. Skills come only from skills_pool and identified gaps
Deterministic scoring picks the top-14 pool skill candidates and identifies all JD gap skills. The LLM then names 3 categories with 3-5 skills each (drawn from the candidates) and picks up to 4 Familiar With gap skills. Total displayed: 10-14 pool skills + 0-4 gap skills. Distribution is flexible — the LLM optimizes for clean semantic grouping, not for hitting fixed counts. Familiar With is treated as a 4th category and ordered against the others by aggregate match score (NOT pinned to first position). Post-validation enforces source-set membership and regenerates on violation.

### 7. Built resumes are diff-validated
After assembly, a diff check confirms only permitted regions changed. Any unexpected change → BUILD_FAILURE, never "fixed up."

### 8. Header is never modified
The DOCX assembler MUST NOT touch any paragraph before the first "WORK EXPERIENCE" Heading1. The header contains embedded hyperlinks (GitHub, LinkedIn, Certificates) that must survive every build untouched.

### 9. Hyperlink integrity on cloned blocks
Project and certificate hyperlinks MUST be updated by modifying the relationship file (`word/_rels/document.xml.rels`) to point to URLs from master_profile. Visible link text ("Code →", "Verify Here") MUST remain unchanged.

### 10. No double submissions
On submission failure, retry exactly once. Never twice. Duplicate applications are worse than a missed one.

### 11. Free tier only
Every service must be on a free tier with no paid graduation. Validated: Neon PostgreSQL (3GB), Oracle Cloud Always Free VM (200GB), Gemini 2.0 Flash (1500 calls/day), Telegram Bot API, Google Sheets/Docs API, GitHub Actions (Iterations 1-4 only).

### 12. Gemini call budget — 3 calls max per job
Call 1a (parse, always), Call 1b (title + skills + cover letter, if score passes AND picked for application), Call 2 (batched form questions, if needed). Don't add a 4th. New LLM needs get bundled into an existing call.

Queued jobs do NOT trigger Call 1b until they're picked.

### 13. Cycle-aware application picking
At end of each run: pick top N from {new eligible jobs + active queue}, sort by `final_score`. N=3 during 8-11am IST, N=1 otherwise. Unpicked eligible jobs go to `application_queue` with 12-hour expiry.

### 14. Queue decay
`application_queue` entries expire 12 hours after `queued_at`. Expired entries move to `not_applied` with reason `STALE`. Sunday cleanup enforces this.

### 15. Interview integrity is non-negotiable
The user must be able to defend every word on every submitted resume. Any feature that changes something the user can't speak to in an interview is rejected, even if it improves match scores.

### 16. Every submitted resume is kept forever
`resumes/applied/{job_id}_{timestamp}.pdf` is permanent — never auto-cleaned. The `applied` table stores `resume_path` plus a full `selection_json` snapshot. The audit trail must always resolve.

### 17. Bullets are never hard-deleted
When a bullet is removed from `master_profile.yaml`, the `master_bullets` row is marked `is_active=false`. Same for `master_summaries` and `master_title_aliases`. Old applied records must always resolve to the exact content used.

### 18. CHANGELOG.md must reflect every code-affecting change
The repo has a `CHANGELOG.md` at the root. Every change to code, schema, config, or specs that would survive a `git push` MUST be reflected in the `[Unreleased]` section of the changelog before the user pushes. See the dedicated "Changelog discipline" section below for the format, rules, and the push protocol.

---

## Architecture quick reference

```
LAYER 1   Scheduler              Oracle Cloud cron
LAYER 2   Scraper                JobSpy (Indeed, Glassdoor) with serial rotation
LAYER 3   JD Parser              Gemini Call 1a — ALWAYS runs
LAYER 4   Scoring Engine         Master profile selection + cycle picking
LAYER 5   Resume Builder         Gemini Call 1b — only when picked for application
LAYER 6   Application Sender     Gemini Call 2 — if unknown form questions
LAYER 7   State (PostgreSQL)     Neon free tier with pgvector
LAYER 8   Notifications          Telegram morning digest
LAYER 9   Analytics              Sheets (live view) + Docs (monthly report)
```

### Selection rules — locked

```
EXPERIENCE
  score      = best_alias_score × 0.30 + top3_bullet_avg × 0.70
  max 3, min 2, threshold 0.45
  force-include strongest 2 if fewer pass
  bullets: exactly 3 per experience, top by score
  order: best-match at position 1 if match gap > 0.20, else recency;
         positions 2+ by recency

PROJECT
  score      = name_score × 0.20 + topN_bullet_avg × 0.80
  N = number of bullets actually displayed (2 or 3)
  max 3, min 2, threshold 0.50
  force-include best 2 if fewer pass — section NEVER hidden
  bullets: min 2, max 3, force-include best 2 if fewer pass
  order: score-descending

SUMMARY
  selected from pre-written pool by JD match — NO LLM generation

SKILLS
  Step 1: deterministic scoring picks top-14 pool candidates by JD match
  Step 2: deterministic scoring of all JD gap skills (not in pool)
  Step 3: LLM names exactly 3 categories, assigns 3-5 skills each from
          the 14 candidates. LLM also picks up to 4 Familiar With gap skills.
  Step 4: deterministic ordering within each category by skill score
  Step 5: ALL 4 categories (3 LLM-named + Familiar With) ordered by
          aggregate match score, descending. Familiar With NOT pinned.
  Flexibility: 10-14 pool skills total, 0-4 gap skills, distribution
               optimized for clean grouping not for hitting a count
  Pydantic + post-validation enforces source-set membership

SECTION ORDER
  Summary (fixed) → Work Experience (fixed) →
  [Skills / Projects ordered by match] → Education (fixed) →
  Certifications (fixed, last)

FINAL SCORE
  fit × 0.55 + success_prob × 0.30 + recency × 0.10 + project × 0.05
  apply threshold: final_score >= 0.50
```

### Application picking — cycle-aware

```
Each run, after scoring:
  N = 3 if 8am <= now < 11am IST else 1
  Combine new eligible (final_score >= 0.50) with active queue
  Sort by final_score, descending
  Pick top N → send to Layer 5
  Unpicked → INSERT into application_queue (expires_at = now + 12h)
```

### Personal config locked

```
User:                  Vishnujan Narayanan
Experience:            1.5 years
Years required ceiling: 5 (jobs requiring more get rejected)
Job type:              Fulltime only
Location:              All allowed except Delhi NCR (Delhi, Gurgaon,
                       Gurugram, Noida, Ghaziabad, Faridabad)
Visa:                  No filter (open to international)
Expected salary:       JD upper bound if specified, else 6 LPA default
Current salary:        100000 numeric / strategic redirect text
Upload filename:       Vishnujan_Narayanan_Resume.pdf
Cover filename:        Vishnujan_Narayanan_Cover_Letter.pdf
Familiar With max:     4 adjacent-domain skills
Unknown question mode: Safe (hold for review)
Company blocklist:     None
```

---

## Tech stack

```
Language          Python 3.11+
Scraping          JobSpy (Indeed, Glassdoor)
Browser           Playwright
NLP validation    spaCy
Embeddings        sentence-transformers (all-MiniLM-L6-v2)
LLM               Gemini 2.0 Flash via Instructor (Pydantic-enforced)
Database          PostgreSQL (Neon) with pgvector
DB driver         psycopg3
ORM               SQLAlchemy 2.0
Resume building   python-docx (template manipulation)
PDF conversion    LibreOffice headless
PDF rendering     reportlab
Notifications     Telegram Bot API (python-telegram-bot)
Reporting         gspread, Google Docs API
Logging           structlog
```

### Why Instructor + Pydantic everywhere

LLM outputs are unreliable without structure enforcement. Every Gemini call returns a Pydantic model. `max_length`, `Literal` types, and `Field` validators run at the API level — schema violations are physically impossible. Never parse JSON from LLM strings manually; use Instructor.

---

## Code conventions

### Modularity

Each layer is its own module with a clear input/output contract. Layer 4's selection algorithm is pure functions with no I/O. Configuration values live in `config/config.yaml`, never hardcoded.

### Imports

```python
# Standard lib first
import asyncio
import json
from datetime import datetime
from typing import Literal

# Third-party
from sqlalchemy import select
from pydantic import BaseModel, Field

# Local
from src.state.models import AllJobs, Applied
from src.llm.client import gemini_client
```

### Async vs sync

- Playwright code: async
- Database access: async with SQLAlchemy 2.0 async session
- JobSpy: sync (run via asyncio.to_thread if needed)
- Gemini calls: sync (Instructor is sync; cheap enough)
- sentence-transformers: sync (local, fast)

### Error handling

```python
# Good — specific exceptions, structured logging
try:
    result = await submit_application(page, job)
except PlaywrightTimeoutError as e:
    log.error("submission_timeout", job_id=job.id, error=str(e))
    await state.mark_failed(job.id, reason="APPLY_FAILURE", detail=f"timeout: {e}")

# Bad — bare except, swallowed errors
try:
    result = await submit_application(page, job)
except:
    pass
```

### Database access

```python
async with async_session() as session:
    job = await session.get(AllJobs, job_id)
    job.outcome = "applied"
    await session.commit()
```

### Pydantic schemas live in src/llm/schemas.py
Don't define one-off schemas in implementation files. Centralize them so the LLM contract is auditable.

### Configuration

`config/config.yaml` is the source of truth for runtime config. `.env` is ONLY for secrets (API keys, DATABASE_URL).

```python
# Good
from src.config import settings
threshold = settings.selection.experience.threshold

# Bad — hardcoded magic numbers
threshold = 0.45
```

---

## The master profile rebuild

`src/state/master_profile.py` owns the rebuild logic:

```
On every bot run, check master_profile.yaml mtime against master_meta.
If changed:
  1. Validate the YAML schema (fail loudly + Telegram alert if invalid)
  2. Generate master_profile.json
  3. Diff against DB:
     - new bullets       → embed + insert (is_active=true)
     - changed bullets   → re-embed + update
     - removed bullets   → set is_active=false, deactivated_at=now
     - new/changed summaries, title aliases → same pattern
  4. Update master_meta timestamps
```

Never hard-delete from `master_bullets`, `master_summaries`, or `master_title_aliases`. Deactivate only.

---

## DOCX assembler (Layer 5)

The assembler uses **structural detection (Option A)** — no markers or template tags. It reads the template's Heading1 structure to identify sections, then clones the first sub-block within each variable section to use as a pattern.

**Preservation guarantees:**

- Header (before first Heading1): NEVER touched — preserves embedded hyperlinks for GitHub, LinkedIn, Certificates
- Paragraph properties (tab stops, spacing, indentation, alignment): inherited via XML deep clone
- Font, size, color, bold, italic: preserved per run
- Native bullet list references (`<w:numPr>` with `numId`): preserved
- Project hyperlinks: `r:id` target in relationships file updated to `project.link`, visible "Code →" text unchanged
- Certificate hyperlinks: `r:id` target updated to `cert.verify_link`, visible "Verify Here" text unchanged

---

## Logging

Use structlog. Every Gemini call, application attempt, and failure logged with `event` name, `job_id` if applicable, latency in ms, and outcome/error.

```python
log.info("gemini_call_1a", job_id=job.id, tokens=850, latency_ms=1200)
log.info("resume_built", job_id=job.id, experiences=3, projects=2)
log.info("application_submitted", job_id=job.id, portal="indeed", score=0.81)
log.info("queue_decayed", job_id=job.id, queued_for_hours=12)
log.error("session_expired", portal="indeed")
```

---

## Testing strategy

### Unit tests
- Mock the LLM client — never hit Gemini in tests
- Mock the database with a pytest fixture or in-memory SQLite
- Mock Playwright — don't launch browsers in unit tests
- Layer 4's selection algorithm is pure Python — test thoroughly with synthetic master profiles and JDs

### Integration tests
- Real database (Neon test branch — free)
- Real Gemini calls used sparingly
- Skip Playwright in CI; test manually in dev

### Dry-run mode

The orchestrator MUST support `--dry-run`: scrape, parse, score, and build for real, but never submit. Logs what it WOULD have done. Use this for the first week before enabling auto-apply.

### CLI debug commands

```bash
python -m src.cli.inspect --job-id=XYZ        # show full pipeline state per job
python -m src.cli.dryrun                       # full pipeline without submission
python -m src.cli.reparse                      # rebuild master profile from YAML
python -m src.cli.queue                        # inspect application_queue
```

---

## Changelog discipline

The repo has a `CHANGELOG.md` at the root. Every code-affecting change MUST be recorded there before the user pushes to git.

### File format

The changelog follows the Keep a Changelog convention. Initialize it at Iteration 0 with this exact structure:

```markdown
# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project loosely tracks iterations rather than semver.

## [Unreleased]

### Added
- (pending entries go here)

### Changed
- (pending entries go here)

### Fixed
- (pending entries go here)

### Removed
- (pending entries go here)

---

## [Iteration 0] — YYYY-MM-DD

### Added
- Initial scaffold: repo structure, requirements.txt, config.yaml,
  .env.example, master_profile.example.yaml, smoke test, README,
  this changelog.
```

Use only these four section types: `Added`, `Changed`, `Fixed`, `Removed`. Drop any section that has no entries in a given release. Do NOT introduce other categories (no `Security`, `Deprecated`, `Notes`, etc.) — keep it simple.

### What goes in the changelog

INCLUDE these:
- New layers, modules, or files that affect runtime behavior
- Modifications to existing logic (selection rules, thresholds, scoring formulas)
- Schema migrations (any change to DB tables)
- Config changes (anything in `config.yaml`)
- New dependencies in `requirements.txt`
- Bug fixes that change observable behavior
- Removals of code, config, or dependencies
- Changes to PRD, architecture, or CLAUDE.md spec documents

DO NOT include these:
- Whitespace, formatting, or comment-only changes
- Local test runs or debug logs
- Files that are gitignored
- Refactors that don't change behavior (mention in commit message instead)

### Writing style

Each entry is one line, present tense, describes what changed from the user's perspective. Reference the layer or module if relevant.

Good entries:
```
- Layer 4: scoring formula now uses top-3 bullet average instead of top-4
- Config: added `selection.experience.threshold` default 0.45
- Schema: added `application_queue` table with 12-hour expiry
- Fixed double-submission bug in Layer 6 when network timeout occurred
- Removed: LinkedIn scraper (unused after architecture lock-in)
```

Bad entries:
```
- updated code         (too vague)
- fixed bug            (which bug?)
- refactored Layer 5   (refactor isn't a behavior change)
- improved performance (improved how? what was the bottleneck?)
```

### When entries get added

EVERY time you (the agent) make a code-affecting change, append an entry to the appropriate subsection of `[Unreleased]` in the same edit session. Do not batch — add the entry as soon as the change is made. If you forget, add it before finishing your response.

If the user makes a manual change and tells you about it, treat it like one of your own changes and add the entry.

### The git push protocol

When the user says "I'm going to push to git" (or "I'm pushing", "ready to push", or similar):

1. **Stop and review `[Unreleased]` first.** If the section is empty, the changelog is up to date — confirm with the user.
2. **Convert `[Unreleased]` into a dated iteration entry.** Pick the iteration number based on what stage the project is in (Iteration 0, 1, 2, etc.). If the work is between iterations or is a small fix during an iteration, use a sub-version like `Iteration 2.1`.
3. **Date the entry with today's date** in `YYYY-MM-DD` format.
4. **Re-create an empty `[Unreleased]` section** at the top with all four subsections empty (or omitted).
5. **Show the user the dated entry before they push** so they can review and edit.
6. **Do not auto-commit or auto-push.** The user runs `git add`, `git commit`, and `git push` themselves.

Example transformation when the user says "I'm pushing":

BEFORE:
```markdown
## [Unreleased]

### Added
- Layer 2: JobSpy integration with serial rotation
- Config: added search_rotation_state table

### Fixed
- Layer 3: spaCy validator no longer crashes on empty JDs
```

AFTER (today is 2026-06-15):
```markdown
## [Unreleased]

## [Iteration 2.1] — 2026-06-15

### Added
- Layer 2: JobSpy integration with serial rotation
- Config: added search_rotation_state table

### Fixed
- Layer 3: spaCy validator no longer crashes on empty JDs
```

### When you forget to update the changelog

If you realize mid-session that you made changes without updating the changelog, the response is NOT "I'll do it later." Stop your current task, add the missing entries, then resume.

If the user catches you missing an entry: add it immediately, no defensiveness, no justifying. The changelog discipline is brittle if entries get skipped — every missed entry breaks the audit chain for that period.

### Iteration boundaries

When you complete an iteration (Iteration 0, 1, 2, etc.), the closing entry should clearly mark it. Example:

```markdown
## [Iteration 1] — 2026-05-30

### Added
- End-to-end skeleton: all 9 layers wired with stub implementations
- Telegram dry-run notification confirmed working
- Iteration 1 acceptance criteria met (see architecture doc Section 8)

### Changed
- Layer 7: switched from sync to async SQLAlchemy session
```

This is the user's signal that an iteration is complete and they can move to the next one.

---

## When in doubt

1. Architecture conflict? The architecture doc wins.
2. Free vs paid? Free wins, every time.
3. Interview safety vs match score? Interview safety wins.
4. More LLM calls vs simpler code? Simpler code wins (call budget is tight).
5. More features vs reliability? Reliability wins.
6. Convention vs cleverness? Convention wins.
7. LinkedIn vs anything else? Anything else wins.

---

## Things explicitly NOT to do

- Don't write bullet-authoring or bullet-rewriting logic. Bullets are user-written, sacred.
- Don't let the LLM select bullets — sentence-transformers scoring does that.
- Don't add LinkedIn code paths.
- Don't add fields to `JDParsed` that aren't used by Layer 4 or 5.
- Don't store credentials anywhere except `.env` (gitignored).
- Don't add a web dashboard. Iteration 7 may add an MCP server.
- Don't cache resumes or cover letters. Every build is fresh.
- Don't hard-delete rows from master_bullets / master_summaries / master_title_aliases.
- Don't write to master_profile.yaml from code. The user owns that file.
- Don't bypass the diff check on built resumes.
- Don't add retry loops > 1 attempt on submission.
- Don't apply to the same company within 10 days (cooldown is automatic).
- Don't hide the projects section — it always shows 2-3 projects.
- Don't let the LLM return a job title outside safe_title_aliases.
- Don't let the LLM include a skill not in skills_pool.
- Don't touch the resume header — it has embedded hyperlinks that must survive.
- Don't apply to more than the cycle quota (3 peak / 1 off-peak) per run.
- Don't introduce paid services. Period.
- Don't skip changelog updates. Every code-affecting change goes into `[Unreleased]` in the same session.
- Don't auto-commit or auto-push to git. The user does that manually.

---

## Onboarding checklist

For an AI working on this code:

- [ ] Read `PRD.md`
- [ ] Read `job_automation_architecture.md`
- [ ] Read this file
- [ ] Skim `config/config.yaml` for runtime config
- [ ] Skim `src/llm/schemas.py` for LLM contracts
- [ ] Check `requirements.txt` — don't suggest other libraries without need

For a human contributor:

- [ ] All of the above, plus:
- [ ] Set up Neon free tier, DATABASE_URL into `.env`
- [ ] Set up Gemini API key into `.env`
- [ ] Set up Telegram bot via @BotFather, token into `.env`
- [ ] Write `master_profile.yaml` with bullet pools, safe_title_aliases, skills_pool
- [ ] Place resume template in `resumes/templates/`
- [ ] Run `python -m src.cli.reparse`
- [ ] Run in `--dry-run` for 1 week before enabling auto-apply

---

## Definition of done for any code change

- Implements the requirement from PRD or architecture
- Violates no hard rule above
- Has unit tests (mock external services)
- Logs structured events for observability
- Handles failures explicitly (no bare except)
- Introduces no new paid dependencies
- Respects the 3-call Gemini budget
- Doesn't break existing tests
- Updates this file or the architecture doc if it changes any locked decision
- **CHANGELOG.md `[Unreleased]` section updated with an entry describing the change**

---

**End of CLAUDE.md.**
