"""v2 — Layer 6 operator dashboard.

The DB session and the pipeline subprocess are mocked; the Jinja templates
and the FastAPI routing are real, so a template that references a missing
field fails here rather than in the browser.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import src.endpoint.app as app_module
from src.endpoint import dashboard, runner
from src.config import settings

_NOW = datetime(2026, 8, 8, 10, 30, tzinfo=timezone.utc)


@pytest.fixture
def client():
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _reset_run_state():
    """Runner state is module-level; don't let one test leak into the next."""
    runner._state = runner.RunState()
    yield
    runner._state = runner.RunState()


def _match_view(**overrides) -> dict:
    view = {
        "job_id": "linkedin-123",
        "company": "Fintech Inc",
        "title": "Backend Engineer",
        "score": 0.78,
        "fit": 0.75,
        "success_prob": 0.82,
        "location": "Bangalore, India",
        "location_type": "remote",
        "salary": "10 LPA",
        "source": "linkedin",
        "status": "pending",
        "gap_skills": ["Kafka"],
        "apply_url": "https://linkedin.com/jobs/123",
        "pdf_url": "/resume/linkedin-123.pdf",
        "docx_url": "/resume/linkedin-123.docx",
        "built_at": _NOW.isoformat(),
        "scraped_at": _NOW.isoformat(),
    }
    view.update(overrides)
    return view


