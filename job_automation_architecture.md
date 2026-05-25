# Job Application Automation System — Architecture

**Owner:** Vishnujan Narayanan
**Cost:** $0/month forever
**Status:** Iteration 0 complete, Iteration 0.1 (AWS prep) pending, ready for Iteration 1

---

## 1. Goal

Fully autonomous job application bot that:

- Detects new job postings within ~10 minutes of being posted
- Scores them via a multi-factor relevance model
- Builds a custom-tailored resume per JD from a master profile pool
- Submits applications automatically where possible
- Logs every scraped job and every applied resume permanently
- Generates weekly career intelligence reports
- Runs while the user sleeps
- Uses real AWS services (S3, CloudWatch, IAM) for operational pieces
- Costs $0/month

---

## 2. Personal Configuration

```
Name                     Vishnujan Narayanan
Years of experience      1.5 years
Job type                 Fulltime only
Years required ceiling   5 years
Location policy          Allow everything except disallowed regions
Disallowed regions       Delhi NCR (Delhi, Gurgaon, Gurugram, Noida,
                         Ghaziabad, Faridabad)
Visa policy              Open to international relocation — no filter
Resume profile source    master_profile.yaml (gitignored)

Salary
  Expected default       6 LPA
  Expected per JD        JD upper bound if specified, else 6 LPA
  Current text fields    Strategic redirect (no number)
  Current numeric        100000

Upload filenames
  Resume                 Vishnujan_Narayanan_Resume.pdf
  Cover letter           Vishnujan_Narayanan_Cover_Letter.pdf

Question handling        Safe mode (hold for review on unknown)
Company blocklist        None
Familiar With            Max 4 adjacent gap skills
```

### 2.1 Target role categories

```
backend, data, ml, fullstack, devops, finance_market, quant
```

### 2.2 Search keywords (rotation order)

Stored in `config.yaml` under `scraper.search_rotation.terms` — 24 terms covering all 7 categories.

### 2.3 Role acceptance clusters

Maps each search term to acceptable role titles and Gemini categories for smart matching. Lives in `config.yaml` under `parser.role_clusters`. A search for "backend engineer" accepts jobs titled "software developer" if Gemini classifies them as backend or fullstack.

---

## 3. Master Profile Model

### 3.1 master_profile.yaml structure

Single gitignored YAML file with:
- `personal` — contact info and links
- `summaries` — pre-written summary pool (any size), each tagged with role categories
- `work_experience` — entries with `bullet_pool` and `safe_title_aliases` allow-list
- `projects` — entries with `bullet_pool` and embedded `link`
- `skills_pool` — flat list of all defensible skills
- `education` — degree entries with dates and scores
- `certifications` — entries with `verify_link`

### 3.2 Lifecycle

Bot run detects YAML mtime change. On change:

1. Validate schema → if invalid, Telegram alert, abort run
2. Generate canonical `master_profile.json`
3. Diff against DB:
   - New bullets → embed via sentence-transformers, insert (`is_active=true`)
   - Changed bullets → re-embed, update
   - Removed bullets → mark `is_active=false`, set `deactivated_at=now`
4. Update `master_meta.master_profile_processed_at`

Bullets, summaries, and title aliases are **never** hard-deleted. Deactivation only. Preserves audit trail.

### 3.3 Pre-computed embeddings

Once at rebuild — every bullet, summary, title alias, and skill gets a `vector(384)` via sentence-transformers, stored in DB.

---

## 4. Architecture — 9 Layers

```
LAYER 1   Scheduler
LAYER 2   Scraper             (Indeed, Glassdoor)
LAYER 3   JD Parser           (Gemini Call 1a)
LAYER 4   Scoring Engine
LAYER 5   Resume Builder      (Gemini Call 1b)
LAYER 6   Application Sender  (Gemini Call 2 if needed)
LAYER 7   State Management    (PostgreSQL on Neon + AWS S3 for files)
LAYER 8   Notifications       (Telegram + CloudWatch alarms)
LAYER 9   Analytics           (Sheets + Docs)
```

---

### Layer 1 — Scheduler

Oracle Cloud cron (Iteration 5 onward), local cron (Iterations 1-4).

```
Peak       Every 40 min, 8am-6pm IST, weekdays
Off-peak   Every 4 hours, nights and weekends
Sunday     8am IST → Layer 9 weekly report + storage cleanup
```

Cycle-aware application quotas:
```
8am-11am IST (peak): apply to top 3 from this run's batch
After 11am: apply to top 1 from this run's batch
```

---

### Layer 2 — Scraper

**Sources:** Indeed (Iterations 1+). Glassdoor (Iteration 4). Naukri (Iteration 6). LinkedIn never (account-ban risk).

**Serial search rotation with short-circuit:**

```
Each run picks up where last run left off
For each keyword in rotation:
  Query JobSpy with India + hours_old
  Insert every result into all_jobs (permanent archive)
  Apply hard filters
  Count passing results
  If passing count >= 20 → advance rotation, stop
  Else → next keyword
Update search_rotation_state.current_index
```

