# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project loosely tracks iterations rather than semver.

## [Unreleased]

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
