"""Read across both ``selection_json`` shapes.

The v3 pivot changed the durable artifact: ``{summary_id, experiences[], projects[],
skills, section_order}`` became ``{version: 2, entries[], jd_keywords, ...}``. The
85 pre-pivot rows in ``applied`` are NOT migrated — they are historical records
kept for later analysis, and they reference bullet ids and a ``summary_id`` from a
profile that has since been regenerated, so nothing could re-render them faithfully
anyway (PIVOT_V3.md D10).

They still have to be *readable*, though: the dashboard lists them and the monthly
analytics report counts them. This module is the one place that knows both shapes,
so the version check does not get copy-pasted into three call sites and drift.

Rendering is a different matter — ``src/endpoint/cache.py`` refuses a v1 row
outright rather than producing a resume against a template it was never built for.
"""

from __future__ import annotations

from typing import Any


def version_of(selection_json: Any) -> int:
    """1 for a pre-pivot selection, 2 for a current one, 0 if unreadable."""
    if not isinstance(selection_json, dict):
        return 0
    version = selection_json.get("version")
    if isinstance(version, int):
        return version
    # v1 predates the field entirely; it is identifiable by its own shape.
    return 1 if "experiences" in selection_json else 0


def title_alias_of(selection_json: Any, fallback: str = "") -> str:
    """The displayed title of the first entry, whichever shape the row uses."""
    if not isinstance(selection_json, dict):
        return fallback
    if version_of(selection_json) == 1:
        first = (selection_json.get("experiences") or [None])[0]
    else:
        first = (selection_json.get("entries") or [None])[0]
    if isinstance(first, dict):
        return first.get("title_alias") or fallback
    return fallback


def coverage_of(selection_json: Any) -> float | None:
    """Keyword coverage, or ``None`` for a v1 row that never measured it."""
    if not isinstance(selection_json, dict) or version_of(selection_json) != 2:
        return None
    value = selection_json.get("keyword_coverage")
    return float(value) if isinstance(value, (int, float)) else None
