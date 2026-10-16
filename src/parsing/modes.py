"""Parse modes, their result type and the strategy registry.

A mode is anything with a ``mode`` and ``parse(job) -> JDParsed``. The
pipeline registers the two from #71 (``llm.LLMParser``,
``dictionary.DictionaryParser``) on first use; a third (a cheaper model, a hybrid
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

    ``mode`` is the parser that PRODUCED ``parsed``, so a failed LLM call that
    fell back reads ``DICTIONARY`` with ``routed`` still ``LLM`` and
    ``fallback_reason`` saying why. ``decision`` is what gets stored
    (``parse_mode`` / ``parse_route_reason``); ``parsed`` is None for a job
    that was carried over.

    ``dict_score`` / ``dict_fit`` are kept for every job, LLM-routed ones
    included, so the two parsers can always be compared.
    """

    parsed: JDParsed | None
    mode: ParseMode | None
    routed: ParseMode | None
    decision: RouteDecision
    dict_score: float
    dict_fit: float
    fallback_reason: str | None = None
    shadow: ShadowDiff | None = None
    extra: dict = field(default_factory=dict)

    @property
    def fell_back(self) -> bool:
        return self.mode is not self.routed


_REGISTRY: dict[ParseMode, ParseStrategy] = {}


def register(strategy: ParseStrategy, *, replace: bool = False) -> ParseStrategy:
    """Make ``strategy`` the parser for its mode. Returns it, so it can decorate
    an instance factory or be swapped in a test with ``replace=True``."""
    raise NotImplementedError("#71: registry")


def get_strategy(mode: ParseMode) -> ParseStrategy:
    """The registered strategy for ``mode``; KeyError if none is."""
    raise NotImplementedError("#71: registry")
