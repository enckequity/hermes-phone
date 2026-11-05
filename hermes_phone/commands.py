"""The ``/phone`` slash command. Handler signature is ``fn(raw_args, **kwargs)``; it never raises.

Only a human can type a slash command, so ``/phone approve`` is the one place an approval is
recorded. The approval is bound to the exact call id, the approving user/session and a timestamp.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from . import brief as briefmod
from . import consent, relay, results, safety, states, tools
from . import mode as modemod

log = logging.getLogger(__name__)

USAGE = ("Usage: /phone approve <id> [confirm] | cancel <id> | consent <id> <field> | "
         "status [id] | transcript <id> | log [n] | retry <id> | answer <qid> [text|decline] | "
         "hangup [id] | check")
CHOICES = ["Call", "Edit", "Cancel"]


def phone_command(raw_args: str = "", **kwargs: Any) -> str:
    try:
        parts = (raw_args or "").split()
        if not parts:
            return USAGE
        sub, rest = parts[0].lower(), parts[1:]
        handler = _SUBCOMMANDS.get(sub)
        if handler is None:
            return USAGE
        return handler(rest, kwargs)
    except Exception:  # noqa: BLE001
        log.exception("hermes-phone command failed")
        return "hermes-phone: that command failed internally."


def _approver(kwargs: dict) -> tuple[str, str]:
    session = str(kwargs.get("session_id") or kwargs.get("session_key") or "")
    user = str(kwargs.get("user_id") or kwargs.get("session_key") or kwargs.get("platform")
               or "local")
    return user, session


def _cmd_approve(rest: list[str], kwargs: dict) -> str:
    if not rest:
        return "Usage: /phone approve <id> [confirm]"
    call_id = rest[0]
    confirm = len(rest) > 1 and rest[1].lower() == "confirm"
    svc = tools.get_service()
    store = svc.store
    call = states.expire_if_stale(store, call_id)
    if call is None:
        return f"Unknown call id {call_id}."
    if call["state"] == states.APPROVED:
        # Approved earlier but never dialed (the model did not follow up): the user is asking
        # again, so dial now. phone_call re-checks the approval (unexpired, unused) itself.
        return _dial_now(call_id, call, kwargs, silent=True)
    if call["state"] != states.AWAITING_APPROVAL:
        return f"Call {call_id} is {call['state']}; only a call awaiting approval can be approved."
    refused = recheck_dialable(call)
    if refused:
        return refused

    if not confirm:
        choice = _ask(svc, f"Place call {call_id} to {call['callee_name']} ({call['number']})?")
        if choice is None:
            return (f"Interactive buttons are not available here. To approve exactly this call, "
                    f"send: /phone approve {call_id} confirm")
        choice = str(choice).strip().lower()
        if choice == "edit":
            states.transition(store, call_id, states.CANCELED, reason="edit_requested")
            return (f"Call {call_id} canceled so it can be edited. Tell me what to change and I "
                    "will prepare a new brief.")
        if choice != "call":
            states.transition(store, call_id, states.CANCELED, reason="user_canceled")
            return f"Call {call_id} canceled."
    record_user_approval(call_id, kwargs)
    # The user's explicit tap on Call (or `confirm`) IS the go-ahead: dial right here instead
    # of depending on the model to call phone_call afterwards. phone_call still re-checks the
    # approval, blocklist, allowlist, quiet hours and rate limits (defense in depth).
    return _dial_now(call_id, call, kwargs, silent=True)


def recheck_dialable(call: dict) -> str:
    """Hard safety re-check at approval time so the user never approves an undialable call.

    Returns a refusal message, or "" when the number may be approved."""
    svc = tools.get_service()
    e164, reason = safety.validate_number(call["number"])
    if e164 is None or e164 != call["number"]:
        return f"Cannot approve: {reason}"
    allow = safety.check_allowlist(call["number"], svc.environ)
    if not allow.ok:
        return f"Cannot approve: {allow.reason}"
    return ""


def approve_by_reply(call_id: str, kwargs: dict) -> str:
    """Approve and dial the one card waiting in this chat, from the user's plain reply.

    Same checks as ``/phone approve <id> confirm``; only the hook calls it, and only with text
    the human typed (see docs/ARCHITECTURE.md, safety)."""
    store = tools.get_service().store
    call = states.expire_if_stale(store, call_id)
    if call is None or call["state"] != states.AWAITING_APPROVAL:
        return f"Call {call_id} is no longer waiting for approval."
    refused = recheck_dialable(call)
    if refused:
        return refused
    record_user_approval(call_id, kwargs)
    return _dial_now(call_id, call, kwargs, ack=APPROVED_ACK)


def cancel_by_reply(call_id: str) -> str:
    return _cmd_cancel([call_id], {})


def record_user_approval(call_id: str, kwargs: dict) -> None:
    """Record the user's approval for exactly this call id (no dialing)."""
    store = tools.get_service().store
    user, session = _approver(kwargs)
    store.record_approval(call_id, user_id=user, session_id=session, ttl_s=states.APPROVAL_TTL_S)
    states.transition(store, call_id, states.APPROVED, approved_at=store.clock(),
                      user_id=user, session_id=session)


