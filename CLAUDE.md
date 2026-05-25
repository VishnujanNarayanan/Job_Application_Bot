# CLAUDE.md — Instructions for AI Assistants Working on This Project

This file is the canonical context for any AI assistant collaborating on this codebase. Read this FIRST before suggesting or writing any code.

---

## What this project is

A fully autonomous job application bot for **Vishnujan Narayanan**. It scrapes Indian job portals (Indeed, Glassdoor), scores listings, builds a custom-tailored resume per JD from a master profile, submits applications automatically, and uses AWS (S3, CloudWatch, IAM) for storage and observability — all on $0/month free infrastructure.

**Read these in order before doing anything:**

1. `PRD.md` — what this product is and isn't
2. `job_automation_architecture.md` — full 9-layer architecture (source of truth)
3. This file — practical conventions when writing code

**Current build status:** Iteration 0 (scaffold) is complete. Iteration 0.1 (AWS preparation) is the next step before Iteration 1 begins.

---

## Hard rules — never violate these

### 1. The LLM never writes or selects bullet content
Every bullet on every resume comes verbatim from `master_profile.yaml`. The LLM's only jobs are: pick a title alias from an allow-list, name 3 skill categories and assign skills from pre-scored candidates, pick Familiar With gap skills, write cover letter text, and answer form questions. Bullet selection is done by sentence-transformers scoring, not the LLM. Skill candidates are picked by deterministic scoring before the LLM ever sees them.

### 2. master_profile.yaml is the single source of truth
All work, projects, bullets, summaries, skills, education, and certifications live in `master_profile.yaml` (gitignored). The system reads it, never writes to it. The user edits it manually.

### 3. No LinkedIn — ever
LinkedIn scraping or auto-apply is forbidden. The user's main account uses LinkedIn and ban risk is unacceptable. Don't add LinkedIn code paths even "just in case."

### 4. No fabrication anywhere
LLM-generated content must reference only what exists in master profile text. Post-generation validation checks tech names and numbers against master profile. Regenerate up to 2x; if still failing, raise BUILD_FAILURE.

### 5. Job titles come only from safe_title_aliases
Each experience has a `safe_title_aliases` allow-list. The LLM picks the best match per JD, enforced via `Literal[tuple(safe_title_aliases)]`. Physically cannot return an unlisted title.

### 6. Skills come only from skills_pool and identified gaps
Deterministic scoring picks the top-14 pool skill candidates and identifies all JD gap skills. The LLM then names 3 categories with 3-5 skills each (drawn from the candidates) and picks up to 4 Familiar With gap skills. Total displayed: 10-14 pool skills + 0-4 gap skills. Distribution is flexible — the LLM optimizes for clean semantic grouping, not for hitting fixed counts. Familiar With is treated as a 4th category and ordered against the others by aggregate match score (NOT pinned to first position). Post-validation enforces source-set membership and regenerates on violation.

### 7. Built resumes are diff-validated
After assembly, a diff check confirms only permitted regions changed. Any unexpected change → BUILD_FAILURE.

### 8. Header is never modified
The DOCX assembler MUST NOT touch any paragraph before the first "WORK EXPERIENCE" Heading1. The header contains embedded hyperlinks (GitHub, LinkedIn, Certificates) that must survive every build untouched.

### 9. Hyperlink integrity on cloned blocks
Project and certificate hyperlinks MUST be updated by modifying the relationship file (`word/_rels/document.xml.rels`) to point to URLs from master_profile. Visible link text ("Code →", "Verify Here") MUST remain unchanged.

### 10. No double submissions
On submission failure, retry exactly once. Never twice. Duplicate applications are worse than a missed one.

### 11. Free tier only — including AWS
Every service must be on a free tier with no paid graduation. Validated: Neon PostgreSQL (3GB), Oracle Cloud Always Free VM (200GB), Gemini 2.0 Flash (1500 calls/day), Telegram Bot API, Google Sheets/Docs API, AWS S3 (5GB free tier), AWS CloudWatch (5GB ingest/month free), AWS IAM (free), AWS SQS (1M requests/month free).

