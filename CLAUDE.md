# CLAUDE.md — Instructions for AI Assistants Working on This Project

Canonical context for any AI assistant on this codebase. Read this FIRST.

---

## ⚠ Migration Note — the design pivoted after Iteration 1

You (or a previous agent) built Iterations 0-1 against an EARLIER design that **auto-applied to jobs via Playwright**. That approach is gone. Read this before doing anything in Iteration 2.

```
OLD (Iterations 0-1 built stubs for this)      NEW (build toward this)
─────────────────────────────────────────      ──────────────────────────────
auto-apply via Playwright                  →    user applies manually; bot
                                                hands them a tailored resume
store rendered PDFs in S3                   →    store ~2 KB selection_json in
                                                Postgres; render PDF/DOCX on
                                                demand via an endpoint
no LinkedIn (account-ban risk)              →    LinkedIn back as a LISTINGS
                                                source (JobSpy reads public
                                                listings; never logs into or
                                                acts on the user's account —
                                                so no account-ban risk)
cycle quotas (3 peak / 1 off-peak)          →    notify on EVERY match >= 0.50
4-category form questions, answer bank,     →    all removed
salary auto-fill, anti-AI form voice,
Gemini Call 2
2-3 Gemini calls/job                        →    max 2 calls/job
```

### Cleanup to do FIRST in Iteration 2 (remove obsolete Iteration-1 stubs)

```
DELETE:
  - src/sender/ entirely (Playwright, fields, questions, bank,
    cover_letter form logic, voice-for-form-answers)
  - Gemini Call 2 stub + references
  - cycle-quota / top-N picking in the scorer

DROP (write a migration to remove if already created):
  - answer_bank table
  - pending_review table
  - application_queue table

KEEP / ADAPT:
  - Layers 1-4 stubs → make real (add LinkedIn to scraper sources)
  - Layer 5 → output selection_json, NOT a stored PDF
  - Layer 7 → drop the 3 tables above; add render_cache table;
    add jd_embedding + near_duplicate_of columns to all_jobs
  - Layer 8 → bundle apply-link + resume-link in the notification
  - Layer 9 → resume links point to the new endpoint

ADD (new in the pivot):
  - src/endpoint/ — FastAPI app serving /resume/{job_id}.pdf|.docx
    (this is where the DOCX assembler + PDF conversion now live)
  - src/scraper/dedup.py — near-duplicate JD detection (cosine > 0.95)
  - render_cache (S3-backed, 1-month TTL)
```

Update `CHANGELOG.md [Unreleased]` with all removals and additions as you make them.

The rest of this file describes the **target state**. Don't dwell on the history — build toward target.

---

## What this project is

A personal job-application assistant for **Vishnujan Narayanan**. It scrapes Indian job listings (Indeed, Glassdoor, LinkedIn), scores them, builds a tailored resume (as a compact selection) per match, and notifies the user with apply + resume links bundled. The user applies manually. Resumes render on demand from a FastAPI endpoint. $0/month.

**The core value is the JD → tailored-resume engine.**

Read in order: `PRD.md`, `job_automation_architecture.md`, this file.

**Build status:** Iterations 0-1 done (old design). Iteration 2 = pivot cleanup + real data flow.

---

## Hard rules — never violate these

### 1. The LLM never writes or selects bullet content
Bullets come verbatim from `master_profile.yaml`. The LLM only: picks a title alias from an allow-list, names 3 skill categories and assigns skills from pre-scored candidates, picks Familiar With gaps, writes cover letter text. Bullet selection is sentence-transformers math.

### 2. master_profile.yaml is the single source of truth
Read it, never write it. The user edits it.

### 3. Never auto-apply or act on the user's accounts
The system does NOT submit applications, fill forms, answer screening questions, or take any automated action on any portal account. It delivers a resume + apply link; the user applies. (This is the core of the pivot — do not reintroduce auto-apply.)

### 4. LinkedIn is listings-only
JobSpy may read public LinkedIn listings. It must NEVER log into or act on the user's LinkedIn account. The risk that matters (account ban) comes from automated account actions — which we don't do. Scraper-IP throttling is an acceptable, recoverable infrastructure risk.

