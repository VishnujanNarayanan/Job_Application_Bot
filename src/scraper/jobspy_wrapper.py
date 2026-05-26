"""Layer 2 — scraper. Iteration 1: stub that returns 3 fake jobs.

The real JobSpy implementation lands in Iteration 2. The signature
``scrape() -> list[AllJobs]`` is the contract Layer 1 (orchestrator)
depends on; that contract must stay stable across iterations.

Iteration 1 behaviour:
  - Returns 3 hardcoded fake jobs covering the role categories we expect
    to scrape against in production (data eng / ML / backend).
  - Each job_id is suffixed with the run timestamp so re-running the
    dry-run doesn't violate the all_jobs primary-key constraint.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.state.models import AllJobs

_FAKE_JOBS_SEED: list[dict] = [
    {
        "company": "Acme Analytics",
        "role": "Data Engineer",
        "site": "indeed",
        "location": "Bengaluru, KA",
        "job_url": "https://example.invalid/acme-de",
        "jd_text": (
            "Build and maintain data pipelines using Python and Airflow. "
            "Strong SQL required. Experience with Spark a plus."
        ),
    },
    {
        "company": "Beacon AI",
        "role": "ML Engineer",
        "site": "indeed",
        "location": "Remote (India)",
        "job_url": "https://example.invalid/beacon-mle",
        "jd_text": (
            "Productionize ML models. PyTorch, FastAPI, AWS. Experience "
            "with model serving frameworks preferred."
        ),
    },
    {
        "company": "Citadel Labs",
        "role": "Backend Engineer",
        "site": "indeed",
        "location": "Hyderabad, TG",
        "job_url": "https://example.invalid/citadel-be",
        "jd_text": (
            "Build microservices in Python or Go. Strong Postgres, "
            "Kafka, and Kubernetes experience."
        ),
    },
]


def scrape() -> list[AllJobs]:
    """Return 3 fake `AllJobs` rows ready for insertion.

    Caller is responsible for the actual ``session.add_all() + commit``.
    """
    run_ts = int(datetime.now(timezone.utc).timestamp())
    return [
        AllJobs(
            job_id=f"stub-{i}-{run_ts}",
            posted_at=datetime.now(timezone.utc),
            **seed,
        )
        for i, seed in enumerate(_FAKE_JOBS_SEED, start=1)
    ]