Explicitly forbidden AWS services (cost money): RDS, Lambda for runtime, ECS, Fargate, EKS, EventBridge as primary scheduler, SNS.

### 12. Gemini call budget — 3 calls max per job
Call 1a (parse, always), Call 1b (title + skills + cover letter, if score passes AND picked for application), Call 2 (batched form questions, if needed). Don't add a 4th. New LLM needs get bundled into an existing call.

Queued jobs do NOT trigger Call 1b until they're picked.

### 13. Cycle-aware application picking
At end of each run: pick top N from {new eligible jobs + active queue}, sort by `final_score`. N=3 during 8-11am IST, N=1 otherwise. Unpicked eligible jobs go to `application_queue` with 12-hour expiry.

### 14. Queue decay
`application_queue` entries expire 12 hours after `queued_at`. Expired entries move to `not_applied` with reason `STALE`.

### 15. Interview integrity is non-negotiable
The user must be able to defend every word on every submitted resume. Any feature that changes something the user can't speak to in an interview is rejected, even if it improves match scores.

### 16. Every submitted resume is kept forever in S3
Resumes upload to `s3://{bucket}/resumes/applied/{job_id}_{timestamp}.{ext}` with versioning enabled. NEVER auto-deleted. The `applied` table stores `resume_s3_uri` + a full `selection_json` snapshot. The audit trail must always resolve via presigned URLs.

### 17. Bullets are never hard-deleted
When a bullet is removed from `master_profile.yaml`, the `master_bullets` row is marked `is_active=false`. Same for `master_summaries` and `master_title_aliases`.

### 18. AWS credentials never in code
AWS access keys MUST live in `.env` (gitignored). Use `boto3.Session()` reading from environment. Never hardcode keys, never commit `.env`. Rotate quarterly.

### 19. AWS IAM minimal permissions
The runtime IAM user/role MUST have only: `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on the bot's bucket, `logs:CreateLogStream`, `logs:PutLogEvents`, `cloudwatch:PutMetricData` on the bot's namespace. NO bucket-level operations. NO other AWS services.

### 20. CHANGELOG.md must reflect every code-affecting change
The repo has `CHANGELOG.md` at the root. Every code, schema, config, or spec change MUST be reflected in `[Unreleased]` before the user pushes. See "Changelog discipline" section below.

---

## Architecture quick reference

```
LAYER 1   Scheduler              Oracle Cloud cron (iter 5+), local (1-4)
LAYER 2   Scraper                JobSpy (Indeed iter 1+, Glassdoor iter 4)
LAYER 3   JD Parser              Gemini Call 1a — ALWAYS runs
LAYER 4   Scoring Engine         Master profile selection + cycle picking
LAYER 5   Resume Builder         Gemini Call 1b + S3 upload (iter 2+)
LAYER 6   Application Sender     Gemini Call 2 + S3 download for upload
LAYER 7   State                  Neon (Postgres+pgvector) + AWS S3 (iter 2+)
LAYER 8   Notifications          Telegram + CloudWatch alarms (iter 3+)
LAYER 9   Analytics              Sheets (presigned URLs) + Docs
```

### Selection rules — locked

```
EXPERIENCE
  score      = best_alias_score × 0.30 + top3_bullet_avg × 0.70
  max 3, min 2, threshold 0.45
  force-include strongest 2 if fewer pass
  bullets: exactly 3 per experience, top by score
  order: best-match at position 1 if match gap > 0.20, else recency

PROJECT
  score      = name_score × 0.20 + topN_bullet_avg × 0.80
  N = bullets actually displayed (2 or 3)
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
  Flexibility: 10-14 pool skills total, 0-4 gap skills

SECTION ORDER
  Summary (fixed) → Work Experience (fixed) →
  [Skills / Projects ordered by match] → Education (fixed) →
  Certifications (fixed, last)

