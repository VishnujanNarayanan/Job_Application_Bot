"""Layer 5 — title + skills selection without an LLM (replaces Call 1b).

Call 1b used to ask Gemini for three things. None of them actually needed a
language model:

  * **Title alias** — picking the closest entry from a fixed allow-list is
    cosine similarity, and doing it directly is strictly better than asking a
    model and then validating: an out-of-set title becomes structurally
    impossible rather than something to catch afterwards.
  * **Skill categories** — the *skills* were already chosen deterministically
    by Layer 4 (top-N by JD cosine); only the grouping came from the LLM, which
    kept reaching for "Miscellaneous", "Other" and the rest of the old
    banned-names list. Grouping is now an exact lookup against
    ``selection.skills.taxonomy``, and which categories get shown is ranked by
    JD match — so the headings still differ per job (a data role surfaces
    "Data Engineering & ETL", a quant role "Quantitative Finance") without a
    model inventing them.

    Embedding similarity was tried first and rejected: cosines between a short
    tech token and a category phrase do not encode the taxonomic relationship.
    Measured on this pool, correct pairings scored 0.106-0.375 and wrong ones
    0.054-0.347 — overlapping ranges, so no threshold separates them
    ("Grafana -> Monitoring" 0.106 vs "Linux -> Web & Frontend" 0.347). Knowing
    which bucket a tool belongs in is world knowledge; a hand-written map is
    exact and free.
  * **Cover letter** — genuine text generation, but the output was written to
    ``applied.cover_letter_text`` and never read: no renderer, not in the
    Telegram message, not on the dashboard, not in the CSV index. Dropped.

What this buys: the per-match Gemini call disappears (halving calls on a
matched job), the skills post-validation layer becomes unnecessary because
nothing unvalidated can be produced, results stop varying run to run, and a
model retirement can no longer break resume building — which it did on
2026-08-08.

Call 1a (the JD parse) still needs Gemini: extraction and classification are
categorically different from similarity, and there is no way to read
``years_required = 3`` out of a 384-dimensional embedding.
"""

from __future__ import annotations

import structlog

from src.config import settings
from src.scorer.embeddings import cosine, embed, embed_batch

log = structlog.get_logger(__name__)


def choose_title_alias(
    aliases: list[str],
    jd_role_vec,
    *,
    fallback: str,
) -> str:
    """Pick the alias closest to the JD role, or ``fallback`` if none exist.

    Replaces the LLM's ``title_choices``. Because the choice is an argmax over
    the allow-list, a title outside ``safe_title_aliases`` cannot be returned —
    the guarantee hard rule #6 previously enforced with ``Literal`` plus
    post-validation is now structural.
    """
    if not aliases:
        return fallback
    if len(aliases) == 1:
        return aliases[0]

    vecs = embed_batch(aliases)
    best = max(zip(aliases, vecs), key=lambda pair: cosine(pair[1], jd_role_vec))
    return best[0]


def jd_role_vector(jd_role_summary: str, fallback_text: str = "") -> list[float]:
    """Embedding of the JD's role text, used for title-alias matching."""
    text = (jd_role_summary or fallback_text or "").strip()
    return embed(text) if text else embed(" ")