**Hard filters:**

```
years_required > 5            → reject (HARD_FILTER_LAYER_2)
location in disallowed list   → reject (HARD_FILTER_LAYER_2)
company in cooldown (10d)     → reject (COMPANY_COOLDOWN)
duplicate job_id              → reject (DUPLICATE)
```

Passed jobs → `processing_queue` with status `queued`.

---

### Layer 3 — JD Parser (Gemini Call 1a)

**Tools:** Gemini 2.0 Flash + Instructor, spaCy validator.

```python
class JDParsed(BaseModel):
    required_skills: list[str]
    nice_to_have: list[str]
    responsibilities: list[str]
    role_summary: str
    team_or_product: str | None
    years_experience: int | None
    role_level: Literal["junior", "mid", "senior", "lead"] | None
    role_category: Literal[
        "backend", "data", "ml", "fullstack",
        "devops", "finance_market", "quant", "other"
    ]
    job_type: Literal["fulltime", "internship", "contract"]
    location_type: Literal["remote", "hybrid", "onsite"]
    salary_min_lpa: float | None
    salary_max_lpa: float | None
    salary_currency: Literal["INR_LPA", "INR_monthly", "USD", "EUR", "OTHER"] | None
```

spaCy validates extracted skills exist in JD text. Hard filter re-check on structured data. Role acceptance check against clusters.

---

### Layer 4 — Scoring Engine

#### 4.1 JD embeddings

```
jd_vec_skills  = embed(required + nice_to_have)
jd_vec_resp    = embed(responsibilities + role_summary)
jd_vec_role    = embed(role_summary)
jd_vec_match   = jd_vec_skills + jd_vec_resp
```

#### 4.2 Experience scoring

```python
def score_experience(exp, jd):
    best_alias = max(cosine(embed(a), jd_vec_role) for a in exp.safe_title_aliases)
    bullet_scores = sorted([cosine(b.embedding, jd_vec_match) for b in exp.active_bullets], reverse=True)
    top3_avg = mean(bullet_scores[:3])
    return (best_alias * 0.30) + (top3_avg * 0.70)
```

Rules: threshold 0.45, max 3, min 2, force-include strongest 2. Bullets: exactly 3 per experience, top by score, descending order. Position 1 = highest-scoring IF gap > 0.20, else most recent. Other positions by recency.

#### 4.3 Project scoring

```python
def score_project(proj, jd):
    name = cosine(embed(proj.name), jd_vec_role)
    bullet_scores = sorted([cosine(b.embedding, jd_vec_match) for b in proj.active_bullets], reverse=True)
    passing = [s for s in bullet_scores if s >= 0.40]
    n = min(3, max(2, len(passing)))
    topN_avg = mean(bullet_scores[:n])
    return (name * 0.20) + (topN_avg * 0.80)
```

Score reflects displayed bullet count. Rules: threshold 0.50, max 3, min 2, force-include best 2. **Section never hidden.** Bullets: min 2 / max 3. Order: score-descending.

#### 4.4 Summary selection (deterministic)

Filter pool by JD role_category. Pick highest cosine match. No LLM.

#### 4.5 Skills selection (hybrid)

**Step 1:** Score every skill in `skills_pool` against JD.
**Step 2:** Take top-14 candidates.
**Step 3:** Identify and score gap skills (JD requires, not in pool).
**Step 4:** LLM names 3 categories, assigns 3-5 skills each from candidates, picks up to 4 gap skills for Familiar With.
**Step 5:** Within each category, order skills by JD score descending.
**Step 6:** Compute aggregate score per category INCLUDING Familiar With.
**Step 7:** Order ALL 4 categories (3 LLM-named + Familiar With) by aggregate match, descending. Familiar With NOT pinned.

Total: 10-14 pool skills + 0-4 gaps. LLM optimizes grouping, not count.

**Post-validation:**
1. Category skills must be in top-14 candidates
2. Familiar With skills must be in identified gaps
3. No duplicates across categories
4. Category names not in banned list (Miscellaneous, Other, Soft Skills, etc.)
5. Regenerate once on violation, else BUILD_FAILURE

#### 4.6 Section ordering

```
FIXED:    Summary (1) → Work Experience (2) → ... → Education → Certifications
VARIABLE: Skills and Projects between Work and Education, ordered by match score
```

#### 4.7 Final score

```python
fit_score = (
    best_experience_score * 0.50 +
    summary_score * 0.20 +
    avg_skill_pool_match * 0.30
)

success_prob = seniority_score * 0.60 + recency_score * 0.40
# Seniority: junior→1.0, mid→0.80, senior→0.40, lead→0.15

# recency_score bands: <1h→1.0, 1-3h→0.8, 3-6h→0.6, 6-12h→0.4, >12h→0.2

final_score = (
    fit_score * 0.55 +
    success_prob * 0.30 +
    recency_score * 0.10 +
    best_project_score * 0.05
)
```

