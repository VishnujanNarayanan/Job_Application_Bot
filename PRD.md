# Product Requirements Document — Job Application Automation Bot

**Owner:** Vishnujan Narayanan
**Build status:** Iterations 0-1 complete. Design pivoted before Iteration 2 (see Migration Note).

---

## Migration Note (read first)

After Iteration 1, the product pivoted from **auto-applying** to **assisting manual application**. The bot no longer submits applications. Instead, for every good match it builds a tailored resume and notifies the user with the apply link and resume link bundled together; the user applies themselves.

```
OLD                              NEW
Bot auto-applies via Playwright  →  User applies manually; bot delivers
                                    a tailored resume per match
Stores rendered PDFs in S3       →  Stores tiny selection (~2 KB) in
                                    Postgres; renders PDF/DOCX on demand
No LinkedIn (account-ban risk)   →  LinkedIn back as listings source only
                                    (no login/actions on user's account)
Cycle quotas (3 peak / 1 off)    →  Notify on every match >= 0.50
Form questions, answer bank,     →  Removed (no form filling at all)
salary auto-fill, Gemini Call 2
```

Iterations 0-1 built stubs against the OLD design (including a Playwright sender). Iteration 2 begins with cleanup (remove the obsolete stubs and tables) then builds the new flow. This PRD describes the **target product**; the architecture doc carries the detailed cleanup list.

---

## 1. Problem Statement

Job applications in early-career roles are a numbers game with a freshness gate. Recruiters review in arrival order and stop after the first ~50 candidates. Applying within an hour puts you in the first-look bracket.

A single applicant monitoring Indeed, Glassdoor, and LinkedIn — while tailoring a resume per role — cannot realistically hit that window across the 5-10 relevant listings posted daily. The hard, valuable part is producing a genuinely tailored, defensible resume fast. Submitting the application is the easy part the user can do themselves in two taps.

---

## 2. Goal

Take the job market and hand the user a perfectly tailored resume per good match, making applying a two-tap action:

- Detect new postings within ~10 minutes
- Score them with a multi-factor relevance model
- Build a custom-tailored resume (compact selection) per matched JD
- Notify per match with apply link + resume link bundled
- Render the resume PDF/DOCX on demand
- Keep a permanent, queryable map of every resume to its exact job
- Cost $0/month, run while the user sleeps

**The core deliverable is the JD → tailored-resume engine.**

---

## 3. Non-Goals

- Auto-applying or submitting applications (removed in the pivot)
- Filling forms, answering screening questions, handling CAPTCHAs
- Logging into or acting on the user's LinkedIn (or any) account
- Writing or rewriting bullet content — the LLM only selects from a user-written pool
- Fabricating skills, projects, achievements
- Using job titles outside `safe_title_aliases`
- Replacing networking, referrals, or interview prep
- Investment, legal, or career counselling advice
- Paid AWS services (RDS, Lambda runtime, ECS/Fargate, EKS)

---

## 4. Target User

Single operator per instance (currently Vishnujan), an Indian early-career developer (1.5 years), targeting fulltime roles across backend, data, ml, fullstack, devops, finance_market dev, quant.

**Instance-ready, not multi-tenant.** The system runs as a single-operator tool, but nothing is hardcoded to one person — all operator identity lives in `config.yaml`, `.env`, and `master_profile.yaml`. A different person clones the repo, fills their own files and accounts, and runs their own fully independent instance with no code changes. True multi-tenant SaaS (one system serving many users with auth and isolation) is explicitly out of scope. See NFR-11.

---

## 5. The Master Profile Model

`master_profile.yaml` (gitignored) is the single source of truth: `personal`, `summaries` (pool, tagged by role category), `work_experience` (each with `bullet_pool` + `safe_title_aliases`), `projects` (each with `bullet_pool` + `link`), flat `skills_pool`, `education`, `certifications`.

Per JD, deterministic scoring picks top-14 skill candidates; the LLM names 3 categories with 3-5 skills each plus up to 4 Familiar With gaps; all 4 categories order by aggregate match. Summaries are selected deterministically from the pool. The LLM never writes bullets.

### How a resume is produced per JD

