# Job Application Bot — Build Status

**Project:** Personal job-application assistant (single-operator instance)
**Current version:** v2.0.0 (see `CHANGELOG.md`)
**Active branch:** `fv2` — 276 tests passing, **not merged to main**
**Parent branch:** `v2/part11-local-inference` (pushed, also unmerged)
**Date:** 2026-08-09

> ⚠ A full data-flow audit on 2026-08-09 found one **critical** and one **high**
> severity defect in the live pipeline. See **`AUDIT_2026-08-09.md`**. Both are
> now fixed on branch `fv2` (extraction 71% empty → 0%; recency no longer
> pinned to the floor), but **scores written before 2026-08-09 were produced
> under both defects and should not be trusted**. No existing DB row has been
> repaired — that still needs a decision.

## Layer Completion Status

| Layer | Component | Status | Notes |
|-------|-----------|--------|-------|
| 1 | Scheduler | ✅ Real | GitHub Actions `pipeline.yml`, manual dispatch (cron off — free-tier minutes) |
| 2 | Scraper | 🟡 Degraded | JobSpy **LinkedIn only**; Indeed/Glassdoor commented out (`apis.indeed.com` timeouts) |
| 3 | JD Parser | ✅ Real | Instructor + `JSON_SCHEMA` on the shared transport; 0/12 empty on real JDs, median 14.5 skills |
| 4 | Scoring | ✅ Real | Recency inferred from `scraped_at` + scrape window when the portal gives no date |
| 5 | Resume Builder | ✅ Real | Deterministic selection → `selection_json` |
| 6 | Endpoint | ✅ Real | FastAPI `/resume/{job_id}.pdf\|.docx` + `/dashboard` |
| 7 | State | 🟡 Partial | Neon + pgvector working; new `not_applied` rows carry scores, but the 693 existing NULL rows remain |
| 8 | Notifications | ✅ Real | Telegram, presigned S3 links, build-time pre-render |
| 9 | Analytics | ✅ Real | Google Sheets index + monthly Docs report |

## LLM Setup (reworked 2026-08-09)

Chain is `ollama → groq → gemini`, configured under `llm` (primary) and
`llm.fallbacks` in `config/config.yaml`. Order is cost: free providers are
exhausted before a metered one is touched.

| Provider | Model | State | Notes |
|----------|-------|-------|-------|
| **ollama** (primary) | `qwen2.5:7b-instruct` | ✅ Live | Local, RTX 4050 6GB, runs on **Windows** not WSL. Instructor `JSON_SCHEMA` over `/v1`. `max_attempts: 1`. Unclipped JD. Not reachable from GitHub Actions — see open issue 11. |
| **groq** | `llama-3.3-70b-versatile` | ✅ Live | Verified 1.6s. Free tier 1,000 req/day **and 100,000 tokens/day** (~45 parses). The token cap is not exposed in rate-limit headers. Only provider a remote run can reach. |
| **gemini** | `gemini-2.5-flash-lite` | 🔴 Dead | Project at monthly spend cap. |
| **cerebras** | — | ⚪ Disabled | Key authenticates and `models.list()` works, but a real call returns `402 payment_required`. Pinned disabled by a test. |

`OLLAMA_BASE_URL` lives in `.env` because the WSL→Windows default-route host
changes every boot; `src/config.py` does `${VAR}` expansion.

Single-provider check: `.venv/bin/python -m src.cli.llm_check <provider>`
(`--list <provider>` for model ids). It reaches disabled entries too.

## Live Data Snapshot (2026-08-09)

```
all_jobs            745      every job now carries a verdict (0 unjudged)
  linkedin          472      posted_at present on 1 (0.2%)
  indeed            273      posted_at present on 273 (100%)
applied              63      59 marked "matched" in all_jobs.outcome
not_applied         693      0 rows have any score recorded
company_cooldown     51
```

Verdict reasons: LOW_SCORE 420, HARD_FILTER_LAYER_3 123, DUPLICATE 97,
ROLE_MISMATCH 22, PARSE_FAILURE 15, NEAR_DUPLICATE 14, BUILD_FAILURE 2.
No COMPANY_COOLDOWN or LOCATION_DISALLOWED rows exist — see audit issue 4.

