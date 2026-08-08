"""v2 — multi-term rotation, build-time pre-render, and presigned links.

Covers the three pieces that make a phone-triggered run useful:
  * `rotation.current_terms` — several search terms per run, since runs are
    manual now and one-per-run would need 24 clicks to sweep the list.
  * `cache.prerender` — render at build time so links survive a laptop that
    is switched off (the documented amendment to hard rule #8).
  * `notifications` — presigned S3 URLs win over endpoint URLs when present.

AWS is mocked throughout; nothing external is contacted.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.scraper import rotation


# ---------------------------------------------------------------------------
# rotation.current_terms
# ---------------------------------------------------------------------------

def _session_at(index: int) -> MagicMock:
    session = MagicMock()
    session.scalar.return_value = str(index)
    return session


TERMS = ["a", "b", "c", "d", "e"]


def test_current_terms_takes_count_from_the_current_index():
    assert rotation.current_terms(_session_at(0), TERMS, 3) == ["a", "b", "c"]
    assert rotation.current_terms(_session_at(2), TERMS, 3) == ["c", "d", "e"]


def test_current_terms_wraps_past_the_end():
    assert rotation.current_terms(_session_at(3), TERMS, 4) == ["d", "e", "a", "b"]


def test_current_terms_dedupes_when_count_exceeds_list():
    """A count past the list length wraps onto itself; don't scrape twice."""
    assert rotation.current_terms(_session_at(0), TERMS, 8) == TERMS


def test_current_terms_of_one_matches_current_term():
    session = _session_at(2)
    assert rotation.current_terms(session, TERMS, 1) == [rotation.current_term(session, TERMS)]


def test_current_terms_rejects_bad_input():
    with pytest.raises(ValueError):
        rotation.current_terms(_session_at(0), [], 3)
    with pytest.raises(ValueError):
        rotation.current_terms(_session_at(0), TERMS, 0)


def test_advance_moves_by_step_and_wraps():
    session = _session_at(3)
    session.get.return_value = None
    assert rotation.advance(session, TERMS, step=4) == 2


def test_advance_defaults_to_single_step():
    session = _session_at(0)
    session.get.return_value = None
    assert rotation.advance(session, TERMS) == 1


def test_consecutive_runs_sweep_the_whole_list():
    """Advancing by the number of terms used must cover the list without gaps."""
    index = 0
    seen: list[str] = []
    for _ in range(5):  # 5 runs x 4 terms covers 20 of a 24-term list
        session = _session_at(index)
        picked = rotation.current_terms(session, list("abcdefghijklmnopqrstuvwx"), 4)
        seen.extend(picked)
        index = (index + len(picked)) % 24

    assert len(seen) == len(set(seen)) == 20


# ---------------------------------------------------------------------------
# cache.prerender
# ---------------------------------------------------------------------------

@pytest.fixture
def applied_row():
    row = MagicMock()
    row.selection_json = {"template_version": "abc12345", "job_id": "j1"}
    return row


def test_prerender_returns_presigned_urls_for_both_formats(applied_row):
    from src.endpoint import cache

    session = MagicMock()
    session.get.return_value = applied_row
    selection = MagicMock(template_version="abc12345")

    with patch.object(cache, "get_or_build", return_value=(b"x", "application/pdf")), \
         patch.object(cache.StoredSelection, "model_validate", return_value=selection), \
         patch("src.aws.s3.cache_presigned_url", side_effect=lambda k, e, s: f"https://s3/{k}.{e}"):
        urls = cache.prerender("j1", session, expires_seconds=604800)

    assert urls == {
        "pdf": "https://s3/j1_abc12345.pdf",
        "docx": "https://s3/j1_abc12345.docx",
    }


def test_prerender_returns_empty_without_a_selection():
    from src.endpoint import cache

    session = MagicMock()
    session.get.return_value = None

    assert cache.prerender("j1", session, expires_seconds=60) == {}


def test_prerender_survives_one_format_failing(applied_row):
    """A PDF conversion failure must not cost us the DOCX link or the message."""
    from src.endpoint import cache

    session = MagicMock()
    session.get.return_value = applied_row
    selection = MagicMock(template_version="abc12345")

    def flaky(job_id, ext, sess):
        if ext == "pdf":
            raise RuntimeError("libreoffice exploded")
        return (b"x", "application/docx")

    with patch.object(cache, "get_or_build", side_effect=flaky), \
         patch.object(cache.StoredSelection, "model_validate", return_value=selection), \
         patch("src.aws.s3.cache_presigned_url", side_effect=lambda k, e, s: f"https://s3/{k}.{e}"):
        urls = cache.prerender("j1", session, expires_seconds=60)

    assert set(urls) == {"docx"}