APPROVED_ACK = "\U0001F44D"
STATUS_WAIT_S = 3.0


def _status_posted(call_id: str) -> bool:
    """True once the watcher has posted (or can no longer post) the one live status message."""
    watcher = tools.get_service().watchers.get(call_id)
    deadline = time.monotonic() + STATUS_WAIT_S
    while watcher is not None and time.monotonic() < deadline:
        if getattr(watcher, "_status_handle", None) is not None:
            return True
        time.sleep(0.05)
    return watcher is not None and getattr(watcher, "_status_handle", None) is not None


def _dial_now(call_id: str, call: dict, kwargs: dict, *, silent: bool = False,
              ack: str = "") -> str:
    """Dial. When the watcher's live status message is out, that is the only message: a slash
    handler then replies with nothing (Hermes sends no reply for an empty result), and a plain
    reply (whose block message must be non-empty) with ``ack``."""
    data = json.loads(tools.phone_call({"call_id": call_id}, **kwargs))
    if "error" in data:
        return (f"Approved call {call_id}, but dialing failed: {data['error']}\n"
                f"Details: /phone status {call_id}")
    if (silent or ack) and _status_posted(call_id):
        return ack
    if tools.get_service().mode == "web":
        return (f"Approved. Ringing in your browser for {call['callee_name']}, call {call_id}: "
                "open the link I just posted and tap Answer. The call is placed to no phone; "
                "you are the callee. Live updates and any questions from the other side will "
                f"appear here. Emergency stop: /phone hangup {call_id}")
    return (f"Approved. Dialing {call['callee_name']} ({call['number']}) now, call {call_id}. "
            "Live updates and any questions from the other side will appear here. "
            f"Emergency stop: /phone hangup {call_id}")


def _ask(svc: tools.PhoneService, question: str) -> str | None:
    asker = getattr(svc.ctx, "ask_choice", None)
    if asker is None:
        return None
    try:
        return asker(question, list(CHOICES))
    except Exception:  # noqa: BLE001
        return None


def _cmd_cancel(rest: list[str], kwargs: dict) -> str:
    if not rest:
        return "Usage: /phone cancel <id>"
    store = tools.get_service().store
    call = states.expire_if_stale(store, rest[0])
    if call is None:
        return f"Unknown call id {rest[0]}."
    if call["state"] in (states.DRAFT, states.AWAITING_APPROVAL, states.APPROVED):
        states.transition(store, rest[0], states.CANCELED, reason="user_canceled")
        return f"Call {rest[0]} canceled."
    if call["state"] in states.LIVE:
        return f"That call is already live. End it with /phone hangup {rest[0]}."
    return f"Call {rest[0]} is already {call['state']}."


