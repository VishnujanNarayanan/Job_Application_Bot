"""Learn from what the operator submits (#154).

When a form is submitted (or a multi-page form moves to its next page), the
userscript sends back each field it saw: the question, what it filled (if
anything) and what the field holds now. From that:

* a field it couldn't answer, now filled in        -> a new learned answer;
* a field it filled that the operator changed      -> the correction replaces
                                                      the answer it came from;
* a learned answer submitted unchanged             -> one more confirmation.

A learned answer is filled with an amber outline until it has been submitted
unchanged ``TRUSTED_AFTER`` times; after that it fills like any other.

Not everything is worth keeping. Never learned:

* facts from master_profile / config, and answers worked out per form (salary,
  dates, age, visas: their source is ``fact:...``): the profile stays the
  source of truth, and a per-job figure is not a standing answer;
* salary questions, and open prompts ("why us", "tell us about", cover
  letters, anything longer than a short answer): they belong to one job;
* files, lone checkboxes (consent), empty fields.

Questions about one company ("have you worked here before", "who referred
you", "why <company>") are kept for that company only.

The store is ``data/ats_learned.yaml`` (gitignored), rewritten whole by code;
the operator may edit or delete entries in it.
"""

from __future__ import annotations

import re
import threading
from datetime import date
from pathlib import Path
from typing import Any

import structlog
import yaml

from src.autofill.facts import ANSWERS_PATH, LEARNED_PATH, load_answers_file, load_learned
from src.autofill.matcher import LIKELY, SAME, _content, ats_of, is_generic, norm, question_score

log = structlog.get_logger(__name__)

_LOCK = threading.Lock()
_MAX_TEXT = 200
_MAX_WORDS = 25

_HEADER = """\
# Answers learned from forms you submitted (#154). Written by the endpoint;
# gitignored. Edit an `answer:` or delete an entry freely.
#
#   ask        the question as the form put it (other wordings match too)
#   answer     what you submitted
#   replaces   the data/ats_answers.yaml answer this one corrects, if any
#   company    set when the question is about one company: used only there
#   confirms   times submitted unchanged; from 2 on it fills without an outline
#   history    earlier answers, newest last
"""

# Per job, never a standing answer.
_PER_JOB = re.compile(
    r"\b(ctc|salary|salaries|compensation|stipend|package|lpa|remuneration|pay|wage|wages)\b"
    r"|\bwhy\b|\btell us\b|\bdescribe\b|\bcover letter\b|\bmotivat\w*|\banything else\b"
    r"|\badditional information\b|\bcomments?\b|\babout yourself\b|\bsummary\b|\bexplain\b")
_COMPANY = re.compile(
    r"\b(previously|ever|before|formerly|past)\b.*\b(work\w*|employ\w*|appl\w*|intern\w*|interview\w*)\b"
    r"|\breferr\w*|\brefer(red|ral)?\b|\b(this|our) (company|role|position|team|organisation|organization|job)\b"
    r"|\bjoin us\b|\bwith us\b|\bfor us\b")
_SKIP_TYPES = {"file", "checkbox", "password", "hidden"}
_TRUST_SOURCES = ("answers:", "unanswered:", "learned:")


def _empty(value: Any) -> bool:
    if isinstance(value, list):
        return not any(str(v).strip() for v in value)
    return value is None or not str(value).strip()


def _same_value(a: Any, b: Any) -> bool:
    def flat(v: Any) -> tuple[str, ...]:
        items = v if isinstance(v, list) else [v]
        return tuple(sorted(norm(str(x)) for x in items if str(x).strip()))
    return flat(a) == flat(b)


def _company_words(job: dict[str, Any], site: str) -> set[str]:
    words = set(_content(str(job.get("company") or "")))
    slug = site.split("/", 1)[1] if "/" in site else ""
    if slug:
        words.add(norm(slug).replace(" ", ""))
    return {w for w in words if len(w) >= 4}


def worth_keeping(field: dict[str, Any]) -> bool:
    """Whether a submitted field holds a standing answer worth saving."""
    q = (field.get("question") or "").strip()
    value = field.get("value")
    if not q or is_generic(q) or _empty(value):
        return False
    if (field.get("type") or "") in _SKIP_TYPES:
        return False
    if str(field.get("source") or "").startswith("fact:"):
        return False
    if _PER_JOB.search(norm(q)):
        return False
    if isinstance(value, str):
        if len(value) > _MAX_TEXT or ((field.get("type") == "textarea") and len(value.split()) > _MAX_WORDS):
            return False
    return True