### 5. No fabrication
LLM content references only what's in master profile text. Post-gen validation checks tech + numbers; regenerate up to 2x; else BUILD_FAILURE.

### 6. Job titles only from safe_title_aliases
Enforced via `Literal[tuple(safe_title_aliases)]`.

### 7. Skills only from skills_pool (and gaps for Familiar With)
Deterministic scoring picks top-14 pool candidates. LLM names 3 categories with 3-5 skills each from candidates + up to 4 Familiar With gap skills. Total 10-14 pool + 0-4 gaps. Flexible distribution, optimized for grouping. Familiar With ordered against the other 3 by aggregate match (NOT pinned first). Post-validation enforces source-set membership.

### 8. Resumes are rendered on demand, not stored as PDFs at build time
Layer 5 writes `selection_json` (~2 KB). The endpoint renders PDF/DOCX when the user clicks. Don't store rendered PDFs at build time. Don't build a permanent PDF pile.

### 9. Diff-validate every render
On render, a diff check confirms only permitted regions changed. Any unexpected change → BUILD_FAILURE.

### 10. Header is never modified
The assembler must not touch any paragraph before the first "WORK EXPERIENCE" Heading1 (preserves embedded GitHub/LinkedIn/Certificates hyperlinks).

### 11. Hyperlink integrity on cloned blocks
Update project ("Code →") and certificate ("Verify Here") hyperlink targets via `r:id` in `word/_rels/document.xml.rels`; visible text unchanged.

### 12. Free tier only — including AWS
Neon (3GB), Oracle Cloud Always Free VM (200GB), Gemini 2.0 Flash (1500/day), Telegram, Google Sheets/Docs, AWS S3 (5GB), CloudWatch (5GB/month), IAM, SQS (1M/month). Forbidden (cost money): RDS, Lambda for runtime, ECS, Fargate, EKS, EventBridge as scheduler, SNS. (One tiny ping-Telegram Lambda for CloudWatch alarms is the sole acceptable Lambda use.)

### 13. Gemini call budget — 2 calls max per job
Call 1a (parse, always), Call 1b (title + skills + cover letter, if score >= 0.50). No Call 2 (form questions are gone). Don't add a third.

### 14. Notify on every match — no quotas
Every job >= 0.50 builds a selection and triggers a notification. No top-N picking, no application_queue, no decay (those rationed auto-applications, which are gone).

### 15. Near-duplicate dedup
Before notifying, if a JD's embedding is > 0.95 cosine to an already-notified job, mark NEAR_DUPLICATE, link to the original, skip. Prevents double-notifying cross-portal reposts.

### 16. Interview integrity is non-negotiable
The user must defend every word on every resume. Reject anything that changes what they can't speak to, even if it raises match scores.

### 17. Selections are permanent; bullets never hard-deleted
`applied.selection_json` is the durable per-job artifact. Removing a bullet from the YAML sets `master_bullets.is_active=false` (same for summaries, title aliases) — never a hard delete, so old selections always resolve.

### 18. AWS credentials never in code
AWS keys in `.env` only (gitignored). `boto3.Session()` reads from env. Rotate quarterly.

### 19. AWS IAM minimal permissions
Only `s3:PutObject/GetObject/DeleteObject` on the bot's bucket and `logs:CreateLogStream/PutLogEvents` + `cloudwatch:PutMetricData` on the bot's namespace. No bucket-level ops, no other services.

### 20. CHANGELOG.md reflects every code-affecting change
Append to `[Unreleased]` in the same session as any change. See Changelog discipline below.

### 21. Instance-ready — no hardcoded operator identity
The system runs as a single-operator tool but MUST be runnable by anyone else as an independent instance with zero code edits. No operator-specific literal (name, email, filename, region) may appear in source — all of it comes from `config.yaml`, `.env`, `master_profile.yaml`, `resumes/templates/`. Resume/cover filenames are derived from `operator.full_name` in config, never hardcoded. This is NOT multi-tenant SaaS: do NOT add `user_id` columns, authentication, accounts, or tenant isolation. One instance = one operator. Keep code modular so a future SaaS is possible, but don't build it.

---

## Architecture quick reference