def _cmd_consent(rest: list[str], kwargs: dict) -> str:
    if len(rest) < 2:
        return "Usage: /phone consent <id> <field>  (fields: " + ", ".join(
            consent.SENSITIVE_FIELDS) + ")"
    call_id, field = rest[0], rest[1].lower()
    if consent.is_forbidden_field(field):
        return f"{field} is never shared on a call, with or without consent."
    if not consent.grantable(field):
        return "Unknown field. Fields: " + ", ".join(consent.SENSITIVE_FIELDS)
    store = tools.get_service().store
    call = states.expire_if_stale(store, call_id)
    if call is None:
        return f"Unknown call id {call_id}."
    user, _ = _approver(kwargs)
    if call["state"] in states.LIVE:  # only in answer to a question the callee asked
        msg = relay.grant_for_question(tools.get_service(), call, field, user)
        return msg or (f"Call {call_id} is live. Consent can only be given now for a sensitive "
                       "question that is waiting for you (see its card).")
    if call["state"] not in (states.AWAITING_APPROVAL, states.APPROVED):
        return f"Call {call_id} is {call['state']}; consent can only be given before dialing."
    store.grant_consent(call_id, field, user)
    new = briefmod.refresh_brief(call, store.consents(call_id))
    old = call["brief"]
    store.update_brief(call_id, dict(new, facts_raw=old.get("facts_raw", {}),
                                     sensitive_requested=old.get("sensitive_requested", [])))
    return f"Consent for {field} recorded for call {call_id} only.\n" + briefmod.format_card(
        new, call_id)


def _cmd_status(rest: list[str], kwargs: dict) -> str:
    out = tools.phone_status({"call_id": rest[0]} if rest else {"limit": 5})
    data = json.loads(out)
    if "error" in data:
        return data["error"]
    calls = [data["call"]] if "call" in data else data["calls"]
    if not calls:
        return "No calls yet."
    lines = []
    for c in calls:
        lines.append(f"{c['call_id']}  {c['state']}  {c['callee_name']} {c['number']}")
        if c.get("reason"):
            # Verbatim provider error (code, message, fix hint) so the user sees the real cause.
            lines.append(f"   reason: {c['reason']}")
    return "\n".join(lines)


def _cmd_transcript(rest: list[str], kwargs: dict) -> str:
    if not rest:
        return "Usage: /phone transcript <id>"
    svc = tools.get_service()
    data = json.loads(tools.phone_transcript({"call_id": rest[0]}))
    if "error" in data:
        return data["error"]
    if not svc.config("store_transcripts", True):
        return (data.get("note") or "Transcripts are not stored.") + (
            "\n" + data["summary"] if data.get("summary") else "")
    call = svc.store.get_call(rest[0])
    messages = results.transcript_messages(call, svc.store.transcript(rest[0])) if call else []
    if not messages:
        return data.get("note") or "No transcript recorded."
    if len(messages) == 1:
        return messages[0]
    poster = getattr(svc.ctx, "post_message", None)
    if poster is None:
        return "\n\n".join(messages)
    for text in messages:  # one Telegram-safe message per part, in order
        try:
            poster(text)
        except Exception:  # noqa: BLE001
            return "\n\n".join(messages)
    return f"Transcript sent in {len(messages)} messages."


def _cmd_log(rest: list[str], kwargs: dict) -> str:
    try:
        limit = int(rest[0]) if rest else 10
    except ValueError:
        limit = 10
    store = tools.get_service().store
    rows = store.recent_calls(max(1, min(50, limit)))
    if not rows:
        return "No calls yet."
    return "\n".join(results.log_line(c) for c in rows)


def _cmd_retry(rest: list[str], kwargs: dict) -> str:
    """New call from the same brief. Never dials: the user must approve the new card."""
    if not rest:
        return "Usage: /phone retry <id>"
    svc = tools.get_service()
    store = svc.store
    old = store.get_call(rest[0])
    if old is None:
        return f"Unknown call id {rest[0]}."
    if old["state"] not in states.TERMINAL:
        return f"Call {old['id']} is {old['state']}; only a finished call can be retried."
    outcome = results.effective_outcome(old)
    if old["state"] in (states.CANCELED, states.EXPIRED) or outcome not in results.RETRYABLE:
        return (f"Call {old['id']} ended as {outcome}; only no answer, busy, voicemail, "
                "needs follow-up or failed calls can be retried.")
    if old.get("retry_of"):
        return (f"Call {old['id']} was already a retry of {old['retry_of']}. One retry per "
                "brief; ask me to prepare a new call if you still need this.")
    done = store.retries_of(old["id"])
    if done:
        return f"Call {old['id']} was already retried as {done[0]['id']}. One retry per brief."
    brief = old["brief"]
    facts = dict(brief.get("facts_raw") or {})
    requested = list(brief.get("sensitive_requested") or [])
    for key, value in results.late_answer_facts(store, old["id"]).items():
        field = consent.field_for_fact(key, value)
        if field in consent.FORBIDDEN_FIELDS:
            continue
        if field and field not in requested:
            requested.append(field)  # shown on the new card as needing consent
        facts.setdefault(key, value)
    lim = brief.get("limits", {})
    args = {"callee_name": old["callee_name"], "number": old["number"],
            "number_source": old.get("number_source") or "", "goal": old["goal"],
            "facts_to_share": facts, "sensitive_fields": requested,
            "language": brief.get("language", "en"), "max_duration_s": lim.get("max_duration_s"),
            "record": bool(lim.get("recording")),
            "leave_voicemail": bool(lim.get("voicemail_message")),
            "override_quiet_hours": bool(brief.get("quiet_hours_override"))}
    data = json.loads(tools._phone_prepare_safe(args, kwargs, retry_of=old["id"]))
    if "error" in data:
        return f"Could not prepare the retry: {data['error']}"
    new = data["call_id"]
    head = (f"Retry prepared as {new} (new approval needed, nothing is dialed yet). "
            f"Approve with /phone approve {new}.")
    return head if data.get("card_posted") else head + "\n" + data["card"]


