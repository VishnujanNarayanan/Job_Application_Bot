"""A provider-side schema rejection must not cost five attempts.

Regression cover for 2026-10-07: Groq rejected a parse server-side with
`400 tool_use_failed ... /role_level: expected string, but got null` for an
ad that states no seniority. The client re-sent the identical request five
times, then handed the job to the slowest provider in the chain.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pydantic import BaseModel

from src.llm import client as llm_client
from src.llm.client import _is_permanent
from src.llm.schemas import JDParsed

GROQ_REJECTION = (
    "Error code: 400 - {'error': {'message': 'Tool call validation failed: tool "
    "call validation failed: parameters for tool JDParsed did not match schema: "
    "errors: [`/role_level`: expected string, but got null]', 'type': "
    "'invalid_request_error', 'code': 'tool_use_failed'}}"
)


class Dummy(BaseModel):
    value: str


def test_the_schema_sent_to_providers_admits_a_null_role_level():
    prop = JDParsed.model_json_schema()["properties"]["role_level"]
    assert {"type": "null"} in prop["anyOf"]


def test_a_null_role_level_still_reads_as_a_real_tier():
    parsed = JDParsed(
        role_summary="Regulatory role.", role_category="other", role_level=None,
        years_required=0, location_type="remote",
    )
    assert parsed.role_level == "mid"


def test_a_server_side_schema_rejection_is_permanent():
    assert _is_permanent(RuntimeError(GROQ_REJECTION)) is True


def test_a_schema_rejection_moves_to_the_next_provider_after_one_attempt():
    rejecting = MagicMock()
    rejecting.chat.completions.create.side_effect = RuntimeError(GROQ_REJECTION)
    good = MagicMock()
    good.chat.completions.create.return_value = Dummy(value="from-next")

    def pick(which="primary"):
        return rejecting if which == "primary" else good

    with patch.object(llm_client, "get_client", side_effect=pick), \
         patch.object(llm_client, "rotate", side_effect=lambda chain, _i: chain), \
         patch("time.sleep") as slept:
        result = llm_client.complete(Dummy, "prompt")

    assert result.value == "from-next"
    assert rejecting.chat.completions.create.call_count == 1
    slept.assert_not_called()


def test_a_rejection_does_not_retire_the_provider_for_the_run():
    """It's about one job's output, not the provider: the next job still tries it."""
    rejecting = MagicMock()
    rejecting.chat.completions.create.side_effect = RuntimeError(GROQ_REJECTION)
    good = MagicMock()
    good.chat.completions.create.return_value = Dummy(value="ok")

    def pick(which="primary"):
        return rejecting if which == "primary" else good

    with patch.object(llm_client, "get_client", side_effect=pick), \
         patch.object(llm_client, "rotate", side_effect=lambda chain, _i: chain), \
         patch("time.sleep"):
        llm_client.complete(Dummy, "prompt")
        llm_client.complete(Dummy, "prompt")

    assert rejecting.chat.completions.create.call_count == 2
    assert "primary" not in llm_client._DEAD_PROVIDERS
