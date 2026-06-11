# Job Application Automation System — Architecture

**Owner:** Vishnujan Narayanan
**Cost:** $0/month forever
**Build status:** Iterations 0-1 complete. Pivoting design before Iteration 2 (see Migration Note).

---

## Migration Note (read first)

The project pivoted after Iteration 1. The earlier design auto-applied to jobs via Playwright. **It no longer does.**

**What we did (Iterations 0-1):** scaffolded the repo and built an end-to-end skeleton with stub implementations for all 9 layers — including a Playwright-based auto-apply sender (old Layer 6), S3-as-primary-resume-storage, and an Indeed/Glassdoor-only scraper.

**What we're pivoting to (Iteration 2 onward):**

```
OLD                                  NEW
auto-apply via Playwright       →    you apply manually; bot hands you
                                     a tailored resume per match
store rendered PDFs in S3        →    store tiny selection_json in
                                     Postgres; render PDF on-demand
                                     from an endpoint
no LinkedIn (ban risk)           →    LinkedIn back as a listings source
                                     (JobSpy reads public listings only;
                                     never logs into or acts on your
                                     account, so no account-ban risk)
cycle quotas (3 peak / 1 off)    →    notify on every match >= 0.50
4-category form questions        →    removed (no form filling)
answer bank, form-answer voice   →    removed
Gemini Call 2 (form questions)   →    removed; max 2 calls per job now
```

**What the agent must clean up before/early in Iteration 2** (audit the existing stubs and remove what's now obsolete):

```
REMOVE / REPURPOSE:
  - The Playwright sender stub (old Layer 6 auto-apply) — gone entirely
  - Any session-cookie handling for portal login — gone
  - Any form-field classification / salary-form / yes-no handling — gone
  - answer_bank table + stubs — gone
  - pending_review table + review-of-form-answers flow — gone
    (note: Telegram review of Cat-3 form answers is gone; there are
     no form answers anymore)
  - Gemini Call 2 stub — gone
  - voice.py form-answer validation — gone (cover-letter voice stays)
  - S3-stores-every-resume logic — replaced by on-demand rendering
  - The fixed cycle-quota picking (3/1) — replaced by notify-all-matches

KEEP / ADAPT:
  - Layers 1-4 stubs (scheduler, scraper, parser, scorer) — keep,
    make real in Iteration 2 (add LinkedIn to scraper sources)
  - Layer 5 (builder) — adapt: produce selection_json, NOT a stored PDF
  - Layer 7 (state) — adapt schema: drop answer_bank/pending_review,
    keep selection storage, add pdf_cache + near_duplicate tracking
  - Layer 8 (notifications) — adapt: bundle apply-link + resume-link
  - Layer 9 (analytics) — keep, resume links now point to the endpoint

ADD (new in this pivot):
  - Resume endpoint (FastAPI on the VM): renders PDF/DOCX on demand
  - pdf_cache: 1-month TTL, backed by S3 (survives VM restarts)
  - near-duplicate JD detection (cosine > 0.95)
```

The rest of this document describes the **target architecture** as it should be after the pivot. Where a section is materially changed by the pivot, it's noted inline. Don't over-index on the history — build toward the target state below.

---

## 1. Goal

Take the job market and hand the user a perfectly tailored resume per good match, making applying a two-tap action the user performs themselves.

The system:
- Detects new postings within ~10 minutes
- Scores them via a multi-factor relevance model
- Builds a custom-tailored resume (as a compact selection) per matched JD
- Notifies the user per match with the apply link and a resume link bundled together
- Renders the resume PDF/DOCX on demand from a tiny endpoint
- Keeps a permanent, queryable record mapping every resume to its exact job
- Runs on $0/month infrastructure

**The core value is the JD → tailored-resume engine.** Everything else serves that.

---

## 2. Operator Configuration

This system is **instance-ready**: it runs as a single-operator tool, but nothing is hardcoded to a specific person. Every value below comes from `config.yaml`, `.env`, and `master_profile.yaml`. A different person clones the repo, fills their own files, and runs their own independent instance — no code changes. The values shown here are the current operator's (Vishnujan's); they live in config, not in source. See Section 11 (Instance-Readiness).

```
Name                     (config: operator.full_name)
Years of experience      (config: operator.years_experience)
Job type                 Fulltime only (config: filters.job_type)
Years required ceiling   5 (config: filters.years_ceiling)
Location policy          Allow all except disallowed regions
Disallowed regions       (config: filters.disallowed_regions)
                         current: Delhi NCR (Delhi, Gurgaon, Gurugram,
                         Noida, Ghaziabad, Faridabad)
Visa policy              No filter (config: filters.visa_filter=false)

Salary (informational — cover letter + notification only)
  Expected default       (config: salary.default_expected_lpa) — current 6
  Expected per JD        JD upper bound if specified, else default

Resume source            master_profile.yaml (gitignored, per-operator)
Familiar With            Max 4 adjacent gap skills (config)
```

