# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project loosely tracks iterations rather than semver.

## [Unreleased]

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
