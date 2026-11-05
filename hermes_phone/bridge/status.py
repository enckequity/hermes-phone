"""Twilio status callbacks -> call state machine. Idempotent: duplicates change nothing."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import states
from ..store import Store
from . import twilio_io

REASONS = {
    states.NO_ANSWER: "No answer.",
    states.BUSY: "Line busy.",
    states.FAILED: "The call could not be connected.",
    states.CANCELED: "The call was canceled before it was answered.",
}
PRE_ANSWER = (states.DIALING, states.RINGING)


def _advance(store: Store, call_id: str, new: str, **fields: Any) -> bool:
    call = store.get_call(call_id)
    if call is None or not states.can_transition(call["state"], new):
        return False
    try:
        states.transition(store, call_id, new, **fields)
    except states.IllegalTransition:  # lost a race with another writer: treat as duplicate
        return False
    return True


def apply_status(store: Store, call_id: str, params: dict[str, str],
                 on_voicemail: Callable[[str], None] | None = None,
                 hangup: Callable[[str], None] | None = None) -> str:
    """Apply one callback. Returns a short note ('applied', 'duplicate', 'ignored')."""
    call = store.get_call(call_id)
    if call is None:
        return "unknown"
    status = (params.get("CallStatus") or "").lower()
    answered_by = params.get("AnsweredBy") or ""
    sid = params.get("CallSid") or ""
    store.add_event(call_id, "twilio_status", {"status": status, "answered_by": answered_by})
    if call.get("provider_sid") and sid and sid != call["provider_sid"]:
        store.add_event(call_id, "twilio_sid_mismatch", {})
        return "ignored"
    if sid and not call.get("provider_sid"):
        store.update_call(call_id, provider_sid=sid)
    duration = params.get("CallDuration", "")
    if duration.isdigit():
        store.update_call(call_id, duration_s=int(duration))

    if answered_by and twilio_io.is_machine(answered_by):
        return _voicemail(store, call, answered_by, on_voicemail, hangup, sid)
    if not status or status in twilio_io.IGNORED_STATUSES:
        return "ignored"
    target = twilio_io.STATUS_MAP.get(status)
    if target is None:
        return "ignored"
    state = store.get_call(call_id)["state"]  # type: ignore[index]
    if target == states.COMPLETED:
        if state in PRE_ANSWER:
            _advance(store, call_id, states.IN_PROGRESS)
        return _terminal(store, call_id, target)
    if target in (states.RINGING, states.IN_PROGRESS):
        ok = state in PRE_ANSWER and (target == states.IN_PROGRESS or state == states.DIALING)
        return "applied" if ok and _advance(store, call_id, target) else "duplicate"
    return _terminal(store, call_id, target)


def _terminal(store: Store, call_id: str, target: str) -> str:
    call = store.get_call(call_id)
    assert call is not None
    if not states.can_transition(call["state"], target):
        return "duplicate"
    if target == states.COMPLETED:
        outcome, reason = ("" if call.get("result_json") else "needs_followup"), "completed"
        summary = "The call ended without a recorded result."
    else:
        outcome, reason = target, target
        summary = REASONS.get(target, "")
    if not call.get("result_json"):
        store.set_result_once(call_id, {"outcome": outcome, "summary": summary,
                                        "reason": reason}, outcome)
    fields: dict[str, Any] = {"reason": reason if not call.get("reason") else call["reason"]}
    if outcome and not call.get("outcome"):
        fields["outcome"] = outcome
    return "applied" if _advance(store, call_id, target, **fields) else "duplicate"


def _voicemail(store: Store, call: dict, answered_by: str, on_voicemail, hangup, sid: str) -> str:
    call_id = call["id"]
    leave = bool(call["brief"]["limits"].get("voicemail_message"))
    state = store.get_call(call_id)["state"]  # type: ignore[index]
    applied = "duplicate"
    if state in PRE_ANSWER:
        _advance(store, call_id, states.IN_PROGRESS)
    if _advance(store, call_id, states.VOICEMAIL, reason="voicemail"):
        applied = "applied"
        if not leave:
            store.set_result_once(call_id, {
                "outcome": "voicemail", "reason": "voicemail",
                "summary": "Reached voicemail, did not leave a message."}, "voicemail")
            store.update_call(call_id, outcome="voicemail")
    if on_voicemail is not None:
        on_voicemail(answered_by)
    elif not leave and hangup is not None and sid:
        hangup(sid)
    return applied