Apply threshold: final_score >= 0.50.

#### 4.8 Cycle-aware picking

```
At end of Layer 4 per run:
  cycle = "peak" if 8am-11am IST else "regular"
  N = 3 if peak else 1
  Combine new eligible (>= 0.50) with active 12-hour queue
  Sort by final_score, descending
  Pick top N → Layer 5
  Unpicked → application_queue (expires after 12h → STALE)
```

---

### Layer 5 — Resume Builder (Gemini Call 1b)

#### 5.1 Single combined LLM call

```python
class ResumeBuildLLMOutput(BaseModel):
    title_choices: dict[str, str]   # Literal[tuple(safe_title_aliases)]
    skills_selection: SkillsSelection
    cover_letter_text: str = Field(max_length=900)
```

#### 5.2 DOCX assembler — structural detection

Clone-and-fill the user's template DOCX. Never rebuild paragraphs from scratch.

**Region detection:** walk by Heading1 paragraphs to identify sections.

**Header preservation:** paragraphs before WORK EXPERIENCE never touched (preserves GitHub, LinkedIn, Certificates hyperlinks).

**Build flow:**
1. Open template
2. Replace summary text
3. WORK EXPERIENCE: clone first experience block as template, fill per selected experience
4. SKILLS: clone skill row template, fill per ordered category
5. PROJECTS: same pattern; update embedded "Code →" hyperlink target via `r:id` modification in `word/_rels/document.xml.rels`
6. EDUCATION: untouched
7. CERTIFICATES: clone per certification, update "Verify Here" hyperlink target
8. Reorder Skills/Projects sections per section_order
9. Diff validate
10. Save to S3 at `s3://{bucket}/resumes/applied/{job_id}_{timestamp}.docx`
11. Convert to PDF via LibreOffice headless
12. Upload PDF to S3 at `s3://{bucket}/resumes/applied/{job_id}_{timestamp}.pdf`
13. Record in `applied` table with `resume_s3_uri`

**Preservation guarantees:** tab stops, spacing, fonts, colors, native bullet list refs (numId) — all inherited via XML deep clone. Hyperlinks updated by `r:id` target swap, visible text unchanged.

#### 5.3 PDF storage — AWS S3 (Iteration 2 onward)

```
Bucket structure:
  s3://{config.aws.s3_bucket}/
    resumes/applied/{job_id}_{timestamp}.pdf    permanent
    resumes/applied/{job_id}_{timestamp}.docx   permanent
    cover_letters/{job_id}_{timestamp}.pdf      permanent (when applicable)

Lifecycle:
  resumes/applied/*    NEVER auto-deleted (audit trail)
  Versioning enabled
  IAM role with minimal s3:PutObject + s3:GetObject permissions
  Bucket private (no public access)
  Presigned URLs generated on demand for Sheets/Telegram links
```

The `applied` table stores the S3 URI, not local paths. PDFs accessible from anywhere via presigned URLs.

---

### Layer 6 — Application Sender

#### 6.1 Decision tree

```
Tailored PDF ready in S3
  ↓ download to local /tmp for Playwright upload
Indeed Easy Apply           → proceed
Glassdoor external redirect → MANUAL_REQUIRED
CAPTCHA / unknown form      → MANUAL_REQUIRED
```

#### 6.2 Session management

First run manual: log in to Indeed, cookies saved to `data/sessions/indeed.json`. Subsequent: load cookies, skip login. Expired: immediate alert, skip portal.

#### 6.3 Multi-page flow

Discover fields per page → classify (profile/salary/upload/question/yesno) → fill non-question fields → collect unknown questions → batch via Gemini Call 2 if needed → safe mode holds for review → submit final page → log to `applied`.

Page signature loop detection. Max 10 pages.

#### 6.4 Field handling

```
Resume upload    → download from S3 to /tmp, upload, delete /tmp
                   Renamed to "Vishnujan_Narayanan_Resume.pdf"
Profile fields   → master_profile.personal
Years exp        → 1.5
Notice period    → Immediate
Work auth        → Yes

Expected salary
  text     → f"{expected_salary_lpa} LPA"
  numeric  → expected_salary_lpa * 100000
  dropdown → band containing value
  slider   → expected_salary_lpa * 100000

Current salary
  text     → strategic redirect
  numeric  → 100000
  dropdown → band containing 100000
```

#### 6.5 Cover letter

Detect field type. Textarea → fill from Call 1b text. File upload → render to PDF, upload to S3, download for form upload as `Vishnujan_Narayanan_Cover_Letter.pdf`. No field → skip.

#### 6.6 Question handling — 4 categories

```
Cat 1   Profile lookup       auto-fill from master_profile.personal
Cat 2   Resume-derived       auto-fill from master profile
Cat 3   Judgement            bank match → adapt → review
Cat 4   Legally sensitive    MANUAL_REQUIRED
```

Cat 3 two-stage: question pattern match → JD context match → auto-fill (>0.80), adapt (<0.80), or fresh draft.