FINAL SCORE
  fit × 0.55 + success_prob × 0.30 + recency × 0.10 + project × 0.05
  success_prob = seniority × 0.60 + recency × 0.40
  Seniority: junior→1.0, mid→0.80, senior→0.40, lead→0.15
  apply threshold: final_score >= 0.50
```

### Application picking — cycle-aware

```
Each run, after scoring:
  N = 3 if 8am <= now < 11am IST else 1
  Combine new eligible (>= 0.50) with active queue
  Sort by final_score, descending
  Pick top N → Layer 5
  Unpicked → application_queue (expires_at = now + 12h)
```

### Personal config locked

```
User:                  Vishnujan Narayanan
Experience:            1.5 years
Years required ceiling: 5
Job type:              Fulltime only
Location:              All allowed except Delhi NCR (Delhi, Gurgaon,
                       Gurugram, Noida, Ghaziabad, Faridabad)
Visa:                  No filter (open to international)
Expected salary:       JD upper bound if specified, else 6 LPA
Current salary:        100000 numeric / strategic redirect text
Upload filename:       Vishnujan_Narayanan_Resume.pdf
Cover filename:        Vishnujan_Narayanan_Cover_Letter.pdf
Familiar With max:     4 adjacent-domain skills
Unknown question mode: Safe (hold for review)
Company blocklist:     None
Company cooldown:      10 days
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
Logging           structlog → watchtower → CloudWatch (iter 3+)
AWS SDK           boto3
File storage      AWS S3 (iter 2+)
Region            ap-south-1 (Mumbai)
```

### Why Instructor + Pydantic everywhere

LLM outputs are unreliable without structure enforcement. Every Gemini call returns a Pydantic model. `max_length`, `Literal` types, and `Field` validators run at the API level — schema violations are physically impossible. Never parse JSON from LLM strings manually; use Instructor.

---

## AWS conventions

### Region

All AWS resources MUST live in `ap-south-1` (Mumbai). Don't create resources in other regions. Set `AWS_REGION=ap-south-1` in `.env` and let boto3 inherit from environment.

### Credentials

```python
# Good — boto3 reads from environment
import boto3
session = boto3.Session()  # uses AWS_ACCESS_KEY_ID / SECRET / REGION
s3 = session.client('s3')

# Bad — hardcoded keys
s3 = boto3.client('s3', aws_access_key_id='AKIA...', aws_secret_access_key='...')
```

### S3 operations

```python
# Upload — used by Layer 5 after PDF generation
def upload_resume(local_path: Path, job_id: str, ext: str) -> str:
    key = f"resumes/applied/{job_id}_{timestamp()}.{ext}"
    s3.upload_file(local_path, settings.aws.s3_bucket, key)
    return f"s3://{settings.aws.s3_bucket}/{key}"

# Download — used by Layer 6 before form upload
def download_for_submission(s3_uri: str, target: Path):
    key = s3_uri.replace(f"s3://{settings.aws.s3_bucket}/", "")
    s3.download_file(settings.aws.s3_bucket, key, target)

# Presigned URL — used by Layer 8 (Telegram) and Layer 9 (Sheets)
def generate_presigned_url(s3_uri: str) -> str:
    key = s3_uri.replace(f"s3://{settings.aws.s3_bucket}/", "")
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': settings.aws.s3_bucket, 'Key': key},
        ExpiresIn=settings.aws.presigned_url_expiry_seconds,
    )
```

### CloudWatch logging

Use `watchtower` as a structlog handler. Configure once at startup:

```python
import watchtower
handler = watchtower.CloudWatchLogHandler(
    log_group=settings.aws.cloudwatch_log_group,
    stream_name=f"jobbot-{date.today().isoformat()}",
)
```

Don't put a CloudWatch call inside the hot path — let watchtower batch.

### CloudWatch alarms

Configured via Terraform or AWS Console (one-time setup, not in code). Code's job: emit the right structured logs and metrics. Example metric filter:

```
Filter:  { $.event = "application_submitted" && $.outcome = "APPLY_FAILURE" }
Metric:  ApplyFailureCount
Alarm:   sum >= 3 over 24h → SNS topic → Lambda (ping Telegram)
```

The only Lambda we use is a tiny "ping-telegram-on-alarm" function (free tier covers this comfortably; 1M invocations/month free, we'd use < 100).

---

## Code conventions

### Modularity

Each layer is its own module with a clear input/output contract. Layer 4's selection algorithm is pure functions with no I/O. Configuration values live in `config/config.yaml`, never hardcoded.

### Imports

```python
# Standard lib
import asyncio
from datetime import datetime
from typing import Literal

