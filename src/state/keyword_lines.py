"""#138: every keyword an entry proves must reach its ``keyword_lines``.

An entry's grouped lines can only print keywords that have a phrase. A bullet
added later with a new tool, and no phrase for it, would quietly leave that
tool out of every grouped line, so this check runs where profile edits take
effect: ``python -m src.cli.reparse`` and ``python -m src.cli.assets push``
refuse a profile with gaps, and ``python -m tools.keyword_lines_check`` lists
them.

For each entry that has ``keyword_lines``:
  * missing  - skill terms some bullet (main or extra, any block) proves that no
               phrase covers and ``skip`` does not name. Fix: add a phrase with
               that bullet as its source, and add it to every frame whose verb
               is true for it (or a new frame), or add the term to ``skip``
               with the reason (wrong sense, domain word, combined spelling).
  * loose    - phrases in no frame: reported for review, never an error.
Entries without ``keyword_lines`` are listed as not drafted yet.

Skill terms are the profile's ``skills_pool`` plus the skills registry (#188).
How to write phrases, frames and endings: docs/profile_edits.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EntryGaps:
    entry_id: str
    phrases: int
    frames: int
    proven: int
    missing: list[tuple[str, str]] = field(default_factory=list)  # (term, bullet id)
    loose: list[str] = field(default_factory=list)
    stale_skip: list[str] = field(default_factory=list)


@dataclass
class Report:
    entries: list[EntryGaps]
    not_drafted: list[str]

    @property
    def missing(self) -> int:
        return sum(len(e.missing) for e in self.entries)

    def render(self) -> str:
        out = []
        for e in self.entries:
            out.append(f"== {e.entry_id}: {e.phrases} phrases, {e.frames} frames, "
                       f"{e.proven} proven terms, {len(e.missing)} missing")
            out += [f"   MISSING   {t}   (proven by {b})" for t, b in e.missing]
            if e.loose:
                out.append(f"   NO FRAME  {', '.join(e.loose)}")
            if e.stale_skip:
                out.append(f"   STALE SKIP {', '.join(e.stale_skip)}")
        if self.not_drafted:
            out.append(f"not drafted yet (no keyword_lines): {', '.join(self.not_drafted)}")
        return "\n".join(out)


def skill_terms(raw: dict) -> set[str]:
    from src.state import registry

    return set(raw.get("skills_pool") or []) | set(registry.skill_terms())


def check(profile: Any, terms: set[str], only: set[str] | None = None) -> Report:
    """``profile`` is a validated ``MasterProfile``."""
    from src.scorer.keywords import Keyword, covered_by, norm

    kws = tuple(Keyword(t, 1.0) for t in sorted(terms))
    entries, not_drafted = [], []
    for e in profile.entries:
        if only and e.id not in only:
            continue
        kl = e.keyword_lines
        if kl is None:
            not_drafted.append(e.id)
            continue
        proven: dict[str, str] = {}
        for rb in e.role_blocks:
            for b in rb.all_bullets:
                for t in covered_by(norm(b.text), kws):
                    proven.setdefault(t, b.id)
        said: set[str] = set()
        for ph in kl.phrases:
            said |= covered_by(norm(ph.text), kws)
        skip = {norm(s) for s in kl.skip}
        proven_norm = {norm(t) for t in proven}
        in_frame = {i for f in kl.frames for i in f.allowed}
        entries.append(EntryGaps(
            entry_id=e.id, phrases=len(kl.phrases), frames=len(kl.frames), proven=len(proven),
            missing=sorted((t, b) for t, b in proven.items() if t not in said and norm(t) not in skip),
            loose=[ph.id for ph in kl.phrases if ph.id not in in_frame],
            stale_skip=sorted(s for s in kl.skip if norm(s) not in proven_norm),
        ))
    return Report(entries, not_drafted)


def check_yaml(path) -> Report:
    import yaml

    from src.state.master_profile import MasterProfile

    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return check(MasterProfile.model_validate(raw), skill_terms(raw))


GATE_HELP = (
    "A bullet proves a keyword that has no phrase in its entry's keyword_lines (#138).\n"
    "Add a phrase (source = that bullet) and put it in every frame whose verb fits,\n"
    "or list the term under skip: with the reason. See docs/profile_edits.md.\n"
    "Run `python -m tools.keyword_lines_check` for the list; "
    "--allow-keyword-gaps overrides."
)
