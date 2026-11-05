"""Comparable tools (#191): partial credit for the same job done in another product.

An advert asks for Google Cloud Storage; the profile never names it but proves
S3. ``config/comparable_tools.yaml`` (written by hand) says the two do the same
job, so the checklist item is split:

* the advert's own ask keeps ``1 - credit`` of its weight and stays a gap;
* the comparable tool the profile proves joins the checklist with ``credit``
  of the weight, so the bullet that proves it is picked and bolded for it.

The total weight is unchanged, so a comparable never scores above the real
thing. The ask still shows as a gap skill (gaps are computed apart from the
score), and the bullet text is never changed: the page names only what was
used. No general programming languages are grouped (see the YAML header).

``scoring.comparable.credit`` sets the share (0 turns the rule off).
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import yaml

from src.config import settings

_PATH = Path(__file__).resolve().parents[2] / "config" / "comparable_tools.yaml"


@lru_cache(maxsize=1)
def _groups() -> tuple[tuple[str, tuple[str, ...]], ...]:
    raw = yaml.safe_load(_PATH.read_text(encoding="utf-8")) if _PATH.exists() else {}
    return tuple((str(g["name"]), tuple(str(t) for t in g["tools"]))
                 for g in (raw or {}).get("groups") or [])


def credit() -> float:
    cfg = settings.scoring.get("comparable") or {}
    return max(0.0, min(1.0, float(cfg.get("credit", 0.0))))


@lru_cache(maxsize=4096)
def comparables(token: str) -> tuple[str, ...]:
    """Other tools in ``token``'s groups, group order kept; empty if none."""
    from src.scorer.keywords import canonical_key

    key = canonical_key(token)
    out: list[str] = []
    for _name, tools in _groups():
        keys = [canonical_key(t) for t in tools]
        if key in keys:
            out += [t for t, k in zip(tools, keys) if k != key and t not in out]
    return tuple(out)


def group_of(a: str, b: str) -> str | None:
    """The name of a group holding both tools, if any."""
    from src.scorer.keywords import canonical_key

    ka, kb = canonical_key(a), canonical_key(b)
    for name, tools in _groups():
        keys = {canonical_key(t) for t in tools}
        if ka in keys and kb in keys:
            return name
    return None


def with_comparables(keywords: Sequence, evidence: Sequence[str]) -> tuple:
    """``keywords`` with each ask the profile cannot prove split between itself
    and the first comparable tool the profile does prove (see module doc).

    ``evidence``: normalised profile texts (skills pool lines and bullets).
    Each stand-in carries ``stands_for`` = the advert's ask."""
    from src.scorer.keywords import canonical_key, matches

    c = credit()
    if c <= 0 or not keywords:
        return tuple(keywords)
    present = {canonical_key(k.token) for k in keywords}

    def proven(tok: str) -> bool:
        return any(matches(tok, e) for e in evidence)

    out: list = []
    for k in keywords:
        stand_in = None
        if comparables(k.token) and not proven(k.token):
            stand_in = next((t for t in comparables(k.token)
                             if canonical_key(t) not in present and proven(t)), None)
        if stand_in is None:
            out.append(k)
            continue
        present.add(canonical_key(stand_in))
        out.append(replace(k, weight=k.weight * (1 - c), unit=k.unit * (1 - c)))
        out.append(replace(k, token=stand_in, weight=k.weight * c, unit=k.unit * c, stands_for=k.token))
    return tuple(out)