#### 6.7 Human voice

Banned words (leverage, scalable, robust, holistic, etc.) and structures (three-point lists, opening with credentials) checked post-generation. Fabrication check: tech names + numbers must exist in master_profile text. Regenerate up to 2x.

---

### Layer 7 — State Management

#### 7.1 Database — PostgreSQL on Neon

3GB free, pgvector, psycopg3, SQLAlchemy 2.0 async, pool_pre_ping enabled.

#### 7.2 File storage — AWS S3 (Iteration 2 onward)

```
s3://{bucket}/resumes/applied/   permanent, versioned, IAM-restricted
s3://{bucket}/cover_letters/     permanent
```

Local filesystem used only for transient files (Playwright session cookies, screenshots in manual queue, logs being shipped to CloudWatch).

#### 7.3 Schema

```sql
-- All_jobs: permanent archive of every scraped job
CREATE TABLE all_jobs (
    job_id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    site TEXT NOT NULL,
    location TEXT,
    job_url TEXT,
    posted_at TIMESTAMPTZ,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    jd_text TEXT,
    required_skills JSONB,
    nice_to_have JSONB,
    responsibilities JSONB,
    role_summary TEXT,
    team_or_product TEXT,
    years_required INTEGER,
    role_level TEXT,
    role_category TEXT,
    job_type TEXT,
    location_type TEXT,
    salary_min_lpa REAL,
    salary_max_lpa REAL,
    salary_currency TEXT,
    outcome TEXT,
    outcome_at TIMESTAMPTZ
);
CREATE INDEX idx_all_jobs_scraped ON all_jobs(scraped_at DESC);
CREATE INDEX idx_all_jobs_outcome ON all_jobs(outcome);
CREATE INDEX idx_all_jobs_skills ON all_jobs USING GIN(required_skills);

-- Applied: jobs successfully submitted, with audit trail
CREATE TABLE applied (
    job_id TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    apply_type TEXT,
    resume_s3_uri TEXT,                -- S3 URI to PDF
    resume_s3_key TEXT,                -- bucket key for presigned URL generation
    cover_letter_text TEXT,
    cover_letter_used BOOLEAN DEFAULT FALSE,
    cover_letter_s3_uri TEXT NULL,
    selection_json JSONB,
    expected_salary_lpa REAL,
    fit_score REAL,
    success_prob REAL,
    recency_score REAL,
    final_score REAL,
    gap_skills JSONB,
    application_status TEXT,
    failure_reason TEXT,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

-- Not_applied: structured reasons for every rejected job
CREATE TABLE not_applied (
    job_id TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    reason_category TEXT NOT NULL,
    reason_detail TEXT,
    fit_score REAL,
    success_prob REAL,
    recency_score REAL,
    final_score REAL,
    gap_skills JSONB,
    in_field BOOLEAN,
    not_applied_at TIMESTAMPTZ DEFAULT NOW()
);

-- Application queue (12-hour decay)
CREATE TABLE application_queue (
    job_id TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    final_score REAL,
    status TEXT,
    queued_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);
CREATE INDEX idx_queue_status ON application_queue(status, expires_at);

-- Processing queue (transient per-run)
CREATE TABLE processing_queue (
    job_id TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    status TEXT,
    queued_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Master profile bullets (NEVER hard-deleted)
CREATE TABLE master_bullets (
    bullet_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL,
    parent_type TEXT NOT NULL,
    text TEXT NOT NULL,
    tags JSONB,
    embedding vector(384),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deactivated_at TIMESTAMPTZ NULL
);
CREATE INDEX idx_bullets_embedding ON master_bullets USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_bullets_parent ON master_bullets(parent_id, parent_type);
CREATE INDEX idx_bullets_active ON master_bullets(is_active);

CREATE TABLE master_summaries (
    summary_id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    tags JSONB,
    role_categories JSONB,
    embedding vector(384),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_summaries_embedding ON master_summaries USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE master_title_aliases (
    id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    embedding vector(384),
    is_active BOOLEAN DEFAULT TRUE
);
CREATE INDEX idx_aliases_parent ON master_title_aliases(parent_id);
CREATE INDEX idx_aliases_embedding ON master_title_aliases USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE master_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE search_rotation_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE answer_bank (
    id TEXT PRIMARY KEY,
    question_patterns JSONB,
    jd_contexts JSONB,
    answer TEXT,
    approved_by_user BOOLEAN DEFAULT FALSE,
    times_used INTEGER DEFAULT 0,
    last_used TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE pending_review (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    company TEXT,
    role TEXT,
    question_text TEXT,
    question_category INTEGER,
    bank_match_id TEXT,
    bank_match_score REAL,
    gemini_draft TEXT,
    user_answer TEXT,
    status TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ
);

CREATE TABLE portal_health (
    site TEXT PRIMARY KEY,
    last_run TIMESTAMPTZ,
    result_count INTEGER,
    consecutive_zeros INTEGER DEFAULT 0,
    last_error TEXT
);

CREATE TABLE company_cooldown (
    company TEXT PRIMARY KEY,
    last_applied_at TIMESTAMPTZ
);
```