Match rate by run: 10.6% (06-11), 25.9% (06-24), 24.2% (07-01), 13.8% (08-08),
**2.8% (08-09 backfill)**.

## Deployment

- **Pipeline:** GitHub Actions, manual dispatch from the phone. Cron is
  commented out — 2,000 free min/month vs ~5 min a run.
- **Endpoint + dashboard:** on the laptop, `http://localhost:8000/dashboard`.
- **Remote access:** Tailscale Serve (Windows-side), reachable at
  `https://asus-tuf-f16-vishnu.tail106cde.ts.net`. **ngrok was dropped in v2**
  — it published an unauthenticated endpoint to the whole internet on a
  guessable URL scheme.
- **Pre-render:** `prerender.enabled: true`. Matched jobs are rendered at build
  time to the S3 `{ext}_cache/` prefix (1-month lifecycle) so phone-triggered
  runs produce links that work while the laptop is shut. Presigned URLs capped
  at 7 days by SigV4.
- **Repo is PUBLIC.** Actions minutes are free but run logs are world-readable.

## Open Issues (from `AUDIT_2026-08-09.md`)

Fixed on `fv2` (code only — no DB row touched): issues 1, 2, 3, 4, 9, 10.

Still open, all needing a live-database write or a decision:

| # | Issue | Severity | Cost |
|---|-------|----------|------|
| 5 | 8 of 15 NEAR_DUPLICATEs are false positives | Medium | 8 jobs wrongly suppressed, never scored |
| 6 | 11 jobs in both `applied` and `not_applied` | Low | Contradictory verdicts, not root-caused |
| 7 | `role_level` disagrees across providers | Low | Up to 0.117 of final |
| 11 | Ollama unreachable from GitHub Actions | Medium | 100% of remote parses fall to Groq, which exhausted its daily cap mid-run |
| — | 693 existing `not_applied` rows have NULL scores | Medium | Only new rows carry scores; history stays blind |
| — | Re-run the backfill under the fixed parser | High | 171 jobs were judged with no skills extracted |

## Other Pending Work

- **Indeed/Glassdoor:** re-enable once `apis.indeed.com` timeouts resolve.
  LinkedIn-only is why issue 2 hits 100% of traffic.
- **Resume length:** 5 pages → reduce; driven by static template content, not
  the assembler.
- **Embedding pre-filter:** skip the parse entirely for obvious non-matches —
  the real token saving, at no accuracy cost (noted in the `jd_text_default`
  config comment).

## Test Coverage

- **276 unit tests pass** (~11s), `.venv/bin/python -m pytest -q`
- CI parity checked by running the suite from a tracked-files-only checkout
  with no `.env` (273 passed, 3 skipped) — this caught a fix that would have
  turned CI red, because `${OLLAMA_BASE_URL}` cannot expand on a runner
- Integration tested against real Neon + GCP + S3
- ⚠ Green tests have repeatedly not meant working. A config-key typo killed
  every Telegram notification for weeks; audit issue 1 sat under 269 passing
  tests because `[]` is schema-valid. **Prefer real verification.** The parser
  fix was accepted only after 12 real JDs went through `src.parser.parse()`
  against the live model, not because the suite went green.

## Config & Secrets

- `config/config.yaml` — tunables (thresholds, sources, filters, LLM chain)
- `.env` — secrets (API keys, DB URL, AWS/GCP creds, `OLLAMA_BASE_URL`).
  Gitignored. Never commit secrets.
- `master_profile.yaml` — single source of truth for resume content (user-edited)
- Template: `resumes/templates/Templete.docx` (typo preserved)

## Key Architecture Decisions Locked

1. **No auto-apply** — user applies manually; bot delivers tailored resume + link
2. **Resume rendering on demand** — no PDF pile; build-time pre-render is a
   cache with a 1-month TTL, not a pile