```
LAYER 1   Scheduler              cron (Oracle iter 5+, local 1-4)
LAYER 2   Scraper                JobSpy: Indeed, Glassdoor, LinkedIn (listings)
                                 + near-duplicate detection
LAYER 3   JD Parser              Gemini Call 1a — ALWAYS
LAYER 4   Scoring Engine         selection; notify every match >= 0.50
LAYER 5   Resume Builder         Gemini Call 1b → selection_json (no PDF)
LAYER 6   Application Assist     notification + FastAPI resume endpoint
LAYER 7   State                  Neon (Postgres+pgvector) + S3 cache/backup
LAYER 8   Notifications          Telegram (apply+resume links) + CloudWatch
LAYER 9   Analytics              Sheets index + monthly Docs
```

### Selection rules — locked

```
EXPERIENCE  score = alias × 0.30 + top3_bullet_avg × 0.70
            max 3, min 2, threshold 0.45, force-include 2
            bullets: exactly 3, top by score
            order: best-match at #1 if gap > 0.20 else recency

PROJECT     score = name × 0.20 + topN_bullet_avg × 0.80 (N = shown)
            max 3, min 2, threshold 0.50, force-include 2, NEVER hidden
            bullets: min 2, max 3, descending

SUMMARY     deterministic pool selection by JD match — no LLM

SKILLS      top-14 pool candidates → LLM names 3 categories (3-5 each) +
            up to 4 Familiar With gaps → order skills within category →
            order all 4 categories by aggregate match (Familiar With NOT pinned)
            10-14 pool + 0-4 gaps, flexible

SECTION     Summary → Work → [Skills/Projects by match] → Education → Certs

FINAL       fit × 0.55 + success_prob × 0.30 + recency × 0.10 + project × 0.05
            success_prob = seniority × 0.60 + recency × 0.40
            seniority: junior 1.0, mid 0.80, senior 0.40, lead 0.15
            match threshold: >= 0.50 → build + notify
```

### Operator config — all from config.yaml/.env/master_profile (instance-ready)

```
Operator: (config.operator.full_name) | Experience: (config) | Fulltime only
Years ceiling: 5 | Disallowed: Delhi NCR | Visa: no filter      (all config)
Expected salary: JD upper bound else default (config) — informational only
Familiar With max: 4 | Company cooldown: 10 days               (all config)
Sources: Indeed, Glassdoor, LinkedIn (listings only) | Naukri: Iteration 6

Current operator values are Vishnujan's, but they live in config, NOT in
source. No operator name/email/filename literal in src/. Resume filename
derived: "{operator.full_name→underscores}_Resume.pdf".
```

---

## Tech stack

```
Python 3.11+ · JobSpy (listings) · spaCy · sentence-transformers (all-MiniLM-L6-v2)
Gemini 2.0 Flash via Instructor + Pydantic · PostgreSQL on Neon + pgvector
psycopg3 · SQLAlchemy 2.0 · python-docx · LibreOffice headless · FastAPI (endpoint)
boto3 · AWS S3 (cache+backup) · structlog → watchtower → CloudWatch
Telegram Bot API · gspread + Google Docs API · Region ap-south-1
NO Playwright (removed in pivot)
```

Every Gemini call returns a Pydantic model via Instructor. Never parse JSON from LLM strings manually.

---

## The resume endpoint (Layer 6) — new center of gravity

```
FastAPI app on the Oracle VM.
GET /resume/{job_id}.pdf
GET /resume/{job_id}.docx

  1. Check render_cache for {job_id}_{current_template_version}_{ext}
  2. Hit (within 1-month TTL, template matches) → serve from S3
  3. Miss:
       load selection_json from applied
       load current template
       assemble DOCX (structural detection, Section 6.3 of arch doc)
       if pdf: LibreOffice convert
       upload to S3 cache (1-month expiry), record in render_cache
       serve
  4. If stored template_version != current → re-render against current

~5s cold, instant cached. ~100 lines. Foundation for the optional
Iteration 7 dashboard.
```

The DOCX assembler lives in `src/endpoint/assembler.py` now (was in the old builder). Header never touched; hyperlinks updated by r:id; tab stops/spacing/fonts/list-refs preserved via XML deep clone.