#### 7.4 not_applied reason categories

```
HARD_FILTER_LAYER_2     Rejected at scrape on raw text
HARD_FILTER_LAYER_3     Rejected after Gemini parse on structured data
ROLE_MISMATCH           JD role doesn't match search term cluster
LOCATION_DISALLOWED     JD location in disallowed regions
LOW_SCORE               Passed filters, final_score < 0.50
STALE                   Queued but expired 12-hour decay
PARSE_FAILURE           Couldn't parse JD
BUILD_FAILURE           Layer 5 failed
APPLY_FAILURE           Submission failure
MANUAL_REQUIRED         Can't auto-apply
REJECTED_BY_USER        Marked skip during review
COMPANY_COOLDOWN        Same company applied <10 days ago
DUPLICATE               Already in all_jobs
```

#### 7.5 Failure handling

```
Network blip          → pool_pre_ping auto-recovers
Neon down             → backoff 3x → buffer to data/pending_writes.jsonl
S3 down               → keep DOCX/PDF locally in /tmp, retry upload next run
                        Record applied with placeholder S3 URI flagged for retry
Cold start            → ~500ms accepted
```

---

### Layer 8 — Notifications

**Telegram morning digest (8am IST):**
- Applied last 24h with final_score and presigned S3 URLs to resumes
- Skipped counts by category
- Manual-required jobs with apply URLs and resume PDF presigned links
- Failures, portal health, Sheets link

**Telegram review requests (immediate, Cat 3 questions):** inline approve/edit/skip.

**CloudWatch alarms (Iteration 3 onward):**
- APPLY_FAILURE rate > 3 in 24h → SNS → Telegram
- Session expired → immediate Telegram (CloudWatch logs + alarm)
- 3 consecutive zero-result scrapes → Telegram
- master_profile validation failure → Telegram

Telegram remains the primary user-facing channel. CloudWatch provides the operational backbone (alarms, metric filters on structured logs) without replacing Telegram.

---

### Layer 9 — Analytics

**Sheets (live view from Postgres):**
- Sheet 1: Applied jobs with presigned S3 URLs to PDFs
- Sheet 2: Relevant skipped jobs
- Sheet 3: Manual-required jobs

**Google Docs (monthly):** Sunday report synthesized by Gemini covering skill demand, recurring gaps (30%+ alert), companies hiring, salary ranges.

---

## 5. AWS Integration

### 5.1 Services used

```
S3              Iteration 2  → DOCX/PDF resumes, cover letters
                              versioned, IAM-restricted, presigned URLs
IAM             Iteration 2  → role for Oracle VM with minimal S3 permissions
                              Iteration 3: extend with CloudWatch logs/metrics
CloudWatch      Iteration 3  → structured log ingestion, metric filters,
                              alarms on APPLY_FAILURE rate and session expiry
SQS             Iteration 6  → decouple scraper from parser/scorer workers
                              ONLY if volume justifies (multi-portal scaling)
```

### 5.2 Services explicitly NOT used

```
RDS             Neon free tier is forever free with pgvector
Lambda          Bad fit for Playwright + Chromium
ECS / Fargate   Oracle Always Free VM cheaper and persistent
EventBridge     Cron on Oracle VM is sufficient
EKS             Massive overkill
SNS             Telegram is the user channel; CloudWatch alarms invoke
                Telegram bot directly via webhook
```

### 5.3 Cost model

```
S3              ~1GB at year 10. Free tier: 5GB. → $0 forever
CloudWatch      Logs: free tier 5GB ingest/month. Bot logs ~100MB/month. → $0
IAM             Free
SQS             Free tier 1M requests/month. We'd use <10K/month. → $0
                If volume grows past free tier, ~$0.40/M requests.
Total AWS       $0/month
```

If AWS Free Tier billing alarms ever fire, the answer is to disable the offending service, never to start paying.

### 5.4 IAM policy (minimal)

```
Role:    job-bot-oracle-vm-role
Trust:   Oracle Cloud VM (via AWS access key + secret on VM,
         rotated quarterly — or via AWS SSO if Oracle integrates)

Permissions:
  s3:PutObject, s3:GetObject, s3:DeleteObject  on the bot's bucket
  logs:CreateLogStream, logs:PutLogEvents      on the bot's log group
  cloudwatch:PutMetricData                     on the bot's namespace
  
Explicitly DENIED:
  s3:*Bucket actions (no bucket-level ops from runtime)
  iam:* (no privilege escalation)
  ec2:*, rds:*, lambda:* (not in scope)
```

### 5.5 Failure handling for AWS dependency