3. **Master profile immutable from code** — only read + validate
4. **LLM never writes or selects bullets** — selection is embeddings math
5. **Skills from pool** — LLM picks from pre-scored candidates + up to 4 gaps
6. **Max 2 LLM calls/job** — currently 1 (Call 1b removed; `max_calls_per_job: 1`)
7. **Notify every match >= 0.50** — no quotas
8. **Free tier only** — Neon, Gemini/Groq free, Telegram, GCP, AWS S3
9. **Instance-ready** — no operator identity hardcoded in `src/`

## Running Locally

```bash
# Endpoint + dashboard
.venv/bin/python -m uvicorn src.endpoint.app:app --host 127.0.0.1 --port 8000

# Pipeline
.venv/bin/python -m src.main
.venv/bin/python -m src.cli.dryrun        # NOTE: still WRITES to Postgres;
                                          # only the Telegram send is skipped
# Diagnostics
.venv/bin/python -m src.cli.llm_check <provider>
.venv/bin/python -m src.cli.inspect --job-id=XYZ
.venv/bin/python -m src.cli.backfill --dry-run
```

`python` is not on PATH — always use `.venv/bin/python`.

---

## Resume here — session handoff (2026-08-09)

### What changed this session

A full data-flow audit (`AUDIT_2026-08-09.md`), then fixes for everything it
found that could be fixed in code. All on `fv2`, 276 tests passing.

The headline: the 2.3% backfill match rate was **not** recency, which was the
working hypothesis going in. It was the local model omitting
`required_skills`, `nice_to_have` and `responsibilities` from 71% of JDs,
which zeroed 0.165 of the final score. Recency was a real but separate defect.
Both are fixed; extraction went 71% empty → **0% empty** on real JDs.

### The next decision (highest value)

**Re-run the backfill under the fixed parser.** 171 jobs were judged with zero
skills extracted, so their verdicts are meaningless. This rewrites live rows,
which is why it was not done unattended.

```bash
.venv/bin/python -m src.cli.backfill --dry-run     # count only, changes nothing
.venv/bin/python -m src.cli.backfill --limit 20    # work in bites
```

Before running it, know that Groq's 100k tokens/day cap was **exhausted on
2026-08-09** and Gemini is spend-capped, so the run depends on ollama being
up. Check first:

```bash
.venv/bin/python -m src.cli.llm_check ollama
```

### Also open, in rough priority order

1. **Ollama from CI** (audit issue 11). The tailnet already serves it —
   `http://asus-tuf-f16-vishnu.tail106cde.ts.net:11434` answered during the
   audit. Needs a `tailscale/github-action` step, an `OLLAMA_BASE_URL` secret,
   and a **decision on the ACL**: a tagged ephemeral OAuth client limited to
   port 11434 on the laptop. Ollama has no auth, so whatever routes to it can
   use the GPU. Not started — deliberately, pending that decision.
2. **8 false-positive NEAR_DUPLICATEs** — cosine 0.83–0.93 on corrected
   vectors, all different roles at the same company. They were suppressed
   without ever being scored. Needs a DB write.
3. **693 `not_applied` rows with NULL scores** — new rows now carry scores;
   history does not. Backfilling them is only possible for LOW_SCORE rows,
   whose final score survives as a string in `reason_detail`.
4. **11 jobs in both `applied` and `not_applied`** — not root-caused. Leading
   theory: one `job_id` arriving from two search terms in a single run, with
   `existing_ids` computed once at batch start.
5. **Indeed/Glassdoor re-enable** — LinkedIn-only is why the missing
   `posted_at` hit 100% of traffic.

### Things not to re-derive

- Recency's 0.22 coefficient is correct arithmetic; it just wasn't the cause.
  The control that settles it: the 08-08 live run had the same 0.30 recency
  floor and still matched 13.8% vs the backfill's 2.8%.
- `num_ctx` truncation is **refuted** — `prompt_eval_count` peaks at 2301
  tokens against a 4096 default.
- Ollama's `/v1` **does** honour `response_format: json_schema` (0.32.6). The
  old comment saying otherwise was stale and is what motivated the bespoke
  transport that has now been deleted.
- Green tests did not catch any of this. Verify against real JDs via
  `src.parser.parse()`, and check CI parity from a tracked-files-only checkout
  with no `.env` — that is how the `${OLLAMA_BASE_URL}` CI breakage was found
  before it turned CI red.
