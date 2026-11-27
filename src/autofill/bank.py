"""Growing the answer bank from the forms the operator opens (#154).

A question the autofill cannot answer is appended to data/ats_answers.yaml
under ``unanswered:`` with ``answer: null``. The operator fills the answer in
place, and every later form asking the same thing (in any wording close enough)
gets it.

Code never rewrites that file -- PyYAML would drop the operator's comments --
it only appends, which is why ``unanswered:`` must stay the file's last key.

Every question seen, answered or not, also goes to
data/ats_questions_seen.jsonl (questions and options only), which
``python -m tools.ats_catalog_sync`` merges into the public catalog
docs/ats_questions.yaml.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import yaml

from src.autofill.facts import ANSWERS_PATH, load_answers_file
from src.autofill.matcher import LIKELY, _content, is_generic, norm, question_score

log = structlog.get_logger(__name__)

SEEN_PATH = ANSWERS_PATH.parent / "ats_questions_seen.jsonl"
OPENS_PATH = ANSWERS_PATH.parent / "logs" / "autofill_opens.jsonl"
_LOCK = threading.Lock()
#: Longer than this is a paragraph of instructions, not a question.
_MAX_QUESTION = 300

_HEADER = """\
# Answers for application forms (#154). GITIGNORED: never commit this file.
#
# Facts already in master_profile.yaml / config.yaml (name, email, phone,
# links, current employer, education, years of experience, relocation) are
# filled from there; set a key here only to override one ("city: Bengaluru").
#
# salary: CTC in LPA. Expected CTC = the middle of the advert's range, never
#   below expected_min_lpa; the floor when the advert gives no range.
# answers: key -> {asks: [phrasings], answer: ...}. A null answer is skipped.
# unanswered: questions seen on a form with no answer yet. Fill `answer:` in
#   place. Keep this key LAST: the endpoint appends to the end of the file.

"""


def _same_question(a: str, b: str) -> bool:
    if norm(a) == norm(b):
        return True
    ca, cb = _content(a), _content(b)
    return bool(ca and cb) and len(ca & cb) / len(ca | cb) >= 0.75


def _known_asks(data: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    """Every stored question, answered or not, with its options."""
    asks = [(e.get("ask", ""), e.get("options") or [])
            for e in data.get("unanswered") or [] if isinstance(e, dict)]
    for entry in (data.get("answers") or {}).values():
        if isinstance(entry, dict):
            asks.extend((a, []) for a in entry.get("asks") or [])
    return [(a, o) for a, o in asks if a]


def _entry_yaml(entry: dict[str, Any]) -> str:
    body = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=1000)
    return "".join("  " + line if line.strip() else line for line in body.splitlines(True))


def record_unanswered(questions: list[dict[str, Any]], seen_at: str,
                      path: Path = ANSWERS_PATH) -> int:
    """Append questions not yet in the bank. Returns how many were added."""
    with _LOCK:
        data = load_answers_file(path)
        known = _known_asks(data)
        new: list[dict[str, Any]] = []
        for q in questions:
            text = (q.get("question") or "").strip()
            if not text or len(text) > _MAX_QUESTION or is_generic(text):
                continue
            # Already in the bank in some wording: one answer there covers it.
            if any(question_score(text, k, q.get("options"), o) >= LIKELY for k, o in known):
                continue
            entry: dict[str, Any] = {"ask": text, "type": q.get("type") or "text"}
            options = [o for o in q.get("options") or [] if str(o).strip()][:40]
            if options:
                entry["options"] = options
            entry["seen_at"] = seen_at
            entry["answer"] = None
            new.append(entry)
            known.append((text, entry.get("options") or []))
        if not new:
            return 0
        original = path.read_text(encoding="utf-8") if path.exists() else _HEADER
        text = original if original.endswith("\n") else original + "\n"
        if not re.search(r"^unanswered:", text, re.M):
            text += "\nunanswered:\n"
        else:
            text = re.sub(r"^unanswered:[ \t]*\[\][ \t]*$", "unanswered:", text, count=1, flags=re.M)
        text += "".join(_entry_yaml(e) for e in new)
        # Never leave the operator a file that no longer parses.
        try:
            check = yaml.safe_load(text) or {}
            assert len(check.get("unanswered") or []) == len(data.get("unanswered") or []) + len(new)
        except Exception as exc:  # noqa: BLE001
            log.warning("autofill_bank_append_refused", error=str(exc), path=str(path))
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        log.info("autofill_unanswered_added", count=len(new), seen_at=seen_at)
        return len(new)


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_seen(fields: list[dict[str, Any]], seen_at: str, path: Path = SEEN_PATH) -> None:
    """Questions and options only, for the public catalog. Never a value."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for f in fields:
        text = (f.get("question") or "").strip()
        if not text or len(text) > _MAX_QUESTION or is_generic(text):
            continue
        row = {"seen": now, "seen_at": seen_at, "text": text, "type": f.get("type") or "text"}
        if f.get("options"):
            row["options"] = [str(o) for o in f["options"]][:40]
        rows.append(row)
    _append_jsonl(path, rows)


def record_open(job_id: str | None, url: str, seen_at: str, path: Path = OPENS_PATH) -> None:
    """The operator opened an application form for this match (the "click")."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _append_jsonl(path, [{"at": now, "job_id": job_id, "seen_at": seen_at, "url": url[:500]}])
