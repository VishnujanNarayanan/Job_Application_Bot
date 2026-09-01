# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project loosely tracks iterations rather than semver.

## [Unreleased]

### Added

- `PIVOT_V3.md`: the plan for moving to the Headless Headhunter resume template
  — no Skills section, no Summary section, keyword-coverage scoring, and a
  master profile shaped like the `bullet-extract` skill's `role_blocks` output
- Layer 4: `src/scorer/keywords.py` — JD keyword extraction and the literal
  matcher that defines "covered". A port of `resume guide/score_coverage.py`,
  held to it by parity tests, so the offline grader and the live selector agree
- Layer 4: `src/scorer/qualifications.py` — the 128-title sheet, consulted as a
  selection tie-break only. A canonical token the JD never mentions can never
  pull a bullet onto the resume
- `data/job_qualifications.md` vendored into the repo (with `make refresh-quals`
  / `make check-quals`) so the GitHub Actions runner has the sheet
- Layer 7: `master_bullets.block_id/role/bullet_index/is_summary/is_extra` and
  `master_title_aliases.block_id` (migration `0009_role_blocks`)
- `src/state/selection_compat.py`: reads both selection shapes, so the 85
  pre-pivot rows stay listable in the dashboard and the monthly report

### Changed

- Layer 4: bullets are selected by greedy keyword set-cover instead of top-3
  cosine. Each entry takes the bullet adding the most uncovered JD keyword
  weight, stopping when nothing new is left to say. The covered set resets per
  entry, so the first entry is free to clear the whole checklist alone
- Layer 4: bullets per entry are tenure-capped (3 under 6 months, 6 under 18,
  else 8; projects 5) rather than exactly 3
- Layer 4: bullets are pooled across all of an entry's `role_blocks`, with an
  off-role bullet's gain scaled by its block's JD relevance — so it wins only
  when nothing on-role covers that keyword
- Layer 4: `fit = 0.55*best_experience + 0.45*keyword_coverage`, replacing
  `0.50*best_experience + 0.20*summary + 0.30*avg_skill_pool`. The two removed
  terms scored content the new template does not put on the page
- Layer 4: work and projects are selected and scored by one code path; section
  order is fixed (Work History, then Projects)
- Layer 4: `build_jd_context` embeds 3 vectors per job instead of
  `3 + len(skills)` — the per-skill vectors only fed the Skills section
- Layer 5: `selection_json` is v2 — `{version, entries[], jd_keywords,
  keyword_coverage, lead_entry_coverage}` replacing `{summary_id, experiences[],
  projects[], skills, section_order}`
- Layer 6: the assembler detects structure (bold Normal headings, Heading-3
  entry lines, `numId=1` bullets) instead of matching Heading-1 text, and mints
  the second section heading by cloning the template's one
- Layer 6: hard rules #9/#10 collapse into one frozen PREFIX — everything before
  the work heading, now including Education & Certificates — diff-asserted with
  a single c14n comparison
- Layer 6: the endpoint returns 409 for a pre-pivot selection instead of
  rendering it against a template it was never built for
- Layer 7: `master_profile.yaml` is `role_blocks`-shaped, mirroring the
  bullet-extract output; an empty `skills_pool` warns instead of raising
- Layer 3/4: `hit()` now matches at alphanumeric boundaries. A raw substring
  test counted `Java` as covered by "javascript", `SQL` by "postgresql", `R` by
  "ran" and `Go` by "django" — marking keywords covered by bullets that never
  claimed them. Fixed in `resume guide/score_coverage.py` too
- The `bullet-extract` skill: Step 12's self-check rewritten against Step 11's
  schema (it demanded ~8 banned fields), Step 10 `_meta` trimmed to the four
  emitted keys, and new Step 6f `extra_bullets` — an off-checklist bullet is now
  demoted to a recovery pool rather than cut, and cross-cutting skills go in
  every block that serves them
- `resume guide/score_coverage.py`: walks `work_experience` (it only ever walked
  `projects`), survives a missing baseline, and adds `--per-title`

### Added

- Layer 4: `employment_type` on work entries. Freelance engagements are separate
  entries that compete on merit like projects — any, all or none may appear —
  but render under Work History with `Freelance · <dates>` in the dates slot.
  Employment is force-included; position is earned, not pinned
  (`selection.freelance`)
- Layer 6: an entry is never stranded across a page break. The header and the
  first `endpoint.render.keep_together_ratio` (0.66) of its bullets are bound by
  a `keepNext` chain, so the group moves to the next page rather than splitting;
  `keepLines` stops a single bullet's own wrapped lines splitting
- Layer 6: section headings are `Heading 2`, so sections collapse in Word and
  appear in the navigation pane. Appearance is unchanged — runs carry Arial /
  10.5 / bold as direct formatting — but colour is pinned explicitly, since the
  style's own colour is blue. `_is_section_heading` still accepts bold-Normal
- `tools/build_headless_template.py` — builds the operator's template from the
  pristine one: header, links, Education & Certificates, bullet glyph, section
  heading styles. Reads identity from `master_profile.yaml` and display strings
  from `operator.resume_header` in config, so no operator literal enters source
- `tools/measure_pdf_spacing.py` — reads line positions out of a rendered PDF.
  Matching paragraph properties does not mean correct rendered gaps; that
  assumption hid a doubled blank line for several review rounds

### Fixed

- Layer 4: the same sentence could render twice in one entry. The bullet pool
  deduped by id, but the extractor writes each accomplishment "re-worded in
  every block it honestly serves", so an entry legitimately holds near-twins
  under different ids. The greedy alone does not catch them — once coverage is
  exhausted every candidate has gain 0, and the floor then fills the remaining
  slots by cosine, which picks the twin of a bullet already on the page. Now
  deduped on normalised text too, with the lead block's wording winning
- Layer 6: the bullet glyph is `U+2022` in Arial at 16pt with `lineRule="exact"`,
  replacing `U+25CF` in Noto Sans Symbols at the inherited size. Word lacks that
  font and substituted a small dot while LibreOffice drew the true heavy circle,
  so the DOCX and the PDF disagreed. The pin is required because any glyph above
  10.5pt inflates the line box — measured, uniform 18.1pt spacing became an
  uneven 19-23pt
- Layer 6: one blank paragraph before each section heading, not two. The template
  shipped 36pt before Education against 18pt before the work heading — the same
  kind of boundary at double the gap
- Layer 6: a project's entry line overflowed. The full repo URL in the right tab
  slot measured 112 characters against an ~89-character budget (Arial 10.5
  across a 6.5in text width); it now renders as a short hyperlinked "Code →" at
  59. Reverses PIVOT_V3.md D8 — that ban existed because the old code
  hand-patched the rels XML inside the saved zip, where a dangling `r:id` makes
  LibreOffice refuse the file and fails the PDF render. The link is now minted
  through python-docx's `part.relate_to()`, and a test asserts no dangling
  relationship survives assembly

### Known issues

