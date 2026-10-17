"""Parse modes, their result type and the strategy registry.

A mode is anything with a ``mode`` and ``parse(job) -> JDParsed``. The two
built-in modes (``llm.LLMParser``, ``dictionary.DictionaryParser``) register
themselves on first use; a third (a cheaper model, a hybrid
that sends only the unmatched sections to the LLM) is one ``register`` call
and needs no change to the router or the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.llm.schemas import JDParsed
    from src.parsing.router import RouteDecision
    from src.parsing.shadow import ShadowDiff
    from src.state.models import AllJobs


class ParseMode(str, Enum):
    """How a job was parsed. The value is what gets logged and stored."""

    LLM = "llm"
    DICTIONARY = "dictionary"


@runtime_checkable
class ParseStrategy(Protocol):
    """One way of turning a scraped row into a :class:`JDParsed`."""

    mode: ParseMode

    def parse(self, job: AllJobs) -> JDParsed: ...


@dataclass
class ParseResult:
    """What the pipeline hands back to the caller.

    ``decision`` is what gets stored (``parse_mode`` / ``parse_route_reason``).
    ``parsed`` is the parse the verdict comes from: the LLM's on the LLM
    route, the dictionary's on every other route, None when carried over.
    ``mode`` is the parser that produced it, so an LLM call that failed and
    fell back reads ``DICTIONARY`` with ``fallback_reason`` saying why.

    The dictionary parse and its score are kept for every job, LLM-routed
    ones included, so the two parsers can always be compared.
    """

    parsed: JDParsed | None
    mode: ParseMode | None
    decision: RouteDecision
    dict_parsed: JDParsed
    #: The dictionary parse scored against the profile (``SelectionResult``).
    dict_result: object
    llm_called: bool = False
    fallback_reason: str | None = None
    shadow: ShadowDiff | None = None
    extra: dict = field(default_factory=dict)

    @property
    def dict_score(self) -> float:
        return float(self.dict_result.final_score)

    @property
    def dict_fit(self) -> float:
        return float(self.dict_result.fit)

    @property
    def fell_back(self) -> bool:
        return self.fallback_reason is not None


_REGISTRY: dict[ParseMode, ParseStrategy] = {}


def register(strategy: ParseStrategy, *, replace: bool = False) -> ParseStrategy:
    """Make ``strategy`` the parser for its mode. Returns it, so a test can
    swap one in with ``replace=True``. Registering a second parser for a mode
    without ``replace`` is an error rather than a silent swap."""
    if strategy.mode in _REGISTRY and not replace:
        raise ValueError(f"a parser is already registered for {strategy.mode.value}")
    _REGISTRY[strategy.mode] = strategy
    return strategy


def get_strategy(mode: ParseMode) -> ParseStrategy:
    """The registered strategy for ``mode``. The two built-in modes register
    themselves on first use; any other unregistered mode is a KeyError."""
    if mode not in _REGISTRY:
        if mode is ParseMode.LLM:
            from src.parsing.llm import LLMParser

            register(LLMParser())
        elif mode is ParseMode.DICTIONARY:
            from src.parsing.dictionary import DictionaryParser

            register(DictionaryParser())
    return _REGISTRY[mode]


def reset_registry() -> None:
    """Forget every registered parser (tests; a reloaded vocabulary)."""
    _REGISTRY.clear()
