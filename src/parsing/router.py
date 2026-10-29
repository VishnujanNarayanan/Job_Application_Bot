"""Which parser decides a job, from its dictionary score (#71).

Every job is parsed and scored in dictionary mode first (free); that score
picks the route. Cut-offs from the 2026-10-17 replay of 1,433 parsed jobs
under the weighted_count fit (#101):

    dictionary score   route             replay
    < 0.437            SKIPPED_FLOOR     #100: same number skipped as 0.40 under `share`
    0.437 - 0.51       DICTIONARY        #71 lower band: decided LOW_SCORE
    0.51 - 0.88        LLM               the LLM parse decides
    >= 0.88            DICTIONARY        #71 top band

At 0.51 / 0.88: as many LLM calls saved as 0.44 / 0.75 under `share`, 9 LLM
matches lost instead of 17. With `enabled: false` only the #100 floor applies
and everything at or above it goes to the LLM.

The cut-offs depend on the profile and the fit formula (#101). Re-derive
them from the stored ``dict_score`` / ``parse_mode`` columns when either
changes, and bump ``RoutingRules.version`` so populations don't mix.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Route(str, Enum):
    """Stored as ``all_jobs.parse_mode``."""

    LLM = "llm"
    DICTIONARY = "dictionary"
    DICTIONARY_FALLBACK = "dictionary_fallback"  # LLM call failed; set by the pipeline
    SKIPPED_FLOOR = "skipped_floor"              # #100
    CARRIED_OVER = "carried_over"                # budget cap; no verdict this run


class Reason(str, Enum):
    """Stored as ``all_jobs.parse_route_reason``."""

    BELOW_FLOOR = "below_floor"
    BELOW_SKIP = "below_skip"
    MIDDLE_BAND = "middle_band"
    ABOVE_TRUST = "above_trust"
    BUDGET_CAP = "budget_cap"
    ROUTING_DISABLED = "routing_disabled"
    LLM_ERROR = "llm_error"                      # set by the pipeline on fallback
    LLM_UNAVAILABLE = "llm_unavailable"          # the run's allowed providers are spent
    FORCED = "forced"                            # mode passed explicitly (replays, tests)


@dataclass(frozen=True)
class RoutingRules:
    """Cut-offs on the dictionary score (``fit x applicant multiplier``).

    ``enabled=False`` is today's behaviour: everything at or above the floor
    goes to the LLM. ``floor`` alone is #100; ``skip_below`` / ``trust_above``
    / ``daily_llm_budget`` are #71.
    """

    enabled: bool = False
    floor: float = 0.437
    skip_below: float = 0.51
    trust_above: float = 0.88
    #: LLM parses allowed per day before the budget rule applies; None = no cap.
    daily_llm_budget: int | None = None
    #: Stored with every job as ``routing_version``; bump when a cut-off moves.
    version: str = "2026-10-17"


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reason: Reason


def rules_from_config() -> RoutingRules:
    """``scoring.parse_routing`` from config.yaml; missing keys keep the
    defaults above. ``floor: 0`` turns the #100 floor off."""
    from src.config import settings

    raw = settings.scoring.get("parse_routing")
    get = raw.get if raw is not None else (lambda _k, d=None: d)
    defaults = RoutingRules()
    budget = get("daily_llm_budget", None)
    rules = RoutingRules(
        enabled=bool(get("enabled", defaults.enabled)),
        floor=float(get("floor", defaults.floor)),
        skip_below=float(get("skip_below", defaults.skip_below)),
        trust_above=float(get("trust_above", defaults.trust_above)),
        daily_llm_budget=int(budget) if budget is not None else None,
        version=str(get("version", defaults.version)),
    )
    if not rules.floor <= rules.skip_below <= rules.trust_above:
        raise ValueError(
            "scoring.parse_routing needs floor <= skip_below <= trust_above, got "
            f"{rules.floor} / {rules.skip_below} / {rules.trust_above}")
    return rules


def llm_calls_today(session, *, now=None) -> int:
    """LLM parses routed since UTC midnight, counted from ``all_jobs``
    (``llm_called`` true, by ``routed_at``) so the cap holds across runs."""
    from datetime import datetime, timezone

    from sqlalchemy import func, select

    from src.state.models import AllJobs

    now = now or datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(session.scalar(
        select(func.count()).select_from(AllJobs)
        .where(AllJobs.llm_called.is_(True), AllJobs.routed_at >= midnight)
    ) or 0)


def route(dict_score: float, *, llm_calls_today: int, rules: RoutingRules) -> RouteDecision:
    """The route for one job, from its dictionary score.

    1. ``dict_score < rules.floor``                    -> SKIPPED_FLOOR / BELOW_FLOOR
    2. ``not rules.enabled``                           -> LLM / ROUTING_DISABLED
    3. ``dict_score < rules.skip_below``               -> DICTIONARY / BELOW_SKIP
    4. ``dict_score >= rules.trust_above``             -> DICTIONARY / ABOVE_TRUST
    5. budget set and ``llm_calls_today >= budget``    -> CARRIED_OVER / BUDGET_CAP
    6. otherwise                                       -> LLM / MIDDLE_BAND

    The dictionary's own hard filters (years ceiling, job type) are not
    applied here: until #71 is enabled every job at or above the floor goes
    to the LLM, which applies them on its own parse.
    """
    if dict_score < rules.floor:
        return RouteDecision(Route.SKIPPED_FLOOR, Reason.BELOW_FLOOR)
    if not rules.enabled:
        return RouteDecision(Route.LLM, Reason.ROUTING_DISABLED)
    if dict_score < rules.skip_below:
        return RouteDecision(Route.DICTIONARY, Reason.BELOW_SKIP)
    if dict_score >= rules.trust_above:
        return RouteDecision(Route.DICTIONARY, Reason.ABOVE_TRUST)
    if rules.daily_llm_budget is not None and llm_calls_today >= rules.daily_llm_budget:
        return RouteDecision(Route.CARRIED_OVER, Reason.BUDGET_CAP)
    return RouteDecision(Route.LLM, Reason.MIDDLE_BAND)