---

## AWS conventions

Region: `ap-south-1` only. Credentials via `boto3.Session()` from env — never hardcoded.

```python
# S3 cache (endpoint)
def cache_put(local_path, cache_key, ext):
    s3.upload_file(local_path, settings.aws.s3_bucket, f"{ext}_cache/{cache_key}.{ext}")

def cache_get(cache_key, ext) -> str | None:
    # return s3 uri if exists and not expired (check render_cache table)

# selection backup (daily)
def backup_selections(date_str): ...   # export applied.selection_json to s3://.../backups/

# CloudWatch via watchtower handler at startup (iter 3+)
```

CloudWatch alarms (configured once via console/Terraform, not code): metric filters on `BUILD_FAILURE`, endpoint 5xx, scraper zero-results → tiny ping-Telegram Lambda.

---

## Code conventions

Modularity: each layer its own module; Layer 4 selection is pure functions; all tunables in `config/config.yaml`, never hardcoded.

Async: FastAPI + Playwright-free endpoint async; DB async (SQLAlchemy 2.0); JobSpy sync (to_thread if needed); Gemini sync; sentence-transformers sync; boto3 sync.

Error handling: specific exceptions, structured logging, no bare except.

Config: `config.yaml` for tunables, `.env` for secrets (API keys, DB URL, AWS keys).

```python
from src.config import settings
threshold = settings.scoring.experience.threshold
bucket = settings.aws.s3_bucket
```

Pydantic schemas centralized in `src/llm/schemas.py`.

---

## The master profile rebuild

`src/state/master_profile.py`: on mtime change → validate → JSON → diff against DB (embed/upsert new+changed, deactivate removed) → update master_meta. Never hard-delete; deactivate only.

---

## Logging

structlog; ships to CloudWatch via watchtower from Iteration 3+. Critical event names (CloudWatch metric filters depend on them):

```
BUILD_FAILURE        resume selection build failed
render_failed        endpoint failed to render
near_duplicate       JD deduped against an existing job
gemini_failure       LLM call failed
s3_cache_failed      cache write failed
master_profile_validation_failure
```

```python
log.info("match_found", job_id=j, score=0.78, source="linkedin")
log.info("selection_built", job_id=j, experiences=3, projects=2)
log.info("notification_sent", job_id=j)
log.info("resume_rendered", job_id=j, fmt="pdf", cache="miss", ms=4800)
```

---

## Testing strategy

Unit: mock LLM client (never hit Gemini), mock DB (in-memory SQLite or fixture), mock boto3 with `moto`, FastAPI via `TestClient`. Layer 4 selection is pure Python — test thoroughly with synthetic profiles + JDs.

Integration: Neon test branch, sparing real Gemini, real S3 test bucket.

Dry-run: orchestrator `--dry-run` scrapes/parses/scores/builds selections + sends notifications to a test chat, but the endpoint and links are real. Use for several days before trusting selections.

CLI:
```bash
python -m src.cli.inspect --job-id=XYZ     # pipeline state per job
python -m src.cli.dryrun                    # full pipeline, test chat
python -m src.cli.reparse                   # rebuild master profile
python -m src.cli.render --job-id=XYZ       # force re-render a resume
python -m src.cli.aws_check                 # verify S3 + IAM + CloudWatch
```

---

## Changelog discipline

`CHANGELOG.md` at root, Keep a Changelog format. Sections: `Added`, `Changed`, `Fixed`, `Removed` (drop empty ones; no others).

INCLUDE: new layers/modules/files, logic changes, schema migrations, config changes, new deps, behavior-changing fixes, removals, spec-doc changes, AWS resource changes.
EXCLUDE: whitespace/format/comments, local test runs, gitignored files, no-behavior refactors.

Style: one line, present tense, user perspective, reference the layer.

```
Good: "Layer 5: output selection_json instead of stored PDF"
      "Removed: Playwright sender and answer_bank table"
Bad:  "updated code", "fixed bug", "refactored"
```

Append to `[Unreleased]` in the same session as any code-affecting change. Don't batch.

### Git push protocol

