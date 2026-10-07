<h1 align="center">Job Application Bot</h1>

<p align="center">
  Finds new job listings, scores each one against your profile, and builds a tailored resume for
  every good match. Matches arrive on Telegram; nothing is ever applied to automatically.
</p>

<p align="center">
  <a href="https://github.com/VishnujanNarayanan/Job_Application_Bot/releases/tag/v3.1.0"><img alt="Version" src="https://img.shields.io/badge/version-v3.1.0-2ea44f"/></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white"/>
  <img alt="Tests" src="https://img.shields.io/badge/tests-648_passing-3FB950?logo=pytest&logoColor=white"/>
  <a href="CHANGELOG.md"><img alt="Changelog" src="https://img.shields.io/badge/changelog-CHANGELOG.md-blue"/></a>
  <br>
  <a href="https://vishnujan.dev/"><img alt="Portfolio" src="https://img.shields.io/badge/Portfolio-vishnujan.dev-3b5998?logo=googlechrome&logoColor=white"/></a>
  <a href="https://github.com/VishnujanNarayanan"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-VishnujanNarayanan-181717?logo=github&logoColor=white"/></a>
  <a href="https://www.linkedin.com/in/vishnujan-narayanan"><img alt="LinkedIn" src="https://img.shields.io/badge/LinkedIn-Vishnujan_Narayanan-0A66C2"/></a>
</p>

<p align="center">
  <a href="#getting-started">Getting started</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#usage">Usage</a> ·
  <a href="#limitations">Limitations</a>
</p>

---

## What it does

- **Scrapes** recent LinkedIn listings for your search terms.
- **Parses** each job ad with an LLM (Groq, Gemini or OpenRouter, rotating) into structured
  requirements.
- **Scores** the job against your profile and the number of applicants; anything above the
  threshold is a match.
- **Builds a tailored resume** (PDF and DOCX) for each match, using only bullets you wrote. The LLM
  never writes resume text.
- **Notifies you on Telegram** with Apply, Resume PDF, Resume DOCX, Mark applied and Dismiss
  buttons, and shows everything on a local dashboard.

It never applies to anything: you review the resume and click Apply yourself.

## How it works

```
search terms → scrape (JobSpy) → filters → LLM parse → score → match?
                                                                 ├─ no  → logged as skipped
                                                                 └─ yes → tailored resume → Telegram + dashboard
```

Jobs, scores and your profile live in Postgres (pgvector for the bullet embeddings). Resumes are
assembled from your DOCX template and converted to PDF with LibreOffice. Design notes are in
[`docs/`](docs/); the full history is in [CHANGELOG.md](CHANGELOG.md).

## Getting started

### What you need