def _cmd_answer(rest: list[str], kwargs: dict) -> str:
    svc = tools.get_service()
    if not rest:
        return relay.list_open(svc)
    user, _ = _approver(kwargs)
    if len(rest) == 1:
        return relay.choose_answer(svc, rest[0], user)
    return relay.answer_question(svc, rest[0], " ".join(rest[1:]), user)


def _cmd_hangup(rest: list[str], kwargs: dict) -> str:
    return relay.hangup(tools.get_service(), rest[0] if rest else "")


def _mask(e164: str) -> str:
    return e164[:2] + "•" * max(len(e164) - 6, 0) + e164[-4:] if len(e164) > 6 else "•••"


_PLACEHOLDER_MARKERS = ("xxxxxxxx", "555555", "your-tunnel", "trycloudflare.com", "<")


def _looks_placeholder(value: str) -> bool:
    low = value.lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


def _cmd_check(rest: list[str], kwargs: dict) -> str:
    """Readiness report. Shows variable NAMES and masked numbers only, never secret values."""
    env = tools.get_service().environ
    chosen = modemod.resolve(env)
    how = {"explicit": "set by HERMES_PHONE_MODE", "auto": "automatic: Twilio is not "
           "configured, OpenAI and PUBLIC_BASE_URL are", "default": "default"}[chosen.how]
    lines = ["hermes-phone setup check:",
             f"- mode: {chosen.name} ({how})"
             + (f"; missing: {', '.join(chosen.missing)}" if chosen.missing else "")]
    raw = env.get(safety.ALLOWLIST_ENV)
    if env.get(safety.ALLOW_ALL_ENV, "").strip() == "1":
        lines.append(f"- allowlist: OFF ({safety.ALLOW_ALL_ENV}=1)")
    elif raw is None:
        lines.append(f"- allowlist: {safety.ALLOWLIST_ENV} is NOT visible to the plugin")
    else:
        items = [i.strip() for i in raw.split(",") if i.strip()]
        ok = sorted(safety.parse_allowlist(raw))
        bad = len(items) - len(ok)
        shown = ", ".join(_mask(n) + (" (PLACEHOLDER)" if "555555" in n else "")
                          for n in ok) or "none"
        lines.append(f"- allowlist: {len(ok)} valid number(s): {shown}"
                     + (f"; {bad} entr{'y' if bad == 1 else 'ies'} not valid E.164" if bad else ""))
    names = ("OPENAI_API_KEY", "PUBLIC_BASE_URL") if chosen.name == modemod.WEB else (
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "OPENAI_API_KEY",
        "PUBLIC_BASE_URL")
    for name in names:
        value = (env.get(name) or "").strip()
        state = "set" if value else "MISSING"
        if value and _looks_placeholder(value):
            state = "PLACEHOLDER from .env.example, replace with your real value"
        lines.append(f"- {name}: {state}")
    return "\n".join(lines)


_SUBCOMMANDS = {
    "approve": _cmd_approve, "cancel": _cmd_cancel, "consent": _cmd_consent,
    "status": _cmd_status, "transcript": _cmd_transcript, "log": _cmd_log,
    "retry": _cmd_retry, "answer": _cmd_answer, "hangup": _cmd_hangup, "check": _cmd_check,
}
