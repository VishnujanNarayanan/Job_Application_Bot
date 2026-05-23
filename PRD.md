# Product Requirements Document — Job Application Automation Bot

**Owner:** Vishnujan Narayanan
**Status:** Approved for build

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
- Logs structured outcomes for every scraped job
- Keeps every submitted resume permanently for audit and interview prep
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
9. Deterministic scoring picks top-14 pool skill candidates; LLM names 3 categories and assigns skills; LLM picks Familiar With gaps (max 4); all 4 categories ordered by aggregate match
10. LLM writes cover letter (max 900 chars)
11. Section order: Summary → Work → [Skills/Projects by match] → Education → Certs
12. Assemble DOCX from template, convert to PDF
13. Save permanently to resumes/applied/{job_id}_{timestamp}.pdf
```

---

## 6. Success Metrics

### Primary
- Time-to-application < 60 minutes from posting (target < 30 min on weekday peak)
- Application volume: 3 per peak run (8-11am IST), 1 per off-peak run
- Zero duplicate applications to the same job
- Zero fabricated content on any submitted resume (enforced by bullet origin + Pydantic constraints)
- Every applied resume retrievable via a Sheets link (permanent audit trail)

### Secondary
- Recruiter response rate comparable to or better than a manual baseline
- System uptime 95%+ during a 90-day job hunt
- Operating cost $0/month, validated monthly
- Manual intervention rate under 20% of decisions

### Anti-metrics
- Pure application count (spam erodes signal)
- Match score inflation (gaming the threshold defeats the purpose)
- Resume length (focused beats padded)

---

## 7. User Stories

```
As Vishnujan, I want to:

1. Maintain ONE master_profile.yaml with everything I've done — all
   defensible bullets per role, with my own safe_title_aliases for each.

2. Sleep through the night and wake to a Telegram digest of jobs the bot
   applied to overnight, with clickable links to the exact PDF sent.

3. Be confident that every bullet on every submitted resume is one I wrote,
   every job title is on my safe_title_aliases list, and every skill in
   "Familiar With" is genuinely adjacent to my actual skills.

4. Have the bot apply only to the top-matching jobs per run (3 during the
   8-11am IST peak window, 1 otherwise) — never spray applications.

5. Have good-match jobs that didn't make the top-N stay queued for 12 hours,
   so they get a fair chance against the next batch instead of being lost.

