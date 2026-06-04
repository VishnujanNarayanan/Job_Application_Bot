# TODO

Possible issues, rough edges, and deferred fixes to address later.
Not a changelog (see `CHANGELOG.md`) and not design rationale.

Each entry: short description, where it lives, why it's deferred.

## Iteration 1

_(no entries yet)_

## Iteration 2 — Phase B (carried over from the pivot cleanup)

These were deliberately deferred out of Phase A (which was kept purely
subtractive). Do them in Phase B alongside the real data flow.

Done (Step 1 — data layer):
- [x] **Operator config rename + derived filenames.** `config.personal` →
  `config.operator.full_name`; `filters:` + `salary:` split out; filenames
  derived in `src/config.py` (`resume_filename`/`cover_filename`).
- [x] **`applied` table reshape.** Dropped the 7 auto-apply columns; renamed
  `applied_at` → `built_at`; added `template_version`, `notified_at`,
  `user_status`. Migration `0003_pivot_schema.py`.
- [x] **`render_cache` table.** Added per architecture §7.3.
- [x] **`all_jobs` columns.** Added `jd_embedding vector(384)` +
  `near_duplicate_of` as a self-referential FK (`fk_all_jobs_near_duplicate_of`)
  + ivfflat index.
- [x] **Re-home salary default.** Now `salary.default_expected_lpa` (6.0).

### Step 2 progress checkpoint (Layers 2/3/4 real)

Build order: Layer 2 → Layer 3 → master_profile rebuild → Layer 4. Hard
gate: PAUSE before any live Telegram send (dry-run to a test chat first).

- [x] config: scraper enables indeed+glassdoor+linkedin, `near_duplicate_threshold: 0.95`, `near_duplicate_lookback: 500`
- [x] `src/scorer/embeddings.py` — `embed`/`embed_batch`/`cosine`, lazy cached model (offline-testable)
- [x] `src/reasons.py` — not_applied reason-category constants (§7.4)
- [x] `src/scraper/filters.py` — pure predicates (location/years/cooldown) + DB helpers (existing_job_ids, company_last_notified)
- [x] `src/scraper/dedup.py` — pure `find_near_duplicate` + `canonical_embeddings` candidate fetch; `resolve_batch` enforces FK insert-order (originals flushed before duplicates link)
- [x] `src/scraper/rotation.py` — rotation state get/advance (modulo wrap). NOTE: 20-job short-circuit is a scrape-loop concern, enforced by the orchestrator (Layer 1), not rotation.
- [x] `src/scraper/jobspy_wrapper.py` — real JobSpy scrape across 3 sites → AllJobs (lazy import, pure row mapping, mockable)
- [x] Layer 2 tests (`tests/test_iteration_2_scraper.py`, 15 offline)
- [x] Layer 3: real Gemini Call 1a (`src/llm/client.py` Instructor transport + `src/parser.py`) + expanded `JDParsed` (apply_url, salary_*, location_type, job_type, team_or_product) + spaCy skill grounding + role-cluster acceptance (`cluster_for_term`/`role_accepted`). Tests in `tests/test_iteration_2_parser.py`.
- [x] Layer 4: `selector.py` (experience/project/bullet/summary/skills-candidates), `ordering.py`, real `apply_decision.py` (`evaluate` → `SelectionResult`, final_score, threshold 0.50, NO quotas). Pure functions, synthetic-data tests in `tests/test_iteration_2_scorer.py`.
- [x] master_profile rebuild (`src/state/master_profile.py`): `MasterProfile` schema + validators, mtime-gated `rebuild` (validate → canonical JSON → embed/diff/upsert → deactivate removed), pure `plan_sync` diff, and the **candidate loader** `load_profile` (JSON structure + DB embeddings → `scorer.selector.Profile`). Skills + project names stored in `master_bullets` (`parent_type='skill'`/`'project_name'`); no `master_skills` table. Tests in `tests/test_iteration_2_master_profile.py`.
- [x] **Layer 4 JD-vector fix.** Replaced the single-embedding `JDContext` with the architecture's three query vectors (`vec_role`/`vec_match`/`vec_skills`, §4.1) + `build_jd_context`; experiences/projects score titles vs `vec_role` and bullets vs `vec_match`, skills vs `vec_skills`, summary vs `vec_role`.
- [ ] **Orchestrator rewiring (Step-2 integration).** `src/main.py` currently raises `NotImplementedError`. Wire the real pipeline end to end: rotation term → scrape (3 sites) → embed → `dedup.resolve_batch` → hard filters (location/years/cooldown/dup) → parse (Call 1a) → role-acceptance → load `Profile` → Layer 4 `evaluate` → (>=0.50) Layer 5 build → record `applied`/`not_applied` → notify. Lands right before the dry-run. HARD GATE: test chat only until approved. Also clean the stale "3 fake jobs / fixed JDParsed" prose still in `src/main.py`'s docstring while here.

