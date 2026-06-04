# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project loosely tracks iterations rather than semver.

## [Unreleased]

### Added

### Changed

### Removed

---

## [Iteration 2.0] — 2026-06-04

### Added
- Iteration 2 Phase A + Phase B data layer — pivot cleanup plus the real data flow (Layers 2-4 + master_profile rebuild/loader), built layer by layer. Layers 5/6, orchestrator wiring, AWS, and Layers 8/9 remain for later Iteration-2 steps.
- Config: `src/config.py` — central `settings` accessor loading `config/config.yaml` once with recursive attribute access (`settings.scoring.final.fit`); missing keys raise `AttributeError`. Exposes operator-identity helpers `resume_filename` / `cover_filename`, derived from `operator.full_name` (no operator literal in source — CLAUDE.md rule #21 / NFR-11); validates `operator.full_name` is present at load.
- Layer 7: `render_cache` model + table (architecture §7.3) tracking S3-backed PDF/DOCX renders (`cache_key` PK, `job_id`, `format`, `template_version`, `s3_uri`, `created_at`, `expires_at` + `idx_render_cache_expiry`).
- Layer 7: `all_jobs` gains `jd_embedding vector(384)` + `near_duplicate_of` (self-referential FK `fk_all_jobs_near_duplicate_of` → `all_jobs.job_id`, + ivfflat cosine index `idx_all_jobs_embedding`) for near-duplicate detection. The FK means Layer 2 dedup must commit the original before linking a duplicate.
- Layer 7: migration `0003_pivot_schema.py` (`0002` → `0003`) — reshapes `applied`, adds `render_cache`, extends `all_jobs`. Offline SQL generation verified for the full `0001→0002→0003` chain.
- Tests: `tests/test_iteration_2_data.py` — config accessor, operator-agnostic filename derivation, fail-fast on missing `full_name`, and the `applied`/`all_jobs`/`render_cache` schema reshape (10 offline tests).
- `src/reasons.py` — centralised `not_applied.reason_category` constants (architecture §7.4) so every layer and the Iteration-3 CloudWatch metric filters share the same strings.
- Layer 4: `src/scorer/embeddings.py` — real `embed`/`embed_batch`/`cosine` on sentence-transformers (all-MiniLM-L6-v2, 384-dim); model lazy-loaded and cached so import is cheap and `cosine` is pure (offline-testable).
- Layer 2: `src/scraper/filters.py` — pure hard-filter predicates (`location_disallowed`, `exceeds_years_ceiling`, `company_in_cooldown`) + thin DB lookups (`existing_job_ids`, `company_last_notified`).
- Layer 2: `src/scraper/dedup.py` — near-duplicate JD detection (`find_near_duplicate` pure cosine, `>0.95` strict; `resolve_batch` classifies a scraped batch and persists originals before linking duplicates so the `near_duplicate_of` self-FK target always exists, including same-run cross-portal reposts). Duplicates marked `NEAR_DUPLICATE`.
- Layer 2: `src/scraper/rotation.py` — serial search-term rotation persisted in `search_rotation_state` (`current_term`/`advance`, modulo-wrapping index).
- Tests: `tests/test_iteration_2_scraper.py` — 15 offline tests for filters, cosine math, dedup (incl. the same-run repost insert-order case via a fake session), rotation (one-table SQLite), and the JobSpy row mapping (faked `jobspy` module).
- Layer 3: `src/llm/client.py` — real Gemini 2.0 Flash transport via Instructor (`get_client` lazy + cached, `complete(response_model, prompt, system=)` with exponential backoff per `config.llm.backoff`; final failure logs `gemini_failure` and raises `LLMError`). SDK imported lazily so the module is import-cheap and `complete` is injectable for tests.
- Layer 3: `src/parser.py` replaces the Iteration-1 fixed-`JDParsed` stub with real Gemini Call 1a — `parse(job, complete=)` runs the call, then grounds `required_skills`/`nice_to_have` against the JD text (substring fast path + spaCy-lemma fallback) to drop fabricated skills. Adds pure role-cluster acceptance helpers `cluster_for_term` / `role_accepted` (config `parser.role_clusters`) for the orchestrator's ROLE_MISMATCH decision. `apply_to_row` now also copies `team_or_product`, `job_type`, `location_type`, `salary_min_lpa`, `salary_max_lpa`, `salary_currency`.
- LLM schema: `JDParsed` extended with `team_or_product`, `job_type`, `location_type`, `apply_url`, `salary_min_lpa`, `salary_max_lpa`, `salary_currency` (all optional; each consumed by Layer 4 scoring or Layer 8 notification).
- Layer 3: `src/llm/prompts.py` — `jd_parse_system()` (anti-fabrication system instruction) + `jd_parse_prompt(job, role_categories)` builder.
- Tests: `tests/test_iteration_2_parser.py` — 8 offline tests for parse (stub transport), skill grounding (substring/dedup/lemma fallback), expanded `apply_to_row`, and role-cluster acceptance.
- Layer 4: `src/scorer/selector.py` — pure, deterministic selection (no LLM): candidate dataclasses (`Profile`, `ExperienceCand`, `ProjectCand`, `SummaryCand`, `SkillCand`, `JDContext`) + `select_experiences` (alias×0.30 + top3-bullet-avg×0.70, threshold 0.45, max 3, force-include top-2), `select_projects` (name×0.20 + topN-bullet-avg×0.80, threshold 0.50, never hidden, bullets min 2/max 3 descending), `select_summary` (role_category-first then cosine, fallback to all), `select_skill_candidates` (top-14 pool by cosine).
- Layer 4: `src/scorer/ordering.py` — `order_experiences` (best-match at #1 when score gap > 0.20 else recency) + `skills_before_projects` (section order by aggregate JD match).
- Layer 4: `src/scorer/apply_decision.py` rewritten — replaces the Iteration-1 reject-all `decide()`/`Decision` stub with the real scoring engine: `seniority_score`, `recency_score` (banded), and `evaluate(profile, jd, now=)` returning a `SelectionResult` (fit = best_exp×0.50 + summary×0.20 + avg-skill×0.30; success_prob = seniority×0.60 + recency×0.40; final = fit×0.55 + success_prob×0.30 + recency×0.10 + project×0.05; apply when final >= 0.50). No quotas, no top-N (hard rule #14).
- Tests: `tests/test_iteration_2_scorer.py` — 17 offline tests with synthetic profiles/JDs covering experience blend + force-include + cap, match-then-recency ordering, projects-never-hidden + descending bullets, category-first summary, skill ranking/cap, seniority/recency primitives, the three-vector `build_jd_context`, and end-to-end `evaluate` apply/skip + section-order flag.
- Layer 4: `src/scorer/embeddings.py` gains `add(a, b)` (element-wise vector sum) for the JD match vector.
- Layer 4: `src/scorer/selector.py` `build_jd_context(parsed, posted_at=, embed_batch_fn=)` — the single per-job embed (architecture §4.1): one batched call producing `vec_role = embed(role_summary)`, `vec_skills = embed(required + nice_to_have)`, and `vec_match = vec_skills + vec_resp`.
- Layer 7 / §3: `src/state/master_profile.py` — full `MasterProfile` Pydantic schema (personal/summaries/work_experience/projects/skills_pool/education/certifications) with validators (actual_title ∈ safe_title_aliases per rule #6, projects ≥2 bullets, globally-unique ids, non-empty skills_pool). `rebuild(session, path=, embed_fn=, force=)`: mtime short-circuit → validate → write canonical `master_profile.json` → embed + diff/upsert into `master_bullets`/`master_summaries`/`master_title_aliases` → deactivate removed (never hard-delete, rule #17) → record `master_meta`. Pure diff brain `plan_sync` (insert/update/reactivate/deactivate/unchanged). Skills stored in `master_bullets` as `parent_type='skill'` and project names as `parent_type='project_name'` (no `master_skills` table per §7.3; honors "only the JD is embedded per run").
- Layer 7 / Layer 4 bridge: `load_profile(session, json_path=)` — the candidate loader joining canonical-JSON structure (company, dates, project name/link) with active DB embeddings (bullets/skills/project-name/aliases/summaries) into a `scorer.selector.Profile`.
- Tests: `tests/test_iteration_2_master_profile.py` — 11 offline tests (schema validators, `desired_bullets` incl. skill/project_name rows, `plan_sync` all branches, `rebuild` clean-insert + mtime skip/force via a fake session + injected embed, `load_profile` JSON↔DB join + missing-JSON error).

### Changed
- Config: renamed `personal:` → `operator:` (`name` → `full_name`, `years_of_experience` → `years_experience`, `timezone`); split hard filters into `filters:` (`job_type`, `years_ceiling`, `visa_filter`, `disallowed_regions`, `company_blocklist`); re-homed the expected-salary default to top-level `salary.default_expected_lpa` (6.0). Dropped the redundant `parser.years_required_ceiling` (now single-sourced at `filters.years_ceiling`). `tests/test_smoke.py` required-section set updated accordingly.
- Layer 7: reshaped the `applied` model/table (architecture §7.3) — dropped `apply_type`, `resume_s3_uri`, `resume_s3_key`, `cover_letter_used`, `cover_letter_s3_uri`, `application_status`, `failure_reason`; renamed `applied_at` → `built_at`; added `template_version`, `notified_at`, `user_status` (default `pending`). `selection_json` retained as the durable per-job artifact.
- Layer 2: `src/scraper/jobspy_wrapper.py` replaces the Iteration-1 fake-3-jobs stub with the real JobSpy scrape — `scrape(search_term, *, sites, country, results_wanted, hours_old)` reads public listings across Indeed/Glassdoor/LinkedIn (listings-only; never logs into an account — rule #4), de-duplicates by `job_id` within a call, and returns `AllJobs` rows (caller persists). JobSpy imported lazily; the row→`AllJobs` mapping is pure.
- Tests: `tests/test_iteration_1.py` trimmed as the skeleton became real — scraper-stub (Layer 2), parser-stub (Layer 3), and reject-all `decide()` (Layer 4) tests removed; only the Layer-7 models-import smoke check remains. Layer 2/3/4 coverage lives in the new Iteration-2 test files.
- Orchestrator: `src/main.py` Iteration-1 linear skeleton dismantled now that Layers 2/3/4 are real modules — `main()` raises `NotImplementedError` pending the dedicated Step-2 orchestrator wiring (rotation-driven scrape → embed → dedup → filters → parse + role-acceptance → master_profile candidate load → Layer 4 `evaluate` → Layer 5 build → notify), which lands right before the test-chat dry-run. Still importable.
- Tests: `tests/test_smoke.py` no longer asserts `src/sender/*`, `src/state/queue.py`, `data/sessions`, `data/manual_queue`, or `answer_bank_seed.example.yaml` exist; required-config-section set drops `queue` and `sender`. `tests/test_iteration_1.py` drops the `AnswerBank`/`ApplicationQueue`/`PendingReview` imports. `tests/conftest.py` and `src/state/cleanup.py` docstrings updated to drop Playwright/manual_queue references.
- `TODO.md`: recorded the Phase B carry-overs (operator-config rename, `applied` reshape, `render_cache`, `all_jobs` near-duplicate columns, salary-default re-home, cover-letter voice re-home, Sheet-tab rename, reportlab/cover_pdf decision) plus the Step-3 Layer-5 scope decisions (selection_json-only build, drop `expected_salary`, add `gap_skills`, defer Layer 6 until a template exists).

### Removed
- Iteration 2 Phase A — pivot cleanup (removes obsolete auto-apply stubs per the Migration Note). Behaviour-affecting work (real data flow) is Phase B.
- Layer 6: deleted `src/sender/` entirely — Playwright auto-apply driver (`indeed.py`, `glassdoor.py`), form-field discovery/classification (`fields.py`), four-category question handler (`questions.py`), answer bank (`bank.py`), cover-letter form fill (`cover_letter.py`), cover-letter PDF renderer (`pdf_render.py`), form-answer voice validator (`voice.py`). The system no longer submits applications or acts on any account.
- Layer 7: deleted `src/state/queue.py` (`application_queue` 12-hour decay stub).
- Layer 7: dropped the `ApplicationQueue`, `AnswerBank`, and `PendingReview` models from `src/state/models.py`.
- Layer 7: migration `0002_drop_autoapply_tables.py` drops the `application_queue` (+ `idx_queue_status`), `answer_bank`, and `pending_review` tables (forward-only with `IF EXISTS` guards so it's idempotent; `0001` left intact as history; `downgrade` recreates them).
- Layer 5: removed the Gemini Call 2 references (form questions) — budget is now 2 calls/job max. `config.llm.max_calls_per_job` 3 → 2; LLM comment block and `src/llm/prompts.py` docstring updated.
- Layer 4: removed cycle-quota / top-N picking from config and docstrings — `config.scheduler.cycle_quota` and `config.scheduler.peak_hours` deleted, `config.queue` (12-hour decay / `STALE`) deleted; `src/scorer/apply_decision.py` and `src/main.py` docstrings updated to "notify every match >= 0.50, no quotas".
- Config: deleted the `sender:` section (Playwright upload filenames, `max_form_pages`, `retry_limit`, `question_mode`, `profile_fields`, current-salary answer-bank wiring) and `voice.few_shot_size` (answer-bank reference).
- Config: removed obsolete keys — `storage.sessions_dir`, `storage.manual_queue_dir`, `storage.cleanup.manual_queue_retention_days`; `notifications.critical_alert_triggers` entries `playwright_crash` and `session_expired`; `analytics.sheets.manual_required_tab`.
- Removed `playwright==1.49.0` from `requirements.txt`.
- Removed `answer_bank_seed.example.yaml` and the `data/sessions/` and `data/manual_queue/` directories.
- `.env.example`: removed the `INDEED_EMAIL` / portal-session-login section.

---

## [Iteration 1] — 2026-05-26

### Added
- Layer 7: 13-table SQLAlchemy 2.0 schema in `src/state/models.py` (`all_jobs`, `applied`, `not_applied`, `application_queue`, `processing_queue`, `master_bullets`, `master_summaries`, `master_title_aliases`, `master_meta`, `search_rotation_state`, `answer_bank`, `pending_review`, `portal_health`, `company_cooldown`).
- Layer 7: `src/state/db.py` — synchronous SQLAlchemy engine and `session_scope()` context manager. Reads pool config from `config/config.yaml`, rewrites `postgresql://` to `postgresql+psycopg://` so psycopg3 is used.
- Layer 7: Alembic scaffold — `alembic.ini`, `src/state/migrations/env.py`, `script.py.mako`, and initial migration `0001_initial_schema.py` creating all 13 tables. Loads `DATABASE_URL` from `.env` via python-dotenv.
- Layer 2: `src/scraper/jobspy_wrapper.py` returns 3 stub `AllJobs` rows so the rest of the pipeline runs end-to-end (real JobSpy lands in iter 2).
- Layer 3: `src/parser.py` wires Gemini Call 1a — returns a real `JDParsed` per scraped job and `apply_to_row()` to populate AllJobs fields.
- Layer 4: `src/scorer/apply_decision.py` — `decide()` returns LOW_SCORE while `master_bullets` is empty (iter 1 reject-all behaviour).
- Layer 8: `src/notifications.py` — `send_dry_run_summary()` sends a Telegram message at end of run.
- LLM schema: `src/llm/schemas.py` adds `JDParsed` (Instructor-enforced Pydantic model) for Gemini Call 1a.
- Orchestrator: `src/main.py` wires Layers 2/3/4/7/8 end-to-end with structlog JSON output to stderr; `--dry-run` flag default for iter 1.
- Tests: `tests/test_iteration_1.py` adds 8 offline tests covering scraper, parser, scorer stubs, and model importability (DB and Telegram mocked).
- `TODO.md` at repo root for tracking deferred issues separate from the changelog.

### Changed
- `tests/test_smoke.py`: `test_main_raises_not_implemented` → `test_main_importable` — `src.main.main()` now drives the real pipeline instead of raising.

### Fixed
- `src/main.py`: raised `httpx` and `httpcore` loggers to WARNING in `_configure_logging()` so `python-telegram-bot`'s underlying HTTP client no longer logs the full request URL (which embeds the bot token) at INFO. Prevents token leakage into stderr and, once watchtower is wired in iter 3, into CloudWatch.

---

## [Iteration 0.1] — 2026-05-26

### Added
- `.env.example`: added AWS section (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION=ap-south-1`, `AWS_S3_BUCKET`, `AWS_CLOUDWATCH_LOG_GROUP=/job-bot/runtime`, `AWS_CLOUDWATCH_NAMESPACE=JobBot`) so contributors can mirror the spec's AWS requirements. Defaults match CLAUDE.md hard rules #11, #18, #19.
- `src/cli/aws_check.py`: real implementation of the AWS connectivity verifier (replaces no-op stub). Exercises the minimal-permission set from CLAUDE.md hard rule #19: S3 put/get/delete on the bot bucket, CloudWatch CreateLogStream + PutLogEvents on the bot log group, CloudWatch PutMetricData on the bot namespace, plus a negative IAM check that fails the run if the runtime user can call `iam:ListUsers` (proves no privilege escalation surface). Loads config from `.env` via python-dotenv. Exit 0 on all-pass, 1 on any failure.

### Changed
- `requirements.txt`: added `boto3==1.35.76` and `watchtower==3.3.1` (AWS SDK + CloudWatch log handler) and `moto[s3,cloudwatch,logs]==5.0.22` (test-time AWS mocking, per CLAUDE.md testing strategy).

---

## [Iteration 0.0.2] — 2026-05-25

### Changed
- CLAUDE.md hard rule #11 rewritten as "Free tier only — with hard billing caps". Now honest about S3's 12-month free tier (~$0.30/year post-tier) and mandates layered billing safeguards: $0.01 tripwire, $0.50 warning alarm, $1 Budget Action that auto-detaches the runtime IAM policy. Always-free vs conditionally-free services split out. SNS allowed only as alarm-to-Lambda-to-Telegram bridge.
- CLAUDE.md onboarding checklist: AWS setup reordered to put billing safeguards FIRST, before any S3/CloudWatch/IAM resource. Introduces separate `job-bot-budgets` IAM role for the kill switch. Mandates verification by manually triggering the $0.01 alert path.
- CLAUDE.md "Things NOT to do": added prohibitions on raising/disabling budget caps, granting runtime user any budget/IAM/alarm permissions, and creating AWS resources before safeguards are verified. `us-east-1` carved out as the one allowed non-`ap-south-1` region (billing metrics only publish there).
- PRD NFR-1 rewritten: drops the absolute "$0/month" claim, states the $0.30/year post-12-month reality, and pins the $1/month hard cap enforced by Budget Action.
- PRD: added FR-21 "Billing guardrails" — mandates the three-layer safeguard stack (tripwire / warning / hard stop), the separate `job-bot-budgets` IAM role for the kill switch, and a verification step before going live.
- PRD FR-20: IAM minimal permissions now explicitly excludes budget/billing operations from the runtime user.
- PRD risk table: AWS bill surprise row upgraded to describe the three-layer mitigation; added new "Runtime user tampers with kill switch" row covered by the separated `job-bot-budgets` role.
- Architecture Section 5.3 (cost model): rewritten as a per-service table distinguishing always-free vs 12-month-free tiers; states the $1/month hard cap and what "budget breach as incident" means.
- Architecture: new Section 5.5 "Billing safeguards" with the three-layer stack, role separation diagram (`job-bot-runtime` vs `job-bot-budgets` vs owner), per-threshold incident response, and a verification procedure for manually exercising both alert paths. Original 5.5 (Failure handling) renumbered to 5.6.

---

## [Iteration 0.0.1] — 2026-05-25

### Changed
- Spec across CLAUDE.md, PRD.md, architecture: project now uses AWS (S3 + CloudWatch + IAM) for resume storage and observability. Spec-only changes; no AWS resources created yet (Iteration 0.1 pending).
- CLAUDE.md: new "AWS conventions" section — region locked to `ap-south-1`, `boto3.Session()` reading credentials from env, S3 upload/download/presigned-URL helper sketches, structlog → `watchtower` → CloudWatch, alarm-via-metric-filter pattern.
- CLAUDE.md hard rules: added #18 (AWS credentials live in `.env` only, rotated quarterly) and #19 (IAM minimal permissions: `s3:PutObject/GetObject/DeleteObject` on bot bucket + `logs:CreateLogStream/PutLogEvents` + `cloudwatch:PutMetricData` on bot namespace, nothing else). Changelog rule renumbered to #20.
- CLAUDE.md: free-tier rule now explicitly forbids RDS, Lambda for runtime, ECS, Fargate, EKS, EventBridge as primary scheduler, SNS.
- CLAUDE.md: `success_prob` formula spelled out — `seniority × 0.60 + recency × 0.40` (junior 1.0 / mid 0.80 / senior 0.40 / lead 0.15); previously underspecified.
- CLAUDE.md: layer reference table now annotates per-iteration introduction (S3 from iter 2+, CloudWatch from iter 3+, Glassdoor from iter 4); added "Current build status" pointer at top.
- CLAUDE.md: onboarding checklist now requires AWS account setup (IAM user `job-bot-runtime`, versioned/private S3 bucket, billing alarm at $1) plus `python -m src.cli.aws_check`.
- CLAUDE.md: "When in doubt" gained #8 (paid AWS feature loses to alternative); "Things NOT to do" gained AWS-specific prohibitions (no paid services, keys only in `.env`, no IAM perms beyond Section 5.4, no resources outside `ap-south-1`, no local resume copies after upload).
- PRD: new section 8.8 "AWS Integration" (S3, IAM, CloudWatch, conditional SQS); subsequent sub-sections renumbered to 8.10.
- PRD: added FR-18 (S3 versioned artifact storage, table stores S3 URIs not local paths), FR-19 (CloudWatch alarms for APPLY_FAILURE rate >3/24h and session_expired, routed to Telegram within 10 minutes), FR-20 (IAM minimal permissions). FR-9 and FR-10 reworded to reference S3 URIs and presigned URLs.
- PRD: added NFR-9 (AWS resources MUST live in `ap-south-1`).
- PRD: user stories extended to 13 — added CloudWatch alarms story, AWS Free Tier inclusion, "real AWS production experience" learning goal; success metrics gained billing-dashboard validation and CloudWatch alarm timing; anti-metrics gained "AWS bill > $0".
- PRD: risk table extended with AWS credentials leak, billing surprise, public-bucket misconfiguration, S3 region outage, CloudWatch unavailable; Out-of-Scope explicitly excludes paid AWS services and multi-region AWS deployment.
- Architecture: new Section 5 "AWS Integration" (services-by-iteration table, cost analysis, IAM policy spec, AWS failure-handling matrix); all subsequent sections renumbered.
- Architecture: Layer 5 now uploads both DOCX and PDF to `s3://{bucket}/resumes/applied/{job_id}_{timestamp}.{ext}` after build.
- Architecture: Layer 6 now downloads the resume from S3 to `/tmp` before form upload and deletes it after.
- Architecture: Layer 7 file storage moved to S3 (`resumes/applied/`, `cover_letters/`); local filesystem reserved for transient files only (session cookies, manual-queue screenshots, in-flight logs).
- Architecture: `applied` table schema gains `resume_s3_uri`, `resume_s3_key`, `cover_letter_s3_uri` columns.
- Architecture: Layer 8 gains CloudWatch alarms (APPLY_FAILURE rate, session_expired) routed to Telegram; Telegram remains user channel.
- Architecture: Layer 9 Sheets now serves presigned S3 URLs to resume PDFs instead of local paths.
- Architecture: large inline blocks (24-term `search_terms` list, full `role_clusters` mapping, full `master_profile.yaml` schema) removed from doc body and referenced as `config/config.yaml` — body now describes structure and behavior only.
- All three docs: status header updated to "Iteration 0 complete, Iteration 0.1 (AWS prep) pending, Iteration 1 ready".

### Removed
- CLAUDE.md / PRD / architecture: "Sunday cleanup" no longer cited as the enforcement mechanism for the 12-hour queue decay — decay logic stands on its own.

---

## [Iteration 0] — 2026-05-23

### Added
- Full repo structure per architecture doc Section 7: all `src/` subpackages
  (`scraper`, `scorer`, `builder`, `sender`, `state`, `llm`, `cli`),
  `config/`, `resumes/`, `data/`, `tests/` with appropriate `.gitkeep` files.
- `.gitignore` covering `master_profile.yaml`, `.env`, `resumes/applied/`,
  `data/sessions/`, `data/manual_queue/`, `data/logs/`,
  `data/pending_writes.jsonl`, `config/google_service_account.json`,
  `__pycache__/`, `*.pyc`, `.venv/`, `.pytest_cache/`.
- `requirements.txt` pinned per architecture doc Section 6 (Python 3.11+,
  JobSpy, Playwright, spaCy, sentence-transformers, google-generativeai +
  Instructor + Pydantic, SQLAlchemy 2.0 + psycopg3 + pgvector + Alembic,
  python-docx, reportlab, python-telegram-bot, gspread, google-api-python-client,
  structlog, pyyaml, python-dotenv, pytest, pytest-asyncio).
- `config/config.yaml` with every tunable from the architecture doc:
  experience/project/skills thresholds and weights, final-score formula weights
  (fit 0.55 / success_prob 0.30 / recency 0.10 / project 0.05), apply
  threshold 0.50, cycle quotas (peak 3 / off-peak 1, 8–11am IST), queue
  decay 12h, company cooldown 10 days, years ceiling 5, match-then-recency
  gap 0.20, short-circuit count 20, search-term rotation list (24 terms),
  disallowed regions (Delhi NCR: Delhi, Gurgaon, Gurugram, Noida, Ghaziabad,
  Faridabad), role_clusters (7 clusters, merged under `parser:` — no separate
  file), banned LLM category names, salary defaults (6 LPA / 100000), upload
  filenames, retry limit 1, max form pages 10, skills top-14 candidates,
  3 categories × 3–5 skills, familiar_with max 4, recency score bands,
  seniority scores, cover-letter PDF render contract (Arial 11pt, 2cm margins,
  single page, no header/footer).
- `scoring.success_prob` formula simplified to `seniority * 0.60 + recency * 0.40`
  (resolved undefined `applicant_score` / `age_score` from architecture doc).
- `config/role_clusters.yaml` intentionally absent — clusters live in
  `config/config.yaml` under `parser.role_clusters` per project decision.
- `.env.example` listing all required secrets (DATABASE_URL, GEMINI_API_KEY,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GOOGLE_SHEETS_ID, GOOGLE_DOC_ID,
  GOOGLE_APPLICATION_CREDENTIALS, INDEED_EMAIL, TZ, LOG_LEVEL).
- `master_profile.example.yaml` with full schema, `REPLACE_ME` placeholders,
  and field-explanation comments. Flat `skills_pool` with comment explaining
  that categorisation happens per-JD at build time (not stored here).
- `answer_bank_seed.example.yaml` with four seed entry patterns
  (current_salary_redirect, notice_period, why_company_generic, relocation).
- `src/main.py` orchestrator with full Layers 1–9 docstring contract;
  raises `NotImplementedError` in Iteration 0.
- `src/state/master_profile.py` with empty `MasterProfile(BaseModel)` stub
  at the agreed location (full schema lands in Iteration 2).
- Stub modules (docstring-only) for all 33 Layer 2–9 source files:
  `src/scheduler.py`, `src/parser.py`, `src/notifications.py`,
  `src/analytics.py`, all files under `src/scraper/`, `src/scorer/`,
  `src/builder/`, `src/sender/`, `src/state/`, `src/llm/`, `src/cli/`.
- `tests/conftest.py` with `repo_root` and `config_path` fixtures.
- `tests/test_smoke.py` with 9 passing tests verifying repo layout, absent
  `role_clusters.yaml`, config section coverage, role_clusters content,
  `success_prob` weight sum, final score weight sum, `MasterProfile` import,
  and `main()` raising `NotImplementedError`.
- `README.md` with setup instructions, external account table, and CLI
  reference.
- `CHANGELOG.md` (this file), initialised per CLAUDE.md discipline.
- Iteration 0 acceptance criteria met: `pytest` passes 9/9, project tree
  matches architecture doc Section 7.