6. Have the resume's skills section dynamically reflect what each JD wants —
   3 LLM-named categories with 3-5 skills each, plus Familiar With for gaps,
   all ordered by JD match (Familiar With ranks up or down based on how
   strongly the JD needs skills I don't have).

7. See a weekly Sunday report telling me which skills appeared in 30%+ of
   relevant JDs so I know what to upskill.

8. Know about jobs the bot couldn't auto-apply to and have the pre-built
   resume PDF ready to upload manually.

9. Review and approve judgement-call question answers (Category 3) via
   Telegram, with approved answers saved to a bank for future reuse.

10. Open the applied table 6 months later, click any row, and see the exact
    PDF submitted — even if I've since edited my YAML.

11. Run the system on free infrastructure (Neon, Oracle Cloud, Gemini free
    tier) indefinitely without paying.
```

---

## 8. Core Features

### 8.1 Scraping (Layer 2)
Indeed and Glassdoor via JobSpy. Naukri added in Iteration 6. Serial keyword rotation: one search term per run; short-circuit when ≥20 jobs pass hard filters. Rotation state persists across runs. Hard filters at scrape time (years ≤ 5, location not in disallowed list, cooldown, duplicate).

### 8.2 JD Parsing (Layer 3)
Gemini 2.0 Flash + Instructor produces structured `JDParsed` output (skills, responsibilities, role context, salary range, metadata). spaCy validates that extracted skills appear in JD text. Smart role acceptance via title pattern matching + Gemini category against per-search-term acceptance clusters.

### 8.3 Scoring & Selection (Layer 4)
Deterministic selection from the master profile. Experience score = best alias × 0.30 + top-3 bullet avg × 0.70. Project score = name × 0.20 + topN bullet avg × 0.80. Composite `final_score` = fit × 0.55 + success_prob × 0.30 + recency × 0.10 + project × 0.05. Apply threshold 0.50.

**Cycle-based application picking:** At end of Layer 4 per run, combine new eligible jobs with the 12-hour-decay queue, sort by score, pick top N (3 during 8-11am IST peak, 1 otherwise). Unpicked eligible jobs go into the application queue.

### 8.4 Resume Building (Layer 5)
Selection logic is pure Python. Deterministic scoring picks top-14 pool skill candidates and scores all JD gap skills. One Gemini call (1b) returns title aliases (Pydantic-enforced from allow-lists), skills selection (3 LLM-named categories with 3-5 skills each from candidates + 0-4 Familiar With gap skills, total 10-14 pool + 0-4 gaps, all 4 categories ordered by aggregate match), and cover letter text. The DOCX is assembled from the user's template via structural detection — paragraphs are cloned to preserve all styling (tab stops, spacing, fonts, list references, embedded hyperlinks). Project and certificate hyperlinks are updated by modifying relationship file targets while keeping visible link text unchanged.

### 8.5 Application Submission (Layer 6)
Playwright drives Indeed Easy Apply. Multi-page forms handled atomically (no partial submits). Field types detected and filled (profile, salary, upload, questions). Resume always uploaded as `Vishnujan_Narayanan_Resume.pdf`.

Salary handling:
- Expected = JD upper bound if specified, else 6 LPA default
- Current text fields = strategic redirect (no number disclosed)
- Current numeric fields = 100000 (honest)

### 8.6 Question Handling (Layer 6)
Four-category classification: profile lookup, resume-derived, judgement (bank match or held for review in safe mode), and legally-sensitive (manual required). An answer bank grows from user-approved responses. Generated content passes anti-AI voice constraints and a fabrication check against master_profile text.

### 8.7 State Management (Layer 7)
PostgreSQL on Neon free tier with pgvector. Permanent `all_jobs` archive, separate `applied` / `not_applied` tables with 13 structured reason categories, `application_queue` for the 12-hour decay, and a `master_bullets` table where bullets are never hard-deleted (only deactivated) to keep audit trail intact.

### 8.8 Notifications (Layer 8)
Telegram morning digest, immediate critical alerts, inline review requests for Category 3 questions.

### 8.9 Analytics (Layer 9)
Google Sheets live views (applied / skipped / manual-required) with clickable links to exact submitted PDFs. Monthly Google Doc with Gemini-synthesised Sunday report covering skill demand, recurring gaps, and patterns.

---

## 9. Functional Requirements

### FR-1 Freshness
The system MUST detect new postings within 40 minutes during weekday peak and within 4 hours otherwise.

### FR-2 Deduplication
The system MUST never apply to the same `job_id` twice.

### FR-3 Cooldown
The system MUST NOT apply to the same company more than once within 10 days.

### FR-4 Interview integrity
The LLM MUST NOT write or rewrite any bullet. Bullets on a submitted resume MUST come verbatim from the user's `master_profile.yaml`. A diff check MUST run before saving any built resume; any change outside permitted regions MUST cause rejection.

### FR-5 No fabrication
The system MUST NOT add skills, technologies, projects, or numbers to any LLM output unless they appear in master profile text. Post-generation validation MUST catch and regenerate violations.

### FR-6 Title integrity
Job titles on a built resume MUST come from that experience's `safe_title_aliases` list, enforced at the API level via a Pydantic `Literal` constraint.

### FR-7 Skills integrity
Skills shown in LLM-named categories on a built resume MUST come from `master_profile.skills_pool`. Skills shown in Familiar With MUST come from JD-identified gaps (skills required by the JD that are NOT in the pool). Post-validation MUST verify both source-set memberships and reject/regenerate on violation.

### FR-8 No double submission
On submission failure, the system MUST retry at most once. After two failures, the job is marked `APPLY_FAILURE` and never retried.

### FR-9 Manual-required fallback
When auto-apply is impossible, the system MUST save the built resume PDF, log to `not_applied` with reason `MANUAL_REQUIRED` and a specific detail, and surface it in the next Telegram digest with the apply URL and resume path.

### FR-10 Audit trail
Every scraped job MUST be logged in `all_jobs` with structured parsed data. Every submitted resume MUST be saved permanently in `resumes/applied/` and referenced from the `applied` table along with a full selection snapshot.

### FR-11 Safe mode default
Unknown Category 3 questions MUST NOT be auto-submitted in safe mode. The application MUST be abandoned, the question stored in `pending_review`, and a Telegram alert sent.

### FR-12 Salary handling
Expected salary fields use `jd.salary_max_lpa` if specified, else 6 LPA default. Current salary text fields use a strategic redirect with no number. Current salary numeric fields use the honest value 100000.

### FR-13 Minimum content guarantees
Every built resume MUST show at least 2 work experiences and at least 2 projects. If fewer pass their thresholds, the strongest are force-included. The projects section is never hidden.

### FR-14 Cycle-aware application picking
At end of each run's scoring, the system MUST apply only to the top N jobs by `final_score` where N = 3 during 8-11am IST and 1 otherwise. Other eligible jobs are queued.

### FR-15 Queue decay
The application queue MUST expire entries 12 hours after their `queued_at` timestamp. On expiry, the entry is moved to `not_applied` with reason `STALE`.

### FR-16 No LinkedIn
The system MUST NOT scrape or interact with LinkedIn. This protects the user's main account from any ban risk.

### FR-17 Hyperlink preservation
The DOCX assembler MUST preserve all header hyperlinks unchanged. Project and certificate hyperlinks MUST be updated to point to the URLs in master_profile while keeping the visible link text unchanged.

---

## 10. Non-Functional Requirements

### NFR-1 Cost
Total operating cost MUST be $0/month. Every service used MUST be on a free tier with no paid graduation required.

### NFR-2 Privacy
Credentials MUST NEVER be stored in plaintext. Only session cookies persist. Re-authentication is manual.

### NFR-3 Resilience
The system MUST handle Neon outages (buffer to local jsonl, drain next run), Gemini rate limits (exponential backoff), JobSpy breakages (portal_health tracking + alert), and mid-flow session expiry (skip portal, alert).

### NFR-4 Maintainability
Each layer MUST be independently testable. Layer 4's selection algorithm MUST be pure functions with no I/O. Configuration values (thresholds, weights, region lists) MUST live in `config.yaml`, not hardcoded.

### NFR-5 Observability
Every Gemini call, application attempt, and failure MUST be logged with structured events including job_id, event name, latency, and outcome.

### NFR-6 Performance
A single cron run MUST complete in under 5 minutes for typical batches. A single resume build MUST complete in roughly 5 seconds.

### NFR-7 Debuggability
CLI commands MUST exist for inspecting per-job pipeline state, dry-running individual layers, and re-parsing the master profile on demand.

### NFR-8 Changelog
The repo MUST contain a `CHANGELOG.md` initialized at Iteration 0 and updated on every code-affecting change. Before any push to git, the `[Unreleased]` section MUST be converted to a dated iteration entry. See CLAUDE.md for the full discipline.

---

## 11. Out of Scope

- LinkedIn entirely (account-ban risk)
- Multi-user / SaaS deployment
- A web dashboard (Iteration 7 may add an MCP server)
- Email parsing or auto-reply
- Interview scheduling
- Salary negotiation guidance
- Resume A/B testing
- Real-time push alerts
- Naukri and Internshala (Iteration 6)
- Anti-detection beyond basic measures (Iteration 6 if needed)
- Cover letter caching (each is single-use)
- Resume caching (every JD gets a fresh build)
- Any rewriting of bullet content (interview-safety constraint)

---

## 12. Constraints & Assumptions

### Constraints
- Free-tier limits: Gemini 1500 calls/day, Neon 3GB, Oracle Cloud 200GB disk, GitHub Actions 2000 min/month (Iterations 1-4 only)
- Indeed may eventually detect bot traffic — may require a residential proxy in Iteration 6
- JobSpy is community-maintained and may break when portals change HTML
- Indian job market context (LPA salaries, Indian portals, IST timezone)

### Assumptions
- The user maintains a complete, honest `master_profile.yaml` with defensible bullets, accurate `safe_title_aliases`, and a comprehensive `skills_pool`
- The user logs in manually once per portal at setup
- The user responds to Telegram review requests within a reasonable time
- Gemini 2.0 Flash free tier remains available throughout the job hunt
- The user's DOCX template remains structurally stable (predictable Heading1/Heading2 patterns)

---

## 13. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Indeed blocks JobSpy IPs | High | Iteration 6 residential proxy; Glassdoor as partial backup |
| Gemini fabricates content | High | spaCy validation, post-gen regex checks, regenerate up to 2x |
| Built resume modifies content outside allowed regions | High | Diff check rejects before save |
| LLM picks an unauthorized job title | High | Pydantic Literal constraint from safe_title_aliases |
| LLM picks a skill not in skills_pool | High | Post-validation rejects, regenerate, BUILD_FAILURE if persistent |
| Same job applied twice across portals | Medium | Strict job_id dedup |
| Session cookies expire silently | Medium | Detect login page mid-flow, immediate alert |
| Free tier limits exceeded | Medium | Conservative call budget (~600/day expected) |
| master_profile.yaml has an undefendable bullet | Medium | User responsibility — only add bullets you can defend |
| User misses a Telegram review for too long | Low | Job stays in pending_review indefinitely |
| pgvector slow on a large bullet pool | Low | IVFFlat index; query stays well under 50ms |
| LLM voice still sounds robotic | Medium | Few-shot from approved bank, banned-pattern checks |
| Application queue grows unbounded | Low | 12-hour expiry, Sunday cleanup |
| DOCX template structure changes | Medium | Assembler reads structure dynamically; document expectations clearly |
| Embedded hyperlinks broken on clone | High | Explicit r:id target update logic, tested on every build |

---

## 14. Approval & Sign-Off

- **Architecture:** locked, see `job_automation_architecture.md`
- **Build start:** Iteration 0 (scaffold) ready to begin
- **Owner approval:** Vishnujan Narayanan (single-user product)

---

**End of PRD.**