- Layer 6: hyperlinks added programmatically are valid in the DOCX but are NOT
  exported to the PDF by LibreOffice — it renders the text and ignores the
  reference. Reproduced with raw XML injection, so it is not a python-docx
  fault; links authored in Word do export. Affects the header links and the
  project `Code →` links. See PIVOT_V3.md §12 for the isolation and the options
- Layer 4: every threshold is a v2 value calibrated against the old scoring
  formula. Measured on real ads, entry scores land at 0.12-0.31 against
  thresholds of 0.332/0.344, so freelance entries never appear, and no job
  reaches the 0.50 apply threshold. PIVOT_V3.md Stage 6

### Removed

- Layer 4: `select_summary`, `select_skill_candidates`, `score_experience`,
  `score_project`, `skills_before_projects` — the Skills and Summary sections
  they served are gone from the template
- Layer 5: `assign_skill_categories` and the 130-line `selection.skills.taxonomy`
  config block; `SkillCategory`, `StoredSkills`, `SelectedExpEntry`,
  `SelectedProjEntry`
- Layer 6: `src/endpoint/hyperlinks.py`. The template has no hyperlink elements
  and no hyperlink relationships; a project's repo URL is plain text in the
  entry line's right tab slot, which avoids minting synthetic `r:id`s whose
  dangling-reference failure mode breaks the LibreOffice PDF render
- Layer 7: `master_bullets` rows with `parent_type='project_name'` and all
  `master_summaries` rows are deactivated (never deleted — hard rule #17)

## [v2.0.2] — 2026-08-19

### Fixed

- Layer 3: the Groq fallback calls `openai/gpt-oss-120b` — `llama-3.3-70b-versatile`
  was retired and 404'd every request, so with the local model unreachable and
  Gemini over its spend cap, every job failed to parse and the run produced
  nothing
- Layer 3: a schema mistake by the local model is reasked (`llm.validation_reask`)
  instead of falling straight through to a hosted provider — `max_attempts: 1`
  treated "answered with an invalid field" the same as "laptop unreachable",
  and re-sending an identical prompt does not fix a validation error anyway
- Layer 3: the local model is `qwen2.5:3b-instruct`, replacing the 7B, which
  overflowed a 6 GB GPU by ~1 GB and ran the remainder on a CPU already
  contended with WSL — 92-250s per parse, and it froze the machine. The 3B
  fits VRAM whole: 6.6s per parse, no spill. Trade-off is a rougher read of
  `years_required`, which feeds 30% of the final score
- Layer 3: an over-long skill is split into the terms it names rather than
  rejected — "Experience with Docker, Kubernetes, CI/CD pipelines, and MLOps
  practices" becomes four skills. On a JD where the model returned 11 whole
  bullet lines and none of the 18 technologies named, all 18 are recovered
- Layer 3: skill lead-ins and generic tails are stripped, so "Strong
  programming skills in Python" yields `Python` (it previously exceeded the
  length bound and was dropped, losing Python from an ML ad) and "CI/CD
  pipelines" / "AWS services" / "RAG architectures" yield the bare terms
- Layer 3: requirement boilerplate is rejected before splitting, so a degree
  line no longer becomes the fake skills "Engineering", "Data Science", "AI".
  The degree words require context — a bare "master" or "degree" would reject
  "Master Data Management" and "degree of parallelism", and because the test
  precedes splitting it would take every technology in that bullet with it
- Layer 3: `nice_to_have` drops what `required_skills` already names — on one
  JD all 11 nice-to-haves were duplicates, doubling those JD query vectors
- Layer 3: a skill is bounded at 40 characters, a figure taken from the
  recorded rejections rather than guessed — at 30 the bound cut through real
  certifications ("Docker Certified Associate", "Kubernetes Application
  Developer") and genuine prose does not start until the mid-forties
- Layer 3: the local provider gives up after 180s rather than 600s. One 7.5k
  ad hung Ollama for the full ceiling and, with the fallbacks behind it, cost a
  single job 630 seconds; a parse that has not finished in 180s is stuck, and
  the chain has a hosted fallback so giving up early is cheap
- Layer 3: the LLM is sampled at `temperature: 0.6`, chosen by sweeping recall
  against a hand-checked answer key (`python -m src.cli.temp_sweep`) rather
  than by how tidy the output looked. Over 30 technologies from two real ads:
  0 → 67%, 0.4 → 76%, the model default → 77%, 0.6 → 87%. Both extremes lose —
  at 0 the model writes the category and drops the "(LoRA, QLoRA, SFT, DPO)"
  the ad spelled out
- Layer 3: a parenthesis is split as its own list instead of being cut through,
  so "(DeepSpeed, FSDP)" no longer truncates to "…frameworks (DeepSpeed"; the
  head and the bracketed contents are both kept, which also brings long
  certification names inside the length bound
- Layer 3: the skill filter no longer drops entries on length, which deleted
  the blobs that at least contained the technology names while keeping the
  boilerplate; the schema bound replaced it, and the filter now only rejects
  short non-skills (degrees, years-of-experience, "Equal Opportunity Employer")


### Added

- Layer 3: `skill_vocabulary` — a technology vocabulary learned from the corpus
  of already-parsed ads, scanned against every JD so a technology outside the
  operator's pool is not lost when the model omits it. Recurrence is the
  filter: a term must have been extracted from 5+ distinct listings, since a
  hallucination does not recur across unrelated ads. Over 767 jobs that keeps
  237 terms and discards 4,340 one-off strings. Build with
  `python -m src.cli.vocabulary --rebuild`
- Layer 3: a pool skill the JD names outright is added to `required_skills`
  whatever the model returned. The model under-reports — on one ad it returned
  "experience working with LLMs" and dropped the "(e.g., GPT-3/4, Claude,
  Mistral)" that followed, on another it extracted Java and missed Python from
  a Python job. Only exact word-boundary matches are added, so this can never
  introduce something the ad does not say
- Layer 3: `parse_eval` table and `python -m src.cli.parse_eval` record the
  prompt as sent and the object as returned for every parse, so a parser change
  can be compared over the same listings instead of judged from a handful of
  parses read off the terminal

## [v2.0.1] — 2026-08-09

### Fixed

- Layer 3: the local model no longer omits `required_skills`, `nice_to_have` and
  `responsibilities` — it dropped them from 71% of real JDs, zeroing the skill
  component of `fit` (0.165 of the final score)
- Layer 3: a null `role_level` is read as unknown instead of throwing the whole
  call away — Groq rejects the tool call server-side, wasting a full call
  against a daily token cap
- Layer 4: a listing with no `posted_at` is dated from `scraped_at` plus half
  the scrape window, instead of scoring as older than every band. LinkedIn
  supplies a posting date on 1 of 472 listings and is the only enabled source,
  so nearly every job was paying the maximum age penalty
- Layer 4: with no timestamp of any kind, recency scores neutral (`unknown`,
  0.65) rather than worst — an absent measurement is not a stale posting
- Layer 7: `not_applied` rows record `fit`/`success_prob`/`recency`/`final`;
  all 693 existing rows have NULL scores because the result was discarded
- Layer 7: company-cooldown and disallowed-location rejections are persisted
  instead of dropped for not yet existing in `all_jobs` — neither reason had
  ever been recorded, across 693 rows
