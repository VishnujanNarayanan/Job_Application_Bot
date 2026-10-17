"""The single call site for Layer 3: dictionary parse, route, LLM, fall back."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from src.parsing.modes import ParseMode, ParseResult, get_strategy
from src.parsing.router import Reason, Route, RouteDecision, RoutingRules, route, rules_from_config

if TYPE_CHECKING:
    from src.state.models import AllJobs

log = structlog.get_logger(__name__)


def parse_job(
    job: AllJobs,
    *,
    profile,
    llm_calls_today: int = 0,
    rules: RoutingRules | None = None,
    mode: ParseMode | None = None,
    scrape_window_hours: float | None = None,
    shadow: bool = False,
    fallback: bool = True,
) -> ParseResult:
    """Parse ``job`` and return the result with the route that decided it.

    1. Dictionary parse (free) and score it against ``profile``
       (``tailor.score_job``). Kept on the result whatever happens next.
    2. ``mode`` given -> use it and skip routing (replays, tests). Otherwise
       ``router.route(dict_score, ...)`` with ``rules`` (default: config).
    3. ``SKIPPED_FLOOR`` / ``DICTIONARY`` -> the dictionary parse is the
       result. ``CARRIED_OVER`` -> ``parsed`` is None; the caller leaves the
       job without a verdict so the next run picks it up.
    4. ``LLM`` -> run the LLM strategy. On ``LLMError``, with ``fallback`` on
       and routing enabled, use the dictionary parse (``DICTIONARY_FALLBACK``
       / ``LLM_ERROR``); with routing off the error propagates and the job
       carries over, as before #71. ``LLMBudgetError`` always propagates: the
       caller stops the batch on it.
    5. ``shadow`` on and the LLM succeeded -> ``shadow.compare`` the two
       parses and log the difference; a shadow failure never fails the job.
       Off by default until ``shadow`` is implemented (#71).

    The caller stores the route on the row (``record_route``).
    """
    from src.llm.client import LLMBudgetError, LLMError
    from src.tailor import score_job

    rules = rules or rules_from_config()
    dict_parsed = get_strategy(ParseMode.DICTIONARY).parse(job)
    dict_result = score_job(profile, job, dict_parsed, scrape_window_hours=scrape_window_hours)

    if mode is not None:
        decision = RouteDecision(Route.LLM if mode is ParseMode.LLM else Route.DICTIONARY,
                                 Reason.FORCED)
    else:
        decision = route(dict_result.final_score, llm_calls_today=llm_calls_today, rules=rules)

    result = ParseResult(parsed=None, mode=None, decision=decision,
                         dict_parsed=dict_parsed, dict_result=dict_result)
    if decision.route in (Route.SKIPPED_FLOOR, Route.DICTIONARY):
        result.parsed, result.mode = dict_parsed, ParseMode.DICTIONARY
    elif decision.route is Route.LLM:
        result.llm_called = True
        try:
            result.parsed = get_strategy(ParseMode.LLM).parse(job)
            result.mode = ParseMode.LLM
        except LLMBudgetError:
            raise
        except LLMError as exc:
            if not (fallback and rules.enabled):
                raise
            result.parsed, result.mode = dict_parsed, ParseMode.DICTIONARY
            result.decision = RouteDecision(Route.DICTIONARY_FALLBACK, Reason.LLM_ERROR)
            result.fallback_reason = str(exc)[:200]
        if shadow and result.mode is ParseMode.LLM:
            try:
                from src.parsing import shadow as shadow_mod

                result.shadow = shadow_mod.compare(job, result.parsed, dict_parsed)
                shadow_mod.log_diff(result.shadow)
            except Exception as exc:  # noqa: BLE001 - the comparison must not fail the job
                log.warning("parse_shadow_failed", job_id=job.job_id, error=str(exc)[:160])

    log.info("job_routed", job_id=job.job_id, route=result.decision.route.value,
             reason=result.decision.reason.value, dict_score=round(result.dict_score, 3),
             dict_fit=round(result.dict_fit, 3), llm_called=result.llm_called,
             fallback=result.fallback_reason)
    return result


def record_route(job: AllJobs, result: ParseResult, rules: RoutingRules, *, now) -> None:
    """Write the route columns onto the row (#71 step 7). In place, no I/O."""
    job.parse_mode = result.decision.route.value
    job.parse_route_reason = result.decision.reason.value
    job.dict_score = round(result.dict_score, 4)
    job.dict_fit = round(result.dict_fit, 4)
    job.dict_years = result.dict_parsed.years_required
    job.dict_job_type = result.dict_parsed.job_type
    job.llm_called = result.llm_called
    job.routing_version = rules.version
    job.routed_at = now