Resume upload filename is derived, never literal: `{operator.full_name with underscores}_Resume.pdf`. No operator-specific string ever appears in source code.

### 2.1 Target role categories

```
backend, data, ml, fullstack, devops, finance_market, quant
```

### 2.2 Search keywords

Stored in `config.yaml` under `scraper.search_rotation.terms` — 24 terms covering all 7 categories.

### 2.3 Role acceptance clusters

In `config.yaml` under `parser.role_clusters`. Maps each search term to acceptable role titles and Gemini categories for smart matching. A "backend engineer" search accepts a "software developer" job if Gemini classifies it backend or fullstack.

---

## 3. Master Profile Model

### 3.1 Structure (`master_profile.yaml`, gitignored)

- `personal` — contact info and links
- `summaries` — pre-written summary pool, each tagged with role categories
- `work_experience` — entries with `bullet_pool` and `safe_title_aliases`
- `projects` — entries with `bullet_pool` and embedded `link`
- `skills_pool` — flat list of all defensible skills
- `education` — degree entries
- `certifications` — entries with `verify_link`

### 3.2 Lifecycle

On bot run, if `master_profile.yaml` mtime changed: validate → generate `master_profile.json` → diff against DB (new/changed bullets embed + upsert, removed bullets marked `is_active=false`) → update `master_meta`. Bullets, summaries, and title aliases are never hard-deleted.

### 3.3 Pre-computed embeddings

Every bullet, summary, title alias, and skill gets a `vector(384)` via sentence-transformers at rebuild, stored in DB. Only the JD is embedded per run.

---

## 4. Architecture — 9 Layers

```
LAYER 1   Scheduler
LAYER 2   Scraper             (Indeed, Glassdoor, LinkedIn — listings only)
LAYER 3   JD Parser           (Gemini Call 1a)
LAYER 4   Scoring Engine
LAYER 5   Resume Builder      (Gemini Call 1b → selection_json)
LAYER 6   Application Assist  (notify + on-demand resume endpoint)
LAYER 7   State Management    (Postgres on Neon + S3 cache/backup)
LAYER 8   Notifications       (Telegram + CloudWatch alarms)
LAYER 9   Analytics           (Sheets + Docs)
```

Max **2** Gemini calls per job now (Call 1a parse, Call 1b build). Old Call 2 for form questions is removed.

---

### Layer 1 — Scheduler

Oracle Cloud cron (Iteration 5 onward), local cron (Iterations 1-4).

```
Peak       Every 40 min, 8am-6pm IST, weekdays
Off-peak   Every 4 hours, nights and weekends
Sunday     8am IST → weekly report + cache cleanup
```

No application quotas — every match >= 0.50 produces a notification.

---

### Layer 2 — Scraper

**Sources:** Indeed, Glassdoor, LinkedIn — all via JobSpy, **listings only**. Naukri in Iteration 6.

**LinkedIn note:** JobSpy reads public LinkedIn job listings. It does NOT log into the user's account or perform any action on it. The risk profile is "scraper IP could be rate-limited" (infrastructure, recoverable), NOT "user account banned" (which is what the earlier no-LinkedIn rule guarded against, and which no longer applies because there's no auto-apply).

**Serial search rotation with short-circuit:**

```
Each run continues rotation from search_rotation_state.current_index
For each keyword:
  JobSpy query (India + hours_old) across enabled sources
  Insert every result into all_jobs (permanent archive)
  Apply hard filters + near-duplicate check
  Count passing results
  If passing >= 20 → advance rotation, stop
  Else → next keyword
```

**Hard filters:**

```
years_required > 5            → reject (HARD_FILTER_LAYER_2)
location in disallowed list   → reject (LOCATION_DISALLOWED)
company in cooldown (10d)     → reject (COMPANY_COOLDOWN)
duplicate job_id              → reject (DUPLICATE)
near-duplicate JD (>0.95)     → reject (NEAR_DUPLICATE), link to original
```

Passed jobs → `processing_queue`.

**Near-duplicate detection (new):** before queuing, embed the JD and compare against recent jobs' JD embeddings. If cosine > 0.95 to an already-notified job, mark `NEAR_DUPLICATE`, store a reference to the original job_id, and skip — prevents two notifications for the same role posted across portals or reworded.

---

### Layer 3 — JD Parser (Gemini Call 1a)

Gemini 2.0 Flash + Instructor, spaCy validator.

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
    apply_url: str   # the link the user taps to apply
