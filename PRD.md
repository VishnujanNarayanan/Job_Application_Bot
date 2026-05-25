# Product Requirements Document — Job Application Automation Bot

**Owner:** Vishnujan Narayanan
**Status:** Iteration 0 complete, Iteration 0.1 (AWS prep) pending, Iteration 1 ready to begin

---

## 1. Problem Statement

Job applications in early-career roles are a numbers game with a freshness gate. Recruiters review applications in the order received and stop after the first ~50 candidates. Applying within an hour of posting puts you in the first-look bracket; applying after a day puts you in the noise.

A single applicant manually monitoring Indeed, Glassdoor, and other portals — while tailoring resumes per role and writing cover letters — cannot realistically hit this freshness window across the 5-10 relevant listings posted daily.

Existing solutions either spam applications without quality (eroding signal), or require manual approval per job (defeating the time-saving purpose).

---

## 2. Goal

Build a fully autonomous system that:

- Detects new job postings across portals within ~10 minutes
- Scores them using a multi-factor relevance model
- Builds a custom-tailored resume per JD from a master profile pool
- Submits applications to the best-matching jobs per run (cycle-aware quotas)
- Stores every submitted resume permanently in AWS S3 for audit and interview prep
- Logs structured outcomes for every scraped job
- Uses CloudWatch for operational observability (alarms, metric filters)
- Generates weekly career intelligence reports
- Costs $0/month forever
- Runs while the user sleeps

---

## 3. Non-Goals

This product explicitly does NOT:

