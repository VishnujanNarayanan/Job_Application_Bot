"""Which parser decides a job, from its dictionary score (#71).

Every job is parsed and scored in dictionary mode first (free); that score
picks the route. Cut-offs from the 2026-10-16 replay of 1,323 parsed jobs:

    dictionary score   route             replay
    < 0.40             SKIPPED_FLOOR     #100: 279 jobs, 0 LLM applies lost
    0.40 - 0.44        DICTIONARY        #71 lower band: decided LOW_SCORE
    0.44 - 0.75        LLM               the LLM parse decides
    >= 0.75            DICTIONARY        #71 top band: 94% agree, 0 applies lost

At 0.44 / 0.75: 42% fewer LLM calls, 7 of 232 LLM applies lost, 6 extra.
Until #71 is enabled only the #100 floor applies and everything at or above
it goes to the LLM.

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


@dataclass(frozen=True)
class RoutingRules:
    """Cut-offs on the dictionary score (``fit x applicant multiplier``).

    ``enabled=False`` is today's behaviour: everything at or above the floor
    goes to the LLM. ``floor`` alone is #100; ``skip_below`` / ``trust_above``
    / ``daily_llm_budget`` are #71.
    """

    enabled: bool = False
    floor: float = 0.40
    skip_below: float = 0.44
    trust_above: float = 0.75
    #: LLM parses allowed per day before the budget rule applies; None = no cap.
    daily_llm_budget: int | None = None
    #: Stored with every job as ``routing_version``; bump when a cut-off moves.
    version: str = "2026-10-16"


@dataclass(frozen=True)
class RouteDecision:
    route: Route
    reason: Reason


def rules_from_config() -> RoutingRules:
    """``parse_routing`` from config.yaml (``scoring.parse_floor`` for #100);
    missing keys keep the defaults above. Validates floor <= skip_below <=
    trust_above."""
    raise NotImplementedError("#71: read parse_routing config")


def llm_calls_today(session) -> int:
    """LLM parses made since local midnight, counted from ``all_jobs``
    (``llm_called`` true, by parse time) so the cap holds across runs."""
    raise NotImplementedError("#71: count today's LLM parses")


def route(dict_score: float, *, llm_calls_today: int, rules: RoutingRules) -> RouteDecision:
    """The route for one job, from its dictionary score.

    1. ``dict_score < rules.floor``                    -> SKIPPED_FLOOR / BELOW_FLOOR
    2. ``not rules.enabled``                           -> LLM / ROUTING_DISABLED
    3. ``dict_score < rules.skip_below``               -> DICTIONARY / BELOW_SKIP
    4. ``dict_score >= rules.trust_above``             -> DICTIONARY / ABOVE_TRUST
    5. budget set and ``llm_calls_today >= budget``    -> CARRIED_OVER / BUDGET_CAP
    6. otherwise                                       -> LLM / MIDDLE_BAND

    The dictionary's own hard filters (years ceiling, job type) run before
    this, in the pipeline: a job they reject is decided without a score.
    """
    raise NotImplementedError("#71: route")
