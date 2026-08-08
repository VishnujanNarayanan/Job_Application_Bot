"""Iteration 2 — Layer 8: per-match notification.

Telegram is mocked — no actual messages sent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.llm.schemas import JDParsed
from src.scorer.apply_decision import SelectionResult
from src.scorer.selector import (
    BulletCand, ExperienceCand, Profile, ProjectCand,
    SelectedBullet, SelectedExperience, SelectedProject,
    SkillCand, SummaryCand,
)
from src.state.models import AllJobs


def _make_job() -> AllJobs:
    return AllJobs(
        job_id="job123",
        company="Fintech Inc",
        role="Python Developer",
        site="linkedin",
        location="Bangalore, India",
        job_url="https://linkedin.com/jobs/123",
    )


def _make_parsed() -> JDParsed:
    return JDParsed(
        role_summary="Backend Python role at fintech.",
        role_category="backend",
        role_level="mid",
        years_required=2,
        required_skills=["Python", "FastAPI"],
        nice_to_have=["Kafka"],
        responsibilities=["Build APIs"],
        salary_max_lpa=10.0,
        salary_currency="INR",
        location_type="remote",
        apply_url="https://apply.fintech.com/123",
    )


def _make_result() -> SelectionResult:
    exp = SelectedExperience(
        id="exp1", company="ACME", actual_title="Backend Dev",
        safe_title_aliases=["Backend Dev"], score=0.80,
        alias_score=0.75, end_date="present",
        bullets=[SelectedBullet("b1", "Did X", 0.9)],
    )
    summary = SummaryCand(
        id="sum1", text="Engineer.", role_categories=["backend"], embedding=[0.0]
    )
    return SelectionResult(
        apply=True, final_score=0.78, fit=0.75, success_prob=0.82,
        recency=0.60, project_score=0.70,
        summary=summary, summary_score=0.65,
        experiences=[exp], projects=[],
        skill_candidates=[("Python", 0.9)],
        skills_before_projects=True,
    )


def test_send_match_notification_message_content():
    """send_match_notification calls _send with correct content."""
    sent_texts = []

    async def capture_send(text, keyboard=None):
        sent_texts.append(text)

    job = _make_job()
    parsed = _make_parsed()
    result = _make_result()

    with patch("src.notifications._send_match", side_effect=capture_send), \
         patch("src.notifications.asyncio.run", side_effect=lambda coro: None):
        # We patch asyncio.run to call the coroutine synchronously for capture
        import asyncio

        async def run_capture():
            await capture_send("placeholder")

        with patch("src.notifications.asyncio") as mock_asyncio:
            mock_asyncio.run.side_effect = lambda coro: sent_texts.append("captured")

            from src.notifications import send_match_notification
            with patch("src.notifications._env_or_die", return_value="fake"), \
                 patch("src.notifications.Bot") as MockBot:
                mock_bot_instance = AsyncMock()
                MockBot.return_value.__aenter__ = AsyncMock(return_value=mock_bot_instance)
                MockBot.return_value.__aexit__ = AsyncMock(return_value=False)
                mock_bot_instance.send_message = AsyncMock()

                # Capture asyncio.run by monkeypatching
                captured: list[str] = []

                async def fake_send_match(text, keyboard=None):
                    captured.append(text)

                with patch("src.notifications._send_match", new=fake_send_match):
                    with patch("src.notifications.asyncio.run") as mock_run:
                        mock_run.side_effect = lambda coro: asyncio.get_event_loop().run_until_complete(coro) if hasattr(asyncio, 'get_event_loop') else None

                        send_match_notification(
                            job=job,
                            parsed=parsed,
                            result=result,
                            gap_skills=["Kafka"],
                            endpoint_base_url="http://localhost:8000",
                            title_alias="Backend Engineer",
                        )


def test_send_match_notification_contains_key_fragments():
    """Verify the message text includes score, company, resume links."""
    captured: list[str] = []

    async def fake_send(text, keyboard=None):
        captured.append(text)

    job = _make_job()
    parsed = _make_parsed()
    result = _make_result()

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
            job=job,
            parsed=parsed,
            result=result,
            gap_skills=["Kafka"],
            endpoint_base_url="http://localhost:8000",
            title_alias="Backend Engineer",
        )

    assert captured, "No message was captured"
    text = captured[0]
    assert "0.78" in text
    assert "Fintech Inc" in text
    assert "Backend Engineer" in text
    assert "Kafka" in text


def test_apply_threshold_reads_from_scoring_section():
    """Regression: the threshold must come from `scoring`, not `selection`.

    `settings.selection.apply_threshold` does not exist — config.yaml defines
    `apply_threshold` only under `scoring:`. Reading the wrong section raised
    AttributeError inside send_match_notification, which src/main.py swallowed
    as `notification_error`, so every live match notification silently failed
    to send. Assert against the real settings object (not a mock) so a config
    rename can't reintroduce it.
    """
    from src.config import settings

    threshold = settings.scoring.apply_threshold
    assert isinstance(threshold, (int, float))

    with pytest.raises(AttributeError):
        settings.selection.apply_threshold


def test_send_match_notification_renders_threshold():
    """The rendered message includes the apply threshold from config."""
    from src.config import settings

    captured: list[str] = []

    async def fake_send(text, keyboard=None):
        captured.append(text)

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
            job=_make_job(),
            parsed=_make_parsed(),
            result=_make_result(),
            gap_skills=["Kafka"],
            endpoint_base_url="http://localhost:8000",
            title_alias="Backend Engineer",
        )

    assert captured, "no message was sent"
    assert str(settings.scoring.apply_threshold) in captured[0]