def company_scope(question: str, job: dict[str, Any], site: str) -> str | None:
    n = norm(question)
    if _COMPANY.search(n) or any(re.search(rf"\b{re.escape(w)}\b", n) for w in _company_words(job, site)):
        return site or None
    return None


def _null_entry_source(question: str, options: Any, answers_path: Path) -> str | None:
    """The answers-file entry still waiting for this question's answer, if any."""
    data = load_answers_file(answers_path)
    best: tuple[str | None, float] = (None, 0.0)
    for key, entry in (data.get("answers") or {}).items():
        if isinstance(entry, dict) and entry.get("answer") is None:
            for ask in entry.get("asks") or []:
                s = question_score(question, ask, options)
                if s >= LIKELY and s > best[1]:
                    best = (f"answers:{key}", s)
    for entry in data.get("unanswered") or []:
        if isinstance(entry, dict) and entry.get("answer") is None and entry.get("ask"):
            s = question_score(question, entry["ask"], options, entry.get("options"))
            if s >= LIKELY and s > best[1]:
                best = (f"unanswered:{entry['ask']}", s)
    return best[0]


def _save(rows: list[dict[str, Any]], path: Path) -> None:
    body = yaml.safe_dump({"learned": rows}, sort_keys=False, allow_unicode=True, width=110)
    text = _HEADER + "\n" + body
    assert len((yaml.safe_load(text) or {}).get("learned") or []) == len(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _next_id(rows: list[dict[str, Any]]) -> str:
    nums = [int(m.group(1)) for r in rows if (m := re.fullmatch(r"l(\d+)", str(r.get("id"))))]
    return f"l{max(nums, default=0) + 1}"


def _correct(row: dict[str, Any], value: Any, today: str) -> None:
    if row.get("answer") is not None and not _same_value(row["answer"], value):
        row.setdefault("history", []).append({"answer": row["answer"], "until": today})
    row["answer"] = value
    row["confirms"] = 0
    row["updated"] = today


def record_submission(fields: list[dict[str, Any]], url: str, job: dict[str, Any] | None = None,
                      path: Path = LEARNED_PATH, answers_path: Path = ANSWERS_PATH,
                      today: date | None = None) -> dict[str, int]:
    """Learn from one submitted page. Returns counts: added, corrected, confirmed."""
    job = job or {}
    site = ats_of(url)
    day = (today or date.today()).isoformat()
    counts = {"added": 0, "corrected": 0, "confirmed": 0}
    with _LOCK:
        rows = load_learned(path)
        by_id = {r["id"]: r for r in rows}
        changed_file = False
        for f in fields:
            if not worth_keeping(f):
                continue
            question = f["question"].strip()
            value = f["value"]
            options = [str(o) for o in f.get("options") or []][:40]
            source = str(f.get("source") or "")
            filled = f.get("filled")
            changed = filled is None or not _same_value(value, filled)

            if source.startswith("learned:"):
                row = by_id.get(source.split(":", 1)[1])
                if row is None:
                    continue
                if changed:
                    _correct(row, value, day)
                    counts["corrected"] += 1
                else:
                    row["confirms"] = int(row.get("confirms") or 0) + 1
                    row["last_used"] = day
                    counts["confirmed"] += 1
                changed_file = True
                continue
            if not changed:
                continue  # an answer from the operator's own file, kept as it was

            scope = company_scope(question, job, site)
            replaces = source if source.startswith(_TRUST_SOURCES) else None
            if replaces is None and scope is None:
                replaces = _null_entry_source(question, options, answers_path)
            if replaces:
                row = next((r for r in rows if r.get("replaces") == replaces and r.get("company") == scope), None)
            else:
                row = max(((question_score(question, r.get("ask", ""), options, r.get("options")), r)
                           for r in rows if not r.get("replaces") and r.get("company") == scope),
                          default=(0.0, None), key=lambda t: t[0])
                row = row[1] if row[0] >= SAME else None
            if row is not None:
                _correct(row, value, day)
                counts["corrected"] += 1
            else:
                row = {"id": _next_id(rows), "ask": question, "type": f.get("type") or "text"}
                if options:
                    row["options"] = options
                row.update(answer=value, replaces=replaces, company=scope, confirms=0,
                           added=day, updated=day, seen_at=site)
                rows.append(row)
                by_id[row["id"]] = row
                counts["added"] += 1
            changed_file = True
        if changed_file:
            try:
                _save(rows, path)
            except Exception as exc:  # noqa: BLE001 - never lose the file over one bad write
                log.warning("autofill_learn_write_failed", error=str(exc)[:200], path=str(path))
                return {"added": 0, "corrected": 0, "confirmed": 0}
    if any(counts.values()):
        log.info("autofill_learned", site=site, **counts)
    return counts