When the user says "I'm going to push" / "pushing" / "ready to push":
1. Review `[Unreleased]`. If empty, confirm it's up to date.
2. Convert `[Unreleased]` → dated iteration entry (`Iteration N` or `Iteration N.x` for mid-iteration fixes), today's date `YYYY-MM-DD`.
3. Recreate empty `[Unreleased]` at top.
4. Show the dated entry before they push.
5. Never auto-commit or auto-push — the user runs git.

If you forget an entry: stop, add it, resume. If caught missing one: add immediately, no defensiveness.

---

## When in doubt

1. Architecture conflict? Architecture doc wins.
2. Free vs paid? Free wins.
3. Interview safety vs match score? Interview safety wins.
4. More LLM calls vs simpler code? Simpler wins.
5. More features vs reliability? Reliability wins.
6. Convention vs cleverness? Convention wins.
7. Auto-apply temptation vs manual-assist? Manual-assist wins (it's the pivot).
8. AWS feature that costs money vs alternative? Alternative wins.
9. Instance-ready config-driven vs hardcoded operator value? Config-driven wins.
10. Build SaaS now vs keep it modular for later? Modular-for-later wins (don't build SaaS).

---

## Things explicitly NOT to do

- Don't reintroduce auto-apply, form filling, or any automated account action.
- Don't add Playwright back.
- Don't log into or act on the user's LinkedIn (listings scraping only).
- Don't write/rewrite bullets; don't let the LLM select bullets.
- Don't store rendered PDFs at build time — render on demand.
- Don't build a permanent PDF pile.
- Don't add `JDParsed` fields unused by Layer 4 or 5.
- Don't store credentials outside `.env`.
- Don't bypass the diff check on renders.
- Don't hard-delete master_bullets / master_summaries / master_title_aliases.
- Don't write to master_profile.yaml from code.
- Don't apply to the same company within 10 days.
- Don't hide the projects section.
- Don't let the LLM return a title outside safe_title_aliases or a skill outside skills_pool/gaps.
- Don't touch the resume header.
- Don't add a 3rd Gemini call.
- Don't add quotas — notify every match >= 0.50.
- Don't use RDS, Lambda for runtime, ECS, Fargate, EKS, EventBridge-as-scheduler, SNS.
- Don't store AWS keys in code or commits.
- Don't grant IAM beyond Section 5.4 of the architecture doc.
- Don't create AWS resources outside ap-south-1.
- Don't skip changelog updates. Don't auto-commit/push.
- Don't hardcode operator identity (name, email, filename) in source — derive from config.
- Don't add user_id columns, auth, accounts, or tenant isolation (instance-ready, not SaaS).

---

## Onboarding checklist

AI working on this code:
- [ ] Read `PRD.md`, `job_automation_architecture.md`, this file (incl. Migration Note)
- [ ] Skim `config/config.yaml` and `src/llm/schemas.py`
- [ ] Before Iteration 2 work: do the cleanup in the Migration Note first

Human contributor:
- [ ] Above, plus:
- [ ] Neon free tier → DATABASE_URL in `.env`
- [ ] Gemini API key in `.env`
- [ ] Telegram bot (@BotFather) token + chat_id in `.env`
- [ ] AWS in ap-south-1: IAM user `job-bot-runtime` (minimal policy),
      S3 bucket `job-bot-{username}` (private, versioning on backups,
      1-month lifecycle on cache prefixes), access key in `.env`,
      billing alarm at $1
- [ ] Write `master_profile.yaml` (bullet pools, safe_title_aliases, skills_pool)
- [ ] Resume template in `resumes/templates/`
- [ ] `python -m src.cli.aws_check`, then `python -m src.cli.reparse`
- [ ] `--dry-run` for several days before trusting selections

---

## Definition of done for any change

- Implements the requirement from PRD or architecture
- Violates no hard rule
- Unit tests (mock external services incl. boto3 via moto, FastAPI via TestClient)
- Structured logging
- Explicit failure handling (no bare except)
- No new paid dependencies
- Respects the 2-call Gemini budget
- Doesn't break existing tests
- Updates this file or the architecture doc if it changes a locked decision
- **CHANGELOG.md `[Unreleased]` updated with an entry describing the change**

---

**End of CLAUDE.md.**