```
1. Score every bullet against the JD (sentence-transformers)
2. Score experiences (alias × 0.30 + top-3 bullet avg × 0.70)
3. Score projects (name × 0.20 + topN bullet avg × 0.80)
4. Select experiences (max 3, min 2, threshold 0.45, force-include 2)
5. Select projects (max 3, min 2, threshold 0.50, force-include 2; never hidden)
6. Select bullets (exactly 3 per experience; 2-3 per project)
7. Pick summary from pool (deterministic)
8. LLM picks title aliases (Pydantic-enforced), skill categories, cover letter
9. Section order: Summary → Work → [Skills/Projects by match] → Education → Certs
10. Write the result as selection_json (~2 KB) — NO PDF stored
11. PDF/DOCX rendered on demand from the endpoint when the user clicks
```

---

## 6. Success Metrics

### Primary
- Time-to-notification < 60 min from posting (target < 30 min weekday peak)
- A notification per match >= 0.50, each with working apply + resume links
- Zero fabricated content on any resume
- Every match maps unambiguously to its resume (via the Sheet + endpoint)
- Storage stays flat (selections are ~2 KB; no PDF pile)

### Secondary
- Resume render < 6s cold, instant cached
- Uptime 95%+ during a 90-day hunt
- $0/month, validated monthly
- Near-duplicate detection prevents repeat notifications for the same role

### Anti-metrics
- Notification spam from duplicates (deduped at >0.95)
- Match-score inflation
- Resume length
- AWS bill > $0

---

## 7. User Stories

```
1. Maintain ONE master_profile.yaml with all defensible bullets and my
   own safe_title_aliases per role.

2. Get a Telegram message per good match with the apply link and a resume
   link side by side, so applying is two taps: open listing, open resume.

3. Never confuse which resume goes with which job — the message bundles
   them, and a Sheet maps every match to its resume and apply link.

4. Never have a pile of resume files to manage — the system stores a tiny
   selection per job and renders the PDF/DOCX only when I click.

5. Improve my template once and have every past resume link reflect the
   improvement automatically (because they re-render from selection).

6. Get a resume in PDF (to submit) or DOCX (to tweak first) from the same
   notification.

7. Have LinkedIn listings included again (safe now — the bot only reads
   public listings, never touches my account).

8. See only genuinely distinct jobs — cross-portal reposts and reworded
   duplicates get deduped, not double-notified.

9. Trust every bullet, title, and skill on every resume is defensible and
   drawn from what I wrote.

10. Open the record months later and see exactly which resume I used for
    which job, even after editing my profile.

11. Get a weekly report on which skills appear most in relevant JDs.

12. Run it all on free infrastructure (Neon, Oracle Cloud, Gemini free
    tier, AWS Free Tier S3/CloudWatch/IAM) indefinitely.

13. Gain real AWS experience — S3 for cache+backup, CloudWatch for
    observability, IAM for least-privilege credentials.
```

---

## 8. Core Features

### 8.1 Scraping (Layer 2)
Indeed, Glassdoor, LinkedIn via JobSpy — **listings only**. Naukri in Iteration 6. Serial keyword rotation, short-circuit at ≥20 passing. Hard filters (years ≤ 5, disallowed regions, cooldown, duplicate). Near-duplicate detection (>0.95 cosine) prevents repeat notifications.

### 8.2 JD Parsing (Layer 3)
Gemini 2.0 Flash + Instructor → structured `JDParsed` (incl. apply_url and salary range). spaCy validates skills. Smart role acceptance via clusters.

### 8.3 Scoring & Selection (Layer 4)
Deterministic selection from the master profile. `final_score` = fit × 0.55 + success_prob × 0.30 + recency × 0.10 + project × 0.05. Threshold 0.50. Every match builds a selection and notifies — no quotas.

### 8.4 Resume Building (Layer 5)
One Gemini call (1b): title aliases (Pydantic-enforced), skills selection (top-14 candidates → 3 LLM-named categories + Familiar With gaps, all ordered by match), cover letter. Output is `selection_json` written to the `applied` table. **No PDF is rendered or stored here.**

### 8.5 Application Assist (Layer 6)
Replaces the old auto-apply sender. Two parts: (a) per-match Telegram notification bundling apply + resume links; (b) a FastAPI endpoint that renders the resume PDF/DOCX on demand from `selection_json` + the current template, caching the render in S3 for 1 month. No Playwright, no form filling, no submission.

### 8.6 State (Layer 7)
Postgres on Neon (structured data, embeddings, selections). S3 for the render cache (survives VM restarts) and daily selection_json backup. `applied` stores selection_json + scores + user_status; `render_cache` tracks cached files. Bullets never hard-deleted.