```

spaCy validates extracted skills appear in JD text. Hard filter re-check on structured data. Role acceptance check against clusters.

---

### Layer 4 — Scoring Engine

#### 4.1 JD embeddings

```
jd_skill_vecs   = [embed(s) for s in (required + nice_to_have)]   # per-skill
jd_vec_skills   = embed(required + nice_to_have)   # blended, only for vec_match
jd_vec_resp     = embed(responsibilities + role_summary)
jd_vec_role     = embed(role_summary)
jd_vec_match    = jd_vec_skills + jd_vec_resp
```

Skills are embedded **individually** (`jd_skill_vecs`): each pool skill scores
against its best-matching individual JD skill (§4.5), so an exact match scores
~1.0 instead of being diluted across a blended centroid. The blended
`jd_vec_skills` is retained only as one component of `jd_vec_match`, which
matches bullets holistically (work done vs. what the JD wants) — §4.2/§4.3.

#### 4.2 Experience scoring

```python
def score_experience(exp, jd):
    best_alias = max(cosine(embed(a), jd_vec_role) for a in exp.safe_title_aliases)
    bullet_scores = sorted([cosine(b.embedding, jd_vec_match) for b in exp.active_bullets], reverse=True)
    return (best_alias * 0.30) + (mean(bullet_scores[:3]) * 0.70)
```

Threshold 0.45, max 3, min 2, force-include strongest 2. Bullets: exactly 3 per experience, top by score, descending. Position 1 = highest-scoring IF gap > 0.20, else most recent; rest by recency.

#### 4.3 Project scoring

```python
def score_project(proj, jd):
    name = cosine(embed(proj.name), jd_vec_role)
    bullet_scores = sorted([cosine(b.embedding, jd_vec_match) for b in proj.active_bullets], reverse=True)
    passing = [s for s in bullet_scores if s >= 0.40]
    n = min(3, max(2, len(passing)))
    return (name * 0.20) + (mean(bullet_scores[:n]) * 0.80)
```

Score reflects displayed bullet count. Threshold 0.50, max 3, min 2, force-include best 2. **Section never hidden.** Bullets min 2 / max 3, descending.

#### 4.4 Summary selection (deterministic)

Filter pool by JD role_category, pick highest cosine match. No LLM.

#### 4.5 Skills selection (hybrid)

```
Step 1: score every pool skill = max(cosine(pool_skill, jd_skill)
        for jd_skill in jd_skill_vecs)   # best individual match, not centroid
Step 2: take top-14 candidates
Step 3: identify + score gap skills (JD requires, not in pool)
Step 4: LLM names 3 categories, assigns 3-5 skills each from candidates,
        picks up to 4 gap skills for Familiar With
Step 5: order skills within each category by score, descending
Step 6: compute aggregate score per category INCLUDING Familiar With
Step 7: order all 4 categories by aggregate match, descending —
        Familiar With NOT pinned
```

Total 10-14 pool skills + 0-4 gaps. LLM optimizes grouping, not count. Post-validation: category skills from top-14, Familiar With from gaps, no duplicates, no banned category names; regenerate once on violation else BUILD_FAILURE.

#### 4.6 Section ordering

```
FIXED:    Summary → Work Experience → ... → Education → Certifications
VARIABLE: Skills and Projects between Work and Education, by match score
```

#### 4.7 Final score

```python
fit_score = best_experience_score * 0.50 + summary_score * 0.20 + avg_skill_pool_match * 0.30
success_prob = seniority_score * 0.60 + recency_score * 0.40
# Seniority: junior→1.0, mid→0.80, senior→0.40, lead→0.15
# Recency bands: <1h→1.0, 1-3h→0.8, 3-6h→0.6, 6-12h→0.4, >12h→0.2
final_score = fit_score * 0.55 + success_prob * 0.30 + recency_score * 0.10 + best_project_score * 0.05
```

#### 4.8 Match decision (no quotas)

```
final_score >= 0.50 → build resume (selection) + notify
final_score <  0.50 → not_applied (LOW_SCORE)
```

Every qualifying match triggers Layer 5 (build selection) and a Layer 8 notification. No top-N picking, no application_queue, no 12-hour decay — those were artifacts of rationing auto-applications, which no longer exist.

#### 4.9 Layer 4 output

```python
class SelectionResult:
    job_id: str
    selected_experiences: list[(ExpId, [BulletId], position)]
    selected_projects: list[(ProjId, [BulletId])]
    selected_summary_id: str
    section_order: list[str]
    top_skill_candidates: list[str]
    top_skill_scores: dict[str, float]
    gap_skills: list[str]
    gap_skill_scores: dict[str, float]
    title_alias_candidates: dict[ExpId, list[str]]
    expected_salary_lpa: float
    final_score: float
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

#### 5.2 Output is selection_json, NOT a stored PDF

The builder resolves the final selection (experiences, bullets, projects, summary, skill categories, title aliases, cover letter) and writes it to the `applied` table as `selection_json` (~2 KB). **No PDF is rendered or stored here.** PDF/DOCX rendering happens on-demand at the endpoint (Layer 6).

