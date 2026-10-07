"""Readable terminal output for `python -m src.main`."""

from __future__ import annotations

import pytest
import structlog

from src.console_log import ConsoleRenderer, short_error

GROQ_400 = (
    "Error code: 400 - {'error': {'message': 'Failed to parse tool call arguments as JSON', "
    "'type': 'invalid_request_error', 'code': 'tool_use_failed', 'failed_generation': "
    "'{\"name\": \"JDParsed\", \"arguments\": {\"apply_url\": null, ' + 'x' * 5000}}"
)


def _render(r, name, level="info", **fields):
    fields.update(event=name, level=level, timestamp="2026-10-07T09:52:39Z")
    try:
        return r(None, level, fields)
    except structlog.DropEvent:
        return None


@pytest.fixture
def r():
    return ConsoleRenderer(colour=False)


def test_short_error_keeps_the_providers_message_not_the_dump():
    assert short_error(GROQ_400) == "Failed to parse tool call arguments as JSON"


def test_short_error_falls_back_to_the_first_line_and_caps_length():
    assert short_error("boom\nsecond line") == "boom"
    assert short_error("x" * 500).endswith("…") and len(short_error("x" * 500)) == 120


@pytest.mark.parametrize("name", [
    "llm_client_built", "docx_assembled", "pdf_converted", "s3_presigned",
    "prerendered", "selection_built", "pool_skills_recovered", "index_exported",
])
def test_plumbing_events_are_hidden(r, name):
    assert _render(r, name) is None


def test_unknown_info_is_hidden_but_unknown_warnings_are_shown(r):
    assert _render(r, "something_new") is None
    out = _render(r, "something_odd", "warning", error=GROQ_400)
    assert "something_odd" in out and "Failed to parse tool call arguments as JSON" in out
    assert "failed_generation" not in out


def test_a_job_reads_as_heading_then_llm_and_verdict(r):
    head = _render(r, "job_started", n=2, of=24, company="Vi-Scan", role="Junior SWE")
    assert head.endswith("[2/24] Junior SWE @ Vi-Scan")
    assert _render(r, "llm_served", provider="gemini-free", seconds=2.73) is None
    verdict = _render(r, "job_scored", score=0.62, threshold=0.6, matched=True, applicants=24)
    assert verdict.strip() == "gemini-free 2.7s · score 0.62 · ✅ MATCH · 24 applicants"


def test_the_llm_is_credited_to_one_job_only(r):
    _render(r, "job_started", n=1, of=2, company="A", role="B")
    _render(r, "llm_served", provider="groq", seconds=1.0)
    _render(r, "job_scored", score=0.3, threshold=0.6, matched=False)
    _render(r, "job_started", n=2, of=2, company="C", role="D")
    assert _render(r, "job_scored", score=0.3, threshold=0.6, matched=False).strip().startswith("score")


def test_a_rejection_and_fallback_are_one_short_line_each(r):
    fail = _render(r, "llm_failure", "error", provider="groq", error=GROQ_400)
    assert fail.strip() == "✗ groq: Failed to parse tool call arguments as JSON"
    assert _render(r, "llm_fallback_engaged", "warning", to_provider="gemini-free").strip() == "→ trying gemini-free"


def test_filtered_jobs_say_why(r):
    _render(r, "llm_served", provider="gemini", seconds=2.2)
    assert _render(r, "job_filtered", reason="HARD_FILTER_LAYER_3", value="8").strip() == \
        "gemini 2.2s · filtered: needs 8 years"


def test_summary_has_duration_and_skip_reasons(r):
    _render(r, "run_started", dry_run=False)
    out = _render(r, "run_complete", scraped=37, matched=6, skipped=31,
                  outcomes={"LOW_SCORE": 15, "TOO_MANY_APPLICANTS": 5})
    assert "■ Done in 0m00s: 6 matched, 31 skipped, 37 scraped" in out
    assert "5 too many applicants to match · 15 score below threshold" in out


def test_non_terminals_keep_json(monkeypatch):
    from src import main as m

    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: False, raising=False)
    assert m._readable_console() is False


def test_log_format_json_wins_in_a_terminal(monkeypatch):
    from src import main as m

    monkeypatch.setattr(m.sys.stderr, "isatty", lambda: True, raising=False)
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert m._readable_console() is False
    monkeypatch.delenv("LOG_FORMAT")
    assert m._readable_console() is True