def test_prerender_omits_formats_that_fail_to_presign(applied_row):
    from src.endpoint import cache

    session = MagicMock()
    session.get.return_value = applied_row
    selection = MagicMock(template_version="abc12345")

    with patch.object(cache, "get_or_build", return_value=(b"x", "application/pdf")), \
         patch.object(cache.StoredSelection, "model_validate", return_value=selection), \
         patch("src.aws.s3.cache_presigned_url", return_value=None):
        assert cache.prerender("j1", session, expires_seconds=60) == {}


# ---------------------------------------------------------------------------
# presign clamping
# ---------------------------------------------------------------------------

def test_presign_clamps_to_the_sigv4_seven_day_maximum():
    """A longer expiry would be rejected by AWS outright, so clamp it."""
    from src.aws import s3

    client = MagicMock()
    client.generate_presigned_url.return_value = "https://s3/signed"
    with patch.object(s3, "_s3", return_value=client), \
         patch.object(s3, "_bucket", return_value="bucket"):
        s3.cache_presigned_url("j1_v1", "pdf", 30 * 24 * 3600)

    assert client.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 7 * 24 * 3600


def test_presign_returns_none_on_error():
    from src.aws import s3

    client = MagicMock()
    client.generate_presigned_url.side_effect = RuntimeError("no creds")
    with patch.object(s3, "_s3", return_value=client), \
         patch.object(s3, "_bucket", return_value="bucket"):
        assert s3.cache_presigned_url("j1_v1", "pdf", 60) is None


# ---------------------------------------------------------------------------
# notification link selection
# ---------------------------------------------------------------------------

def _notify_capturing_buttons(resume_urls):
    """Run send_match_notification and return {label: url} for its buttons."""
    from src.state.models import AllJobs
    from src.llm.schemas import JDParsed
    from src.scorer.apply_decision import SelectionResult
    from src.scorer.selector import SelectedBullet, SelectedExperience, SummaryCand

    job = AllJobs(
        job_id="job123", company="Fintech Inc", role="Python Developer",
        site="linkedin", location="Bangalore, India",
        job_url="https://linkedin.com/jobs/123",
    )
    parsed = JDParsed(
        role_summary="Backend role.", role_category="backend", role_level="mid",
        years_required=2, required_skills=["Python"], nice_to_have=[],
        responsibilities=["Build"], salary_max_lpa=10.0, salary_currency="INR",
        location_type="remote", apply_url="https://apply.example/123",
    )
    result = SelectionResult(
        apply=True, final_score=0.78, fit=0.75, success_prob=0.82,
        recency=0.60, project_score=0.70,
        summary=SummaryCand(id="s1", text="Eng.", role_categories=["backend"],
                            embedding=[0.0]),
        summary_score=0.65,
        experiences=[SelectedExperience(
            id="e1", company="ACME", actual_title="Backend Dev",
            safe_title_aliases=["Backend Dev"], score=0.8, alias_score=0.75,
            end_date="present", bullets=[SelectedBullet("b1", "Did X", 0.9)],
        )],
        projects=[], skill_candidates=[("Python", 0.9)], skills_before_projects=True,
    )

    captured: dict[str, str] = {}

    async def fake_send(text, keyboard=None):
        for row in (keyboard.inline_keyboard if keyboard else []):
            for button in row:
                captured[button.text] = button.url

    with patch("src.notifications._send_match", new=fake_send), \
         patch("src.notifications.asyncio") as mock_asyncio:
        import asyncio

        def run_coro(coro):
            loop = asyncio.new_event_loop()
            loop.run_until_complete(coro)
            loop.close()

        mock_asyncio.run.side_effect = run_coro

        from src.notifications import send_match_notification
        send_match_notification(
            job=job, parsed=parsed, result=result, gap_skills=[],
            endpoint_base_url="http://localhost:8000",
            title_alias="Backend Engineer", resume_urls=resume_urls,
        )
    return captured


def test_presigned_urls_win_over_endpoint_urls():
    """The whole point: these links must work with the laptop switched off."""
    buttons = _notify_capturing_buttons({
        "pdf": "https://s3.example/signed.pdf",
        "docx": "https://s3.example/signed.docx",
    })

    assert buttons["Resume PDF"] == "https://s3.example/signed.pdf"
    assert buttons["Resume DOCX"] == "https://s3.example/signed.docx"


def test_falls_back_to_endpoint_urls_without_prerender():
    buttons = _notify_capturing_buttons(None)

    assert buttons["Resume PDF"] == "http://localhost:8000/resume/job123.pdf"
    assert buttons["Resume DOCX"] == "http://localhost:8000/resume/job123.docx"


def test_falls_back_per_format():
    """A half-successful pre-render still yields two working buttons."""
    buttons = _notify_capturing_buttons({"docx": "https://s3.example/signed.docx"})

    assert buttons["Resume PDF"] == "http://localhost:8000/resume/job123.pdf"
    assert buttons["Resume DOCX"] == "https://s3.example/signed.docx"