# Third-party
from sqlalchemy import select
from pydantic import BaseModel, Field
import boto3

# Local
from src.state.models import AllJobs, Applied
from src.llm.client import gemini_client
from src.aws.s3 import upload_resume, generate_presigned_url
```

### Async vs sync

- Playwright code: async
- Database access: async with SQLAlchemy 2.0 async session
- JobSpy: sync (asyncio.to_thread if needed)
- Gemini calls: sync (Instructor is sync)
- sentence-transformers: sync (local)
- boto3: sync (boto3 is sync; for high-volume use aioboto3 — we don't need it)

### Error handling

```python
# Good
try:
    result = await submit_application(page, job)
except PlaywrightTimeoutError as e:
    log.error("submission_timeout", job_id=job.id, error=str(e))
    await state.mark_failed(job.id, reason="APPLY_FAILURE", detail=f"timeout: {e}")

# Bad — bare except
try:
    result = await submit_application(page, job)
except:
    pass
```

### Configuration

`config/config.yaml` is the source of truth. `.env` is ONLY for secrets (API keys, DATABASE_URL, AWS keys).

```python
# Good
from src.config import settings
threshold = settings.scoring.experience.threshold
bucket = settings.aws.s3_bucket

# Bad
threshold = 0.45
bucket = "job-bot-vishnujan-resumes"
```

---

## DOCX assembler (Layer 5)

Uses **structural detection** — no markers or template tags. Reads the template's Heading1 structure to identify sections, then clones the first sub-block within each variable section to use as a pattern.

**Preservation guarantees:**

- Header (before first Heading1): NEVER touched — preserves embedded hyperlinks
- Paragraph properties (tab stops, spacing, indentation, alignment): inherited via XML deep clone
- Font, size, color, bold, italic: preserved per run
- Native bullet list references (`<w:numPr>` with `numId`): preserved
- Project hyperlinks: `r:id` target in relationships file updated to `project.link`, visible "Code →" text unchanged
- Certificate hyperlinks: `r:id` target updated to `cert.verify_link`, visible "Verify Here" text unchanged

After assembly, upload both DOCX and PDF to S3.

---

## The master profile rebuild

`src/state/master_profile.py` owns the rebuild logic:

```
On every bot run, check master_profile.yaml mtime against master_meta.
If changed:
  1. Validate YAML schema (Telegram alert if invalid)
  2. Generate master_profile.json
  3. Diff against DB:
     - new bullets       → embed + insert (is_active=true)
     - changed bullets   → re-embed + update
     - removed bullets   → set is_active=false
     - same for summaries, title aliases
  4. Update master_meta timestamps