def _patch_page(matches=None, skipped=None):
    """Patch out the DB layer for a page render."""
    session = MagicMock()
    session.scalar.return_value = 1
    # _counts() groups statuses with execute().all(); the views are patched out
    # separately, so an empty grouping is enough here.
    session.execute.return_value.all.return_value = []
    scope = MagicMock()
    scope.__enter__ = MagicMock(return_value=session)
    scope.__exit__ = MagicMock(return_value=False)

    return (
        patch.object(dashboard, "session_scope", return_value=scope),
        patch.object(dashboard, "_match_views", return_value=matches or []),
        patch.object(dashboard, "_skipped_views", return_value=skipped or []),
        patch("src.analytics.export_index", return_value={}),
    )


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def test_root_redirects_to_dashboard(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"


def test_dashboard_renders_a_match_with_all_three_links(client):
    p1, p2, p3, p4 = _patch_page(matches=[_match_view()])
    with p1, p2, p3, p4:
        response = client.get("/dashboard")

    assert response.status_code == 200
    body = response.text
    assert "Backend Engineer" in body
    assert "Fintech Inc" in body
    assert "0.78" in body
    assert "https://linkedin.com/jobs/123" in body
    assert "/resume/linkedin-123.pdf" in body
    assert "/resume/linkedin-123.docx" in body
    assert "Kafka" in body


def test_dashboard_draws_the_score_against_the_threshold(client):
    """The measure is the signature element — the fill and the tick must both
    be positioned from real numbers, not hardcoded."""
    p1, p2, p3, p4 = _patch_page(matches=[_match_view(score=0.78)])
    with p1, p2, p3, p4:
        body = client.get("/dashboard").text

    assert "width: 78.0%" in body
    # From config, not a literal: Stage 6 moved apply_threshold off 0.50.
    assert f"left: {100 * float(settings.scoring.apply_threshold):.1f}%" in body


def test_dashboard_shows_empty_state_without_matches(client):
    p1, p2, p3, p4 = _patch_page(matches=[])
    with p1, p2, p3, p4:
        body = client.get("/dashboard").text

    assert "Nothing waiting on you" in body


def test_dashboard_survives_a_null_score(client):
    """final_score is nullable; a null must not blow up the format filter."""
    p1, p2, p3, p4 = _patch_page(matches=[_match_view(score=None, fit=None)])
    with p1, p2, p3, p4:
        response = client.get("/dashboard")

    assert response.status_code == 200
    assert "0.00" in response.text


def test_dashboard_refreshes_the_csv_index(client):
    """Opening the page is what pulls a phone-triggered run into the CSVs."""
    p1, p2, p3, _ = _patch_page(matches=[])
    with p1, p2, p3, patch("src.analytics.export_index") as export:
        client.get("/dashboard")

    export.assert_called_once()


def test_skipped_page_flags_near_misses(client):
    rows = [
        {"job_id": "a", "company": "A Co", "title": "Dev", "score": 0.48,
         "location": "Pune", "source": "linkedin", "apply_url": "", "skipped_at": None},
        {"job_id": "b", "company": "B Co", "title": "Eng", "score": 0.20,
         "location": "Chennai", "source": "linkedin", "apply_url": "", "skipped_at": None},
    ]
    p1, p2, p3, p4 = _patch_page(skipped=rows)
    with p1, p2, p3, p4:
        body = client.get("/dashboard/skipped").text

    # 0.48 is within 0.05 of the 0.50 line, 0.20 is not.
    assert "miss--near" in body
    assert body.count("miss--near") == 1


def test_pages_escape_untrusted_job_text(client):
    """Company and role come from scraped HTML — they must not render as markup."""
    p1, p2, p3, p4 = _patch_page(
        matches=[_match_view(company="<script>alert(1)</script>")]
    )
    with p1, p2, p3, p4:
        body = client.get("/dashboard").text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

def test_api_jobs_returns_matches(client):
    p1, p2, p3, p4 = _patch_page(matches=[_match_view()])
    with p1, p2, p3, p4:
        response = client.get("/api/jobs")

    assert response.status_code == 200
    assert response.json()["matches"][0]["title"] == "Backend Engineer"


def test_run_status_is_idle_before_any_run(client):
    state = client.get("/api/run/status").json()
    assert state["running"] is False
    assert state["log_lines"] == []


def test_post_run_starts_a_local_run(client):
    with patch.object(runner, "start_local_run", return_value=(True, "Run started.")) as start:
        response = client.post("/api/run", json={"dry_run": True, "target": "local"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    start.assert_called_once_with(True)


def test_post_run_conflicts_when_already_running(client):
    with patch.object(runner, "start_local_run",
                      return_value=(False, "A run is already in progress.")):
        response = client.post("/api/run", json={})

    assert response.status_code == 409
    assert response.json()["ok"] is False


def test_post_run_dispatches_to_github(client):
    with patch.object(runner, "dispatch_github_run",
                      return_value=(True, "Dispatched to GitHub Actions.")) as dispatch:
        response = client.post("/api/run", json={"target": "github", "dry_run": False})

    assert response.status_code == 200
    dispatch.assert_called_once_with(False)


def test_post_run_reports_a_refused_dispatch(client):
    with patch.object(runner, "dispatch_github_run",
                      return_value=(False, "GitHub rejected the dispatch (404).")):
        response = client.post("/api/run", json={"target": "github"})

    assert response.status_code == 502


def test_post_run_rejects_an_unknown_target(client):
    assert client.post("/api/run", json={"target": "mars"}).status_code == 400


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def test_local_run_refuses_to_start_twice():
    runner._state.running = True
    started, message = runner.start_local_run()

    assert started is False
    assert "already in progress" in message


def test_local_run_reports_a_spawn_failure():
    with patch("subprocess.Popen", side_effect=OSError("no python")):
        started, message = runner.start_local_run()

    assert started is False
    assert "no python" in message
    assert runner.get_state()["running"] is False


def test_local_run_streams_output_and_records_exit_code():
    process = MagicMock()
    process.stdout = iter(["line one\n", "line two\n"])
    process.returncode = 0
    process.wait.return_value = 0

    with patch("subprocess.Popen", return_value=process), \
         patch("threading.Thread") as thread:
        started, _ = runner.start_local_run(dry_run=True)
        # Run the drain synchronously instead of on a thread.
        thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])

    assert started is True
    state = runner.get_state()
    assert state["running"] is False
    assert state["returncode"] == 0
    assert state["dry_run"] is True
    assert state["log_lines"] == ["line one", "line two"]


def test_log_buffer_is_bounded():
    """A long run must not grow memory without limit."""
    process = MagicMock()
    process.stdout = iter(f"line {i}\n" for i in range(1200))
    process.returncode = 0

    with patch("subprocess.Popen", return_value=process), \
         patch("threading.Thread") as thread:
        runner.start_local_run()
        thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])

    lines = runner.get_state()["log_lines"]
    assert len(lines) == 500
    assert lines[-1] == "line 1199"


def test_github_dispatch_needs_credentials(monkeypatch):
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    ok, message = runner.dispatch_github_run()

    assert ok is False
    assert "GITHUB_REPO" in message


