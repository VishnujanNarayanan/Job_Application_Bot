"""#154: add questions the autofill has seen to the public catalog.

    python -m tools.ats_catalog_sync            # show what would be added
    python -m tools.ats_catalog_sync --write    # append them to docs/ats_questions.yaml

Reads data/ats_questions_seen.jsonl (written by the endpoint, gitignored),
which holds question text, type and options only -- never an answer. A
question already in the catalog, in the same or a close wording, is skipped.
New entries go under ``questions:`` with ``kind: unsorted`` for the operator
to sort later; appending keeps the catalog's comments as they are.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

from src.autofill.bank import SEEN_PATH, _same_question

_ROOT = Path(__file__).resolve().parent.parent
CATALOG = _ROOT / "docs" / "ats_questions.yaml"


def _slug(text: str, taken: set[str]) -> str:
    base = "_".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split()[:6]) or "question"
    slug, i = base, 2
    while slug in taken:
        slug, i = f"{base}_{i}", i + 1
    taken.add(slug)
    return slug


def new_entries(seen_path: Path = SEEN_PATH, catalog: Path = CATALOG) -> list[dict]:
    data = yaml.safe_load(catalog.read_text(encoding="utf-8")) or {}
    existing = [q.get("text", "") for q in data.get("questions") or []]
    taken = {q.get("id", "") for q in data.get("questions") or []}
    merged: list[dict] = []
    if not seen_path.exists():
        return merged
    for line in seen_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = (row.get("text") or "").strip()
        if not text or any(_same_question(text, e) for e in existing):
            continue
        same = next((m for m in merged if _same_question(text, m["text"])), None)
        if same:
            if row.get("seen_at") and row["seen_at"] not in same["seen_at"]:
                same["seen_at"].append(row["seen_at"])
            continue
        entry = {"id": _slug(text, taken), "text": text, "type": row.get("type") or "text"}
        if row.get("options"):
            entry["options"] = row["options"]
        entry["kind"] = "unsorted"
        entry["seen_at"] = [row["seen_at"]] if row.get("seen_at") else []
        merged.append(entry)
    return merged


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="append to docs/ats_questions.yaml")
    args = ap.parse_args(argv)
    entries = new_entries()
    for e in entries:
        print(f"+ {e['text'][:90]}  [{e['type']}]  {', '.join(e['seen_at'])}")
    print(f"{len(entries)} new question(s)")
    if args.write and entries:
        body = yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=110)
        block = "".join("  " + ln if ln.strip() else ln for ln in body.splitlines(True))
        text = CATALOG.read_text(encoding="utf-8")
        text = (text if text.endswith("\n") else text + "\n") + "\n  # --- seen by the autofill (#154), unsorted ---\n" + block
        before = len((yaml.safe_load(CATALOG.read_text(encoding="utf-8")) or {}).get("questions") or [])
        after = len((yaml.safe_load(text) or {}).get("questions") or [])
        if after != before + len(entries):
            print("refused: the catalog would no longer parse as expected", file=sys.stderr)
            return 1
        CATALOG.write_text(text, encoding="utf-8")
        print(f"appended to {CATALOG.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