- Layer 3: a provider whose `base_url` still holds an unexpanded `${VAR}` fails
  once per run with the missing variable named, instead of one httpx traceback
  per job (25 in the 2026-08-09 Actions run)

### Changed

- Layer 3: Ollama goes through Instructor + `Mode.JSON_SCHEMA` on the shared
  OpenAI transport; the bespoke `/api/chat` client is removed. Ollama 0.32.6's
  `/v1` does honour `response_format: json_schema`, correcting the earlier note
  that it was ignored — and Instructor also states the schema in the prompt,
  which is what stops fields being skipped
- Layer 3: the parse prompt asks for the three list fields explicitly and
  demands exhaustive skill extraction; they previously had no instruction at
  all, while every scalar field did
- Config: `llm.instructor_mode` is `JSON_SCHEMA`; `llm.api` removed
- Config: added `scoring.recency_score.unknown`

### Added

- `AUDIT_2026-08-09.md` — full data-flow audit with measurements
- Tests: verdict recording (scores persisted, pre-filter rejections kept) and
  recency inference from `scraped_at`

## [v2.0.0] — 2026-08-09

### Added
- Layer 6: applied tracking. Migration `0005_user_status_at` adds `applied.user_status_at` and an index on `user_status`; `/dashboard/applied`, `POST /api/jobs/{job_id}/status`, and per-status counts for the nav badges. Matches sort by score (what to look at next), Applied sorts by when the operator acted (a record). The system never submits an application, so confirming on return from the posting is the only way it can learn one happened.
- Layer 6: shared `_job_card.html` macro so the Matches and Applied views cannot drift apart, plus the did-you-apply modal in `base.html`.
- Layer 3: `llm.fallbacks` — an ordered provider chain replacing the single `llm.fallback` block. Each link gets its own backoff budget; a single `fallback:` mapping is still accepted. Ordered cheapest-first so free providers are exhausted before a metered one is reached. Adding a provider is a config edit, not a code change.
- Layer 3: `src/cli/llm_check.py` takes a provider argument — `--list cerebras` lists one provider's models, `llm_check cerebras` calls it directly. Both reach disabled entries, since a candidate the chain never reaches is never tested.
- Layer 1: `.github/workflows/pipeline.yml` — the pipeline as a manually-dispatched GitHub Actions job, so runs no longer require the operator's laptop to be awake. Triggerable from the GitHub mobile app. `schedule:` is present but commented, with the free-tier minutes arithmetic that explains why it is off. `concurrency` prevents overlapping runs, which the `data/run.lock` flock cannot do across runners.
- Layer 6: build-time pre-render (`endpoint/cache.py::prerender`) — matched jobs are rendered to S3 during the run and the Telegram buttons carry presigned URLs, so resume links work while the laptop is off. Amends hard rule #8 by explicit operator decision; stays an expiring cache (existing `{ext}_cache/` 1-month lifecycle + `render_cache` TTL), not a permanent pile. Config: `prerender.enabled`, `prerender.link_expiry_days`.
- Layer 6: operator dashboard — `/dashboard`, `/dashboard/skipped`, `/api/jobs`, `POST /api/run`, `GET /api/run/status`. Server-rendered Jinja + vanilla JS in the existing FastAPI process, no build step. Each row draws its score against the 0.50 threshold to scale rather than printing it; the skipped table flags anything within 0.05 of the line. Phone-first, dark-mode aware, reduced-motion respected.
- Layer 6: `src/endpoint/runner.py` — two run targets. "Here" spawns `python -m src.main` and streams its log to the browser; "GitHub" dispatches the workflow, which survives the laptop sleeping.
- Layer 9: `src/cli/export.py` — `python -m src.cli.export` regenerates the CSV index on demand.
- Layer 2: `src/cli/assets.py` — `push`/`pull` moves `master_profile.yaml`, `master_profile.json` and the resume template through S3. GitHub secrets cap at 48 KB and the profile is ~142 KB, so the bucket that already backs the render cache is the transport; no new service, no new secret.
- Layer 2: `rotation.current_terms()` and a `step` argument on `advance()` — several search terms per run, since manual triggering made one-per-run mean 24 clicks to sweep the list.
- AWS: `s3.cache_presigned_url()` — time-limited public URLs for cached renders, clamped to the SigV4 7-day maximum. Needs only `s3:GetObject`, already granted.