- Use LinkedIn (account-ban risk on the user's main account)
- Write or rewrite bullet content — the LLM never produces bullets, only selects from a user-written pool
- Fabricate skills, projects, or achievements
- Use job titles outside the user's `safe_title_aliases` allow-list per experience
- Apply to jobs requiring login walls beyond Indeed (Naukri added in Iteration 6)
- Bypass CAPTCHAs or anti-bot systems
- Maintain conversation with recruiters or auto-reply to outreach
- Replace networking, referrals, or interview preparation
- Provide investment, legal, or career counselling advice
- Use AWS services that cost money (RDS, Lambda for runtime, ECS/Fargate, EKS)

---

## 4. Target User

**Primary:** Single user (Vishnujan), Indian early-career developer (1.5 years experience), targeting fulltime roles in 7 categories — backend, data, ml, fullstack, devops, finance_market dev, quant.

**Not a multi-tenant product.** No user accounts, no shared infrastructure, no public deployment.

---

## 5. The Master Profile Model

All work, projects, summaries, skills, education, and certifications live in `master_profile.yaml` (gitignored). This is the single source of truth.

Each work experience has:
- `bullet_pool` — every bullet the user can defend
- `safe_title_aliases` — allow-list of acceptable job titles for that role

Each project has:
- `bullet_pool` — defensible bullets
- `link` — GitHub or live URL embedded in the resume

The user maintains a flat `skills_pool` of all defensible skills. Per JD, deterministic scoring picks the top-14 pool candidates by match. The LLM then names 3 categories with 3-5 skills each from those candidates, plus picks up to 4 Familiar With gap skills (JD requirements not in the pool). Distribution is flexible (10-14 pool + 0-4 gaps); the LLM optimizes for semantic grouping. All 4 categories — including Familiar With — are then ordered by aggregate JD match score.

A pre-written `summaries` pool means no LLM ever generates summary prose.

### How a resume is built per JD

```
1. Score every bullet against the JD via sentence-transformers
2. Score every experience: best title alias × 0.30 + top-3 bullet avg × 0.70
3. Score every project:    name × 0.20 + topN bullet avg × 0.80 (N = bullets shown)
4. Select experiences: max 3, min 2, threshold 0.45, force-include 2
5. Select projects:    max 3, min 2, threshold 0.50, force-include 2
                       (projects section never hidden)
6. Select bullets: exactly 3 per experience; 2-3 per project
7. Pick best summary from pool (deterministic, by JD match — no LLM)
8. LLM picks title alias per experience (Pydantic-enforced from allow-list)
9. Deterministic scoring picks top-14 pool skill candidates; LLM names 3 categories
   and assigns skills; LLM picks Familiar With gaps (max 4); all 4 categories
   ordered by aggregate match
10. LLM writes cover letter (max 900 chars)
11. Section order: Summary → Work → [Skills/Projects by match] → Education → Certs
12. Assemble DOCX from template, convert to PDF
13. Upload to S3 at s3://{bucket}/resumes/applied/{job_id}_{timestamp}.pdf
```

---

## 6. Success Metrics

### Primary
- Time-to-application < 60 minutes from posting (target < 30 min on weekday peak)
- Application volume: 3 per peak run (8-11am IST), 1 per off-peak run
- Zero duplicate applications to the same job
- Zero fabricated content on any submitted resume
- Every applied resume accessible via S3 presigned URL (permanent audit trail)

### Secondary
- Recruiter response rate comparable to or better than a manual baseline
- System uptime 95%+ during a 90-day job hunt
- Operating cost $0/month, validated monthly via AWS billing dashboard
- Manual intervention rate under 20% of decisions
- CloudWatch APPLY_FAILURE alarm fires within 10 minutes of threshold breach

### Anti-metrics
- Pure application count (spam erodes signal)
- Match score inflation (gaming the threshold defeats the purpose)
- Resume length (focused beats padded)
- AWS bill > $0 (immediate alert; disable offending service)

---

## 7. User Stories

```
As Vishnujan, I want to:

1. Maintain ONE master_profile.yaml with everything I've done — all
   defensible bullets per role, with my own safe_title_aliases for each.

2. Sleep through the night and wake to a Telegram digest of jobs the bot
   applied to overnight, with clickable presigned S3 links to the exact
   PDF sent.

3. Be confident that every bullet on every submitted resume is one I wrote,
   every job title is on my safe_title_aliases list, and every skill in
   "Familiar With" is genuinely adjacent to my actual skills.

4. Have the bot apply only to the top-matching jobs per run (3 during the
   8-11am IST peak window, 1 otherwise) — never spray applications.

5. Have good-match jobs that didn't make the top-N stay queued for 12 hours,
   so they get a fair chance against the next batch instead of being lost.

6. Have the resume's skills section dynamically reflect what each JD wants —
   3 LLM-named categories with 3-5 skills each, plus Familiar With for gaps,
   all ordered by JD match.

7. See a weekly Sunday report telling me which skills appeared in 30%+ of
   relevant JDs so I know what to upskill.

8. Know about jobs the bot couldn't auto-apply to and have the pre-built
   resume PDF ready (S3 link in the digest) to upload manually.

9. Review and approve judgement-call question answers (Category 3) via
   Telegram, with approved answers saved to a bank for future reuse.

10. Open the applied table 6 months later, click any row, and see the exact
    PDF submitted via a presigned S3 URL — even if I've since edited my YAML.

11. Get a Telegram alert when APPLY_FAILURE rate spikes or Indeed session
    expires, triggered by CloudWatch alarms watching structured logs.

12. Run the system on free infrastructure (Neon, Oracle Cloud, Gemini free
    tier, AWS Free Tier S3/CloudWatch/IAM) indefinitely without paying.

13. Gain real AWS production experience — S3 for storage, CloudWatch for
    observability, IAM for credentials — by using these services to solve
    actual problems in the bot, not tutorial exercises.
```

---

## 8. Core Features

### 8.1 Scraping (Layer 2)
Indeed via JobSpy (Iteration 1+). Glassdoor in Iteration 4. Naukri in Iteration 6. Serial keyword rotation: one search term per run; short-circuit when ≥20 jobs pass hard filters. Hard filters at scrape time (years ≤ 5, location not in disallowed list, cooldown, duplicate).

### 8.2 JD Parsing (Layer 3)
Gemini 2.0 Flash + Instructor produces structured `JDParsed` output. spaCy validates extracted skills appear in JD text. Smart role acceptance via title pattern + Gemini category against per-search-term clusters.

### 8.3 Scoring & Selection (Layer 4)
Deterministic selection from the master profile. Composite `final_score` = fit × 0.55 + success_prob × 0.30 + recency × 0.10 + project × 0.05. Apply threshold 0.50.

**Cycle-based application picking:** At end of Layer 4 per run, combine new eligible jobs with the 12-hour-decay queue, sort by score, pick top N (3 during 8-11am IST peak, 1 otherwise). Unpicked eligible jobs go into the application queue.

### 8.4 Resume Building (Layer 5)
Selection logic is pure Python. Deterministic scoring picks top-14 pool skill candidates and scores all JD gap skills. One Gemini call (1b) returns title aliases, skills selection, and cover letter text. DOCX assembled via structural detection (paragraphs cloned to preserve styling, tab stops, hyperlinks). PDF converted via LibreOffice and uploaded to S3.

### 8.5 Application Submission (Layer 6)
Playwright drives Indeed Easy Apply. Resume downloaded from S3 to /tmp at form-upload time, deleted after. Multi-page forms handled atomically. Field types detected and filled.

Salary handling:
- Expected = JD upper bound if specified, else 6 LPA
- Current text fields = strategic redirect
- Current numeric fields = 100000

### 8.6 Question Handling (Layer 6)
Four-category classification: profile lookup, resume-derived, judgement (bank match or held for review in safe mode), legally-sensitive (manual required). An answer bank grows from user-approved responses.

### 8.7 State Management (Layer 7)
PostgreSQL on Neon free tier with pgvector for structured data and embeddings. AWS S3 for binary artifacts (DOCX/PDF resumes, cover letters). Permanent `all_jobs` archive, separate `applied` / `not_applied` tables with structured reason categories, `application_queue` for 12-hour decay, `master_bullets` never hard-deleted.

### 8.8 AWS Integration
- **S3** (Iteration 2+): versioned, IAM-restricted bucket for all submitted resumes and cover letters. Presigned URLs for Sheets/Telegram access. Never auto-deleted.
- **IAM** (Iteration 2+): minimal-permission user/role for runtime; credentials rotated quarterly.
- **CloudWatch** (Iteration 3+): structured log ingestion via watchtower; metric filters on APPLY_FAILURE / BUILD_FAILURE / MANUAL_REQUIRED; alarms route to Telegram.
- **SQS** (Iteration 6, conditional): scraper-to-worker decoupling only when multi-portal volume justifies.

### 8.9 Notifications (Layer 8)
Telegram morning digest with presigned S3 URLs, immediate critical alerts (via CloudWatch alarms from Iteration 3+), inline review requests for Category 3 questions.

### 8.10 Analytics (Layer 9)
Google Sheets live views (applied / skipped / manual-required) with presigned S3 URLs to submitted PDFs. Monthly Google Doc with Gemini-synthesised Sunday report.

---

## 9. Functional Requirements

### FR-1 Freshness
The system MUST detect new postings within 40 minutes during weekday peak and within 4 hours otherwise.

### FR-2 Deduplication
The system MUST never apply to the same `job_id` twice.

### FR-3 Cooldown
The system MUST NOT apply to the same company more than once within 10 days.

### FR-4 Interview integrity
The LLM MUST NOT write or rewrite any bullet. Bullets on a submitted resume MUST come verbatim from `master_profile.yaml`. A diff check MUST run before saving any built resume.

### FR-5 No fabrication
The system MUST NOT add skills, technologies, projects, or numbers to any LLM output unless they appear in master profile text. Post-generation validation MUST catch and regenerate violations.

### FR-6 Title integrity
Job titles on a built resume MUST come from that experience's `safe_title_aliases` list, enforced via Pydantic `Literal`.

### FR-7 Skills integrity
Skills shown in LLM-named categories MUST come from `master_profile.skills_pool`. Familiar With skills MUST come from JD-identified gaps. Post-validation enforces both.

### FR-8 No double submission
On submission failure, retry at most once. After two failures, mark `APPLY_FAILURE` and never retry.

### FR-9 Manual-required fallback
When auto-apply is impossible, save the built resume PDF to S3, log to `not_applied` with reason `MANUAL_REQUIRED` and specific detail, surface in Telegram digest with apply URL and presigned PDF link.

### FR-10 Audit trail
Every scraped job MUST be logged in `all_jobs` with structured parsed data. Every submitted resume MUST be stored in S3 (versioned, never deleted) and referenced from the `applied` table by S3 URI with full selection snapshot.

### FR-11 Safe mode default
Unknown Category 3 questions MUST NOT be auto-submitted in safe mode. Abandon application, store in `pending_review`, send Telegram alert.

### FR-12 Salary handling
Expected salary = `jd.salary_max_lpa` if specified, else 6 LPA. Current text = strategic redirect. Current numeric = 100000.

### FR-13 Minimum content guarantees
Every built resume MUST show at least 2 work experiences and at least 2 projects. Force-include the strongest if fewer pass thresholds. The projects section is never hidden.

### FR-14 Cycle-aware application picking
At end of each run's scoring, apply only to the top N jobs by `final_score` where N = 3 during 8-11am IST and 1 otherwise. Other eligible jobs queued.

### FR-15 Queue decay
Application queue entries expire 12 hours after `queued_at`. On expiry, move to `not_applied` with reason `STALE`.

### FR-16 No LinkedIn
The system MUST NOT scrape or interact with LinkedIn. Protects user's main account from ban risk.

### FR-17 Hyperlink preservation
The DOCX assembler MUST preserve all header hyperlinks unchanged. Project and certificate hyperlinks MUST be updated to URLs from master_profile while keeping visible link text unchanged.

### FR-18 S3 storage of artifacts
All resumes (DOCX + PDF) and cover letters generated by the system MUST be uploaded to S3 with versioning enabled. The `applied` table stores S3 URIs, not local paths. Presigned URLs (7-day expiry) are generated on demand.

### FR-19 CloudWatch alarms
From Iteration 3+, structured logs MUST be shipped to CloudWatch. Alarms MUST be configured for:
- APPLY_FAILURE rate > 3 in 24h
- session_expired event count > 0 in 1h
- Each alarm MUST route to Telegram within 10 minutes.

### FR-20 IAM minimal permissions
AWS credentials used at runtime MUST have only the permissions required (S3 PutObject/GetObject/DeleteObject on the bot's bucket, CloudWatch logs/metrics on the bot's namespace). No bucket-level operations, no IAM operations, no budget/billing operations, no other AWS service access.

### FR-21 Billing guardrails (mandatory before any AWS resource creation)
The following MUST exist in the AWS account before the first S3 bucket, CloudWatch log group, or IAM runtime user is created:
- AWS Budget at **$0.01/month**, monthly, notify on actual or forecasted breach → email + Telegram (any-charge tripwire)
- AWS Budget at **$1/month** with a **Budget Action** that detaches `job-bot-runtime-policy` from the `job-bot-runtime` user
- CloudWatch billing alarm at **$0.50/month** in `us-east-1` (the only region where billing metrics publish) → SNS → tiny Lambda → Telegram
- Cost Explorer enabled

The Budget Action MUST execute under a SEPARATE IAM role (`job-bot-budgets`) with only `budgets:*` on the bot's budgets and `iam:DetachUserPolicy` scoped to the runtime user/policy. The runtime user MUST NOT have permission to modify, delete, or disable any budget, alarm, or IAM resource — so it cannot tamper with its own kill switch.

Verification: before declaring AWS setup complete, manually trigger the $0.01 budget alert path and confirm Telegram delivery within 10 minutes.

---

## 10. Non-Functional Requirements

### NFR-1 Cost
Total operating cost target is $0/month. Honest caveat: AWS S3's 5GB free tier is 12-month, not always-free; after AWS account turns 1 year old, S3 storage is projected at ~$0.30/year for the bot's ~1GB footprint. Hard cap: $1/month, enforced by an AWS Budget Action that auto-detaches the runtime IAM policy if breached. The bot stops itself before any meaningful charge accrues. If a budget fires, root cause is investigated; the cap is NEVER raised to keep the bot running.

### NFR-2 Privacy
Credentials MUST NEVER be stored in plaintext in code. AWS keys live in `.env` only. Re-authentication for portals is manual.

### NFR-3 Resilience
Handle Neon outages (buffer to local jsonl), Gemini rate limits (exponential backoff), JobSpy breakages (portal_health alert), session expiry (skip portal, alert), S3 outages (buffer to `data/pending_uploads/`, drain next run), CloudWatch outages (log locally, ship next run — non-blocking).

### NFR-4 Maintainability
Each layer independently testable. Layer 4 selection algorithm is pure functions. Configuration values live in `config.yaml`, not hardcoded.

### NFR-5 Observability
Every Gemini call, application attempt, and failure MUST be logged with structured events including job_id, event name, latency, outcome. Logs shipped to CloudWatch from Iteration 3+.

### NFR-6 Performance
A single cron run MUST complete in under 5 minutes for typical batches. A single resume build MUST complete in roughly 5 seconds (including S3 upload).

### NFR-7 Debuggability
CLI commands MUST exist for inspecting per-job pipeline state, dry-running, and re-parsing the master profile on demand.

### NFR-8 Changelog
The repo MUST contain `CHANGELOG.md` initialized at Iteration 0 and updated on every code-affecting change. Before any push, `[Unreleased]` MUST be converted to a dated iteration entry. See CLAUDE.md.

### NFR-9 AWS region locality
All AWS resources MUST be in `ap-south-1` (Mumbai) for minimum latency from Oracle Cloud Mumbai VM and Indian portals.

---

## 11. Out of Scope

- LinkedIn entirely (account-ban risk)
- Multi-user / SaaS deployment
- Web dashboard (Iteration 7 may add an MCP server)
- Email parsing or auto-reply
- Interview scheduling
- Salary negotiation guidance
- Resume A/B testing
- Real-time push alerts
- Naukri and Internshala (Iteration 6)
- Anti-detection beyond basic measures (Iteration 6 if needed)
- Cover letter caching (single-use)
- Resume caching (every JD = fresh build)
- Any rewriting of bullet content
- Paid AWS services (RDS, Lambda runtime, ECS/Fargate, EKS)
- Multi-region AWS deployment

---

## 12. Constraints & Assumptions

### Constraints
- Free-tier limits: Gemini 1500 calls/day, Neon 3GB, Oracle Cloud 200GB disk, AWS Free Tier (S3 5GB, CloudWatch 5GB ingest/month, SQS 1M req/month), GitHub Actions 2000 min/month (Iterations 1-4 only)
- Indeed may eventually detect bot traffic — may require residential proxy in Iteration 6
- JobSpy is community-maintained, may break
- AWS Free Tier has 12-month limits on some services (EC2, RDS) — we use only forever-free services
- Indian job market context (LPA salaries, Indian portals, IST timezone)

### Assumptions
- User maintains complete, honest `master_profile.yaml` with defensible bullets, accurate `safe_title_aliases`, comprehensive `skills_pool`
- User logs in manually once per portal at setup
- User responds to Telegram review requests within reasonable time
- Gemini 2.0 Flash free tier remains available
- DOCX template remains structurally stable
- AWS account in good standing; billing alerts configured at $1
- Oracle VM in Mumbai region (low latency to AWS ap-south-1)

---

## 13. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Indeed blocks JobSpy IPs | High | Iteration 6 residential proxy; Glassdoor partial backup |
| Gemini fabricates content | High | spaCy validation, post-gen regex, regenerate up to 2x |
| Resume modifies outside allowed regions | High | Diff check rejects before save |
| LLM picks unauthorized title | High | Pydantic Literal from safe_title_aliases |
| LLM picks skill not in candidates | High | Post-validation rejects, regenerate, BUILD_FAILURE |
| AWS credentials leak | High | `.env` gitignored; rotate quarterly; minimal IAM permissions |
| AWS bill surprise | High | Layered: $0.01 tripwire (alert), $0.50 alarm (warning), $1 Budget Action auto-detaches runtime IAM policy (hard stop). Bot physically cannot continue spending past $1 |
| Runtime user tampers with kill switch | Medium | Budget Action lives on separate `job-bot-budgets` role; runtime user has zero `iam:*`, `budgets:*`, or `cloudwatch:*alarm*` permissions |
| S3 bucket misconfigured public | High | Block public access enforced; bucket policy denies public |
| Same job applied twice across portals | Medium | Strict job_id dedup |
| Session cookies expire silently | Medium | CloudWatch alarm + Telegram |
| Free tier limits exceeded | Medium | Conservative call budget (~600/day expected) |
| master_profile has undefendable bullet | Medium | User responsibility |
| User misses Telegram review | Low | Job stays in pending_review indefinitely |
| pgvector slow on large bullet pool | Low | IVFFlat index; queries under 50ms |
| LLM voice sounds robotic | Medium | Few-shot from approved bank, banned-pattern checks |
| Application queue grows unbounded | Low | 12-hour expiry, Sunday cleanup |
| DOCX template structure changes | Medium | Assembler reads structure dynamically; document expectations |
| Embedded hyperlinks broken on clone | High | Explicit r:id target update; tested every build |
| S3 region outage | Medium | Buffer to `data/pending_uploads/`, drain on recovery |
| CloudWatch unavailable | Low | Local logs continue; ship when service returns |

---

## 14. Approval & Sign-Off

- **Architecture:** locked, see `job_automation_architecture.md`
- **Build status:** Iteration 0 complete; Iteration 0.1 (AWS prep) pending
- **Owner approval:** Vishnujan Narayanan (single-user product)

---

**End of PRD.**