def test_github_dispatch_posts_the_workflow_input(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO", "user/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")

    response = MagicMock(status_code=204)
    with patch("httpx.post", return_value=response) as post:
        ok, _ = runner.dispatch_github_run(dry_run=True)

    assert ok is True
    url = post.call_args.args[0]
    assert url.endswith("/actions/workflows/pipeline.yml/dispatches")
    assert post.call_args.kwargs["json"]["inputs"] == {"dry_run": "true"}


def test_github_dispatch_reports_a_network_failure(monkeypatch):
    import httpx

    monkeypatch.setenv("GITHUB_REPO", "user/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")

    with patch("httpx.post", side_effect=httpx.ConnectError("offline")):
        ok, message = runner.dispatch_github_run()

    assert ok is False
    assert "Could not reach GitHub" in message


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

def test_data_directory_is_not_served(client):
    """The CSV index holds JD snippets and scores — it must not be reachable."""
    for path in ("/data/index/matches.csv", "/static/../../data/index/matches.csv"):
        assert client.get(path).status_code in (403, 404)


def test_resume_route_still_works_alongside_the_dashboard(client):
    """Mounting the dashboard must not shadow the resume endpoint."""
    with patch.object(app_module, "get_or_build", return_value=(b"%PDF-1.4", "application/pdf")), \
         patch.object(app_module, "session_scope") as scope:
        scope.return_value.__enter__ = MagicMock(return_value=MagicMock())
        scope.return_value.__exit__ = MagicMock(return_value=False)
        response = client.get("/resume/linkedin-123.pdf")

    assert response.status_code == 200
    assert response.content == b"%PDF-1.4"


# ---------------------------------------------------------------------------
# Endpoint base URL (ngrok removal)
# ---------------------------------------------------------------------------

def test_base_url_is_a_passthrough_and_never_probes_the_network():
    """It used to call localhost:4040 to discover an ngrok tunnel.

    Tailscale hostnames are stable, so there is nothing to discover — and a
    network call here would add latency (and a failure mode) to every match
    notification.
    """
    from src.config import resolve_endpoint_base_url

    with patch("urllib.request.urlopen", side_effect=AssertionError("no network")):
        assert (
            resolve_endpoint_base_url("https://box.tailnet.ts.net")
            == "https://box.tailnet.ts.net"
        )


def test_base_url_strips_a_trailing_slash():
    """Resume links are built by concatenation; a trailing slash would
    produce //resume/... ."""
    from src.config import resolve_endpoint_base_url

    assert resolve_endpoint_base_url("http://localhost:8000/") == "http://localhost:8000"


# ---------------------------------------------------------------------------
# Applied tracking
#
# The system deliberately never submits an application, so the operator's own
# confirmation is the only evidence one happened. That makes this endpoint the
# sole writer of applied state — worth pinning down.
# ---------------------------------------------------------------------------

def _patch_status_row(row):
    """Patch the session used by POST /api/jobs/{id}/status."""
    session = MagicMock()
    session.get.return_value = row
    session.scalar.return_value = 0
    session.execute.return_value.all.return_value = []
    scope = MagicMock()
    scope.__enter__ = MagicMock(return_value=session)
    scope.__exit__ = MagicMock(return_value=False)
    return patch.object(dashboard, "session_scope", return_value=scope), session


@pytest.mark.parametrize("status", ["pending", "applied", "dismissed"])
def test_status_endpoint_records_each_transition(client, status):
    row = MagicMock(user_status="pending", user_status_at=None)
    patcher, session = _patch_status_row(row)
    with patcher:
        response = client.post("/api/jobs/linkedin-123/status", json={"status": status})

    assert response.status_code == 200
    assert response.json()["status"] == status
    assert row.user_status == status
    session.commit.assert_called_once()


def test_acting_on_a_job_stamps_the_action_time(client):
    """Applied sorts by this, and the card prints it."""
    row = MagicMock(user_status="pending", user_status_at=None)
    patcher, _ = _patch_status_row(row)
    with patcher:
        client.post("/api/jobs/linkedin-123/status", json={"status": "applied"})

    assert isinstance(row.user_status_at, datetime)
    assert row.user_status_at.tzinfo is not None, "must be timezone-aware"


def test_undo_clears_the_action_time(client):
    """Returning a job to pending must not leave a stale applied date behind,
    or it would sort into Applied with a date and no status."""
    row = MagicMock(user_status="applied", user_status_at=_NOW)
    patcher, _ = _patch_status_row(row)
    with patcher:
        client.post("/api/jobs/linkedin-123/status", json={"status": "pending"})

    assert row.user_status == "pending"
    assert row.user_status_at is None


def test_status_endpoint_returns_fresh_counts_for_the_nav(client):
    """The page updates its badges from this rather than reloading."""
    row = MagicMock(user_status="pending", user_status_at=None)
    patcher, _ = _patch_status_row(row)
    with patcher:
        body = client.post(
            "/api/jobs/linkedin-123/status", json={"status": "applied"}
        ).json()

    assert set(body["counts"]) == {"pending", "applied", "dismissed", "skipped"}


def test_status_endpoint_rejects_an_unknown_status(client):
    row = MagicMock(user_status="pending", user_status_at=None)
    patcher, session = _patch_status_row(row)
    with patcher:
        response = client.post("/api/jobs/linkedin-123/status", json={"status": "hired"})

    assert response.status_code == 400
    session.commit.assert_not_called()


def test_status_endpoint_404s_an_unknown_job(client):
    patcher, _ = _patch_status_row(None)
    with patcher:
        response = client.post("/api/jobs/nope/status", json={"status": "applied"})

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# View ordering — the two views answer different questions
# ---------------------------------------------------------------------------

def _order_by_sql(status: str) -> str:
    """Compile the ORDER BY that _match_views builds for a status."""
    session = MagicMock()
    session.execute.return_value = []
    dashboard._match_views(session, status)
    stmt = session.execute.call_args[0][0]
    return str(stmt).lower()


def test_matches_sort_newest_first():
    """A job ad is perishable.

    Pending used to lead with the best fit, but a high score on a posting that
    closed weeks ago is not a better use of attention than a fresh opening.
    Score still breaks ties, and every card draws it to scale anyway.
    """
    sql = _order_by_sql("pending")
    order = sql.split("order by", 1)[1]
    assert order.index("built_at") < order.index("final_score"), (
        "recency must outrank score"
    )
    assert "user_status_at" not in order


def test_applied_sorts_by_when_the_operator_acted():
    """Applied is a record, read newest first — score no longer decides
    anything once the application is out."""
    sql = _order_by_sql("applied")
    order = sql.split("order by", 1)[1]
    assert "user_status_at desc" in order
    assert order.index("user_status_at") < order.index("built_at")


def test_applied_page_offers_undo_not_apply(client):
    """The settled card must not invite a second application."""
    p1, p2, p3, p4 = _patch_page(
        matches=[_match_view(status="applied", actioned_at=_NOW.isoformat())]
    )
    with p1, p2, p3, p4:
        body = client.get("/dashboard/applied").text

    assert 'data-status="pending"' in body, "Undo must be present"
    assert "js-apply" not in body, "a settled job must not show Apply"
    assert "job--settled" in body


# ---------------------------------------------------------------------------
# Notification delivery — Layer 8
#
# From the live run of 2026-08-08: a real match was found, its resume was
# built, and the notification was lost because one button pointed at
# localhost. The match is the only output that matters; nothing about link
# formatting may swallow it.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost:8000/resume/x.pdf",
    "http://127.0.0.1:8000/resume/x.pdf",
    "http://0.0.0.0:8000/resume/x.pdf",
    "http://laptop.local:8000/resume/x.pdf",
    "",
    "not-a-url",
])
def test_unreachable_urls_are_not_offered_as_buttons(url):
    from src.notifications import _is_public_url

    assert _is_public_url(url) is False


@pytest.mark.parametrize("url", [
    "https://box.tail1234.ts.net/resume/x.pdf",
    "https://job-bot.s3.ap-south-1.amazonaws.com/pdf_cache/x.pdf?X-Amz-Signature=a",
    "https://linkedin.com/jobs/123",
])
def test_reachable_urls_are_offered_as_buttons(url):
    from src.notifications import _is_public_url

    assert _is_public_url(url) is True


def test_a_card_shows_when_the_posting_was_found(client):
    """How old an ad is decides whether applying is worth anything."""
    p1, p2, p3, p4 = _patch_page(matches=[_match_view()])
    with p1, p2, p3, p4:
        body = client.get("/dashboard").text

    assert "data-datetime=" in body
    assert ">Found<" in body