### 8.7 AWS
- S3 (Iteration 2+): render cache (1-month TTL) + selection backup
- IAM (Iteration 2+): minimal permissions
- CloudWatch (Iteration 3+): endpoint + pipeline observability, alarms → Telegram
- SQS (Iteration 6, conditional)

### 8.8 Notifications (Layer 8)
Per-match Telegram message with score, role/company/location/salary/source, [Apply] [PDF] [DOCX] buttons, why-it-matched, gap skills. Optional mark-applied/skip buttons updating `user_status`. CloudWatch alarms route to Telegram from Iteration 3+.

### 8.9 Analytics (Layer 9)
Sheets as the master index: every match mapped to apply link, PDF link, DOCX link, status. Monthly Google Doc Sunday report.

---

## 9. Functional Requirements

### FR-1 Freshness
Detect new postings within 40 min weekday peak, 4 h otherwise.

### FR-2 Deduplication
Never notify twice for the same `job_id`. Additionally, never notify for a JD whose embedding is > 0.95 cosine to an already-notified job (near-duplicate); record the link to the original.

### FR-3 Cooldown
Never build/notify for the same company more than once within 10 days.

### FR-4 Interview integrity
The LLM never writes or rewrites a bullet. Resume bullets come verbatim from `master_profile.yaml`. A diff check runs on every render; changes outside permitted regions fail.

### FR-5 No fabrication
No skills, tech, projects, or numbers in LLM output unless present in master profile text. Post-generation validation regenerates violations.

### FR-6 Title integrity
Resume titles come only from that experience's `safe_title_aliases`, enforced via Pydantic `Literal`.

### FR-7 Skills integrity
LLM-named-category skills come from `skills_pool`; Familiar With skills come from JD-identified gaps. Post-validation enforces both.

### FR-8 Manual application
The system does NOT submit applications. It delivers a tailored resume and the apply link; the user applies. No form filling, no automated action on any account.

### FR-9 Resume on demand
Resumes are rendered on demand from `selection_json` by the endpoint, in PDF or DOCX. Renders are cached in S3 for 1 month. No resume PDFs are stored at build time.

### FR-10 Audit trail / linkage
Every scraped job is in `all_jobs`. Every match stores `selection_json` in `applied`. The Telegram message bundles apply + resume links; the Sheet maps every match to its resume and apply link. The mapping must resolve months later, even after profile edits, via `template_version`-aware re-render.

### FR-11 Template propagation
Improving the resume template re-renders past resumes against the current template on next access (template_version mismatch triggers re-render).

### FR-12 Salary (informational)
Expected salary = JD upper bound if specified else 6 LPA, used only in cover letter text and the notification — never auto-filled on a form.

### FR-13 Minimum content guarantees
Every resume shows ≥2 experiences and ≥2 projects; force-include the strongest if fewer pass. Projects section never hidden.

### FR-14 No quotas
Every match >= 0.50 produces a resume and a notification. No top-N rationing (that existed only to ration auto-applications).

### FR-15 LinkedIn listings only
LinkedIn may be scraped for public listings via JobSpy. The system MUST NOT log into or perform any action on the user's LinkedIn account.

### FR-16 Hyperlink preservation
The assembler preserves header hyperlinks unchanged; updates project/cert hyperlink targets to master_profile URLs while keeping visible link text unchanged.

### FR-17 S3 cache + backup
Rendered PDFs/DOCX are cached in S3 with 1-month expiry (survives VM restarts). The `applied` selections are backed up to S3 daily.

### FR-18 CloudWatch alarms
From Iteration 3+, structured logs ship to CloudWatch; alarms (endpoint errors, BUILD_FAILURE, scraper zero-results) route to Telegram within 10 minutes.

### FR-19 IAM minimal permissions
Runtime credentials limited to S3 object ops on the bot's bucket and CloudWatch logs/metrics on the bot's namespace. No bucket-level or other-service access.

---

## 10. Non-Functional Requirements

### NFR-1 Cost
$0/month. Every service free-tier with no paid graduation. AWS billing alarm at $1; if it fires, disable the offending service.

### NFR-2 Privacy
No plaintext credentials in code. AWS keys and DB URL in `.env` only.

### NFR-3 Resilience
Handle Neon outages (buffer locally), Gemini rate limits (backoff), JobSpy breakage (portal_health alert), scraper IP throttling (fall back to other sources), S3 outages (render without caching), CloudWatch outages (local logs, ship later).

### NFR-4 Maintainability
Each layer independently testable. Layer 4 selection is pure functions. All tunables in `config.yaml`.

