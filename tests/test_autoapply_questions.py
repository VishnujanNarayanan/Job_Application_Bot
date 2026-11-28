"""The questions bot: unknown questions asked once, answers saved for every later form."""

import pytest

from src.autoapply import questions


@pytest.fixture
def bot(monkeypatch, tmp_path):
    sent, learned, updates = [], [], []
    monkeypatch.setattr(questions, "STORE", tmp_path / "questions.json")
    monkeypatch.setenv("TELEGRAM_QUESTIONS_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def call(method, **payload):
        sent.append((method, payload))
        if method == "sendMessage":
            return {"message_id": 100 + len(sent)}
        if method == "getUpdates":
            out = list(updates)
            updates.clear()
            return out
        return {}

    monkeypatch.setattr(questions, "_call", call)
    import src.autofill.learn as learn
    monkeypatch.setattr(learn, "record_submission", lambda fields, url, **kw: learned.extend(fields))
    return sent, learned, updates


JOB = {"job_id": "linkedin-li-1", "company": "Acme", "role": "Data Engineer"}


def test_asked_once_answered_by_button_and_kept(bot) -> None:
    sent, learned, updates = bot
    ask = {"question": "Did you graduate in 2021/2022/2023/2024?", "answer": "Yes", "options": ["Select an option", "Yes", "No"]}
    ids = questions.ask([ask], JOB)
    again = questions.ask([ask], {**JOB, "job_id": "linkedin-li-2"})   # the same question, another job
    assert ids == again and [m for m, _ in sent].count("sendMessage") == 1
    buttons = sent[0][1]["reply_markup"]["inline_keyboard"]
    assert [b[0]["text"] for b in buttons] == ["Yes", "No"]          # no placeholder button
    assert questions.unanswered(ids) == ids

    updates.append({"update_id": 7, "callback_query": {"id": "c", "data": buttons[1][0]["callback_data"],
                                                       "message": {"message_id": 101, "chat": {"id": 42}}}})
    assert questions.poll() == 1
    assert questions.unanswered(ids) == []
    assert learned[0]["question"].startswith("Did you graduate") and learned[0]["value"] == "No"
    assert questions.load()["questions"][0]["jobs"] == ["linkedin-li-1", "linkedin-li-2"]


def test_typed_answers_come_as_replies_and_other_chats_are_ignored(bot) -> None:
    sent, learned, updates = bot
    ids = questions.ask([{"question": "Why do you want to join us?", "answer": "Please refer to my resume"}], JOB)
    msg_id = questions.load()["questions"][0]["message_id"]
    assert sent[0][1]["reply_markup"]["force_reply"] is True
    updates += [{"update_id": 1, "message": {"message_id": 5, "chat": {"id": 999}, "text": "spam",
                                             "reply_to_message": {"message_id": msg_id}}},
                {"update_id": 2, "message": {"message_id": 6, "chat": {"id": 42}, "text": "  The data platform work  ",
                                             "reply_to_message": {"message_id": msg_id}}}]
    assert questions.poll() == 1
    assert learned[0]["value"] == "The data platform work" and questions.unanswered(ids) == []
    assert questions.load()["offset"] == 3