```python
class StoredSelection(BaseModel):
    job_id: str
    summary_id: str
    experiences: list[{exp_id, title_alias, bullet_ids: list[str], position}]
    projects: list[{proj_id, bullet_ids: list[str]}]
    skills: {familiar_with: list[str], categories: list[{name, skills}]}
    section_order: list[str]
    cover_letter_text: str
    template_version: str            # which template this was built against
    built_at: timestamp
```

`template_version` lets the endpoint know whether a cached PDF is stale relative to the current template.

#### 5.3 Why selection-not-PDF

- Storage is ~2 KB per job, not 100-300 KB. Effectively never piles up.
- Improving the template later regenerates every past resume with the new design automatically.
- The selection IS the tailored resume in compact form; the PDF is just a render.

---

### Layer 6 — Application Assist (renamed from Sender)

Two responsibilities. No Playwright, no form filling, no submission.

#### 6.1 Notification builder

For every job scoring >= 0.50, Layer 8 sends a Telegram message bundling everything (see Layer 8 format). The user applies manually.

#### 6.2 Resume endpoint (FastAPI on the VM)

```
GET /resume/{job_id}.pdf
GET /resume/{job_id}.docx

Flow:
  1. Look up pdf_cache (or docx_cache) for this job_id + current template_version
  2. Cache hit (within 1-month TTL, matching template) → serve from S3
  3. Cache miss:
       load StoredSelection from DB
       load current template
       render DOCX via assembler (Option A structural detection)
       if .pdf requested: convert DOCX → PDF via LibreOffice
       upload rendered file to S3 cache with 1-month expiry
       serve it
  4. If StoredSelection's template_version != current template:
       re-render against current template, refresh cache
```

Rendering takes ~5s on a cache miss, instant on a hit. The endpoint is small (~100 lines FastAPI) and doubles as the foundation for the optional Iteration 7 dashboard.

#### 6.3 DOCX assembler — structural detection (unchanged from prior design)

Clone-and-fill the user's template. Header never touched (preserves hyperlinks). Project "Code →" and certificate "Verify Here" hyperlink targets updated via `r:id` modification in `word/_rels/document.xml.rels`, visible text unchanged. Tab stops, spacing, fonts, native bullet list refs preserved via XML deep clone.

This assembler now runs inside the endpoint on demand, not in a batch step.

---

### Layer 7 — State Management

#### 7.1 Database — PostgreSQL on Neon

3GB free, pgvector, psycopg3, SQLAlchemy 2.0 async, pool_pre_ping.

#### 7.2 S3 — cache + backup (Iteration 2 onward)

```
s3://{bucket}/pdf_cache/{job_id}_{template_version}.pdf     1-month expiry
s3://{bucket}/docx_cache/{job_id}_{template_version}.docx   1-month expiry
s3://{bucket}/backups/selection_json/{date}/...             selection_json backup

PDF/DOCX cache survives VM restarts (lives in S3, not VM /tmp).
selection_json backup: daily export of the applied table's selections
  to S3, in case Neon data is lost. Neon also backs up independently;
  this is belt-and-suspenders the user explicitly wanted.
S3 lifecycle rule auto-expires cache objects after 1 month.
Bucket private, versioning on backups prefix, IAM-restricted.
```

#### 7.3 Schema