| | Needed? | Cost | What it's for |
|---|---|---|---|
| **Python 3.11+** | Required | Free | Runs the pipeline |
| **LibreOffice** | Required | Free | Converts the resume DOCX to PDF |
| **PostgreSQL with pgvector** | Required | Free | Stores jobs, scores and your profile. [Neon](https://neon.tech) (hosted, free tier) or a local Docker container |
| **One LLM key** | Required | Free | Parses each job ad. A [Groq](https://console.groq.com) key is enough |
| **Your profile** | Required | — | `master_profile.yaml`, your experience as structured bullets |
| **Telegram bot** | Optional | Free | Sends each match to your phone. Without it, use the dashboard |
| **More LLM keys** (Gemini free, Gemini paid, OpenRouter) | Optional | Free / small | More capacity and fallbacks |
| **AWS S3** | Optional | ~Free | Caches resumes so Telegram links work while your computer is off (`requirements-aws.txt`) |
| **Tailscale** | Optional | Free | Opens the dashboard and resume links from your phone |
| **GitHub Actions** | Optional | Free tier | Runs the pipeline on GitHub's servers |

The minimum is a database, one Groq key, your profile and LibreOffice.

### Setup

**1. Clone and install**

```bash
git clone https://github.com/VishnujanNarayanan/Job_Application_Bot.git
cd Job_Application_Bot

python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # ~1.5 GB installed (CPU-only PyTorch)
```

Using AWS too? Also run `pip install -r requirements-aws.txt`. To run the tests, install
`requirements-dev.txt` instead, which includes everything.

Install LibreOffice too: `sudo apt install libreoffice-writer` (Debian/Ubuntu/WSL),
`brew install --cask libreoffice` (macOS), or the installer from libreoffice.org (Windows).

**2. Create a database**

Either sign up at [Neon](https://neon.tech) and copy the connection string, or run Postgres
locally:

```bash
docker run -d --name jobbot-db -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16
# DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres
```

**3. Fill in `.env`**

```bash
cp .env.example .env
```

Set `DATABASE_URL` and `GROQ_API_KEY`. Every other variable is marked `[OPTIONAL]` in the file and
can stay blank.

**4. Create the tables**

```bash
alembic upgrade head
```

**5. Write your profile**

```bash
cp master_profile.example.yaml master_profile.yaml
```

Edit `master_profile.yaml`: your contact details, jobs and projects, each as bullets. The comments
in the file explain every field. Then check it:

```bash
python -m src.cli.reparse          # validates the profile and loads it into the database
```

**6. Create your resume template**

```bash
python tools/personalize_template.py
```

This copies `resumes/templates/example_template.docx` and fills the header (name, contact line,
education, certificates) from your profile. Open the result,
`resumes/templates/headless_v1.docx`, in Word to check it. Everything below the education block is
generated per job.

**7. Make the config yours**

In `config/config.yaml`, change:

| Setting | Set it to |
|---|---|
| `operator` | Your name, years of experience, time zone, and the resume header lines |
| `scraper.search_terms` | The job titles you want to search for |
| `filters` | Maximum years required, blocked locations and companies |
| `scoring.apply_threshold` | How strict matching is (0–1; higher means fewer, better matches) |
| `prerender.enabled` | `false` if you are not using AWS |
| `aws.s3_bucket` | Your bucket name, if you use AWS |
| `endpoint.base_url` | Your Tailscale URL, if you use Tailscale; otherwise leave it |

**8. Check your LLM key and run**

```bash
python -m src.cli.llm_check groq   # one real call; should print "OK"
python -m src.main --dry-run       # full run without sending match messages
python -m src.main                 # real run
```

In a terminal, `python -m src.main` first asks which search term to use (arrow keys, Enter for
the next term in the rotation). The first run also downloads a ~90 MB embedding model.

**9. See the results**

```bash
uvicorn src.endpoint.app:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000/dashboard> for matches, scores and tailored resumes (PDF and DOCX).
If Telegram is set up, matches also arrive there with Apply, Resume and **Mark applied** buttons.

**10. Run the tests (optional)**

```bash
pip install -r requirements-dev.txt
pytest
```

### Docker (alternative)

The same setup can run in Docker, with two roles from one image:

| Service | Role | Lifecycle |
|---|---|---|
| `endpoint` | Always-on resume server and dashboard, port 8000 | `docker compose up -d endpoint` |
| `pipeline` | One run: scrape → parse → score → build → notify | `docker compose run --rm pipeline` |

```bash
cp .env.example .env                     # then fill it in
cp master_profile.example.yaml master_profile.yaml
touch master_profile.json                # so the bind mount is a file, not a directory
docker compose build
docker compose run --rm pipeline alembic upgrade head
docker compose run --rm pipeline python -m src.cli.reparse
docker compose run --rm pipeline python tools/personalize_template.py
docker compose up -d endpoint
```

Secrets and your profile are `.dockerignore`d and mounted at runtime, never baked into the image.

## Configuration

Secrets live in `.env` (gitignored). Every variable in `.env.example` is marked `[REQUIRED]` or
`[OPTIONAL]` with a note on where to get it. All other settings live in `config/config.yaml`
(checked in); the settings a new user should change are listed in step 7 above.

| Block | Controls |
|---|---|
| `operator` | Your name, years of experience, time zone, resume header lines |
| `filters` | Job type, years ceiling, blocked regions and companies |
| `scraper` | Search terms, terms per run, time budget |
| `scoring` | Every threshold and weight in the selection formula |
| `llm` | Providers, rotation order, timeouts |
| `prerender` | Render resumes during the run and upload them to S3 |
| `endpoint` | Template path, dashboard, Tailscale base URL, GitHub dispatch |
| `notifications` | Telegram progress messages and button polling |
| `analytics` | Local CSV index paths, monthly report settings |

The current values reject jobs asking for more than 5 years' experience, keep full-time roles
only, and exclude Delhi NCR (Delhi, Gurgaon, Gurugram, Noida, Ghaziabad, Faridabad).

## Usage

### Pipeline

```bash
python -m src.main                         # live run; in a terminal, asks which search term
python -m src.main --term "data engineer"  # search one term, no menu
python -m src.main --auto                  # next term in the rotation, no menu
python -m src.main --dry-run               # everything except sending match messages
LOG_FORMAT=json python -m src.main         # raw JSON logs instead of readable lines
docker compose run --rm pipeline           # containerised live run
```

A run lock prevents overlapping runs. A term picked by hand doesn't move the rotation.

### CLI

| Command | Purpose |
|---|---|
| `python -m src.cli.dryrun` | Full pipeline into the test chat |
| `python -m src.cli.inspect --job-id XYZ` | Full pipeline state for one job |
| `python -m src.cli.reparse` | Rebuild the master profile in the DB from YAML |
| `python -m src.cli.report` | Write the monthly analytics report to a text file |
| `python -m src.cli.aws_check` | Verify S3, IAM, and CloudWatch connectivity |
| `python -m src.cli.llm_check <provider>` | Make one real structured call to an LLM provider |
| `python tools/personalize_template.py` | Create your resume template from the example and your profile |

### Endpoint

| Route | Purpose |
|---|---|
| `GET /dashboard` | Matched jobs with apply and resume links, and the Run button |
| `GET /dashboard/skipped` | Jobs that scored below the threshold, near-misses flagged |
| `GET /resume/{job_id}.pdf` | Render or serve the cached tailored resume as PDF |
| `GET /resume/{job_id}.docx` | Same, as DOCX |
| `GET /api/jobs` | Match data as JSON |
| `POST /api/run` | Start a run — `{"dry_run": bool, "target": "local"\|"github"}` |
| `GET /api/run/status` | Poll run progress and log lines |
| `GET /health` | Healthcheck used by the compose healthcheck |

Rendering is roughly 5 s cold, instant when cached.

### Remote access

The endpoint and dashboard have **no authentication**, so the container binds `127.0.0.1` only and
remote access goes over a private Tailscale mesh — reachable from the operator's own devices and
nothing else. ngrok, which published the same unauthenticated surface to the whole internet, was
removed in v2.

One-time setup:

```bash
# In the Tailscale admin console: enable MagicDNS and HTTPS certificates.
tailscale up
tailscale serve --bg 8000        # start_bot.sh does this and prints the URL
```

Then set `endpoint.base_url` in `config/config.yaml` to `https://<host>.<tailnet>.ts.net`.

Use `tailscale serve`, never `tailscale funnel` — `funnel` publishes to the public internet and
would undo the reason ngrok was dropped.

### Local operation

```bash
./scripts/start_bot.sh          # endpoint + dashboard only
./scripts/start_bot.sh 90       # ...and a live run every 90 minutes
./scripts/start_bot.sh once     # ...and a single run, then exit
```

The loop is off by default: runs normally come from the dashboard's Run button or the GitHub
Actions workflow.

### Running with the laptop off

`.github/workflows/pipeline.yml` runs the whole pipeline on GitHub's runners, dispatched manually
from the Actions tab or the GitHub mobile app. Matched resumes are pre-rendered to S3 during the
run and delivered as presigned links, so Telegram notifications stay fully usable — apply link and
resume both — with the laptop shut. Only the dashboard needs the machine awake.

Before the first remote run:

```bash
python -m src.cli.assets push   # profile + template -> S3 (too big for GitHub secrets)
```

Add `DATABASE_URL`, your LLM keys (`GROQ_API_KEY`, `GEMINI_FREE_API_KEY`, ...), `TELEGRAM_*` and
`AWS_*` as repository secrets (Settings → Secrets and variables → Actions, or `gh secret set`).

## Limitations

- **LinkedIn only** for now; Indeed and Glassdoor are switched off.
- **The dashboard needs your computer on.** Telegram messages and (with AWS) resume links work
  with it off; resume links expire after 7 days.
- **Phone access to the dashboard needs Tailscale.** The dashboard has no login, so it is only
  reachable on your private network.
- **One person per install.** No accounts or multi-user support.

---

<p align="center">
  Questions or bugs: <a href="https://github.com/VishnujanNarayanan/Job_Application_Bot/issues">open an issue</a>.
  No licence file is included; all rights reserved.
</p>