```
S3 transient failure   → retry 3x with exponential backoff
S3 persistent failure  → keep file locally in /tmp/pending_uploads/
                         → applied row created with s3_uri NULL and
                           flag pending_upload=true
                         → next successful run drains pending uploads
CloudWatch failure     → log locally to data/logs/, ship next run
                         (CloudWatch is informational, not blocking)
IAM credential expired → immediate Telegram alert, bot halts until fixed
                         (no S3 = no resume storage = no applications)
```

---

## 6. Gemini Call Strategy — 3 calls max per job

```
CALL 1a — JD parse (ALWAYS)                          ~800 tokens
CALL 1b — title + skills + cover letter              ~1800 tokens
          (only if final_score >= 0.50 AND picked)
CALL 2  — batched form questions                     ~2000 tokens
          (only if unknown form questions)

Per-job: 1 / 2 / 3 calls depending on path
Daily expected: ~600 calls (free tier 1500/day)
```

---

## 7. Stack

```
Language          Python 3.11+
Scraping          JobSpy (Indeed, Glassdoor)
Browser           Playwright
NLP validation    spaCy
Embeddings        sentence-transformers (all-MiniLM-L6-v2)
LLM               Gemini 2.0 Flash via Instructor + Pydantic
Database          PostgreSQL on Neon (free tier, pgvector)
DB driver         psycopg3
ORM               SQLAlchemy 2.0
Resume building   python-docx
PDF conversion    LibreOffice headless
PDF rendering     reportlab (cover letter)
File storage      AWS S3 (Iteration 2+)
Observability     structlog → CloudWatch (Iteration 3+)
Auth              AWS IAM role (Iteration 2+)
Notifications     Telegram Bot API
Reporting         gspread, Google Docs API
Hosting iter 1-4  Local machine
Hosting iter 5+   Oracle Cloud Always Free VM
AWS Region        ap-south-1 (Mumbai) for latency
Total cost        $0/month
```

---

## 8. Repo Structure

```
job-bot/
├── master_profile.yaml             (gitignored)
├── resumes/
│   └── templates/
│       ├── resume_template.docx
│       └── cover_template.docx
├── data/
│   ├── sessions/                   (Playwright cookies, gitignored)
│   ├── manual_queue/               (screenshots, gitignored)
│   ├── logs/                       (gitignored, shipped to CloudWatch)
│   ├── pending_uploads/            (S3 upload buffer, gitignored)
│   └── pending_writes.jsonl        (DB outage buffer, gitignored)
├── config/
│   └── config.yaml                 (includes parser.role_clusters, aws.*)
├── src/
│   ├── scheduler.py
│   ├── scraper/
│   │   ├── __init__.py
│   │   ├── jobspy_wrapper.py
│   │   ├── rotation.py
│   │   └── filters.py
│   ├── parser.py
│   ├── scorer/
│   │   ├── __init__.py
│   │   ├── embeddings.py
│   │   ├── selector.py
│   │   ├── ordering.py
│   │   └── apply_decision.py
│   ├── builder/
│   │   ├── __init__.py
│   │   ├── llm_call.py
│   │   ├── skills_validator.py
│   │   ├── assembler.py
│   │   ├── hyperlinks.py
│   │   └── pdf_convert.py
│   ├── sender/
│   │   ├── __init__.py
│   │   ├── indeed.py
│   │   ├── glassdoor.py
│   │   ├── fields.py
│   │   ├── questions.py
│   │   ├── bank.py
│   │   ├── cover_letter.py
│   │   ├── pdf_render.py
│   │   └── voice.py
│   ├── state/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── migrations/
│   │   ├── master_profile.py
│   │   ├── queue.py
│   │   └── cleanup.py
│   ├── aws/
│   │   ├── __init__.py
│   │   ├── s3.py                   (upload, presigned URL generation)
│   │   ├── cloudwatch.py           (log shipping, custom metrics)
│   │   └── iam_session.py          (boto3 session, credential handling)
│   ├── notifications.py
│   ├── analytics.py
│   ├── llm/
│   │   ├── client.py
│   │   ├── schemas.py
│   │   └── prompts.py
│   └── main.py
├── tests/
├── .env
├── .gitignore
├── requirements.txt
├── PRD.md
├── CLAUDE.md
├── CHANGELOG.md
└── README.md
```

---

## 9. Build Sequence

The build proceeds in iterations, not layer-by-layer. Each iteration produces an end-to-end testable system. AWS services are introduced exactly when their purpose becomes real — no intermediate filesystem code that gets replaced by S3 later.

### ITERATION 0 — SCAFFOLD ✅ COMPLETE

Repo structure, requirements.txt, config.yaml (initial), .env.example, tests/conftest.py + smoke test, src/main.py orchestrator docstring, README.md, CHANGELOG.md initialized.

### ITERATION 0.1 — AWS PREPARATION (RETROACTIVE)

Before Iteration 1 begins, the scaffold needs these additions to support AWS integration starting at Iteration 2:

```
1. config.yaml gets an aws: section:

   aws:
     region: "ap-south-1"
     s3_bucket: ""              # to be filled before Iteration 2
     s3_resume_prefix: "resumes/applied/"
     s3_cover_letter_prefix: "cover_letters/"
     cloudwatch_log_group: "/jobbot/main"
     cloudwatch_metric_namespace: "JobBot"
     iam_role_arn: ""           # populated when role is created
     presigned_url_expiry_seconds: 604800   # 7 days

2. .env.example gets AWS keys:

   AWS_ACCESS_KEY_ID=
   AWS_SECRET_ACCESS_KEY=
   AWS_REGION=ap-south-1
   S3_BUCKET=

3. requirements.txt gets:
   boto3
   watchtower         # structlog → CloudWatch handler

4. .gitignore gets:
   data/pending_uploads/

5. New folder: src/aws/ with __init__.py only
   (modules will populate in Iteration 2-3)

6. AWS account setup (user task before Iteration 2):
   - Create IAM user (or role) named job-bot-runtime
   - Attach minimal policy (see Section 5.4)
   - Create S3 bucket job-bot-{username}-resumes in ap-south-1
   - Enable versioning, block public access
   - Generate access key + secret for the IAM user
   - Save to local .env

7. CHANGELOG.md [Unreleased] entry:
   ### Added
   - AWS configuration block in config.yaml
   - AWS credentials in .env.example
   - boto3 and watchtower in requirements.txt
   - src/aws/ package skeleton
```

**Acceptance:** AWS account ready, .env has working credentials, `aws s3 ls s3://{bucket}` works from local machine.

### ITERATION 1 — END-TO-END SKELETON

Goal: minimal cross-layer stubs so the pipeline runs end-to-end. Nothing applies to real portals; nothing makes real LLM calls. AWS not used yet (no resumes built yet to store).

```
1. Layer 7: SQLAlchemy models + Alembic migrations for ALL tables.
   Run migrations against Neon.
2. Layer 1: python -m src.main --dry-run walks 9 layers in sequence.
3. Layer 2 stub: returns 2-3 hardcoded fake jobs, writes to all_jobs.
4. Layer 3 stub: returns fixed JDParsed object.
5. Layer 4 stub: rejects all (since master_bullets is empty), logs
   to not_applied with LOW_SCORE.
6. Layers 5, 6: no-ops with valid function signatures.
7. Layer 8: real Telegram message at end of run.
8. Layer 9: no-op.
```

**Acceptance:** dry-run completes, 3 fake jobs in all_jobs and not_applied, Telegram delivered, pytest green.

User writes master_profile.yaml in parallel.

### ITERATION 2 — REAL DATA FLOW + S3 + IAM

Goal: replace stubs with real implementations. Resumes get built and stored in S3. Still no auto-applying.

```
1. Layer 2 real: JobSpy on Indeed only, serial rotation, short-circuit.
2. Layer 7 extended: master_profile rebuild script (validate, embed, write).
3. Layer 3 real: Gemini Call 1a + spaCy + role acceptance.
4. Layer 4 real: full selection algorithm, cycle picking, queue management.
5. Layer 5 partial: selector + Call 1b + DOCX assembler + PDF conversion.
6. AWS integration:
   - src/aws/iam_session.py: boto3 session from .env
   - src/aws/s3.py: upload_resume(), upload_cover_letter(),
                     generate_presigned_url()
   - Resume saved DIRECTLY to S3 (no local filesystem version kept,
     except for transient /tmp during LibreOffice conversion)
   - applied.resume_s3_uri stored in DB
7. Layer 6: still no-op (no submission yet).
8. Layer 8 extended: morning digest with presigned S3 URLs.
9. Layer 9 minimal: Sheets with presigned S3 URLs in resume column.
```

**Acceptance:**
- Real Indeed jobs scraped and parsed
- Resumes built and uploaded to S3 (verify in AWS console)
- Sheets shows clickable presigned URLs
- Telegram digest delivered
- Dry-run mode for 2-3 days, review every selection, tune thresholds

### ITERATION 3 — ENABLE SUBMISSION + CLOUDWATCH

Goal: auto-apply to Indeed. Operational observability via CloudWatch.

```
1. Layer 6 real: Indeed Easy Apply via Playwright.
   - Resume downloaded from S3 to /tmp before form upload
   - /tmp file deleted after upload
2. Layer 6 fields, cover letter, questions, manual-required flow.
3. Gemini Call 2 for unknown form questions.
4. AWS CloudWatch integration:
   - src/aws/cloudwatch.py: structlog → CloudWatch handler via watchtower
   - Log group: /jobbot/main
   - Metric filters on: APPLY_FAILURE, BUILD_FAILURE, MANUAL_REQUIRED
   - Alarm: APPLY_FAILURE rate > 3 in 24h → CloudWatch alarm → Lambda
     (only Lambda usage: tiny ping-Telegram function) → user Telegram
   - Alarm: session_expired event count > 0 in 1h → same path
5. IAM role extended with logs:CreateLogStream, logs:PutLogEvents,
   cloudwatch:PutMetricData.
```