### Changed
- Layer 3: `llm.jd_text` (`head_chars` 3000, `tail_chars` 1500) bounds how much of a description reaches the model, keeping BOTH ends. Head-only truncation was measured against 667 real listings and rejected: pay is stated in only a third of them and the first mention sits a median 77% of the way in, so a head-only cut at 1,500 chars would have destroyed the salary signal in 72% of the listings carrying one. Measured over 400 jobs: 19.4% fewer characters, worst case 11,812 -> 4,545. Deliberately conservative — clipping harder measurably degraded `years_required` and `role_level`, which drive `success_prob`.
- Layer 3: `JDParsed.role_summary` max_length 500 -> 1500. Providers that validate tool calls server-side reject the whole call when a bound is exceeded, and the retry costs a full call's tokens; a 632-character summary did exactly that.
- Config: Groq is the verified primary (`llama-3.3-70b-versatile`, `TOOLS`), confirmed by a real structured call. Cerebras is present but disabled and pinned so by a test — its key authenticates and `models.list()` returns three models, but a real call returns `402 payment_required`, failing the free-tier rule (#12).
- Layer 3: budget exhaustion advances the provider chain instead of aborting the run. A spend cap is one account's problem; see Fixed for the final abort rule.
- Config: `endpoint.render_cache_ttl_days` (90) replaces the hardcoded 30-day `_CACHE_TTL_DAYS`, read per call. Must match the S3 lifecycle rule on the `{ext}_cache/` prefixes.
- `CHANGELOG.md` is now tracked by git — it records what changed in the code, so it belongs to the repo rather than to one instance.
- Layer 9: the CSV index is DERIVED from Postgres by `analytics.export_index()` rather than appended per job. A phone-triggered run executes on an ephemeral runner whose filesystem is destroyed at job end, so inline appends would be lost — but every column is derivable from `all_jobs`/`applied`/`not_applied`, so regenerating on the laptop picks up remote runs automatically. Idempotent (exporting twice yields identical files) and written atomically via temp file + `os.replace`. Invoked at the end of a local run, from the new CLI, and on dashboard load; skipped when `GITHUB_ACTIONS` is set.
- Layer 9: monthly report writes `data/reports/report-YYYY-MM.txt` instead of appending to a Google Doc.
- Layer 6: `docker-compose.yml` publishes the endpoint on `127.0.0.1:8000` only. The endpoint and dashboard have no authentication, so the port must not be on the LAN; remote access is `tailscale serve --bg 8000`, making the private network the security boundary.
- Layer 6: `resolve_endpoint_base_url()` drops the `localhost:4040` ngrok probe and becomes a pass-through — Tailscale hostnames are stable, so there is nothing to discover, and this removes a network call from every match notification. Now also strips a trailing slash.
- Layer 1: `scripts/start_bot.sh` replaces the ngrok block with a Tailscale check that reports the `.ts.net` dashboard URL, and no longer loops the pipeline by default.
- Layer 2: `scraper.hours_old` raised 2h→24h (peak) and 5h→48h (off-peak). A 2-hour window showed almost nothing on a manually-triggered run; the company cooldown and the `all_jobs` duplicate check prevent the wider window re-notifying.
- Layer 8: `send_match_notification` accepts `resume_urls`, preferring presigned S3 links over endpoint links per format, falling back where a format failed to pre-render.
- Config: `analytics` block replaced (`sheets`/`docs` → `local`/`report`); added `scraper.terms_per_run` (1), `prerender`, and `endpoint.dashboard`.

### Fixed
- Layer 3: ordinary Groq throttling was read as an exhausted account, ending a live run after 5 of 21 jobs. `_BUDGET_ERROR_MARKERS` contained `billing`, and Groq appends an upsell URL (`.../settings/billing`) to every rate-limit message — so a limit that clears in three seconds sent the run to a spend-capped fallback and abandoned it. Classification is now ordered: per-day quota -> exhaustion, per-minute rate -> transient, spend cap -> exhaustion. `billing` is gone and `payment_required` added, since removing it had left the Cerebras 402 paywall looking transient.
- Layer 3: the retry loop ignored the wait the provider stated. Groq replies "Please try again in 11.344999999s"; backing off on a guess (2s, 4s, 8s) retried before the window reopened, so every attempt was refused and the budget was spent on nothing. The stated wait is now honoured (+0.5s margin); exponential backoff applies only when no hint is given, and hints beyond 60s are ignored as daily quotas.
- Layer 3: exhausting the chain raised `LLMBudgetError` whenever ANY provider was out of budget, so a briefly-throttled Groq plus a spend-capped Gemini abandoned a batch at 4 of 13 jobs — Groq's window reopens in seconds and the remaining jobs were servable. The rule is now whether the NEXT job can succeed: abandon only when an account is out of budget AND no provider merely ran out of patience. A retired model plus a capped fallback still aborts, as it should.
- Layer 3: `JDParsed` list fields accept an explicit `null` as empty. Models emit `"responsibilities": null` rather than omitting the key, and a default only applies to an absent key — server-side tool validation rejected the call for it.
- Layer 8: a single unreachable link discarded an entire match notification. Telegram refuses the whole message over one bad button url, and `endpoint.base_url` still held its shipped `http://localhost:8000` placeholder, so a scored match with a built resume was silently lost. Unreachable buttons (localhost, 127.0.0.1, 0.0.0.0, ::1, `.local`, non-http) are now dropped and logged as `notification_button_dropped`, and a refused send is retried once without markup. The prerender tests had passed `http://localhost:8000` as the endpoint base url, which is what hid this.
- Tests: `tests/test_smoke.py` asserted `CHANGELOG.md` stays untracked; it is now deliberately tracked.
- Layer 3: `src/cli/llm_check.py` could not see `.env`. Only `db.py`, `notifications.py` and `aws_check.py` loaded it, so a CLI importing none of them saw no keys at all — the key-verification tool reported "NO KEY" for keys that were set. `load_dotenv()` now lives in `src/config.py`, which every entrypoint imports; it does not override a real environment, so Actions secrets still win.
- Layer 6: dashboard CSS and JS rewritten against the current templates (they still targeted the pre-macro markup, and the run button was wired to a `#run-target` select that no longer exists). Adds the did-you-apply flow, `.js-status` handling, toasts, and an `--on-fill` token fixing ~2.6:1 contrast on filled buttons in dark mode.
- Tests: `tests/test_v2_llm_retry.py` hung forever instead of finishing. Left unconfigured, structlog falls back to its development renderer, whose rich exception formatter pretty-prints every frame local — and the tests pass a `MagicMock` client into code that logs `exc_info`, so rich recursed through the mock's infinitely auto-generated attributes. `tests/conftest.py` now configures structlog session-wide to match `src/main.py` (JSON renderer, no rich). Production was never affected.
- Layer 3: four retry tests asserted a single provider's attempt count but called `complete()`, which since the fallback landed runs the sequence twice and reports a combined error. They now drive `_complete_with("primary", …)`; fallback orchestration keeps its own tests.

### Removed
- Layer 9: Google Sheets and Docs entirely — `gspread`, `google-api-python-client`, `google-auth`, `google-auth-oauthlib`; `GOOGLE_SHEETS_ID`, `GOOGLE_DOC_ID`, `GOOGLE_APPLICATION_CREDENTIALS`; the service-account bind mount from `docker-compose.yml`; and the three per-job `analytics.append_*_row` call sites with the `dry_run` guard they needed.
- Layer 6: ngrok. It published an unauthenticated endpoint to the whole internet on a guessable URL scheme (`/resume/{job_id}` where `job_id` is the portal's own public posting id), which a dashboard listing every match would have turned into a one-request harvest.

## [v1.0.0] — 2026-08-08

First tagged release. The nine-layer pipeline running end to end: LinkedIn scraping, Gemini parsing, deterministic scoring and selection, on-demand resume rendering, Telegram delivery, Google Sheets index and monthly Docs report, ngrok-published endpoint.

### Fixed
- Layer 8: match notifications never sent. `send_match_notification` read `settings.selection.apply_threshold`, but `apply_threshold` is defined only under `scoring:` in `config.yaml`; `Section.__getattr__` raised `AttributeError` before the Telegram send, which `src/main.py` swallowed as `notification_error`. Now reads `settings.scoring.apply_threshold`, matching `src/scorer/apply_decision.py`. Added two regression tests that assert against the real settings object so a config rename cannot reintroduce it.

### Added
- Doc: `README.md` version-history section recording v1.0.0 and the planned v2.0.0 scope.

### Changed
- Doc: rewrite `README.md` — it still described Iteration 0 ("scaffold, no business logic yet") and the pre-pivot design, documenting `playwright install chromium`, `answer_bank_seed.example.yaml`, and a `src.cli.queue` command that no longer exist. Now reflects Iteration 3.2: per-layer status table, the nine-layer architecture diagram, locked selection formula, the no-auto-apply rationale, Docker two-role deploy, the real CLI surface (`dryrun`/`inspect`/`reparse`/`report`/`aws_check`), config and `.env` reference, and current limitations (Layer 1 stub, laptop + ngrok hosting, Indeed/Glassdoor disabled, Gemini free-tier quotas, 5-page resumes).
- Layer 2: temporarily scrape LinkedIn only (dropped `indeed` and `glassdoor` from `scraper.sites`) — `apis.indeed.com` is timing out from the current network, failing the run. Re-add once connectivity is restored.
- Layer 3/5: widen Gemini retry backoff (initial 2s→20s, max 60s→90s, attempts 3→5) so calls survive the free-tier 5-requests/minute rate limit, whose server-suggested waits (~19-55s) the previous ~6s total budget never honored. Also cushions transient 429s on paid tier.
- Layer 3/5: switch Gemini model from `gemini-2.5-flash-lite` to `gemini-2.5-flash`. Free-tier request quota is per-model per-day; flash-lite is only 20/day (repeatedly exhausted, causing 429s) whereas flash is 250/day. Corrected the stale config/`.env.example` comments that claimed flash-lite had higher free-tier RPD.
- Layer 8: Telegram message formatting — removed emojis, added line breaks and bold headings for readability. Match notification now shows threshold (e.g., "threshold: 0.50") to give context on match quality. Button labels simplified (Apply, Resume PDF, Resume DOCX). Dry-run summary improved with better field names (Matched instead of Applied).

## [Iteration 3.2] — 2026-06-26

### Added
- Layer 8: `resolve_endpoint_base_url()` in `src/config.py` — at pipeline runtime, queries the ngrok local API (`localhost:4040`) for the current public HTTPS tunnel URL and uses it for Telegram resume links; falls back to `config.yaml` `endpoint.base_url` when ngrok is not running. Eliminates manual config edits on each ngrok restart when running locally.
- Layer 9: implement Google Sheets index + monthly Docs report in `src/analytics.py` (was a stub). Three Sheets tabs — Matches (every job ≥ 0.50: date, company, role, score, salary, source, Apply/PDF/DOCX links, status, gap skills), Skipped (in-field LOW_SCORE jobs with reason + JD snippet), Near-duplicates (deduped reposts linked to their original). Tabs/headers auto-created on first write. Monthly report aggregates the last 30 days (skill demand with a 30% alert threshold, recurring gaps, hiring companies, salary ranges), synthesizes prose via a single Gemini call (`MonthlyReportLLM` schema), and appends a dated section to the Google Doc. All writers are best-effort: a Google failure or missing config logs and returns, never crashing a run or rolling back DB writes. gspread/Docs clients are lazy + cached.
- Layer 9: `src/cli/report.py` — `python -m src.cli.report` triggers the monthly Docs report (monthly cron / manual).
- Layer 1 (local): `scripts/start_bot.sh` — one-command local runner. Brings up the Docker resume endpoint, starts ngrok on the reserved static domain if not already running, and loops a LIVE pipeline run every N minutes (`./scripts/start_bot.sh [minutes]`, default 40; `0` = run once). The laptop-local stand-in for Oracle cron; Ctrl+C stops the loop and any ngrok it started while leaving the endpoint up.

### Fixed
- Layer 2: LinkedIn now geo-filters to India. JobSpy's `country_indeed` only filters Indeed/Glassdoor — LinkedIn ignores it and was returning worldwide listings. Added a `scraper.location` config ("India") passed through `scrape()` → JobSpy's `location` param (LinkedIn's only geo-filter; also refines Indeed/Glassdoor within the country).
- Layer 8: Telegram resume buttons were silently dropped (`InlineKeyboardMarkup.inline_keyboard` is read-only in python-telegram-bot 21.x). Build the button row first, then construct the markup once, so Apply/PDF/DOCX render again.

### Changed
- Layer 8: richer Telegram match message — location now combines work mode + city (`📍 hybrid · Bangalore, India`), CTC is always shown with an explicit `💰 CTC not listed` fallback when the listing omits salary, and gap skills get a `⚠️` prefix. Apply/PDF/DOCX remain tappable buttons. Same fields flow to the Sheets index via `match_display_fields`.
- Layer 8: `match_display_fields()` in `src/notifications.py` — factors the shared row fields (title, salary, location, apply URL) so the Telegram message and the Sheets index never disagree.
- Layer 6/8: `config.yaml` `endpoint.base_url` set to the operator's reserved static ngrok domain (`https://murky-bonding-epileptic.ngrok-free.dev`) so resume links survive ngrok/laptop restarts; `scripts/start_bot.sh` binds ngrok to that same domain via `--url`. `resolve_endpoint_base_url()` still prefers the live tunnel at runtime, with this as the durable fallback.
- Layer 9: `docker-compose.yml` now bind-mounts `config/google_service_account.json` (read-only) into both services — previously commented out pending Layer 9.
- Layer 9: `src/main.py` hooks the Sheets index into the pipeline — appends a Matches row after each live notification, and a Skipped/Near-duplicate row from the not-applied path. Gated to live runs (dry runs do not pollute the real index).
- Tests: `tests/test_iteration_3_analytics.py` — row content, tab/header creation, disabled-is-noop, Google-failure-is-swallowed, monthly-report config gate (Google APIs mocked).
- Doc: `job_automation_architecture.md` Iteration 7 — promoted the "small dashboard" bullet to a concrete spec for a **local browser dashboard** served by the existing FastAPI app (`/dashboard` matches/skipped/job-detail views + `/dashboard/run` one-off pipeline trigger + scrape-frequency control). Local-only, never exposed via ngrok, no auth (instance-ready, not SaaS). Routes/templates remain a follow-up build.

## [Iteration 3.1] — 2026-06-12

### Added
- Deployment (Layer 1 / Iteration 5): `Dockerfile`, `docker-compose.yml`, `.dockerignore` — one `python:3.11-slim` + `libreoffice-writer` image serving two roles via docker-compose: an always-on `endpoint` service (`uvicorn src.endpoint.app:app`, port 8000, `restart: unless-stopped`, `/health` healthcheck) and an on-demand `pipeline` service (`python -m src.main`, `manual` profile, fired by host cron via `docker compose run --rm pipeline`). Per-operator secrets/profile (`.env`, `master_profile.*`, service-account JSON) are `.dockerignore`d and bind-mounted at runtime, never baked into the image (rule #18). `master_profile.json` is a shared writable mount (pipeline writes, endpoint reads); a named `hf_cache` volume persists the MiniLM embedding model across runs. The same image + compose file is the instance-ready distributable for a second operator (rule #21).
- Deployment: the Dockerfile installs `ttf-mscorefonts-installer` (Debian `contrib`, Microsoft EULA accepted non-interactively) so LibreOffice renders the template's **Arial** font exactly instead of substituting Liberation Sans. Verified — the rendered PDF embeds `ArialMT`/`Arial-BoldMT`/`Arial-ItalicMT`/`Arial-BoldItalicMT`.
- Tests: `tests/test_iteration_2_endpoint.py::test_assemble_docx_preserves_template_formatting` — asserts cloned experience titles keep the template `<w:tabs>`, bullets keep `<w:numPr>`, and the EDUCATION/CERTIFICATES tail stays byte-identical (clone-and-substitute fidelity, hard rule #9).

### Changed
- `src/main.py`: bump the `run_started` log field `iteration` from `2` to `3` to match the current iteration.
- Doc: `job_automation_architecture.md` Iteration 5 (Production Deployment) section rewritten to specify a **Docker-based** deployment — one `python:3.11-slim` + `libreoffice-writer` image, two compose services (always-on `endpoint` via uvicorn + on-demand `pipeline` via `python -m src.main`), Layer 1 scheduler as host cron firing `docker compose run --rm pipeline` (replaces the prior systemd plan). Documents that the same image/compose file doubles as the instance-ready distributable for a second operator, and the arch-aware torch handling (x86 `+cpu` wheel vs arm64 PyPI build) for either Oracle shape.
- Layer 6 (resume assembler): rewrote `src/endpoint/assembler.py` to **clone the template's paragraphs and substitute text only**, instead of rebuilding paragraphs from style names. The old `_make_para`/`_add_run` approach produced bare `<w:p>` elements with only a `pStyle` and runs with no `<w:rPr>`, discarding all of the template's direct formatting — so rendered resumes had wrong/inconsistent fonts and sizes, no bullet glyphs (`<w:numPr>` lost), dates not tab-aligned (`<w:tabs>`/`<w:tab/>` lost), and wrong margins (`<w:ind>` lost). A second bug: `_section_map` captured paragraph indices once and `_replace_section` then mutated the body, so later sections used **stale indices** — SKILLS categories landed under the PROJECTS heading and the EDUCATION heading plus most of its content were dropped. The new assembler deep-copies prototype blocks (first experience block, first project block, a skill line) before any mutation, substitutes text via run-preserving helpers (`_set_tabbed` splits at the `<w:tab/>`; `_set_text`/`_set_skill` keep each run's `rPr`), clones one bullet paragraph per selected bullet (handles variable counts), locates section boundaries by **live element identity** (not indices), and leaves the header, EDUCATION, and CERTIFICATES byte-identical to the template (now diff-validated against a c14n snapshot — hard rule #9). Output renders identically to the template's own layout with only the text changed. Experience titles and project names are uppercased to match the template convention (the `IntenseReference` char-style applies smallCaps, which renders uniform full caps only on already-uppercase text).

### Fixed
- Layer 6 (resume endpoint): fix `src/endpoint/hyperlinks.py` emitting the rewritten `word/_rels/document.xml.rels` under a namespace prefix (`<ns0:Relationships>`). `_REL_NS` was set to the *officeDocument* relationships namespace, but the `<Relationships>`/`<Relationship>` container elements live in the *package* relationships namespace — so re-serialization registered the wrong default namespace and ElementTree auto-prefixed the real one. Word tolerated it, but LibreOffice refused to load the file (`source file could not be loaded`), making **every** PDF render of a resume with a project link fail (`render_failed` / HTTP 500). Also: `_patch_rels` now *adds* a new external hyperlink `Relationship` for the assembler's synthetic `rIdProj*` ids (it previously only updated already-existing ids, leaving dangling `r:id` references). Verified end-to-end — the endpoint renders a valid multi-page PDF inside the Docker container. Corrected `tests/test_iteration_2_endpoint.py::test_update_project_hyperlinks`, which had baked in the same wrong namespace and so never caught the failure; it now asserts prefix-less output and the add-missing-rId path.

## [Iteration 3] — 2026-06-11

### Added
- Layer 8: `src/aws/cloudwatch.py` — `build_handler()` wires a watchtower `CloudWatchLogHandler` to the existing `get_session()` boto3 session. Stream name is `YYYY-MM-DD` (one stream per day). Gracefully returns `None` (never raises) when `AWS_CLOUDWATCH_LOG_GROUP` is unset or any AWS error occurs, so local dev works without credentials. `create_log_group=False` enforces that the log group must pre-exist (IAM constraint — runtime user has `logs:CreateLogStream/PutLogEvents` only).
- Layer 8: wired `build_handler()` into `_configure_logging()` in `src/main.py` and `src/cli/reparse.py` — every structlog JSON event now ships to CloudWatch Logs when `AWS_CLOUDWATCH_LOG_GROUP` is set.
- Tests: `tests/test_cloudwatch_handler.py` — 4 moto-backed tests: absent env var returns None, configured handler returns CloudWatchLogHandler with correct group/stream, session error returns None gracefully, full JSON round-trip verifies events land in the mocked stream.
- CI: `.github/workflows/ci.yml` — GitHub Actions pipeline running on every push to `main` and every pull request. Checks out the repo into a clean Ubuntu runner, sets up Python 3.11 (with pip wheel caching), installs `requirements.txt` from scratch (honest reproducibility check for rule #21), and runs the full `pytest` suite. No secrets required — tests mock Gemini, the DB, boto3 (moto), and FastAPI (TestClient).

### Changed
- Logging: added `structlog.processors.format_exc_info` to the processor chain in `_configure_logging` (`src/main.py` and `src/cli/reparse.py`) so `exc_info` renders as a full traceback in the `exception` JSON field (previously it emitted a useless `"exc_info": true`). Added `exc_info` to four critical error paths: `gemini_failure` (`llm/client.py`), `scrape_site_failed` (`scraper/jobspy_wrapper.py`), `master_profile_validation_failure` (`main.py`), and `resume_request_failed` (`endpoint/app.py`). The two loop-exhausted paths pass the captured exception instance (`exc_info=last_error`/`last_exc`) rather than `True`, since they log outside the `except` block.
- `src/cli/dryrun.py`: implemented — thin wrapper that calls `src.main` with `--dry-run`, so `python -m src.cli.dryrun` now works as documented.
- `src/cli/reparse.py`: implemented — calls `master_profile.rebuild(force=True)` and logs the upsert/deactivation counts.
- `src/cli/inspect.py`, `src/scheduler.py`, `src/analytics.py`, `src/state/cleanup.py`: stripped "Iteration 0 stub" labels; files kept (required by `test_repo_layout`) with docstrings scoped to their future iterations.
- `requirements.txt`: pin torch to the CPU-only wheel (`torch==2.12.0+cpu` via `--extra-index-url https://download.pytorch.org/whl/cpu`, listed before `sentence-transformers`) so installs pull the ~200 MB CPU build instead of the ~2 GB default CUDA build (no GPU in use — dry-run runs `device_name: cpu`).
- `requirements.txt`: install the `en_core_web_sm` spaCy model via direct wheel URL (pinned `3.8.0` to match `spacy==3.8.2`) so a single `pip install -r requirements.txt` provisions everything — no separate `python -m spacy download` step.

### Fixed
- Logging: `notifications.py` no longer silently swallows a failed Telegram keyboard build — it logs `notification_keyboard_failed` (with `job_id`, `error`) before falling back to a keyboard-less message. This was the only silent `except` in the codebase.
- Logging: deduplicated double-counted error events that would inflate CloudWatch metric filters. A single PDF-conversion failure previously logged `render_failed` three times (pdf_convert → cache → app); the middle re-log in `endpoint/cache.py` is removed and the endpoint catch-all in `endpoint/app.py` is renamed to `resume_request_failed` (with `status=500`) to distinguish a request-level failure from the LibreOffice-specific `render_failed`. Likewise the duplicate `s3_cache_failed` in `endpoint/cache.py` is renamed to `s3_cache_degraded` (a `warning`, since it signals graceful degraded-mode serving — `aws/s3.py` already logs the raw `s3_cache_failed`).
- Logging: orchestrator `BUILD_FAILURE` in `main.py` now carries `reason="selection_returned_none"`; the two builder-side `BUILD_FAILURE` events in `builder/llm_call.py` carry `caller="builder"` so the orchestrator and builder events are distinguishable.
- `config.yaml`: corrected `aws.s3_bucket` from the non-existent `job-bot-vishnujan` to the real bucket `application-bot-vishnujan-resumes` (matches `.env`'s `AWS_S3_BUCKET`). The app reads the bucket from config (`settings.aws.s3_bucket`), so it was previously targeting a bucket that doesn't exist; `aws_check.py` (which reads `AWS_S3_BUCKET` from env) had been silently verifying a different name.
- Layer 5: prompt instruction 2 now explicitly states each skill may appear in AT MOST ONE category. Gemini was repeatedly placing the same skill (e.g. `Python`, `SQL`) in multiple categories, causing `BUILD_FAILURE` after all 3 retries. The validator already caught the violation; the prompt now prevents it.
- CI: add a tracked `resumes/applied/.gitkeep` so the gitignored, normally-empty `resumes/applied/` directory exists on a clean checkout (mirrors `resumes/templates/`); fixes `test_repo_layout` failing on the runner.
- CI: supply a throwaway `DATABASE_URL` to the `pytest` step in `ci.yml`. `src/state/db.py` builds the SQLAlchemy engine at import time, so importing `src.endpoint.app` requires the var even though the engine never connects and the DB tests mock `session_scope`; fixes the three endpoint tests failing with `RuntimeError: DATABASE_URL is not set` on the runner.

## [Iteration 2.4] — 2026-06-11

### Changed
- Layer 2 (scraper): `_row_to_job` now strips leading punctuation/separators/whitespace from scraped titles via the new `_clean_title` helper (e.g. Indeed's `": Data Engineer | Azure"` → `"Data Engineer | Azure"`). Opening brackets `(`/`[` are preserved so `"(Remote) Backend"` keeps its bracket; a title that is entirely punctuation collapses to None (unusable, dropped like a blank title). Also fixed a stale `dedup.resolve_batch` reference in the module docstring.

## [Iteration 2.3] — 2026-06-11

### Added
- Layer 7: migration `0004_add_serial_id` — adds `id BIGSERIAL` to `all_jobs` for deterministic insertion-order queries (`ORDER BY id`); existing rows backfilled in heap order, new rows append with the next id.

### Changed
- Layer 4 (scoring): score each pool skill against the **best individual JD skill** (`max(cosine(pool_skill, jd_skill))` over per-skill embeddings) instead of one blended centroid of all required+nice-to-have skills. The old blended vector compressed exact matches catastrophically (pool `Python` vs a JD requiring `Python` scored ~0.13; `SQL` ~0.26; `AWS` ~0.35), holding `avg_skill_pool_match` near ~0.34 and pinning genuine matches just under the 0.50 apply threshold — e.g. a real Data Engineer JD scored 0.483 and was rejected. After the fix that same job scores 0.570 (`avg_skill` 0.34→0.86, exact matches →1.0) and clears threshold, while off-domain jobs (Civil Site Engineer 0.434, Kyriba Consultant 0.336) still correctly fail. No threshold change needed. `src/scorer/selector.py`: `JDContext.vec_skills` (single `Vector`) replaced by `jd_skill_vecs` (`tuple[Vector, ...]`); `build_jd_context` now embeds each skill individually in the same single batched call (no extra Gemini/embedding round-trips); `select_skill_candidates` uses the max-match. Bullet/experience/summary scoring left holistic against `vec_match`/`vec_role` (architecture §4.2/§4.3). **Overrides the locked architecture §4.1/§4.5 (user-approved); doc updated to match (rule #20).**
- Layer 2 (scraper): removed near-duplicate cosine dedup entirely — only exact `job_id` matches are deduped now (within a scrape call, plus the orchestrator's `existing_job_ids` check against `all_jobs`). Every job with a new ID is scored unconditionally. Deleted the now-orphaned `src/scraper/dedup.py` (`find_near_duplicate`, `canonical_embeddings`, `resolve_batch`, `DedupOutcome`); `src/main.py` no longer calls it and adds passing jobs to the session directly after embedding.
- Layer 3 (parser): removed the role-cluster acceptance gate — jobs are no longer rejected with `ROLE_MISMATCH` for falling outside a search term's accepted categories; all jobs passing the Layer-2 hard filters now reach scoring, and Layer 4's match score is the sole relevance filter. Removed `cluster_for_term`/`role_accepted` from `src/parser.py` and the role-acceptance block from the orchestrator.
- Layer 3 (parser): `jd_parse_prompt` no longer receives a restricted `role_categories` allow-list; the `role_category` field is retained (still used by deterministic summary selection) but Gemini classifies it as a free-form slug.
- `config.yaml`: removed `scraper.near_duplicate_threshold`, `scraper.near_duplicate_lookback`, and the entire `parser.role_clusters` block (~95 lines of cluster definitions).
- `src/reasons.py`: removed the `NEAR_DUPLICATE` and `ROLE_MISMATCH` reason constants.
- `src/state/models.py`: added the `id: BigInteger` column (`BIGSERIAL` server default) to `AllJobs`.
- Tests: removed the dedup `find_near_duplicate`/`resolve_batch` tests and the `role_clusters` smoke tests; updated scorer tests for `jd_skill_vecs` (the `jd()` helper and the `build_jd_context` batch-order assertions).

### Removed
- Data: deleted 81 empty-`jd_text` rows (LinkedIn throttle artefacts from earlier runs) from `all_jobs` and `not_applied`.

### Added
- Layer 5: `src/builder/llm_call.py` — Gemini Call 1b driver (title alias + skills categories + cover letter → `StoredSelection`); regenerates up to `config.builder.llm_regenerate_attempts` times on validation or voice failure before returning `None` (BUILD_FAILURE).
- Layer 5: `src/builder/skills_validator.py` — post-generation validation of Call 1b skills output against pool candidates and gap-skills source sets (hard rules #1, #7).
- Layer 5: `src/llm/schemas.py` — added `SkillCategory`, `StoredSkills`, `ResumeBuildLLMOutput` (Call 1b schema), `SelectedExpEntry`, `SelectedProjEntry`, `StoredSelection` (~2 KB selection blob written to `applied.selection_json`).
- Layer 5: `src/llm/prompts.py` — added `build_prompt()` and `build_system()` for Gemini Call 1b.
- Layer 6: `src/endpoint/` — FastAPI app serving `/resume/{job_id}.pdf|.docx` on demand (`app.py`), DOCX assembler with structural detection and section reorder (`assembler.py`), project hyperlink updater (`hyperlinks.py`), LibreOffice PDF converter (`pdf_convert.py`), S3 render-cache orchestrator with 1-month TTL (`cache.py`).
- Layer 7: `src/aws/` — `iam_session.py` (cached `boto3.Session` from env, hard rule #18) + `s3.py` (S3 cache put/get and daily selection_json backup export).
- Layer 8: `send_match_notification()` in `src/notifications.py` — per-match Telegram message with apply + PDF + DOCX inline keyboard buttons (architecture §8 format).
- `config.yaml`: added `aws.region` / `aws.s3_bucket` and `endpoint.base_url` / `endpoint.port` / `endpoint.template_path` sections.
- Tests: `tests/test_iteration_2_builder.py` (10 tests — skills_validator and llm_call with stub transport), `tests/test_iteration_2_endpoint.py` (7 tests — assembler, hyperlinks, cache, FastAPI routes), `tests/test_iteration_2_notifications.py` (2 tests — match notification content).

### Changed
- Layer 2: `src/scraper/jobspy_wrapper.py` — `scrape()` now fetches LinkedIn descriptions (`linkedin_fetch_description`) so LinkedIn jobs carry real `jd_text` (previously empty, which starved the parser and made every empty-text embedding identical → mass false near-duplicates). Added anti-rate-limit measures (hard rule #4): per-site scraping isolation, per-site retry with exponential backoff + jitter, randomised inter-site delay, a lower LinkedIn results cap, and optional proxies — so one throttled portal no longer discards the others' results.
- Layer 2: `src/main.py` — drops listings with empty `jd_text` before embedding (`EMPTY_JD`, logged, not persisted so a transient throttle is retried next run); passes the new `scraper.rate_limit` config into `scrape()`.
- `config.yaml`: added `scraper.linkedin_fetch_description` and the `scraper.rate_limit` block (`per_site`, `max_retries`, `backoff_base_seconds`, `inter_site_delay_seconds`, `linkedin_results_wanted`, `proxies`).
- `src/reasons.py`: added `EMPTY_JD` reason constant.
- Tests: `tests/test_iteration_2_scraper.py` — added per-site failure-isolation/retry test and LinkedIn cap + description-flag test.
- `src/main.py` — replaced `NotImplementedError` stub with full end-to-end pipeline: single-run lock → master_profile rebuild → scrape → L2 hard filters → batch embed → near-duplicate dedup → L3 parse + role acceptance → L4 scoring → L5 build selection → persist `applied` + company cooldown → L8 notify → advance rotation.
- `config.yaml` `builder.template_path`: corrected filename to `resumes/templates/Templete.docx` (matches actual file on disk).
- Tests: `tests/test_smoke.py` expected-paths list now includes `src/endpoint/` and `src/aws/` modules.
- `src/llm/schemas.py` docstring updated to document both Call 1a and Call 1b.
- `src/llm/prompts.py` docstring updated; `SelectionResult` import added.
- `.gitignore`: added `resumes/templates/` (operator-owned resume/cover templates carry personal info + real hyperlinks — rule #21) and `data/run.lock` (single-run lock file written by the orchestrator).
- `requirements.txt`: added `fastapi==0.115.6`, `uvicorn==0.34.0`, `httpx==0.28.1` for the Layer 6 resume endpoint (httpx backs `fastapi.testclient.TestClient`).

### Fixed
- `src/state/db.py` + `src/state/migrations/env.py`: strip all whitespace from `DATABASE_URL` at read time so editor-introduced spaces (e.g. `"require "` from a stale LazyVim buffer) never cause a connection failure.
- Layer 8 / orchestrator: `src/main.py` parse-failure log used reserved structlog kwarg `event=` (collides with the positional event name → `TypeError`); renamed to `reason=`.
- Layer 6: `src/endpoint/pdf_convert.py` render-failure log had the same `event=` collision; renamed to `stage=`.
- Layer 2: `src/main.py` `_write_not_applied()` could violate the `not_applied.job_id` → `all_jobs.job_id` FK and abort the whole commit when a pre-filter-rejected job was never inserted into `all_jobs`; it now writes rows only for job_ids present in `all_jobs`.

### Removed
- Layer 5: deleted the docstring-only stub modules `src/builder/assembler.py`, `src/builder/hyperlinks.py`, `src/builder/pdf_convert.py` — the real implementations live in `src/endpoint/` after the pivot; the empty placeholders served no purpose.

- `master_summaries.yaml` — operator's curated summary pool (50 entries, 10 each across data, ml, quant/fintech, backend, fullstack roles); outside-in slot template (role, doing, in, using, Tools); grounding notes enforce no fabricated metrics (fraud model is LR/notebook-only, trading bot is Binance testnet, no AWS/CI-CD claimed). Supersedes `master_summaries_pool.yaml` (kept as learning reference, not used by the bot). Source of truth for Layer 4 summary selection.

### Changed (operator data)
- `master_profile.yaml` summaries section: merged all 50 summaries from `master_summaries.yaml` into `master_profile.yaml`, replacing the single placeholder `summary_001`. Flattened the per-category structure to a flat list. Mapped `roles` → `role_categories` (schema field); added matching `tags` field. Preserved `surfaces` and `anchored_in` as extra YAML fields (Pydantic drops them at load time; kept for future matcher upgrades). Work experience, projects, skills pool, education, and certifications unchanged.
- `master_resume_extract.yaml` — structured extraction of raw resume material (bullet pool, job-title aliases, skills pool, project summaries) used to build `master_summaries.yaml` and `master_profile.yaml`; not consumed by the bot directly.

### Changed
- `.gitignore`: added `master_resume_extract.yaml`, `master_summaries.yaml`, `master_summaries_pool.yaml` to the user-owned-source-of-truth section (per CLAUDE.md rule #21, these live on the operator's disk, never committed to version control).
- Regenerated `master_summaries.yaml` from scratch, grounded entirely in `master_resume_extract.yaml`. Rewrote every summary "outside-in" (recruiter/non-specialist readable: what was built, at what scale, in what domain) — stripped named algorithms, loss functions, statistical tests, and niche-library jargon (Kruskal-Wallis, Dunn's post-hoc, Earth-Mover's-Distance, label-distribution learning, Haversine, IQR filtering, contrastive fine-tuning, BullMQ, TypeORM, atomic SQL reservation, 384-dim embeddings, VWAP) from the visible `text`, describing the "how" in plain language instead; jargon retained only in `surfaces` (matcher-only, never shown). Kept outcome numbers and scale (95% fraud caught, 0.995 ROC-AUC, 6.4M transactions, 20 years of prices, ~1,665 companies, 9.4M ticks, 1,359 signals, ~2,500 quotes, ~30 endpoints, 14 pages). 50 entries (10 each) across data / ml / quant_fintech / backend / fullstack, each with `id`/`roles`/`surfaces`/`anchored_in`/`text`. Refinement pass: experience written as numerals not words ("1 year" / "1.5 years", no verbal hedging); the `doing` slot now names mainstream tools inline where informative ("scraped in Python (Playwright)", "a NestJS backend streaming over WebSockets", "a React Native (Expo) app", "deployed on Railway and Vercel"); cross-project breadth summaries spell out each piece with its own scope/number and inline tool instead of bare project names. Recomputed experience math against today (2026-06-10): total 1.5 yr; Citesert-only summaries 1 yr. Honesty constraints enforced: fraud model framed notebook-only (no deployment claim), ticket-classifier and age/gender CNN carry no metrics, no trade-backtest P&L figure, trading bot stated as Binance testnet, DekhLaw frontend corrected to vanilla JS (not React) and is the only Railway+Vercel deploy, Docker limited to the e-commerce project, law-firm site carries no live/production claim, Directory-app FastAPI framed as an API surface the app talks to (scaffold-only), no AWS/CI-CD/Kubernetes claims.

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