```sql
CREATE TABLE all_jobs (
    job_id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    role TEXT NOT NULL,
    site TEXT NOT NULL,              -- indeed | glassdoor | linkedin
    location TEXT,
    job_url TEXT,
    apply_url TEXT,
    posted_at TIMESTAMPTZ,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    jd_text TEXT,
    jd_embedding vector(384),        -- for near-duplicate detection
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
    outcome TEXT,                    -- matched | not_applied
    near_duplicate_of TEXT NULL,     -- references all_jobs(job_id)
    outcome_at TIMESTAMPTZ
);
CREATE INDEX idx_all_jobs_scraped ON all_jobs(scraped_at DESC);
CREATE INDEX idx_all_jobs_outcome ON all_jobs(outcome);
CREATE INDEX idx_all_jobs_embedding ON all_jobs USING ivfflat (jd_embedding vector_cosine_ops);

-- Matched jobs: a resume selection was built and the user notified
CREATE TABLE applied (
    job_id TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    selection_json JSONB,            -- the StoredSelection (~2 KB)
    template_version TEXT,
    cover_letter_text TEXT,
    expected_salary_lpa REAL,
    fit_score REAL,
    success_prob REAL,
    recency_score REAL,
    final_score REAL,
    gap_skills JSONB,
    notified_at TIMESTAMPTZ,
    user_status TEXT DEFAULT 'pending',  -- pending | applied | skipped (user sets via Telegram/Sheet)
    built_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE not_applied (
    job_id TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    reason_category TEXT NOT NULL,
    reason_detail TEXT,
    fit_score REAL,
    final_score REAL,
    gap_skills JSONB,
    in_field BOOLEAN,
    not_applied_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE processing_queue (
    job_id TEXT PRIMARY KEY REFERENCES all_jobs(job_id),
    status TEXT,
    queued_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- PDF/DOCX cache tracking (the files live in S3; this tracks them)
CREATE TABLE render_cache (
    cache_key TEXT PRIMARY KEY,      -- {job_id}_{template_version}_{ext}
    job_id TEXT REFERENCES all_jobs(job_id),
    format TEXT,                     -- pdf | docx
    template_version TEXT,
    s3_uri TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ           -- created_at + 1 month
);
CREATE INDEX idx_render_cache_expiry ON render_cache(expires_at);

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

CREATE TABLE master_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE search_rotation_state (key TEXT PRIMARY KEY, value TEXT);

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

Tables removed in the pivot: `answer_bank`, `pending_review`, `application_queue`.

#### 7.4 not_applied reason categories

```
HARD_FILTER_LAYER_2     Rejected at scrape on raw text
HARD_FILTER_LAYER_3     Rejected after Gemini parse
ROLE_MISMATCH           Doesn't match search term cluster
LOCATION_DISALLOWED     Location in disallowed regions
NEAR_DUPLICATE          JD ~identical to an already-notified job (>0.95)
LOW_SCORE               final_score < 0.50
PARSE_FAILURE           Couldn't parse JD
BUILD_FAILURE           Layer 5 selection build failed
COMPANY_COOLDOWN        Same company <10 days ago
DUPLICATE               Exact job_id already seen
```

Removed: APPLY_FAILURE, MANUAL_REQUIRED, REJECTED_BY_USER, STALE (all auto-apply artifacts).

#### 7.5 Failure handling

```
Neon down        → backoff 3x → buffer writes to data/pending_writes.jsonl
S3 down          → endpoint serves a freshly-rendered file without caching
                   it (degraded but functional); backup export retries next run
CloudWatch down  → log locally, ship next run (non-blocking)
Cold start       → ~500ms accepted
```

---

### Layer 8 — Notifications

**Per-match Telegram message (every job >= 0.50, as found):**

```
🎯 0.78
Senior Python Engineer
Acme Fintech · Remote · 12 LPA · via LinkedIn

[📋 Apply]   → apply_url (LinkedIn/Indeed/Glassdoor listing)
[📄 PDF]     → https://{vm}/resume/{job_id}.pdf
[📝 DOCX]    → https://{vm}/resume/{job_id}.docx

Why: strong backend + financial-data match
Gap skills they want: Kafka, Airflow
```

The message bundles apply link and resume links together — it IS the linkage between JD and resume. Tap Apply to go to the listing; tap PDF/DOCX to view/save the tailored resume.

**Optional inline buttons:** "Mark applied" / "Skip" — updates `applied.user_status` so the Sheet reflects what you've acted on.

**CloudWatch alarms (Iteration 3+):** endpoint down, error rate spike, scraper consecutive-zeros, master_profile validation failure → route to Telegram.

---

### Layer 9 — Analytics

**Sheets (live from Postgres) — your master "which-resume-for-which-job" index:**

```
Sheet 1 — Matches
  Date | Company | Role | Match | Salary | Source | Apply Link |
  PDF Link | DOCX Link | Status (pending/applied/skipped) | Gap Skills

You never browse raw files. This sheet is the index. Every row links
to the apply page and the on-demand resume.

Sheet 2 — Relevant skipped (LOW_SCORE but in-field)
Sheet 3 — Near-duplicates (what got deduped, linked to originals)
```

**Google Docs (monthly):** Gemini Sunday report — skill demand, recurring gaps (30%+ alert), companies hiring, salary ranges.

---

## 5. AWS Integration

### 5.1 Services used

```
S3          Iteration 2  → PDF/DOCX render cache (1-month TTL, survives
                          VM restart) + daily selection_json backup
IAM         Iteration 2  → minimal role: S3 cache/backup + CloudWatch
CloudWatch  Iteration 3  → endpoint + pipeline observability, alarms
SQS         Iteration 6  → scraper/worker decoupling, only if volume needs
```

### 5.2 Services explicitly NOT used

```
RDS, Lambda (runtime), ECS, Fargate, EKS, EventBridge (as scheduler), SNS
(reasons: Neon is forever-free with pgvector; Oracle VM is the persistent
host; Telegram + a tiny alarm hook cover notifications)
```

Note: a single tiny Lambda (ping-Telegram-on-CloudWatch-alarm) is the one acceptable Lambda use — free tier covers it (~100 invocations/month vs 1M free).

### 5.3 Cost model

```
S3          cache ~1GB peak + backups ~tens of MB. Free tier 5GB. → $0
CloudWatch  ~100MB/month logs. Free tier 5GB ingest. → $0
IAM, SQS    free / negligible
Total AWS   $0/month. Billing alarm at $1; if it fires, disable the
            offending service, never pay.
