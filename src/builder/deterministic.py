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
from src.llm.schemas import SkillCategory, StoredSkills
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


def _taxonomy() -> dict[str, str]:
    """``{skill_casefold: category}`` from the configured taxonomy."""
    mapping: dict[str, str] = {}
    for category, skills in settings.selection.skills.taxonomy.as_dict().items():
        for skill in skills or []:
            mapping[str(skill).casefold()] = str(category)
    return mapping


def _category_of(
    skill: str,
    taxonomy: dict[str, str],
    skill_vec,
    category_vecs: dict[str, list[float]],
) -> str:
    """The skill's category: exact lookup, falling back to nearest heading.

    The fallback exists so a skill added to master_profile.yaml but not yet to
    the taxonomy still lands somewhere rather than vanishing from the resume.
    It is logged so the gap gets closed — embedding similarity is unreliable
    for this (see the config comment), so it is a stopgap, not the mechanism.
    """
    hit = taxonomy.get(skill.casefold())
    if hit is not None:
        return hit

    nearest = max(
        category_vecs, key=lambda name: cosine(skill_vec, category_vecs[name])
    )
    log.warning("skill_untaxonomised", skill=skill, assigned_to=nearest)
    return nearest


def assign_skill_categories(
    candidates: list[tuple[str, float]],
    gap_skills: list[str],
    jd_role_vec=None,
) -> StoredSkills:
    """Group Layer 4's ranked skills and show the categories this JD wants.

    ``candidates`` is the ranked ``(skill, jd_cosine)`` list — already the
    top-N by JD match, so this decides *grouping and which groups to show*,
    never membership.

    Grouping is an exact taxonomy lookup, so it is always correct. Which
    categories appear still varies per job: categories are ranked by their
    best-matching skill's JD score, so a data-heavy JD surfaces the data
    categories and a backend JD surfaces the backend ones.

    ``gap_skills`` become the "Familiar With" group (capped by
    ``familiar_with_max``), ordered against the others rather than pinned
    first (hard rule #7).
    """
    cfg = settings.selection.skills
    per_max = int(cfg.skills_per_category_max)
    wanted = int(cfg.categories_count)

    if not candidates:
        return StoredSkills(categories=[], familiar_with=[])

    taxonomy = _taxonomy()
    all_categories = [str(c) for c in cfg.taxonomy.as_dict()]

    # Only embed anything if a skill is missing from the taxonomy.
    unknown = [s for s, _ in candidates if s.casefold() not in taxonomy]
    category_vecs: dict[str, list[float]] = {}
    skill_vecs: dict[str, list[float]] = {}
    if unknown:
        category_vecs = dict(zip(all_categories, embed_batch(all_categories)))
        skill_vecs = dict(zip(unknown, embed_batch(unknown)))

    buckets: dict[str, list[tuple[str, float]]] = {}
    for skill, jd_score in candidates:
        category = _category_of(
            skill, taxonomy, skill_vecs.get(skill), category_vecs
        )
        buckets.setdefault(category, []).append((skill, jd_score))

    # Rank categories by their strongest JD match, keep the top `wanted`.
    ranked = sorted(
        buckets.items(),
        key=lambda kv: max(score for _, score in kv[1]),
        reverse=True,
    )[:wanted]

    categories: list[SkillCategory] = []
    for name, entries in ranked:
        entries.sort(key=lambda pair: pair[1], reverse=True)
        categories.append(
            SkillCategory(name=name, skills=[s for s, _ in entries[:per_max]])
        )

    familiar = [str(g) for g in gap_skills][: int(cfg.familiar_with_max)]

    log.info(
        "skills_assigned",
        categories={c.name: len(c.skills) for c in categories},
        familiar_with=len(familiar),
        untaxonomised=len(unknown),
    )
    return StoredSkills(categories=categories, familiar_with=familiar)


def jd_role_vector(jd_role_summary: str, fallback_text: str = "") -> list[float]:
    """Embedding of the JD's role text, used for title-alias matching."""
    text = (jd_role_summary or fallback_text or "").strip()
    return embed(text) if text else embed(" ")