### Step 3 — Layer 5 (resume builder) — scoped decisions (2026-06-04 planning)

- [ ] **Layer 5 = `selection_json` builder ONLY** (Gemini Call 1b). Scope:
  per-experience title alias (dynamic `Literal[tuple(safe_title_aliases)]` +
  per-exp post-validation), 3 skill categories (3-5 each from top-14
  candidates) + "Familiar With" gaps, cover letter (<=900 chars). Post-validate
  (skills source-set membership, banned category names, no dupes, title
  allow-list, cover-letter voice banned-words) → regenerate up to
  `builder.llm_regenerate_attempts` (2) → else `BUILD_FAILURE`. Assemble
  `StoredSelection` → write `applied.selection_json` + scores + `template_version`.
  Build with an **injectable/stubbed LLM** so tests stay offline; real run needs
  `master_profile.yaml` + `GEMINI_API_KEY`. Files: `src/builder/llm_call.py`,
  `src/builder/skills_validator.py`, new `src/builder/build.py`,
  schemas (`SkillsSelection`, `ResumeBuildLLMOutput`, `StoredSelection`),
  `src/llm/prompts.py` Call-1b builder.
- [ ] **Drop `expected_salary` everywhere.** It was an auto-apply/form-fill
  leftover; user fills salary manually and the resume never shows it. Layer 5
  must NOT compute or use it. `applied.expected_salary_lpa` column is now
  vestigial → drop in a small migration (or leave unused). The notification's
  salary is the JD's parsed `salary_max_lpa`, not an "expected" figure.
- [ ] **Add `gap_skills` to the build path.** JD skills (`required_skills` +
  `nice_to_have`) NOT in `skills_pool`, scored by cosine vs `vec_skills`; feeds
  the "Familiar With" row. Compute in the builder (it has the parsed JD), not
  Layer 4. (Layer 4's `SelectionResult` deliberately omits it.)
- [ ] **Layer 6 (assembler + endpoint) deferred until `resume_template.docx`
  exists** (`resumes/templates/` is empty — only `.gitkeep`). The template is
  the operator's real resume, mostly hardcoded; the assembler clones it and
  swaps ONLY: summary text, shown experiences (+ chosen title alias + 3
  bullets), shown projects (+ 2-3 bullets + "Code →" `r:id` target), skills
  categories + "Familiar With", section order (Skills↔Projects), and the cert
  "Verify Here" `r:id` target. NEVER touches header / education / fonts / tab
  stops / visible hyperlink text (rules #10, #11). Cover letter is a separate
  rendered doc.

Still open:
- **Summary query vector (judgment call).** The architecture (§4.4) doesn't
  say which JD vector summaries match against; we chose `vec_role` (role-level
  identity, consistent with titles/project names). Revisit if summary picks
  look off in the dry-run — `vec_match` is the alternative.
- **Re-home cover-letter voice.** `config.voice.banned_words` was kept;
  wire the banned-word/voice check into the Layer 5 builder (the old
  `sender/voice.py` form-answer validator was deleted). → Step 3 (Layer 5).
- **Sheet tabs.** Layer 9 tabs become Matches / Skipped / Near-duplicates
  with apply/PDF/DOCX links + status (Phase A only dropped the
  auto-apply `ManualRequired` tab). → Step 4 (Layer 9).
- **Decide on `reportlab` + `cover_pdf` config.** Both kept for now; the
  endpoint renders via LibreOffice, so likely drop them once cover-letter
  handling is settled. → Step 3 (endpoint).
- **ivfflat index recall.** `idx_all_jobs_embedding` is created on an empty
  table; rebuild (drop+create or REINDEX) once rows accumulate for better
  recall. Same deferral as the master_* ivfflat indexes from `0001`.