```

### 5.4 IAM minimal permissions

```
s3:PutObject, s3:GetObject, s3:DeleteObject   on the bot's bucket
logs:CreateLogStream, logs:PutLogEvents       on the bot's log group
cloudwatch:PutMetricData                      on the bot's namespace
DENIED: bucket-level ops, iam:*, ec2:*, rds:*, lambda:* (except the
        alarm Lambda's own minimal role)
```

### 5.5 Region

All AWS resources in `ap-south-1` (Mumbai).

---

## 6. Gemini Call Strategy — 2 calls max per job

```
CALL 1a — JD parse (ALWAYS)                  ~800 tokens
CALL 1b — title + skills + cover letter      ~1800 tokens
          (only if final_score >= 0.50)

Per-job: 1 call (rejected) or 2 calls (matched)
Daily expected: well within 1500/day free tier
(Old Call 2 for form questions removed.)
```

---

## 7. Stack

```
Language          Python 3.11+
Scraping          JobSpy (Indeed, Glassdoor, LinkedIn — listings only)
NLP validation    spaCy
Embeddings        sentence-transformers (all-MiniLM-L6-v2)
LLM               Gemini 2.0 Flash via Instructor + Pydantic
Database          PostgreSQL on Neon (free, pgvector)
DB driver         psycopg3
ORM               SQLAlchemy 2.0
Resume building   python-docx (in the on-demand endpoint)
PDF conversion    LibreOffice headless (in the endpoint)
Endpoint          FastAPI (serves /resume/{job_id}.pdf|.docx)
Cache + backup    AWS S3 (Iteration 2+)
Observability     structlog → watchtower → CloudWatch (Iteration 3+)
AWS SDK           boto3
Notifications     Telegram Bot API
Reporting         gspread, Google Docs API
Hosting iter 1-4  Local machine
Hosting iter 5+   Oracle Cloud Always Free VM
Region            ap-south-1
NO Playwright     (removed in the pivot)
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
│   ├── logs/                       (gitignored, shipped to CloudWatch)
│   └── pending_writes.jsonl        (DB outage buffer, gitignored)
├── config/
│   └── config.yaml                 (includes parser.role_clusters, aws.*)
├── src/
│   ├── scheduler.py
│   ├── scraper/
│   │   ├── __init__.py
│   │   ├── jobspy_wrapper.py
│   │   ├── rotation.py
│   │   ├── filters.py
│   │   └── dedup.py                (near-duplicate detection)
│   ├── parser.py
│   ├── scorer/
│   │   ├── __init__.py
│   │   ├── embeddings.py
│   │   ├── selector.py
│   │   └── ordering.py
│   ├── builder/
│   │   ├── __init__.py
│   │   ├── llm_call.py
│   │   ├── skills_validator.py
│   │   └── selection.py            (produces StoredSelection)
│   ├── endpoint/                   (NEW — replaces old sender/)
│   │   ├── __init__.py
│   │   ├── app.py                  (FastAPI, /resume routes)
│   │   ├── assembler.py            (DOCX structural detection)
│   │   ├── hyperlinks.py
│   │   ├── pdf_convert.py
│   │   └── cache.py                (S3 cache get/put + TTL)
│   ├── state/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── migrations/
│   │   ├── master_profile.py
│   │   └── cleanup.py
│   ├── aws/
│   │   ├── __init__.py
│   │   ├── s3.py                   (cache objects + backup export)
│   │   ├── cloudwatch.py
│   │   └── iam_session.py
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

Removed from the old structure: `src/sender/` (Playwright, fields, questions, bank, voice-for-forms). The cover-letter text generation stays (in builder/llm_call), but cover-letter PDF rendering, if a user wants it, happens in the endpoint.

---

## 9. Build Sequence

### ITERATION 0 — SCAFFOLD ✅ COMPLETE
### ITERATION 1 — END-TO-END SKELETON ✅ COMPLETE (old design — see Migration Note)

### ITERATION 2 — PIVOT CLEANUP + REAL DATA FLOW

```
A. CLEANUP (do first — remove obsolete Iteration-1 stubs)
   - Delete src/sender/ (Playwright, fields, questions, bank, voice-for-forms)
   - Drop answer_bank, pending_review, application_queue from models +
     migrations (write a migration that drops them if already created)
   - Remove Gemini Call 2 stub and references
   - Remove cycle-quota picking from the scorer
   - Update CHANGELOG [Unreleased] with all removals

B. REAL DATA FLOW
   - Layer 2 real: JobSpy on Indeed + Glassdoor + LinkedIn (listings),
     serial rotation, short-circuit, near-duplicate detection (dedup.py)
   - Layer 7: master_profile rebuild (validate, embed, upsert);
     add render_cache table; add jd_embedding + near_duplicate_of to all_jobs
   - Layer 3 real: Gemini Call 1a + spaCy + role acceptance
   - Layer 4 real: selection algorithm; match decision (no quotas)
   - Layer 5 real: Call 1b → StoredSelection written to applied table
     (NO PDF rendered here)
   - Layer 6 (endpoint): FastAPI app with /resume/{job_id}.pdf|.docx,
     assembler, pdf_convert, S3 cache (cache.py)
   - AWS: src/aws/s3.py (cache put/get + selection_json backup export),
     iam_session.py; S3 bucket with 1-month lifecycle on cache prefixes
   - Layer 8: per-match Telegram notification bundling apply + resume links
   - Layer 9: Sheets index with apply/PDF/DOCX links + status column

Acceptance:
   - Real listings scraped from all three sources
   - Near-duplicates correctly deduped (verify cross-portal repost)
   - Matches produce StoredSelection in DB (no PDF pile)
   - Telegram notification arrives per match with working links
   - Clicking PDF/DOCX renders and serves the tailored resume (~5s cold,
     instant cached); cache survives a VM restart (S3-backed)
   - Sheet shows every match mapped to its resume + apply link
   - Run for several days, review selections, tune thresholds in config
```

### ITERATION 3 — CLOUDWATCH OBSERVABILITY

```
- structlog → CloudWatch via watchtower
- Metric filters: BUILD_FAILURE, endpoint 5xx, scraper zero-results
- Alarms → tiny ping-Telegram Lambda → user
- IAM extended with CloudWatch permissions
```

### ITERATION 4 — POLISH

```
- Monthly Google Doc Sunday report (Gemini synthesis)
- Sheet "mark applied/skipped" round-trip from Telegram buttons
- render_cache cleanup job (expire >1-month entries; S3 lifecycle backstops)
- portal_health monitoring + alerts
- CLI: inspect.py, dryrun.py, render.py (force re-render a job), aws_check.py
```

### ITERATION 5 — PRODUCTION DEPLOYMENT

Deployment is **Docker-based**. A single image bundles Python, the pinned deps
(CPU torch + the spaCy model wheel via `requirements.txt`) and the one system
binary the pip stack can't provide — **LibreOffice headless** (DOCX→PDF in Layer
6). The same image serves two roles, selected by entry command:

```
- Oracle Cloud Always Free VM (the persistent host; replaces the laptop)
- One Dockerfile (python:3.11-slim + libreoffice-writer + fonts + requirements.txt)
- docker-compose with two services from that image:
    endpoint  — uvicorn src.endpoint.app:app, restart: unless-stopped, port 8000
                (always-on; Telegram resume/apply links resolve here)
    pipeline  — python -m src.main, profile "manual", invoked on demand
- Layer 1 scheduler = host cron firing `docker compose run --rm pipeline` at the
  cadence documented in config.yaml `scheduler:` (cron is the scheduler — no
  EventBridge/Lambda, per the free-tier rule). Replaces the old systemd plan.
- One-time on first deploy: `alembic upgrade head` (Neon), `cli.reparse`
  (master profile → DB), `cli.aws_check` (verify S3 + IAM + CloudWatch).
- HTTPS for the endpoint (Caddy or nginx + Let's Encrypt) so Telegram links are
  clickable and secure; set config.yaml `endpoint.base_url` to the VM's URL.
- CloudWatch confirms logs streaming from VM.
```

**Why Docker (beyond the VM):** the codebase is instance-ready (§ rule #21 — no
operator identity in source), so the *same* image + compose file is the
distributable artifact a second person uses to run their own independent copy.
They clone, supply their own `.env` + `master_profile.yaml` + template + Google
service-account JSON (all gitignored, mounted as volumes / `env_file`), and
`docker compose up` — no venv/LibreOffice/spaCy install to fight, no code edits.
Neon stays external (reached via `DATABASE_URL`), so the container is just the app.

**Arch note:** the `torch==2.12.0+cpu` pin is an x86_64 wheel; on an arm64 shape
(Ampere A1) plain `torch==2.12.0` from PyPI is the CPU build. Keep the torch line
arch-aware via environment markers and build the image on the VM so pip resolves
for the host arch — one `requirements.txt` works on either Oracle shape.

### ITERATION 6 — EXPANSION (OPTIONAL)

```
- Naukri as a listings source
- SQS scraper→worker decoupling if volume justifies
- Residential proxy if any source throttles the scraper IP
```

### ITERATION 7 — OPTIONAL

```
- Expand the endpoint into a small dashboard (it already renders resumes;
  add views over matches, statuses, analytics)
- Optional MCP server so Claude can query bot stats conversationally
```