```

Never hard-delete. Deactivate only.

---

## Logging

Use structlog. From Iteration 3+, structlog ships to CloudWatch via watchtower.

```python
log.info("gemini_call_1a", job_id=job.id, tokens=850, latency_ms=1200)
log.info("resume_built", job_id=job.id, experiences=3, projects=2)
log.info("resume_uploaded_to_s3", job_id=job.id, s3_uri=uri, size_kb=size)
log.info("application_submitted", job_id=job.id, portal="indeed", score=0.81)
log.error("APPLY_FAILURE", job_id=job.id, portal="indeed", reason="upload_failed")
```

Critical events to log (CloudWatch metric filters depend on these names):
- `APPLY_FAILURE` — submission failed
- `BUILD_FAILURE` — resume build failed
- `MANUAL_REQUIRED` — needs user intervention
- `session_expired` — portal session dead
- `s3_upload_failed` — S3 connectivity issue
- `gemini_failure` — LLM call failed

---

## Testing strategy

### Unit tests
- Mock LLM client — never hit Gemini in tests
- Mock database with in-memory SQLite or pytest fixture
- Mock Playwright — don't launch browsers
- Mock boto3 with `moto` library (free, popular AWS mocking)
- Layer 4 selection algorithm is pure Python — test thoroughly with synthetic master profiles and JDs

### Integration tests
- Real database (Neon test branch — free)
- Real Gemini calls used sparingly
- Real S3 against test bucket (`s3://job-bot-test-{user}/`)
- Skip Playwright in CI; test manually in dev

### Dry-run mode

The orchestrator MUST support `--dry-run`: scrape, parse, score, build, upload to S3, but never submit. Used Iteration 2-3 to verify selections before enabling auto-apply.

### CLI debug commands

```bash
python -m src.cli.inspect --job-id=XYZ        # show pipeline state per job
python -m src.cli.dryrun                       # full pipeline without submission
python -m src.cli.reparse                      # rebuild master profile from YAML
python -m src.cli.queue                        # inspect application_queue
python -m src.cli.aws_check                    # verify S3+IAM+CloudWatch connectivity
```

---

## Changelog discipline

The repo has `CHANGELOG.md` at the root. Every code-affecting change MUST be recorded there before the user pushes to git.

### File format

Keep a Changelog convention. Initialize at Iteration 0:

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
  .env.example, smoke test, README, this changelog.
