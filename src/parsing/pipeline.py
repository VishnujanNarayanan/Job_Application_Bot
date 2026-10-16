"""The single call site for Layer 3: dictionary parse, route, LLM, fall back."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.parsing.modes import ParseMode, ParseResult

if TYPE_CHECKING:
    from src.parsing.router import RoutingRules
    from src.state.models import AllJobs


def parse_job(
    job: AllJobs,
    *,
    profile,
    llm_calls_today: int,
    rules: RoutingRules | None = None,
    mode: ParseMode | None = None,
    shadow: bool = True,
    fallback: bool = True,
) -> ParseResult:
    """Parse ``job`` and return the result with the route that decided it.

    1. Dictionary parse (free) and score it against ``profile``
       (``tailor.score_job``): this gives ``dict_score`` and ``dict_fit``,
       kept on the result whatever happens next.
    2. ``mode`` given -> use it and skip routing (replays, tests). Otherwise
       ``router.route(dict_score, llm_calls_today=..., rules=rules or
       router.rules_from_config())``.
    3. ``SKIPPED_FLOOR`` / ``DICTIONARY`` -> the dictionary parse is the
       result. ``CARRIED_OVER`` -> ``parsed`` is None; the caller leaves the
       job without a verdict so the next run picks it up.
    4. ``LLM`` -> run the LLM strategy. On ``LLMError`` with ``fallback``
       on, use the dictionary parse: route ``DICTIONARY_FALLBACK``, reason
       ``LLM_ERROR``, ``fallback_reason`` set. ``LLMBudgetError`` still
       propagates: the caller stops the batch on it today.
    5. LLM succeeded and ``shadow`` on -> ``shadow.compare`` the two parses
       and ``shadow.log_diff``; attached as ``result.shadow``. A shadow
       failure is logged and never fails the job.
    6. Log ``job_parsed`` with route, reason, ``dict_score`` and ``mode``.

    The caller stores ``parse_mode``, ``parse_route_reason``, ``dict_score``,
    ``dict_fit``, ``dict_years``, ``dict_job_type``, ``llm_called`` and
    ``routing_version`` on the row (#71 step 7).
    """
    raise NotImplementedError("#71: pipeline")
