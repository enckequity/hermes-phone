"""Web phone mode, shared bits: the callee is a browser page instead of a PSTN phone.

``provider_sid`` is ``web-<id>`` for these calls. The link token lives in ``web_tokens``
(hashed, single use). Both the plugin watcher and the bridge routes call ``expire_stale`` so a
link nobody opens gives exactly one ``no_answer`` result.
"""
from __future__ import annotations

import logging

from . import states
from .store import Store

log = logging.getLogger(__name__)

SID_PREFIX = "web-"
LINK_TTL_S = 600.0  # the link token is valid this long, until it is used
RING_TIMEOUT_S = 60.0  # nobody opened and answered within this long: no_answer
CONNECT_TIMEOUT_S = 30.0  # answered in the page but the media socket never opened


def is_web_sid(sid: str | None) -> bool:
    return bool(sid) and str(sid).startswith(SID_PREFIX)


def answer_url(base_url: str, call_id: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/phone/{call_id}?t={token}"


def link_message(url: str) -> str:
    return ("Ringing in your browser: open this link and tap Answer (works once, about a "
            f"minute).\n{url}")


def expire_stale(store: Store, call_id: str) -> bool:
    """Settle a web call nobody picked up (or whose page never connected). Idempotent; True when
    this call moved the call to a terminal state."""
    call = store.get_call(call_id)
    if call is None or not is_web_sid(call.get("provider_sid")):
        return False
    now = store.clock()
    state = call["state"]
    if state in (states.DIALING, states.RINGING):
        started = store.web_token_created_at(call_id) or call.get("dialed_at") or now
        if now - started < RING_TIMEOUT_S:
            return False
        return _end(store, call, states.NO_ANSWER, "no_answer", "no_answer",
                    "Nobody answered the browser link.")
    if state == states.IN_PROGRESS and not store.media_token_used(call_id):
        minted = store.last_media_token_at(call_id)
        if minted is not None and now - minted >= CONNECT_TIMEOUT_S:
            return _end(store, call, states.FAILED, "browser_never_connected", "failed",
                        "The browser page answered but its audio never connected.")
    return False


def _end(store: Store, call: dict, final: str, reason: str, outcome: str, summary: str) -> bool:
    cid = call["id"]
    store.set_result_once(cid, {"outcome": outcome, "summary": summary, "reason": reason},
                          outcome)
    try:
        states.transition(store, cid, final, reason=reason, outcome=outcome)
    except states.IllegalTransition:
        return False
    return True