---

## 10. Edge Cases & Mitigations

| Edge case | Mitigation |
|---|---|
| Laptop sleeps | Oracle VM (Iteration 5) |
| Same job across portals | job_id dedup + near-duplicate (>0.95) detection |
| Reworded repost | Near-duplicate detection catches it |
| JobSpy HTML breaks | portal_health alert |
| Scraper IP throttled by LinkedIn | Infrastructure-only risk; fall back to other sources; Iteration 6 proxy. User account never touched. |
| Gemini hallucinates skills | spaCy validator |
| Gemini exceeds Familiar With cap | Pydantic max_length=4 |
| Gemini picks unauthorized title | Literal[tuple(aliases)] |
| Gemini skill not in candidates | Post-validation rejects, regenerate |
| master_profile YAML invalid | Validation fails, alert, abort run |
| Bullet referenced by old selection | Stays is_active=false, never deleted |
| User edits master_profile | mtime rebuild; old selections keep their template_version |
| Template improved | Endpoint re-renders past resumes against new template automatically |
| <2 experiences pass threshold | Force-include strongest 2 |
| <2 projects pass threshold | Force-include best 2, never hide section |
| Project <2 qualifying bullets | Force-include best 2 |
| No keyword hits 20 in a run | Process what's found, continue rotation |
| Cache miss on resume click | Render fresh (~5s), cache, serve |
| Cache stale vs new template | template_version mismatch → re-render |
| VM restarts | S3-backed cache survives; selection_json safe in Postgres |
| Neon cold start | ~500ms accepted |
| Neon down | Buffer to local jsonl, drain next run |
| S3 down | Endpoint renders without caching (degraded but works) |
| CloudWatch down | Local logs, ship next run (non-blocking) |
| IAM credentials expired | Telegram alert; endpoint still renders (cache writes fail gracefully) |
| AWS bill alarm fires | Disable offending service, never pay |
| Embedded hyperlinks broken on clone | Explicit r:id target update; tested every render |
| DOCX template structure changes | Document expected structure; assembler reads it dynamically |

---

## 11. Instance-Readiness

The system is built so a different person can run their own independent copy by swapping files — no code changes. This is **instance-ready**, NOT multi-tenant SaaS. Each instance is one person, one deployment, fully isolated.

### What this requires of the code

```
1. ZERO hardcoded operator identity
   No operator name, email, filename, region, or preference literal
   appears in source. All of it comes from config.yaml / .env /
   master_profile.yaml.
   - Resume filename: derived as "{operator.full_name→underscores}_Resume.pdf"
   - Cover filename: "{operator.full_name→underscores}_Cover_Letter.pdf"
   - No "Vishnujan" string anywhere in src/

2. ALL operator data in swappable files
   config.yaml          → name, filters, preferences, AWS bucket, region
   .env                 → that operator's secrets (Gemini, Neon, Telegram,
                          AWS keys)
   master_profile.yaml  → that operator's career
   resumes/templates/   → that operator's resume + cover templates

3. NO shared state between instances
   Each instance has its own Neon DB, own Telegram bot, own AWS bucket,
   own VM, own Google Sheet/Doc. Nothing is keyed by user_id because
   there is only ever one operator per instance.

4. Setup is documented and repeatable
   README walks a new operator through: clone → create their accounts →
   fill .env + config.yaml + master_profile.yaml + template → run.
   The onboarding checklist in CLAUDE.md is operator-agnostic.
```

### What is explicitly deferred (NOT built now)

```
Multi-tenant SaaS — one running system serving many users — is OUT OF
SCOPE. That would need: authentication, user accounts, per-user data
isolation in every table, per-user scheduling, a signup/onboarding UI,
billing, and shared-infrastructure cost management. None of that is
built. Do not add user_id columns, auth, or tenant isolation.
```

### Seams that keep SaaS a clean future option

Without building SaaS, these existing design choices keep the door open:

```
- The 9-layer modular split means a future "user context" object could
  be threaded through layers without restructuring them.
- All operator config already lives in structured files — a future
  SaaS would load these per-user from a store instead of from disk.
- selection_json + master_profile are already self-contained per-operator
  artifacts — naturally partition by user later.
- The resume endpoint is already stateless per request (job_id in,
  resume out) — trivially becomes per-user with a path/token prefix later.

These are observations, not work items. Build the single-operator
instance well; the SaaS path stays open by virtue of clean modularity.
```

### The practical test

```
"Could a second person use this?"
  → Clone repo
  → Create their own Neon, Gemini key, Telegram bot, AWS bucket, Sheet
  → Fill .env, config.yaml, master_profile.yaml, drop in their template
  → Run it
  → Get their own tailored resumes for their own job matches
  → Zero source code edits

If any step would require editing source, that's a hardcoded-identity
bug to fix.
```

---

**End of architecture document.**
