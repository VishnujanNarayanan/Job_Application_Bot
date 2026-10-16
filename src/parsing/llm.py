"""The ``llm`` mode: today's parser, unchanged (#71 step 3).

A thin adapter so the pipeline treats both modes alike. Its skills keep
feeding ``vocabulary rebuild``, which is how new terms reach the dictionary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.parsing.modes import ParseMode

if TYPE_CHECKING:
    from src.llm.schemas import JDParsed
    from src.parser import CompleteFn
    from src.state.models import AllJobs


class LLMParser:
    mode = ParseMode.LLM

    def __init__(self, complete: CompleteFn | None = None) -> None:
        # Injectable transport, as ``parser.parse`` takes, so tests run offline.
        self.complete = complete

    def parse(self, job: AllJobs) -> JDParsed:
        """Delegate to ``src.parser.parse``. Raises ``LLMError`` /
        ``LLMBudgetError`` as it does today; the pipeline decides what a
        failure means."""
        from src.parser import parse

        return parse(job, complete=self.complete)