```

Use only `Added`, `Changed`, `Fixed`, `Removed`. Drop sections with no entries. Do NOT introduce other categories.

### What goes in

INCLUDE:
- New layers, modules, files
- Modifications to logic (selection rules, thresholds, formulas)
- Schema migrations
- Config changes
- New dependencies
- Bug fixes that change observable behavior
- Removals
- Changes to PRD, architecture, or this file
- AWS resource changes (new buckets, IAM policies, alarms)

DO NOT include:
- Whitespace, formatting, comment-only changes
- Local test runs or debug logs
- Files that are gitignored
- Refactors with no behavior change

### Writing style

One line, present tense, user perspective. Reference layer if relevant.

Good:
```
- Layer 5: resumes now upload to S3 instead of local filesystem
- Layer 4: scoring formula uses top-3 bullet average
- Config: added aws section with bucket, log group, region
- Schema: added application_queue table with 12-hour expiry
- Fixed double-submission bug on network timeout
- Removed: local resumes/applied/ filesystem references
```

Bad:
```
- updated code         (vague)
- fixed bug            (which?)
- refactored Layer 5   (refactor isn't behavior)
- improved performance (how?)
```

### When entries get added

EVERY time you make a code-affecting change, append an entry to the appropriate `[Unreleased]` subsection in the same edit session. Don't batch.

### The git push protocol

When the user says "I'm going to push to git" (or "pushing", "ready to push"):

1. Stop and review `[Unreleased]`. If empty, the changelog is up to date — confirm with the user.
2. Convert `[Unreleased]` into a dated iteration entry. Pick the iteration number based on stage (Iteration 0, 1, 2). Small fixes during an iteration: `Iteration 2.1`.
3. Date with today's date in `YYYY-MM-DD`.
4. Re-create empty `[Unreleased]` at top with all four subsections.
5. Show the dated entry before they push for review.
6. Do not auto-commit or auto-push. The user runs git commands.

Example transformation when user says "I'm pushing":

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

### When you forget

Stop current task. Add missing entries. Then resume. The discipline is brittle if entries get skipped.

### Iteration boundaries

When you complete an iteration, the closing entry marks it clearly:

```markdown
## [Iteration 1] — 2026-05-30

### Added
- End-to-end skeleton: all 9 layers wired with stub implementations
- Telegram dry-run notification confirmed working
- Iteration 1 acceptance criteria met (see architecture doc Section 9)
```

---

## When in doubt

1. Architecture conflict? Architecture doc wins.
2. Free vs paid? Free wins, every time.
3. Interview safety vs match score? Interview safety wins.
4. More LLM calls vs simpler code? Simpler code wins.
5. More features vs reliability? Reliability wins.
6. Convention vs cleverness? Convention wins.
7. LinkedIn vs anything else? Anything else wins.
8. AWS feature that costs money vs alternative? Alternative wins.

---

## Things explicitly NOT to do

- Don't write bullet-authoring or bullet-rewriting logic.
- Don't let the LLM select bullets — sentence-transformers scoring does that.
- Don't add LinkedIn code paths.
- Don't add `JDParsed` fields that aren't used by Layer 4 or 5.
- Don't store credentials anywhere except `.env` (gitignored).
- Don't add a web dashboard. Iteration 7 may add an MCP server.
- Don't cache resumes or cover letters. Every build is fresh.
- Don't hard-delete rows from master_bullets / master_summaries / master_title_aliases.
- Don't write to master_profile.yaml from code.
- Don't bypass the diff check on built resumes.
- Don't add retry loops > 1 attempt on submission.
- Don't apply to the same company within 10 days (cooldown is automatic).
- Don't hide the projects section — always shows 2-3.
- Don't let the LLM return a job title outside safe_title_aliases.
- Don't let the LLM include a skill not in skills_pool or identified gaps.
- Don't touch the resume header.
- Don't apply to more than the cycle quota (3 peak / 1 off-peak).
- Don't introduce paid services. Period.
- Don't use RDS, Lambda for runtime, ECS, Fargate, EKS, EventBridge as scheduler.
- Don't store AWS keys in code, in commits, or anywhere except `.env`.
- Don't grant AWS IAM permissions beyond what Section 5.4 of the architecture doc specifies.
- Don't create AWS resources outside `ap-south-1`.
- Don't keep local filesystem copies of resumes after S3 upload (use /tmp transient only).
- Don't skip changelog updates. Every code-affecting change goes into `[Unreleased]` in the same session.
- Don't auto-commit or auto-push to git. The user does that manually.

---

## Onboarding checklist

For an AI working on this code:

- [ ] Read `PRD.md`
- [ ] Read `job_automation_architecture.md`
- [ ] Read this file
- [ ] Skim `config/config.yaml` for runtime config
- [ ] Skim `src/llm/schemas.py` for LLM contracts (after Iteration 2 builds it)
- [ ] Check `requirements.txt` — don't suggest other libraries without need

For a human contributor:

- [ ] All of the above, plus:
- [ ] Set up Neon free tier, DATABASE_URL into `.env`
- [ ] Set up Gemini API key into `.env`
- [ ] Set up Telegram bot via @BotFather, token + chat_id into `.env`
- [ ] Set up AWS account in `ap-south-1`:
      - Create IAM user `job-bot-runtime` with minimal policy
      - Create S3 bucket `job-bot-{username}-resumes` (versioned, block public)
      - Generate access key + secret, save to `.env`
      - Configure billing alarm at $1
- [ ] Write `master_profile.yaml` with bullet pools, safe_title_aliases, skills_pool
- [ ] Place resume template in `resumes/templates/`
- [ ] Run `python -m src.cli.aws_check` to verify connectivity
- [ ] Run `python -m src.cli.reparse`
- [ ] Run in `--dry-run` for 1 week before enabling auto-apply

---

## Definition of done for any code change

- Implements the requirement from PRD or architecture
- Violates no hard rule above
- Has unit tests (mock external services including boto3 via moto)
- Logs structured events for observability
- Handles failures explicitly (no bare except)
- Introduces no new paid dependencies
- Respects the 3-call Gemini budget
- Doesn't break existing tests
- Updates this file or the architecture doc if it changes any locked decision
- **CHANGELOG.md `[Unreleased]` section updated with an entry describing the change**

---

**End of CLAUDE.md.**