**Acceptance:**
- First successful auto-apply confirmed (verify on Indeed)
- CloudWatch logs visible in AWS console
- Test alarm firing reaches Telegram
- Safe mode triggers on unknown questions correctly
- applied table populated with full audit trail

### ITERATION 4 — FEATURE COMPLETION

```
1. Glassdoor scraper (most results MANUAL_REQUIRED — fine, S3 stores PDFs)
2. Monthly Google Doc with Gemini Sunday report
3. answer_bank growth from approved Telegram responses
4. Storage cleanup cron (S3 lifecycle for screenshots/logs >90d;
   resumes/applied/ NEVER deleted)
5. Portal health monitoring
6. CLI debug commands: inspect.py, dryrun.py, queue.py
```

### ITERATION 5 — PRODUCTION DEPLOYMENT

```
1. Provision Oracle Cloud Always Free VM
2. Install Python, LibreOffice, Playwright + Chromium, boto3
3. Transfer code, .env (with IAM credentials), resume template
4. Re-login Indeed manually on VM, save session cookies
5. Set up cron (peak 40min, off-peak 4h, Sunday 8am IST)
6. Set up systemd service for crash recovery
7. CloudWatch confirms logs streaming from VM
```

**Acceptance:** Bot runs 7 days unattended, Telegram digest daily, zero manual intervention.

### ITERATION 6 — EXPANSION (OPTIONAL)

Only when single-portal volume justifies the complexity.

```
1. Naukri scraper + sender
2. SQS between scraper and parser/scorer:
   - Scraper publishes job_ids to JobsScraped queue
   - Worker consumes, parses, scores
   - Decouples scraping rate from processing rate
   - Enables multiple workers in parallel
3. Residential proxy if Indeed/Naukri blocks
```

### ITERATION 7 — OPTIONAL

Custom MCP server exposing bot stats so Claude can act as a conversational dashboard.

---

## 10. Edge Cases & Mitigations

| Edge case | Mitigation |
|---|---|
| Laptop sleeps | Oracle Cloud VM, not local (Iteration 5) |
| Same job across portals | Dedup via all_jobs job_id |
| JobSpy HTML breaks | portal_health alert |
| Indeed IP blocks | Iteration 6 residential proxy |
| Gemini hallucinates skills | spaCy validator catches |
| Gemini exceeds Familiar With cap | Pydantic max_length=4 |
| Gemini picks unauthorized title | Literal[tuple(aliases)] enforces |
| Gemini puts skill not in candidates | Post-validation rejects, regenerate |
| Gemini puts gap skill not in JD gaps | Post-validation rejects, regenerate |
| master_profile YAML invalid | Validation fails, alert, abort run |
| Bullet referenced by old applied row | Stays as is_active=false, never deleted |
| User edits master_profile | Auto-detected on mtime, rebuild next run |
| Fewer than 2 experiences pass threshold | Force-include strongest 2 |
| Fewer than 2 projects pass threshold | Force-include best 2, never hide section |
| Project has <2 qualifying bullets | Force-include best 2 |
| No keyword hits 20 jobs in a run | Process what was found, continue rotation |
| All eligible already in queue | Pick from queue, don't double-count |
| Queue grows unbounded | 12-hour expiry, Sunday cleanup |
| Apply button missing | MANUAL_REQUIRED |
| External redirect mid-flow | MANUAL_REQUIRED |
| CAPTCHA | MANUAL_REQUIRED |
| Session cookie expired | CloudWatch alarm → Telegram (Iteration 3+) |
| Unknown field type | Screenshot, MANUAL_REQUIRED |
| Unknown question | Gemini draft → hold for review (safe mode) |
| Cat 4 question | MANUAL_REQUIRED |
| JD specifies salary above 6 LPA | Use JD upper bound |
| JD specifies salary below 6 LPA | Use JD upper bound (apply anyway) |
| JD silent on salary | Default 6 LPA |
| Cover letter textarea | Fill text from Call 1b |
| Cover letter file upload | Render PDF, upload to S3, download for form |
| No cover letter field | Skip |
| AI-sounding answer | Banned-pattern check, regenerate up to 2x |
| Fabricated content | master_profile text validation, regenerate |
| LLM JSON parse fails | Instructor enforces schema |
| Neon cold start | ~500ms accepted |
| Neon down | Buffer to local jsonl, drain next run |
| S3 down | Buffer to data/pending_uploads/, retry next run |
| S3 region outage | Same as S3 down (region-bound bucket) |
| CloudWatch down | Logs buffered locally, ship next run (non-blocking) |
| IAM credentials expired | Immediate Telegram alert, halt bot |
| AWS bill alarm fires | Disable offending service, never pay |
| Wrong filename on upload | Verified rename before upload |
| Multi-page form unknown Q | Atomic abandon-and-restart on approval |
| Page loop in Playwright | Page signature tracking, max 10 |
| DOCX template structure changes | Document expected structure clearly |
| Embedded hyperlinks broken on clone | Explicit r:id target update logic |

---

**End of architecture document.**
