"""Plain-text replies: answer a callee question or approve a call without typing a command.

Runs from the ``pre_llm_call`` hook. The hook only ever sees a message the human typed in that
chat (the model cannot produce it), so a reply of "call" or "yes" is the same explicit human
decision as ``/phone approve``. Everything is bound to the session that started the call.
A handled message returns the text to show; the hook then blocks the LLM turn (no model call).
Anything not recognised returns None and passes through to the model unchanged.
"""
from __future__ import annotations

import re
from typing import Any

from . import commands, relay, states

MAX_PLAIN_LEN = 300  # longer messages are conversation, not an answer
APPROVE_WORDS = frozenset({"call", "yes", "go", "approve", "да", "звони", "позвони"})
CANCEL_WORDS = frozenset({"cancel", "no", "нет", "отмена", "отменить"})
YES_WORDS = frozenset({"yes", "ok", "okay", "yep", "sure", "да", "ок", "ага"})
DECLINE_WORDS = frozenset({"no", "nope", "decline", "don't share", "dont share", "do not share",
                           "не говори", "нет", "не надо"})
_TRIM = re.compile(r"^[\s\W_]+|[\s\W_]+$")


def normalize(text: str) -> str:
    return _TRIM.sub("", (text or "").lower().replace("’", "'")).strip()


def try_handle(svc: Any, message: Any, session_id: str, sender: str = "",
               platform: str = "") -> str | None:
    """Text to show the user when this message was a reply to a pending card, else None."""
    if not isinstance(message, str) or not session_id:
        return None
    text = message.strip()
    if not text or text.startswith("/") or len(text) > MAX_PLAIN_LEN:
        return None
    store = svc.store
    questions = store.open_questions_for_session(session_id)
    if questions:
        return _answer(svc, questions, text, sender or platform)
    return _approval(svc, session_id, normalize(text), sender, platform)


# ---- questions --------------------------------------------------------------------------------
def _answer(svc: Any, questions: list[dict], text: str, user: str) -> str:
    if len(questions) > 1:
        return ("More than one question is waiting. Answer one by id: /phone answer <id> <text>\n"
                + relay.list_open(svc))
    q = questions[0]
    call = svc.store.get_call(q["call_id"])
    if call is None:
        return ""
    word = normalize(text)
    who = call["callee_name"]
    if word in DECLINE_WORDS:
        relay.answer_question(svc, q["id"], "decline", user)
        return f"Okay, nothing was shared with {who}."
    if q["kind"] == "consent":
        fld = q.get("consent_field") or "that"
        return (f"{who} is asking for your {fld}. That needs your explicit consent for this call "
                f"only: /phone consent {call['id']} {fld} (your stored value), or "
                f"/phone answer {q['id']} <value>. Reply \"decline\" to not share.")
    if word in YES_WORDS:
        suggestion = str(q.get("card_suggestion") or "").strip()
        if not suggestion:
            return relay.NEED_ANSWER
        text = suggestion
    out = relay.answer_question(svc, q["id"], text, user)
    if out.startswith("Sent."):
        return f'Sent to {who}: "{text.strip()[:relay.MAX_ANSWER]}"'
    return out


# ---- approvals --------------------------------------------------------------------------------
def _approval(svc: Any, session_id: str, word: str, sender: str, platform: str) -> str | None:
    if word not in APPROVE_WORDS and word not in CANCEL_WORDS:
        return None
    store = svc.store
    waiting = []
    for row in store.awaiting_for_session(session_id):
        call = states.expire_if_stale(store, row["id"])
        if call is not None and call["state"] == states.AWAITING_APPROVAL:
            waiting.append(call)
    if not waiting:
        return None
    if word in CANCEL_WORDS and len(waiting) > 1:
        for older in waiting[:-1]:
            commands.cancel_by_reply(older["id"])
    call = waiting[-1]  # the newest card is the one the user is looking at
    kwargs = {"user_id": sender or platform or "local", "session_id": session_id,
              "platform": platform}
    if word in APPROVE_WORDS:
        return commands.approve_by_reply(call["id"], kwargs)
    return commands.cancel_by_reply(call["id"])