### NFR-5 Observability
Every Gemini call, render, and failure logged with structured events. Shipped to CloudWatch from Iteration 3+.

### NFR-6 Performance
A cron run completes < 5 min for typical batches. A resume render completes < 6s cold, instant cached.

### NFR-7 Debuggability
CLI commands for per-job inspection, dry-run, master profile re-parse, force re-render, AWS connectivity check.

### NFR-8 Changelog
`CHANGELOG.md` initialized at Iteration 0, updated on every code-affecting change. Before any push, `[Unreleased]` becomes a dated iteration entry. See CLAUDE.md.

### NFR-9 AWS region locality
All AWS resources in `ap-south-1` (Mumbai).

### NFR-10 Endpoint availability
From Iteration 5, the resume endpoint runs as a managed service (systemd) behind HTTPS so Telegram resume links are clickable and secure.

### NFR-11 Instance-readiness
The system MUST be runnable by a different person as an independent instance with zero source-code edits. All operator identity (name, filenames, regions, preferences, secrets, career data, template) MUST come from `config.yaml`, `.env`, `master_profile.yaml`, and `resumes/templates/`. No operator-specific literal (name, email, filename) may appear in source code. Resume/cover filenames MUST be derived from `operator.full_name` in config. The README MUST document operator-agnostic setup. Multi-tenant SaaS (auth, user accounts, per-user data isolation, shared infrastructure) is explicitly OUT OF SCOPE — do not add `user_id` columns, authentication, or tenancy.

---

## 11. Out of Scope

- Auto-applying / form submission (removed)
- Any automated action on the user's portal accounts
- Multi-tenant SaaS (auth, user accounts, per-user isolation, shared infra) — but the system IS instance-ready: a new operator clones and runs their own copy without code changes (NFR-11)
- Email parsing or auto-reply
- Interview scheduling, salary negotiation
- Resume A/B testing
- Naukri (Iteration 6)
- Cover letter caching (single-use)
- Resume PDF storage at build time (rendered on demand instead)
- Bullet rewriting
- Paid AWS services

---

## 12. Constraints & Assumptions

### Constraints
- Free-tier limits: Gemini 1500 calls/day, Neon 3GB, Oracle Cloud 200GB, AWS Free Tier (S3 5GB, CloudWatch 5GB/month, SQS 1M/month)
- JobSpy is community-maintained; LinkedIn listing scraping may throttle the scraper IP (recoverable; account never at risk)
- Indian market context (LPA, IST, Indian portals)
- AWS Free Tier 12-month limits avoided (only forever-free services used)

### Assumptions
- User maintains a complete, honest `master_profile.yaml`
- User applies manually through the provided links
- Gemini 2.0 Flash free tier remains available
- DOCX template structurally stable
- AWS account in good standing; billing alarm at $1
- Oracle VM in Mumbai (low latency to ap-south-1)

---

## 13. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| LinkedIn throttles scraper IP | Medium | Fall back to Indeed/Glassdoor; Iteration 6 proxy. Account never touched. |
| Gemini fabricates content | High | spaCy validation, post-gen checks, regenerate up to 2x |
| Resume modifies outside allowed regions | High | Diff check on every render |
| LLM picks unauthorized title | High | Pydantic Literal from safe_title_aliases |
| LLM picks skill not in candidates | High | Post-validation, regenerate, BUILD_FAILURE |
| Endpoint down → resume links dead | High | systemd auto-restart; CloudWatch alarm; S3 cache serves recent renders |
| AWS credentials leak | High | `.env` gitignored; rotate quarterly; minimal IAM |
| AWS bill surprise | High | Billing alarm at $1; disable offending service |
| S3 bucket public misconfig | High | Block public access; private bucket |
| Near-duplicate threshold too loose | Medium | Strict 0.95; tune during dry-run |
| Template change breaks assembler | Medium | Document expected structure; diff check catches bad renders |
| Embedded hyperlinks broken on clone | High | Explicit r:id update; tested every render |
| Neon down | Medium | Buffer to local jsonl, drain next run |
| LLM voice robotic (cover letter) | Medium | Banned-pattern checks; few-shot |
| Storage somehow grows | Low | Selections ~2 KB; S3 cache 1-month lifecycle |

---

## 14. Approval & Sign-Off

- **Architecture:** locked, see `job_automation_architecture.md`
- **Build status:** Iterations 0-1 complete; Iteration 2 begins with pivot cleanup
- **Owner:** Vishnujan Narayanan (single-user product)

---

**End of PRD.**
