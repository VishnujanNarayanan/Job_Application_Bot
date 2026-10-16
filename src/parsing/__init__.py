"""Layer 3 parse modes: LLM or dictionary-only, routed per job (#71).

Scaffold only -- the interfaces are fixed, the bodies raise NotImplementedError.

One entry point for callers (``main``, ``cli.backfill``, a replay tool)::

    from src.parsing import parse_job
    result = parse_job(job, profile=profile, llm_calls_today=n)   # routed by dictionary score
    result = parse_job(job, profile=profile, llm_calls_today=n,
                       mode=ParseMode.DICTIONARY)                  # forced
    apply_to_row(job, result.parsed); job.parse_mode = result.decision.route

Modules, each replaceable on its own:

  * ``modes``      -- ``ParseMode``, ``ParseResult``, the ``ParseStrategy``
                      protocol and the registry new modes plug into.
  * ``router``     -- which route a job gets from its dictionary score:
                      skip (floor, #100), dictionary, LLM, or carry over.
  * ``dictionary`` -- the no-LLM parser, built from small steps (skill scan,
                      required/nice split, years, responsibilities) driven by
                      a ``DictionaryRules`` object that holds every pattern.
  * ``llm``        -- adapter over today's ``src.parser.parse``, unchanged.
  * ``shadow``     -- on LLM-mode jobs, the free dictionary parse run alongside
                      and the difference logged.
  * ``pipeline``   -- ties them together: dictionary parse and score, route,
                      LLM, fall back, shadow.
"""

from src.parsing.modes import ParseMode, ParseResult, ParseStrategy, get_strategy, register
from src.parsing.pipeline import parse_job

__all__ = [
    "ParseMode",
    "ParseResult",
    "ParseStrategy",
    "get_strategy",
    "parse_job",
    "register",
]
