# Job Application Automation Bot

Fully autonomous job application bot for **Vishnujan Narayanan**.
Scrapes Indian job portals (Indeed; Glassdoor from Iteration 4), scores
listings against a master profile, builds a custom-tailored resume per JD,
and submits applications. Total operating cost: $0/month.

## Read these first

In order — each references the next:

1. `PRD.md` — what this product is and isn't
2. `job_automation_architecture.md` — 9-layer architecture (source of truth)
3. `CLAUDE.md` — hard rules for any contributor or AI working on this code
4. `CHANGELOG.md` — what changed in each iteration

## Iteration status

**Current: Iteration 0 — Scaffold.** No business logic yet. See
`job_automation_architecture.md` Section 8 for the full build sequence.

## Setup (Iteration 0)

```bash
# Python 3.11+ required
python --version

# Create and activate virtualenv
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Download spaCy model
python -m spacy download en_core_web_sm

# Run smoke tests — should pass green
pytest
```

## Setup (Iteration 1+)

```bash
# Copy example files and fill in real values
cp .env.example .env
cp master_profile.example.yaml master_profile.yaml
cp answer_bank_seed.example.yaml answer_bank_seed.yaml

# Install Playwright browsers (needed for Layer 6)
playwright install chromium
```

## External accounts (not needed for Iteration 0)

| Service | First needed | Free tier |
|---|---|---|
| Neon PostgreSQL (pgvector) | Iteration 1 | 3 GB |
| Gemini 2.0 Flash (Google AI Studio) | Iteration 2 | 1500 calls/day |
| Telegram bot (via @BotFather) | Iteration 1 | unlimited |
| Indeed account (manual login → session cookies) | Iteration 3 | free |
| Google Cloud service account (Sheets + Docs API) | Iteration 2 / 4 | free |
| Oracle Cloud Always Free VM | Iteration 5 | 200 GB disk |
| Glassdoor account | Iteration 4 | free |

## Configuration

All runtime tunables live in `config/config.yaml` (checked in, no secrets).
Secrets only in `.env` (gitignored). No magic numbers in `src/`.

## CLI (Iteration 2+)

```bash
python -m src.main --dry-run            # full run, no submission
python -m src.cli.inspect --job-id XYZ # pipeline state for one job
python -m src.cli.queue                 # inspect application_queue
python -m src.cli.reparse               # rebuild master profile from YAML
```

## What this is NOT

- Not LinkedIn — ever (account-ban risk on the user's main account).
- Not a multi-user / SaaS product.
- Not an LLM bullet writer — every bullet comes verbatim from
  `master_profile.yaml`. The LLM only picks title aliases, names skill
  categories, and writes cover letters.
- Not free of human oversight — Telegram inline review for judgement-call
  questions; manual login required for portal sessions.
