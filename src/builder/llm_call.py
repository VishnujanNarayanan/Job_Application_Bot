"""Layer 5 — selection builder: title alias + skills, no LLM.

Accepts the Layer-4 ``SelectionResult`` and ``Profile`` and returns a
``StoredSelection`` ready to be written to ``applied.selection_json``.
Returns ``None`` on irrecoverable failure (caller records BUILD_FAILURE in
``not_applied``).

**Call 1b was removed.** It asked Gemini for a title alias, skill-category
names, and cover-letter text; none needed a language model. Title choice is an
argmax over an allow-list, the skills were already chosen deterministically by
Layer 4 (only their grouping came from the LLM), and the cover letter was
written to the DB and never read by anything. See
``src/builder/deterministic.py`` for the reasoning.

Budget: a matched job now costs ONE Gemini call (Call 1a, the JD parse) rather
than two. Hard rule #13 caps it at two; this is comfortably under.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.builder.deterministic import choose_title_alias, jd_role_vector
from src.config import settings
from src.llm.schemas import SelectedEntryOut, StoredSelection
from src.reasons import BUILD_FAILURE as _BUILD_FAILURE_REASON
from src.scorer.apply_decision import SelectionResult
from src.scorer.selector import Profile

log = structlog.get_logger(__name__)

_ROOT = Path(__file__).resolve().parents[2]


def _template_version() -> str:
    """MD5[:8] of the current template file (cache-bust key)."""
    path = _ROOT / settings.endpoint.template_path
    try:
        digest = hashlib.md5(path.read_bytes()).hexdigest()
        return digest[:8]
    except OSError:
        return "00000000"


def _gap_skills_for_jd(
    required_skills: list[str], skills_pool: list[str]
) -> list[str]:
    """JD required skills not present in the operator's skills pool."""
    pool_lower = {s.casefold() for s in skills_pool}
    return [
        s for s in required_skills
        if s.casefold() not in pool_lower
        and not any(s.casefold() in p for p in pool_lower)
    ]


def build(
    result: SelectionResult,
    profile: Profile,
    jd_role_summary: str,
    jd_required_skills: list[str],
    jd_team_or_product: str | None = None,
    *,
    complete_fn=None,
) -> StoredSelection | None:
    """Build the ``StoredSelection`` for a matched job. No LLM call.

    Layer 4 has already done the work: which entries, which bullets, in what
    order, and what they cover. This function only resolves the one remaining
    choice — which of the lead block's title aliases to display — and packages the
    result into the durable artifact.

    ``complete_fn`` is accepted and ignored: it existed so tests could stub Gemini
    before Call 1b was removed. Kept in the signature so callers do not break.
    """
    if complete_fn is not None:
        log.debug("complete_fn_ignored", reason="call_1b_removed")

    # One embedding of the JD role text, reused for every entry's aliases.
    jd_role_vec = jd_role_vector(jd_role_summary, fallback_text=jd_team_or_product or "")

    by_id = {e.id: e for e in (*profile.work, *profile.projects)}
    entries: list[SelectedEntryOut] = []
    for se in result.entries:
        entry = by_id.get(se.id)
        block = None
        if entry is not None:
            block = next((b for b in entry.blocks if b.block_id == se.block_id), None)
        # Hard rule #6: the displayed title comes from the lead block's allow-list,
        # never from free text. choose_title_alias picks by cosine within that list,
        # so an out-of-set title is structurally impossible.
        aliases = list(block.title_aliases) if block else []
        fallback = (entry.actual_title if entry else "") or se.label
        entries.append(
            SelectedEntryOut(
                kind=se.kind,
                entry_id=se.id,
                block_id=se.block_id,
                title_alias=choose_title_alias(aliases, jd_role_vec, fallback=fallback),
                header_left=se.header_left,
                header_right=se.header_right,
                bullet_ids=[b.id for b in se.bullets],
                covered=sorted(se.covered),
                coverage=se.coverage,
                score=se.score,
                cap=se.cap,
            )
        )

    # A selection with no work entries cannot produce a resume; Layer 4
    # force-includes two, so reaching here means the profile or selection is broken.
    if not any(e.kind == "work" for e in entries):
        log.error(
            BUILD_FAILURE_EVENT,
            caller="builder",
            reason="no_work_entries_selected",
        )
        return None

    return StoredSelection(
        job_id="",  # caller fills in job_id before writing to DB
        entries=entries,
        jd_keywords=[k.token for k in result.jd_keywords],
        keyword_coverage=result.keyword_coverage,
        lead_entry_coverage=result.lead_entry_coverage,
        cover_letter_text="",
        template_version=_template_version(),
        built_at=datetime.now(timezone.utc).isoformat(),
    )


# Event name referenced by CloudWatch metric filters (CLAUDE.md logging section).
BUILD_FAILURE_EVENT = _BUILD_FAILURE_REASON
