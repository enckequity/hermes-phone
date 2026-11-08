"""Question relay, plugin side (SPEC 1.4/1.5): cards, suggestions, answers, kill switch, watcher.

The bridge process writes ``questions`` rows; the watcher thread started by ``phone_call`` turns
them into chat cards, and ``/phone answer`` writes the answer back for the bridge to poll. The
only secrets-adjacent rule enforced here: a sensitive value never becomes a suggestion and never
reaches the voice model unless the user granted consent for that field on that call.

Everything the callee said is shown as untrusted text and is never treated as an instruction.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from . import consent, results, states, webphone
from .store import Store

log = logging.getLogger(__name__)

POLL_S = 0.5
MAX_ANSWER = 500
POST_ATTEMPTS = 3
CARD_ATTEMPTS = 120  # question cards: retry about every second for the whole window
CARD_RETRY_S = 1.0
HANGUP_WAIT_S = 2.0
DECLINE_WORDS = {"decline", "dont-share", "don't-share"}
NAME_FIELDS = re.compile(r"(^|_)name$|^name$")
NOT_USER_NAME = re.compile(r"callee|business|restaurant|company|shop|store|doctor|agent", re.I)

STATUS_LABELS = {
    states.DIALING: "dialing",
    states.RINGING: "ringing",
    states.IN_PROGRESS: "connected",
    states.ON_HOLD_FOR_USER: "on hold, waiting for your answer",
    states.VOICEMAIL: "voicemail reached",
}


# ---- suggestions ------------------------------------------------------------------------------
def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")


def _match_fact(facts: dict[str, Any], field: str) -> str:
    f = _norm(field)
    if not f:
        return ""
    for key, value in facts.items():
        k = _norm(key)
        if k == f or (len(f) >= 4 and len(k) >= 4 and (f in k or k in f)):
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return ""


def memory_lookup(ctx: Any, query: str) -> str:
    """Best-effort read of Hermes memory through ``ctx.dispatch_tool("memory", ...)``.

    Hermes' built-in ``memory`` tool only has add/patch/remove (verified against the source);
    memory is shown to the agent through its own prompt, not through a read action. So this asks
    for a ``search`` and treats any error as "no suggestion". It never raises.
    """
    dispatch = getattr(ctx, "dispatch_tool", None)
    if dispatch is None or not query:
        return ""
    try:
        raw = dispatch("memory", {"action": "search", "target": "user", "query": query})
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001 - unavailable or erroring: skip silently
        return ""
    if not isinstance(data, dict) or data.get("success") is False or data.get("error"):
        return ""
    entries = data.get("entries") or data.get("results") or data.get("matches") or []
    for entry in entries if isinstance(entries, list) else []:  # already ranked by the search
        text = entry if isinstance(entry, str) else str((entry or {}).get("content", ""))
        if text.strip():
            return text.strip()[:MAX_ANSWER]
    return ""


MEMORY_BUDGET_S = 1.5


def _bounded_memory(ctx: Any, query: str) -> str:
    """``memory_lookup`` with a hard time budget: a slow or blocked memory tool must never delay
    a question card (the user is being waited for on a live call)."""
    box: list[str] = []
    bound = contextvars.copy_context()
    def work() -> None:
        box.append(memory_lookup(ctx, query))

    thread = threading.Thread(target=bound.run, args=(work,), daemon=True,
                              name="hermes-phone-memory")
    thread.start()
    thread.join(MEMORY_BUDGET_S)
    return box[0] if box else ""


def suggest(svc: Any, call: dict, q: dict) -> tuple[str, str]:
    """``(suggestion, source)`` for a question card, or ``("", "")``. Never sensitive."""
    if q.get("kind") == "consent":
        return "", ""
    granted = svc.store.consents(call["id"])
    field = q.get("field") or ""
    if consent.classify_question(field, q.get("question") or "") not in (None, *granted):
        return "", ""
    facts, _ = consent.build_safe_facts(call["brief"].get("facts_raw", {}), granted)
    found = _match_fact(facts, field)
    if found:
        return found, "from the call brief"
    user_name = str(svc.config("user_name", "") or "").strip()
    if user_name and NAME_FIELDS.search(_norm(field)) and not NOT_USER_NAME.search(field):
        return user_name, "from your profile"
    remembered = _bounded_memory(svc.ctx, f"{field} {q.get('question', '')}".strip())
    if remembered and not consent.text_fields(remembered, granted):
        return remembered, "from memory"
    guess = str(q.get("suggested") or "").strip()
    if guess and not consent.text_fields(guess, granted):
        return guess, "the voice assistant's guess"
    return "", ""


def stored_value(svc: Any, call: dict, field: str) -> str:
    """The value the user already put in the brief (or settings) for a sensitive field."""
    for key, value in (call["brief"].get("facts_raw") or {}).items():
        if consent.field_for_fact(key, value) == field and isinstance(value, (str, int, float)):
            return str(value)
    if field == "phone":
        return str(svc.config("callback_number", "") or "").strip()
    return ""


# ---- card text --------------------------------------------------------------------------------
def _timeout_s(call: dict) -> int:
    return int(call["brief"].get("limits", {}).get("question_timeout_s", 75))


def question_card(call: dict, q: dict, suggestion: str = "", source: str = "") -> str:
    """Short and human. What the callee said stays quoted so it reads as their words."""
    qid, cid = q["id"], call["id"]
    who = call["callee_name"]
    if q.get("kind") == "consent":
        fld = q.get("consent_field") or "this"
        return "\n".join([
            f'\u2753 {who} asks for your {fld}: "{q["question"]}"',
            "Nothing sensitive is shared unless you allow it for this call only.",
            f"Allow your stored value: /phone consent {cid} {fld}  |  or send a value: "
            f'/phone answer {qid} <value>  |  reply "decline" to not share.'])
    lines = [f'\u2753 {who} asks: "{q["question"]}"']
    if suggestion:
        lines.append(f'Suggested: {suggestion}. Reply "yes" to send it, type another answer, '
                     'or "decline".')
    else:
        lines.append('Reply with the answer, or "decline".')
    return "\n".join(lines)


def resolved_card(call: dict, q: dict) -> str:
    ask = f'Question: "{q["question"]}"'
    status = q["status"]
    if status == "answered":
        shown = "(value not repeated here)" if q.get("kind") == "consent" else \
            f'"{q.get("answer") or ""}"'
        return f"Answered {shown} and passed to the caller. {ask}"
    if status == "declined":
        return f"Declined, nothing was shared. {ask}"
    if status == "late":
        return f"Answered too late; the caller was told we will call back. Saved. {ask}"
    waited = (q.get("answered_at") or 0) - (q.get("created_at") or 0)
    if call.get("state") in states.TERMINAL and 0 <= waited < _timeout_s(call) - 1:
        return f"The call ended before you answered, so nothing was sent. {ask}"
    return (f"No answer in time; the caller was told we will call back. We will "
            f"call back later. {ask}")


AFFIRMATIVES = frozenset({"yes", "ok", "okay", "yep", "yeah", "sure", "да", "ок", "ага", "угу"})
NEED_ANSWER = 'What should I tell them? e.g. "1 person"'


def is_bare_affirmative(text: str) -> bool:
    return re.sub(r"^[\s\W_]+|[\s\W_]+$", "", (text or "").lower()).strip() in AFFIRMATIVES


# ---- answering --------------------------------------------------------------------------------
def answer_question(svc: Any, qid: str, text: str, user: str = "") -> str:
    store: Store = svc.store
    q = store.get_question(qid)
    if q is None:
        return f"Unknown question id {qid}."
    if q["status"] not in ("open", "timeout"):
        return f"Question {qid} is already {q['status']}."
    call = store.get_call(q["call_id"])
    if call is None:
        return f"Unknown call for question {qid}."
    text = (text or "").strip()[:MAX_ANSWER]
    if not text:
        return f"Usage: /phone answer {qid} <text>"
    if is_bare_affirmative(text):
        suggestion = str(q.get("card_suggestion") or "").strip()
        if suggestion and q["kind"] != "consent":
            text = suggestion
        else:
            return NEED_ANSWER
    if text.lower() in DECLINE_WORDS:
        if q["status"] != "open":
            return "The call already moved on; there is nothing to decline any more."
        return "Declined. Nothing is shared." if store.resolve_question(qid, "declined") else \
            f"Question {qid} was already closed."
    granted = store.consents(call["id"])
    is_open = q["status"] == "open"
    if q["kind"] == "consent":
        fld = q.get("consent_field") or ""
        if is_open and fld:
            store.grant_consent(call["id"], fld, user)  # explicit, per call, per field
            granted = granted | {fld}
    blocked = consent.text_fields(text, granted)
    if blocked:
        return ("That looks like " + ", ".join(sorted(blocked)) + " information, which needs "
                f"your consent for this call first: /phone consent {call['id']} "
                f"{sorted(blocked)[0]} (only while the callee is asking for it).")
    final = store.resolve_question(qid, "answered", text)
    if final == "answered":
        store.add_event(call["id"], "answer_recorded", {"question_id": qid})
        return "Sent. The caller gets your answer now."
    if final == "late":
        store.add_event(call["id"], "answer_late", {"question_id": qid})
        return ("That question had already timed out. Your answer is saved for the callback; "
                "it was not passed to the caller.")
    return f"Question {qid} was already closed."


def grant_for_question(svc: Any, call: dict, field: str, user: str) -> str | None:
    """``/phone consent`` during a live call: only for an open consent question on that field.

    Returns the user message, or None when no such question exists.
    """
    store: Store = svc.store
    q = next((x for x in store.questions_for_call(call["id"])
              if x["status"] == "open" and x["kind"] == "consent"
              and x["consent_field"] == field), None)
    if q is None:
        return None
    store.grant_consent(call["id"], field, user)
    value = stored_value(svc, call, field)
    if value and store.resolve_question(q["id"], "answered", value):
        store.add_event(call["id"], "consent_answer", {"question_id": q["id"], "field": field})
        return f"Consent for {field} recorded for this call. Your stored value was passed on."
    return (f"Consent for {field} recorded for this call, but I have no stored value. "
            f"Send /phone answer {q['id']} <value>.")


def choose_answer(svc: Any, qid: str, user: str = "") -> str:
    """``/phone answer <qid>`` with no text: buttons when the surface has them."""
    store: Store = svc.store
    q = store.get_question(qid)
    if q is None:
        return f"Unknown question id {qid}."
    if q["status"] != "open":
        return f"Question {qid} is already {q['status']}."
    call = store.get_call(q["call_id"])
    if call is None:
        return f"Unknown call for question {qid}."
    howto = f"Send /phone answer {qid} <text>, or /phone answer {qid} decline."
    asker = getattr(svc.ctx, "ask_choice", None)
    if asker is None:
        return howto
    guess, _ = suggest(svc, call, q)
    stored = stored_value(svc, call, q.get("consent_field") or "") if q["kind"] == "consent" else ""
    first = guess or ""
    labels = [x for x in (first, "Other", "Don't share") if x]
    prompt = f"They ask: {q['question']!r}. How should I answer?"
    if q["kind"] == "consent":
        labels = ["Allow my stored value", "Other", "Don't share"] if stored else \
            ["Other", "Don't share"]
    try:
        choice = asker(prompt, labels)
    except Exception:  # noqa: BLE001
        choice = None
    if choice is None:
        return howto
    choice = str(choice)
    if choice == "Don't share":
        return answer_question(svc, qid, "decline", user)
    if choice == "Other":
        return f"Send your answer with /phone answer {qid} <text>."
    if choice == "Allow my stored value":
        return answer_question(svc, qid, stored, user)
    return answer_question(svc, qid, choice, user)


def list_open(svc: Any) -> str:
    qs = svc.store.open_questions()
    if not qs:
        return "No open questions."
    return "\n".join(f"{q['id']} (call {q['call_id']}): {q['question']!r} (their words)"
                     for q in qs)


# ---- kill switch ------------------------------------------------------------------------------
def hangup(svc: Any, call_id: str = "", *, wait_s: float | None = None,
           sleep: Callable[[float], None] = time.sleep) -> str:
    """End a live call now: mark it in the DB (the bridge polls), then fall back to Twilio REST."""
    store: Store = svc.store
    if not call_id:
        live = store.calls_in_states(states.LIVE)
        if not live:
            return "No active call to hang up."
        if len(live) > 1:
            return "More than one call is active; use /phone hangup <call_id>."
        call_id = live[0]["id"]
    call = store.get_call(call_id)
    if call is None:
        return f"Unknown call id {call_id}."
    if call["state"] not in states.LIVE:
        return f"Call {call_id} is {call['state']}; nothing to hang up."
    store.request_hangup(call_id)
    store.add_event(call_id, "hangup_requested", {})
    budget = HANGUP_WAIT_S if wait_s is None else wait_s
    end = time.monotonic() + budget
    while time.monotonic() < end:
        cur = store.get_call(call_id)
        if cur is None or cur["state"] in states.TERMINAL:
            return f"Call {call_id} ended (hung up by you)."
        sleep(0.1)
    # The bridge did not react in time: end the line ourselves.
    cur = store.get_call(call_id) or call
    ok = False
    dialer = getattr(svc, "dialer", None)
    hang = getattr(dialer, "hangup", None)
    if hang is not None:
        try:
            ok = bool(hang(cur))
        except Exception:  # noqa: BLE001
            ok = False
    store.set_result_once(call_id, {"outcome": "needs_followup", "reason": "user_hangup",
                                    "summary": "You ended the call."}, "needs_followup")
    if states.can_transition(cur["state"], states.ABORTED):
        try:
            states.transition(store, call_id, states.ABORTED, reason="user_hangup",
                              outcome="needs_followup")
        except states.IllegalTransition:
            pass
    if webphone.is_web_sid(cur.get("provider_sid")):
        return f"Call {call_id} ended (hung up by you)."
    if ok:
        return f"Call {call_id} hung up."
    return (f"Call {call_id} is marked ended, but the phone service did not confirm the line "
            "was dropped. Check your phone.")


# ---- watcher ----------------------------------------------------------------------------------
class CallWatcher:
    """Polls the DB for one call; posts cards and live status into the invoking conversation."""

    def __init__(self, svc: Any, call_id: str, *, poll_s: float = POLL_S) -> None:
        self.svc, self.call_id, self.poll_s = svc, call_id, poll_s
        self.stop = threading.Event()
        self._status_handle: Any = None
        self._status_text = ""
        self._status_fails = 0
        self._link_fails = 0
        self._result_fails = 0
        self._result_done = False
        self._cards: dict[str, Any] = {}
        self._seen: dict[str, str] = {}
        self._fails: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self.thread: threading.Thread | None = None
        self.posted_from: list[int] = []  # thread ids that called post_message (tests)

    # -- lifecycle --
    def start(self) -> threading.Thread:
        """Run in a daemon thread bound to the *current* context, so ``post_message`` reaches the
        conversation that invoked ``phone_call`` (Hermes reads the session from contextvars)."""
        bound = contextvars.copy_context()
        self.thread = threading.Thread(target=bound.run, args=(self.run,), daemon=True,
                                       name=f"hermes-phone-watch-{self.call_id}")
        self.thread.start()
        return self.thread

    def run(self) -> None:
        try:
            deadline = time.monotonic() + self._lifetime_s()
            while not self.stop.is_set():
                if self._step() or time.monotonic() > deadline:
                    break
                self.stop.wait(self.poll_s)
        except Exception:  # noqa: BLE001 - a watcher must never take anything down
            log.exception("hermes-phone watcher crashed for %s", self.call_id)
        finally:
            watchers = getattr(self.svc, "watchers", None)
            if isinstance(watchers, dict):
                watchers.pop(self.call_id, None)

    def _lifetime_s(self) -> float:
        call = self.svc.store.get_call(self.call_id)
        return float((call or {}).get("brief", {}).get("limits", {}).get("max_duration_s", 300)
                     ) + 600.0

    # -- one poll; True when finished --
    def _step(self) -> bool:
        store = self.svc.store
        sync = getattr(getattr(self.svc, "dialer", None), "sync", None)
        if sync is not None:  # remote bridge: push answers, mirror its updates into the store
            sync(store, self.call_id)
        call = store.get_call(self.call_id)
        if call is None:
            return True
        if webphone.is_web_sid(call.get("provider_sid")):
            self._web_link(call)
            if webphone.expire_stale(store, self.call_id):  # nobody answered: one no_answer
                call = store.get_call(self.call_id) or call
        terminal = call["state"] in states.TERMINAL
        for q in store.questions_for_call(self.call_id):
            self._question(call, q)
        self._status(call)
        if terminal:
            self._result(call)
        return terminal and (self._result_done or self._result_fails >= POST_ATTEMPTS)

    def _post(self, text: str) -> Any:
        poster = getattr(self.svc.ctx, "post_message", None)
        if poster is None:
            return None
        self.posted_from.append(threading.get_ident())
        try:
            handle = poster(text)
        except Exception:  # noqa: BLE001
            return None
        return handle if getattr(handle, "ok", True) is not False else None

    @staticmethod
    def _edit(handle: Any, text: str) -> None:
        try:
            handle.edit(text)
        except Exception:  # noqa: BLE001
            pass

    def _web_link(self, call: dict) -> None:
        """Web phone mode: post the plain answer link once so it is a tappable URL."""
        links = getattr(self.svc, "answer_links", {})
        url = links.get(self.call_id)
        if not url or call["state"] not in (states.DIALING, states.RINGING):
            return
        if self._link_fails >= POST_ATTEMPTS:
            return
        if self._post(webphone.link_message(url)) is None:
            self._link_fails += 1
            return
        links.pop(self.call_id, None)
        self.svc.store.add_event(self.call_id, "web_link_posted", {})

    def _status(self, call: dict) -> None:
        state = call["state"]
        if state in states.TERMINAL:
            extra = f", {call['outcome']}" if call.get("outcome") else ""
            label = f"ended ({state}{extra})"
        elif state == states.DIALING and webphone.is_web_sid(call.get("provider_sid")):
            label = "ringing in your browser"
        else:
            label = STATUS_LABELS.get(state, state)
        text = f"Call {call['id']} to {call['callee_name']}: {label}."
        if state in states.TERMINAL:
            # The status message is edited in place, so it keeps its old position in the chat.
            # Its last edit is short; the real result is a separate, newer message.
            text = "Call finished \u2014 result below."
        if text == self._status_text:
            return
        if self._status_handle is None:
            if self._status_fails >= POST_ATTEMPTS:
                return
            handle = self._post(text)
            if handle is None:
                self._status_fails += 1
                return
            self._status_handle = handle
        else:
            self._edit(self._status_handle, text)
        self._status_text = text

    def _result(self, call: dict) -> None:
        """Post the one result message for this call; never twice, even across restarts."""
        if self._result_done or self._result_fails >= POST_ATTEMPTS:
            return
        store = self.svc.store
        fresh = store.get_call(self.call_id) or call
        if fresh.get("result_posted_at") is not None or fresh.get("result_delivered"):
            self._result_done = True  # already posted (earlier run) or handed to the agent
            return
        keep = bool(self.svc.config("store_transcripts", True))
        retried = bool(store.retries_of(self.call_id))
        try:
            text = results.result_message(fresh, self.svc.config, store_transcripts=keep,
                                          retried=retried,
                                          has_transcript=bool(store.transcript(self.call_id)))
        except Exception:  # noqa: BLE001
            log.exception("could not build the result message for %s", self.call_id)
            self._result_fails = POST_ATTEMPTS
            return
        if self._post(text) is None:
            self._result_fails += 1
            return
        self._result_done = True
        store.mark_result_posted(self.call_id)
        store.add_event(self.call_id, "result_posted", {})

    def _question(self, call: dict, q: dict) -> None:
        qid = q["id"]
        handle = self._cards.get(qid)
        if handle is None:
            if qid in self._seen:
                return
            now = time.monotonic()
            if q["status"] != "open":
                # The card never made it out and the question is already closed: say so once,
                # so the user is not left with a silent timeout.
                self._seen[qid] = q["status"]
                if self._post(resolved_card(call, q)) is not None:
                    self.svc.store.add_event(self.call_id, "question_card_posted",
                                             {"question_id": qid, "late": True})
                return
            if self._fails.get(qid, 0) >= CARD_ATTEMPTS or now < self._retry_at.get(qid, 0.0):
                return
            try:
                guess, source = suggest(self.svc, call, q)
            except Exception:  # noqa: BLE001
                guess, source = "", ""
            handle = self._post(question_card(call, q, guess, source))
            if handle is None:
                # Transient (gateway busy, bound session not ready): keep trying for the whole
                # window instead of giving up after a second.
                self._fails[qid] = self._fails.get(qid, 0) + 1
                self._retry_at[qid] = time.monotonic() + CARD_RETRY_S
                return  # pre_llm_call still surfaces it to the agent (card_posted stays 0)
            self._cards[qid] = handle
            self._seen[qid] = "open"
            self.svc.store.mark_card_posted(qid, guess if q.get("kind") != "consent" else "")
            self.svc.store.add_event(self.call_id, "question_card_posted", {"question_id": qid})
            return
        if q["status"] != self._seen.get(qid):
            self._seen[qid] = q["status"]
            self._edit(handle, resolved_card(call, q))


def start_watcher(svc: Any, call_id: str) -> CallWatcher | None:
    """Start (once) the watcher for a freshly dialed call. Never raises."""
    try:
        watchers = svc.watchers
        if call_id in watchers:
            return watchers[call_id]
        poll_s = getattr(getattr(svc, "dialer", None), "poll_s", None) or svc.watch_poll_s
        watcher = CallWatcher(svc, call_id, poll_s=poll_s)
        watchers[call_id] = watcher
        watcher.start()
        return watcher
    except Exception:  # noqa: BLE001
        log.exception("could not start the hermes-phone watcher")
        return None
