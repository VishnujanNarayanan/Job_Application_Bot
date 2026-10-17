"""Shadow comparison (#71 step 4).

On every LLM-mode job the dictionary parse also runs -- it costs nothing --
and the difference is logged. Across enough jobs that shows whether the LLM
share can drop below half without changing apply decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.llm.schemas import JDParsed
    from src.state.models import AllJobs


@dataclass(frozen=True)
class ShadowDiff:
    """How the dictionary parse differs from the LLM parse of one job.

    Skill sets are compared by ``keywords.canonical_key`` so spelling variants
    ("scikit-learn" / "sklearn") are not counted as differences.
    """

    job_id: str
    llm_only: tuple[str, ...]
    dictionary_only: tuple[str, ...]
    years_llm: int
    years_dictionary: int
    #: ``None`` until the scorer is wired in (it needs a profile and session).
    decision_changed: bool | None = None

    @property
    def skills_agree(self) -> bool:
        return not self.llm_only and not self.dictionary_only


def compare(job: AllJobs, llm: JDParsed, dictionary: JDParsed) -> ShadowDiff:
    """Skill and years differences between the two parses of ``job``."""
    raise NotImplementedError("#71: shadow diff")


def decision_changes(job: AllJobs, llm: JDParsed, dictionary: JDParsed, **score_ctx) -> bool:
    """Whether the apply decision differs between the two parses.

    Scores both through ``tailor.score_job`` with the same profile;
    ``score_ctx`` carries whatever that needs (profile, session, config).
    Kept separate from :func:`compare` because it is the expensive part and
    a replay may want it while a live run may not.
    """
    raise NotImplementedError("#71: decision comparison")


def log_diff(diff: ShadowDiff) -> None:
    """One structured ``parse_shadow`` log line per job."""
    raise NotImplementedError("#71: shadow log")
