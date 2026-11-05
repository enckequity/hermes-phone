"""Call state machine (SPEC section 2). Illegal transitions raise ``IllegalTransition``."""
from __future__ import annotations

from typing import Any

from .store import Store

APPROVAL_TTL_S = 900  # 15 minutes, for both the card and the recorded approval

DRAFT = "draft"
AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
DIALING = "dialing"
RINGING = "ringing"
IN_PROGRESS = "in_progress"
ON_HOLD_FOR_USER = "on_hold_for_user"
VOICEMAIL = "voicemail"
COMPLETED = "completed"
NO_ANSWER = "no_answer"
BUSY = "busy"
FAILED = "failed"
CANCELED = "canceled"
EXPIRED = "expired"
ABORTED = "aborted"

TERMINAL = frozenset({COMPLETED, NO_ANSWER, BUSY, FAILED, CANCELED, EXPIRED, ABORTED})
# States in which a live call may exist (used for "1 active call" and for abort).
LIVE = frozenset({DIALING, RINGING, IN_PROGRESS, ON_HOLD_FOR_USER, VOICEMAIL})
# "any active -> aborted": everything non-terminal once approved.
ACTIVE = LIVE | {APPROVED}

ALLOWED: dict[str, frozenset[str]] = {
    DRAFT: frozenset({AWAITING_APPROVAL, CANCELED}),
    AWAITING_APPROVAL: frozenset({APPROVED, CANCELED, EXPIRED}),
    APPROVED: frozenset({DIALING, CANCELED, EXPIRED, ABORTED}),
    DIALING: frozenset({RINGING, IN_PROGRESS, NO_ANSWER, BUSY, FAILED, CANCELED, ABORTED}),
    RINGING: frozenset({IN_PROGRESS, NO_ANSWER, BUSY, FAILED, CANCELED, ABORTED}),
    IN_PROGRESS: frozenset({ON_HOLD_FOR_USER, COMPLETED, VOICEMAIL, FAILED, ABORTED}),
    ON_HOLD_FOR_USER: frozenset({IN_PROGRESS, COMPLETED, FAILED, ABORTED}),
    VOICEMAIL: frozenset({COMPLETED, FAILED, ABORTED}),
}
for _t in TERMINAL:
    ALLOWED[_t] = frozenset()


class IllegalTransition(Exception):
    """Raised for a state change the machine does not allow."""


class UnknownCall(Exception):
    """Raised when a call id does not exist."""


def can_transition(old: str, new: str) -> bool:
    return new in ALLOWED.get(old, frozenset())


def transition(store: Store, call_id: str, new: str, **fields: Any) -> dict:
    """Move a call to ``new`` atomically (compare-and-set) and log an event.

    Terminal transitions stamp ``ended_at``. Returns the refreshed call row.
    """
    call = store.get_call(call_id)
    if call is None:
        raise UnknownCall(call_id)
    old = call["state"]
    if not can_transition(old, new):
        raise IllegalTransition(f"{old} -> {new} is not allowed")
    if new in TERMINAL:
        fields.setdefault("ended_at", store.clock())
    if not store.update_state_if(call_id, old, new, **fields):
        raise IllegalTransition(f"call {call_id} changed state concurrently (was {old})")
    store.add_event(call_id, "state", {"from": old, "to": new})
    return store.get_call(call_id)  # type: ignore[return-value]


def expire_if_stale(store: Store, call_id: str, ttl_s: float = APPROVAL_TTL_S) -> dict | None:
    """Lazily expire an unapproved card or an unused approval older than the TTL."""
    call = store.get_call(call_id)
    if call is None:
        return None
    now = store.clock()
    if call["state"] == AWAITING_APPROVAL and now - call["created_at"] > ttl_s:
        return transition(store, call_id, EXPIRED, reason="approval_ttl")
    if call["state"] == APPROVED:
        appr = store.get_approval(call_id)
        if appr is None or appr["used_at"] is not None or now >= appr["expires_at"]:
            return transition(store, call_id, EXPIRED, reason="approval_ttl")
    return call
