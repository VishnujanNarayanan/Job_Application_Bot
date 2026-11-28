"""Questions the Easy Apply bot can't answer, asked on a Telegram bot of their own.

A job whose form asks something with no saved answer is not sent with a guess:
it waits, unapplied, and each question goes to the operator through a separate
"questions" bot (``TELEGRAM_QUESTIONS_BOT_TOKEN``; same private chat id,
``TELEGRAM_CHAT_ID``). The options come as buttons; anything else is answered by
replying to the message. An answer is saved with the answers learned from
submitted forms (``data/ats_learned.yaml``), so every later form -- LinkedIn or
a company site -- asking the same thing gets it, and the question is never sent
twice. Once all of a waiting job's questions are answered, the next run applies.

State lives in ``data/autoapply/questions.json`` (gitignored).
Plain Bot API calls over HTTPS: short polls (``getUpdates``, timeout 0) from the
endpoint, so nothing needs a public address.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "autoapply" / "questions.json"
_LOCK = threading.Lock()
API = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str | None:
    # The questions bot; without one the main bot carries them, so none is lost.
    return os.environ.get("TELEGRAM_QUESTIONS_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")


def _chat() -> str | None:
    return os.environ.get("TELEGRAM_CHAT_ID")


def load() -> dict[str, Any]:
    try:
        return json.loads(STORE.read_text())
    except (FileNotFoundError, ValueError):
        return {"questions": [], "offset": 0, "next": 1}


def save(state: dict[str, Any]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".part")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    os.replace(tmp, STORE)


def _norm(q: str) -> str:
    from src.autofill.matcher import norm

    return norm(q)


def _call(method: str, **payload: Any) -> dict[str, Any] | None:
    import requests

    token = _token()
    if not token:
        log.warning("questions_bot_missing", why="no TELEGRAM_QUESTIONS_BOT_TOKEN or TELEGRAM_BOT_TOKEN")
        return None
    try:
        r = requests.post(API.format(token=token, method=method), json=payload, timeout=15)
        body = r.json()
        if not body.get("ok"):
            log.warning("questions_bot_call_failed", method=method, error=str(body.get("description"))[:200])
            return None
        return body.get("result")
    except Exception as exc:  # noqa: BLE001 - never break a run over a message
        log.warning("questions_bot_call_failed", method=method, error=str(exc)[:200])
        return None


def _message(q: dict[str, Any], job: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    text = (f"❓ {q['question']}\n\n"
            f"Asked by {job.get('company', '?')} — {job.get('role', '?')}. That job waits, unapplied, "
            f"until you answer; the answer is kept for every later form.")
    if q.get("guess"):
        text += f"\n(Its guess would have been: {q['guess']})"
    opts = [o for o in q.get("options") or [] if o and not o.lower().startswith("select")][:12]
    if opts:
        rows = [[{"text": o[:60], "callback_data": f"qa:{q['id']}:{i}"}] for i, o in enumerate(opts)]
        q["options"] = opts
        return text, {"inline_keyboard": rows}
    return text + "\n\n↩ Reply to this message with your answer.", {"force_reply": True, "input_field_placeholder": "Your answer"}


def ask(asks: list[dict[str, Any]], job: dict[str, Any]) -> list[str]:
    """Record a job's unanswerable questions and send the new ones. Returns their ids."""
    ids = []
    with _LOCK:
        state = load()
        for a in asks:
            question = (a.get("question") or "").strip()
            if not question:
                continue
            key = _norm(question)
            q = next((x for x in state["questions"] if x["key"] == key and x.get("answer") is None), None)
            if q is None:
                q = {"id": f"q{state['next']}", "key": key, "question": question,
                     "options": [str(o) for o in a.get("options") or []], "type": a.get("type") or "",
                     "guess": a.get("answer"), "jobs": [], "asked_at": datetime.now().isoformat(timespec="seconds"),
                     "answer": None, "message_id": None}
                state["next"] += 1
                state["questions"].append(q)
                text, markup = _message(q, job)
                sent = _call("sendMessage", chat_id=_chat(), text=text, reply_markup=markup)
                q["message_id"] = sent.get("message_id") if sent else None
            if job.get("job_id") and job["job_id"] not in q["jobs"]:
                q["jobs"].append(job["job_id"])
            ids.append(q["id"])
        save(state)
    log.info("questions_asked", job_id=job.get("job_id"), questions=ids)
    return ids


def unanswered(ids: list[str]) -> list[str]:
    state = load()
    have = {q["id"]: q for q in state["questions"]}
    return [i for i in ids if i in have and have[i].get("answer") is None]


def _keep(q: dict[str, Any], answer: str) -> None:
    """Save the answer with the learned ones: every later form asking this gets it."""
    from src.autofill.learn import record_submission

    q["answer"] = answer
    q["answered_at"] = datetime.now().isoformat(timespec="seconds")
    record_submission([{"question": q["question"], "value": answer, "options": q.get("options") or [],
                        "type": q.get("type") or ("radio" if q.get("options") else "text"),
                        "source": "", "filled": None}], "https://www.linkedin.com/jobs/")
    log.info("questions_answered", id=q["id"], question=q["question"][:120], answer=answer[:120])


def poll() -> int:
    """Pick up the operator's answers (button taps and replies). Returns how many were saved."""
    chat = str(_chat() or "")
    if not _token() or not chat:
        return 0
    saved = 0
    with _LOCK:
        state = load()
        updates = _call("getUpdates", offset=int(state.get("offset") or 0), timeout=0,
                        allowed_updates=["callback_query", "message"]) or []
        by_id = {q["id"]: q for q in state["questions"]}
        by_msg = {q.get("message_id"): q for q in state["questions"] if q.get("message_id")}
        for u in updates:
            state["offset"] = int(u["update_id"]) + 1
            cq = u.get("callback_query")
            if cq:
                if str(cq.get("message", {}).get("chat", {}).get("id")) != chat:
                    continue
                parts = (cq.get("data") or "").split(":")
                q = by_id.get(parts[1]) if len(parts) == 3 and parts[0] == "qa" else None
                if q is None or not parts[2].isdigit() or int(parts[2]) >= len(q.get("options") or []):
                    _call("answerCallbackQuery", callback_query_id=cq["id"], text="Not a question I know")
                    continue
                answer = q["options"][int(parts[2])]
                _keep(q, answer)
                saved += 1
                _call("answerCallbackQuery", callback_query_id=cq["id"], text=f"Saved: {answer[:40]}")
                _call("editMessageText", chat_id=chat, message_id=cq["message"]["message_id"],
                      text=f"✅ {q['question']}\n→ {answer}\n\nSaved; the waiting job is applied on the next run.")
                continue
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != chat or not msg.get("text"):
                continue
            reply = (msg.get("reply_to_message") or {}).get("message_id")
            q = by_msg.get(reply)
            if q is None:
                if msg["text"].strip().startswith("/start"):
                    _call("sendMessage", chat_id=chat, text="Ready. I'll ask here when a form has a question I can't answer.")
                continue
            _keep(q, msg["text"].strip())
            saved += 1
            _call("sendMessage", chat_id=chat, reply_to_message_id=msg["message_id"],
                  text="✅ Saved; the waiting job is applied on the next run.")
        save(state)
    return saved
