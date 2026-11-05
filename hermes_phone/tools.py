"""Tool handlers and the shared service object.

Handlers follow the Hermes contract: ``handler(args, **kwargs)``, never raise, return JSON
strings. The service holds the injectable pieces (store, clock, dialer, env) so unit tests
do not need Hermes, a network, or the wall clock.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from . import brief as briefmod
from . import consent, results, safety, states
from . import secrets as secretsmod
from .store import Store

MAX_TEXT = 2000
TOOL_ARGS_PHONE_CALL = frozenset({"call_id"})


# ---- dialer interface (Twilio implementation arrives in P2) ---------------------------------
@dataclass
class DialResult:
    ok: bool
    provider_sid: str = ""
    error: str = ""
    answer_url: str = ""  # web phone mode only: the link the user opens to answer


class Dialer(Protocol):
    """Places the actual call. P1 ships only ``NullDialer``; P2 adds the Twilio dialer."""

    def place_call(self, call: dict, payload: dict) -> DialResult: ...


@dataclass
class NullDialer:
    """No-network dialer: records what would have been dialed. Used in P1 and in tests."""

    calls: list[dict] = field(default_factory=list)
    configured: bool = True
    missing: list[str] = field(default_factory=list)

    def place_call(self, call: dict, payload: dict) -> DialResult:
        self.calls.append({"call_id": call["id"], "number": call["number"], "payload": payload})
        return DialResult(ok=True, provider_sid="")


# ---- service ----------------------------------------------------------------------------
class PhoneService:
    def __init__(self, *, ctx: Any = None, store: Store | None = None,
                 store_factory: Callable[[], Store] | None = None,
                 dialer: Dialer | None = None, environ: Mapping[str, str] | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.ctx = ctx
        self._store = store
        self._store_factory = store_factory or Store
        self._dialer = dialer  # None: resolved lazily from env (Twilio if configured)
        self._dialer_auto = False
        self._dialer_kind = ""
        self._environ = environ
        self._now = now
        self._lock = threading.Lock()
        self.watchers: dict[str, Any] = {}  # call_id -> relay.CallWatcher
        self.answer_links: dict[str, str] = {}  # web mode: call_id -> link to post (in memory)
        self.watch_poll_s: float = 0.5
        self.ack_sessions: dict[str, float] = {}  # session id -> when a card was posted for it

    @property
    def environ(self) -> Mapping[str, str]:
        return secretsmod.Environ() if self._environ is None else self._environ

    @property
    def dialer(self) -> Dialer:
        if self._dialer is not None and not self._dialer_auto:
            return self._dialer  # injected (tests)
        from . import dialer as dialermod
        from . import mode as modemod

        # Re-resolved on every access: secrets are profile-scoped, so the answer can differ per
        # turn. Dialers read secrets at use time; only the matching kind is reused (web links).
        chosen = modemod.resolve(self.environ)
        if chosen.missing:
            kind = "null"
        elif chosen.name == modemod.WEB:
            kind = "web"
        else:
            kind = "twilio"
        if self._dialer is None or self._dialer_kind != kind or kind == "null":
            if kind == "null":
                self._dialer = NullDialer(configured=False, missing=chosen.missing)
            elif kind == "web":
                self._dialer = dialermod.WebPhoneDialer(self.environ, store=self.store)
            else:
                self._dialer = dialermod.TwilioDialer(self.environ)
            self._dialer_kind = kind
            self._dialer_auto = True
        return self._dialer

    @property
    def mode(self) -> str:
        """``twilio`` or ``web``: how calls are placed with the current environment."""
        from . import mode as modemod

        return modemod.resolve(self.environ).name

    @dialer.setter
    def dialer(self, value: Dialer) -> None:
        self._dialer = value
        self._dialer_auto = False

    @property
    def store(self) -> Store:
        with self._lock:
            if self._store is None:
                self._store = self._store_factory()
            return self._store

    def now(self) -> datetime:
        """Wall-clock instant for quiet hours; follows the store clock so tests need one fake."""
        if self._now is not None:
            return self._now()
        return datetime.fromtimestamp(self.store.clock(), UTC)

    def config(self, key: str, default: Any = None) -> Any:
        getter = getattr(self.ctx, "get_config", None)
        if getter is None:
            return default
        try:
            value = getter(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def quiet_hours_on(self) -> bool:
        """Quiet hours (08:00-21:00 callee time) are opt-in: config `enforce_quiet_hours: true`
        or env HERMES_PHONE_QUIET_HOURS=on. Off by default: calls only go to numbers the
        developer allowlisted, and every call needs the user's explicit approval."""
        env = str(self.environ.get("HERMES_PHONE_QUIET_HOURS", "")).strip().lower()
        if env in ("on", "1", "true", "yes"):
            return True
        if env in ("off", "0", "false", "no"):
            return False
        return bool(self.config("enforce_quiet_hours", False))

    def config_int(self, key: str, default: int) -> int:
        try:
            return int(self.config(key, default))
        except (TypeError, ValueError):
            return default

    # -- approval checks (shared by hook and handler) --
    def check_approval(self, call_id: str) -> tuple[bool, str]:
        """Is ``call_id`` approved, unexpired and unused? Does not consume the approval."""
        call = states.expire_if_stale(self.store, call_id)
        if call is None:
            return False, "Unknown call id."
        if call["state"] != states.APPROVED:
            return False, f"Call is {call['state']}, not approved."
        appr = self.store.get_approval(call_id)
        if appr is None:
            return False, "No approval recorded for this call id."
        if appr["used_at"] is not None:
            return False, "This approval was already used. Prepare a new call."
        if self.store.clock() >= appr["expires_at"]:
            return False, "The approval expired. Prepare the call again."
        return True, ""


_service: PhoneService | None = None


def configure(**kwargs: Any) -> PhoneService:
    """Install the process-wide service. ``register`` calls this; tests inject fakes."""
    global _service
    _service = PhoneService(**kwargs)
    return _service


def get_service() -> PhoneService:
    global _service
    if _service is None:
        _service = PhoneService()
    return _service


# ---- helpers ----------------------------------------------------------------------------
def _json(**payload: Any) -> str:
    return json.dumps(payload, default=str)


def _error(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, default=str)


def _text(value: Any, name: str, *, required: bool = True, limit: int = MAX_TEXT) -> str:
    if value is None or value == "":
        if required:
            raise ValueError(f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    return value[:limit]


def _call_id(args: Any) -> str:
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    cid = args.get("call_id")
    if not isinstance(cid, str) or not cid.strip() or len(cid) > 64:
        raise ValueError("call_id is required")
    return cid.strip()


def _public_call(call: dict, svc: PhoneService | None = None) -> dict:
    """Call summary safe to hand to the agent: no raw facts, no sensitive values."""
    brief = {k: v for k, v in call["brief"].items() if k != "facts_raw"}
    spoken = call.get("disclosure_spoken")
    extra: dict[str, Any] = {
        "duration_s": results.call_duration_s(call) if call["state"] in states.TERMINAL else None,
        "disclosure_spoken": None if spoken is None else bool(spoken),
        "retry_of": call.get("retry_of"), "result_posted": call.get("result_posted_at") is not None,
    }
    if svc is not None and call["state"] in states.TERMINAL:
        extra["result_text"] = results.result_message(
            call, svc.config, store_transcripts=bool(svc.config("store_transcripts", True)),
            retried=bool(svc.store.retries_of(call["id"])),
            has_transcript=bool(svc.store.transcript(call["id"])))
    return {**extra,
        "call_id": call["id"], "state": call["state"], "callee_name": call["callee_name"],
        "number": call["number"], "goal": brief.get("goal", ""), "outcome": call["outcome"],
        "reason": call["reason"], "result": call["result"], "created_at": call["created_at"],
        "ended_at": call["ended_at"], "brief": brief,
    }


def _session_id(kwargs: Mapping[str, Any]) -> str:
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


# ---- tools ------------------------------------------------------------------------------
def phone_prepare(args: Any, **kwargs: Any) -> str:
    try:
        return _phone_prepare(args, kwargs)
    except ValueError as exc:
        return _error(str(exc))
    except Exception:  # noqa: BLE001 - never raise into Hermes
        return _error("phone_prepare failed internally.")


def _phone_prepare(args: Any, kwargs: Mapping[str, Any], retry_of: str = "") -> str:
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    svc = get_service()
    callee = _text(args.get("callee_name"), "callee_name", limit=200)
    goal = _text(args.get("goal"), "goal")
    source = _text(args.get("number_source"), "number_source", required=False, limit=500)
    language = _text(args.get("language"), "language", required=False, limit=16) or "en"
    raw_number = args.get("number")
    if not isinstance(raw_number, str) or not raw_number.strip():
        raise ValueError("number is required")

    e164, reason = safety.validate_number(raw_number)
    if e164 is None:
        return _error(reason, blocked=True)

    facts = args.get("facts_to_share") or {}
    if not isinstance(facts, dict):
        raise ValueError("facts_to_share must be an object")
    requested = args.get("sensitive_fields") or []
    if not isinstance(requested, list) or not all(isinstance(x, str) for x in requested):
        raise ValueError("sensitive_fields must be a list of strings")
    requested = [x.strip().lower() for x in requested]
    for name in requested:
        if consent.is_forbidden_field(name):
            raise ValueError(f"{name} can never be shared on a call.")
        if not consent.grantable(name):
            raise ValueError(f"unknown sensitive field: {name}")
    for key, value in facts.items():  # forbidden facts are refused loudly at prepare
        if consent.field_for_fact(key, value) in consent.FORBIDDEN_FIELDS:
            raise ValueError(f"fact {key!r} is a cvv/password/otp value and can never be shared.")

    record = bool(args.get("record", False))
    voicemail = bool(args.get("leave_voicemail", False))
    override = bool(args.get("override_quiet_hours", False))
    default_max = svc.config_int("max_duration_s", briefmod.DEFAULT_MAX_DURATION_S)
    max_duration = briefmod.clamp_duration(args.get("max_duration_s", default_max), default_max)

    allow = safety.check_allowlist(e164, svc.environ)
    if not allow.ok:
        return _error(allow.reason, blocked=True)
    quiet = safety.check_quiet_hours(e164, svc.now(), override or not svc.quiet_hours_on())
    if not quiet.ok:
        return _error(quiet.reason, quiet_hours=True)

    user_name = str(svc.config("user_name", "") or "")
    brief = briefmod.build_brief(
        callee_name=callee, number=e164, number_source=source, goal=goal, facts=facts,
        sensitive_requested=requested, granted=set(), user_name=user_name,
        max_duration_s=max_duration, record=record, leave_voicemail=voicemail,
        language=language, quiet_hours_override=override,
        question_timeout_s=svc.config_int("question_timeout_s",
                                          briefmod.DEFAULT_QUESTION_TIMEOUT_S))
    call_id = "c" + uuid.uuid4().hex[:8]
    stored = dict(brief, facts_raw=facts, sensitive_requested=requested)
    store = svc.store
    # One pending card per chat: a new request replaces older unapproved cards instead of
    # making the user cancel them one by one (live feedback: stale cards blocked new requests).
    superseded = []
    for row in store.awaiting_for_session(_session_id(kwargs)):
        try:
            states.transition(store, row["id"], states.CANCELED, reason="superseded")
            superseded.append(row["id"])
        except Exception:  # noqa: BLE001 - a racing approval/expiry wins; nothing to replace
            pass
    store.create_call(call_id, state=states.DRAFT, callee_name=callee, number=e164,
                      number_source=source, goal=goal, brief=stored,
                      session_id=_session_id(kwargs), retry_of=retry_of)
    states.transition(store, call_id, states.AWAITING_APPROVAL)

    card = briefmod.format_card(brief, call_id)
    posted = False
    poster = getattr(svc.ctx, "post_message", None)
    if poster is not None:
        try:
            handle = poster(card)
            posted = getattr(handle, "ok", True) is not False
        except Exception:  # noqa: BLE001
            posted = False
    if posted:
        svc.ack_sessions[_session_id(kwargs)] = time.monotonic()
    return _json(
        call_id=call_id, state=states.AWAITING_APPROVAL, brief=brief, card=card,
        replaced_cards=superseded,
        card_posted=posted, approve_hint=f"/phone approve {call_id}",
        instructions=(
            ("The approval card is ALREADY shown to the user. Reply with ONLY the single "
             "character \U0001F446 and nothing else: no words, no summary, no offers. ")
            if posted else
            ("Show the user the card text above verbatim, then tell them to reply \"call\" "
             f"(or send /phone approve {call_id}). ")
        ) + ("Do not call phone_call yourself: the user's own reply \"call\" or /phone approve "
             "dials. You cannot approve for them."),
    )


def _phone_prepare_safe(args: Any, kwargs: Mapping[str, Any], retry_of: str = "") -> str:
    """``phone_prepare`` for internal callers (``/phone retry``): same checks, links the retry."""
    try:
        return _phone_prepare(args, kwargs, retry_of)
    except ValueError as exc:
        return _error(str(exc))
    except Exception:  # noqa: BLE001
        return _error("prepare failed internally.")


def phone_call(args: Any, **kwargs: Any) -> str:
    try:
        return _phone_call(args, kwargs)
    except ValueError as exc:
        return _error(str(exc))
    except (states.IllegalTransition, states.UnknownCall) as exc:
        return _error(f"Cannot start call: {exc}")
    except Exception:  # noqa: BLE001
        return _error("phone_call failed internally.")


def _phone_call(args: Any, kwargs: Mapping[str, Any]) -> str:
    cid = _call_id(args)
    extra = set(args) - TOOL_ARGS_PHONE_CALL
    if extra:
        raise ValueError(f"phone_call takes only call_id (got {sorted(extra)}).")
    svc = get_service()
    store = svc.store
    ok, reason = svc.check_approval(cid)
    if not ok:
        return _error(f"Not approved: {reason}", approved=False)
    call = store.get_call(cid)
    assert call is not None
    number = call["number"]
    # Defence in depth: re-run every number check at dial time.
    e164, reason = safety.validate_number(number)
    if e164 is None or e164 != number:
        return _error(reason or "Number failed validation.", blocked=True)
    allow = safety.check_allowlist(number, svc.environ)
    if not allow.ok:
        return _error(allow.reason, blocked=True)
    override = bool(call["brief"].get("quiet_hours_override"))
    quiet = safety.check_quiet_hours(number, svc.now(), override or not svc.quiet_hours_on())
    if not quiet.ok:
        return _error(quiet.reason, quiet_hours=True)
    limit = svc.config_int("daily_call_limit", safety.DEFAULT_DAILY_LIMIT)
    rate = safety.check_rate_limits(store, number, daily_limit=limit, exclude_call_id=cid)
    if not rate.ok:
        return _error(rate.reason, rate_limited=True)

    if getattr(svc.ctx, "post_message", None) is None:  # SPEC 4: questions need a way to reach you
        return _error("There is no way to reach you with mid-call questions here (this surface "
                      "cannot post messages), so the call was not started. Nothing was dialed.",
                      relay_unavailable=True)
    dialer = svc.dialer
    if not getattr(dialer, "configured", True):
        missing = ", ".join(getattr(dialer, "missing", []) or ["Twilio settings"])
        return _error("Phone calling is not configured (missing: " + missing + "). Nothing was "
                      "dialed. Set the environment variables and run `hermes phone doctor`.",
                      configured=False)
    preflight = getattr(dialer, "preflight", None)
    if preflight is not None:  # the bridge must be up before an approval is spent
        problem = preflight()
        if problem:
            return _error(problem, bridge_unreachable=True)

    if not store.consume_approval(cid):  # one-use; loses the race -> refused
        return _error("Not approved: approval already used or expired.", approved=False)
    states.transition(store, cid, states.DIALING, dialed_at=store.clock())
    call = store.get_call(cid)
    assert call is not None
    payload = briefmod.voice_payload(call, store.consents(cid))
    result = dialer.place_call(call, payload)
    if not result.ok:
        states.transition(store, cid, states.FAILED, reason=(result.error or "dial_failed")[:500],
                          outcome="failed")
        return _error(f"Dialing failed: {result.error or 'unknown'}", call_id=cid,
                      state=states.FAILED)
    if result.provider_sid:
        store.update_call(cid, provider_sid=result.provider_sid)
    from . import relay

    if result.answer_url:  # web phone mode: the watcher posts the link to this conversation
        svc.answer_links[cid] = result.answer_url
    relay.start_watcher(svc, cid)  # bound to this conversation via copy_context
    if result.answer_url:
        return _json(call_id=cid, state=states.DIALING, mode="web", answer_url=result.answer_url,
                     instructions=("Ringing in the user's browser. The link was posted to the "
                                   "chat; tell them to open it and tap Answer."))
    return _json(call_id=cid, state=states.DIALING)


def phone_status(args: Any, **kwargs: Any) -> str:
    try:
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        store = get_service().store
        cid = args.get("call_id")
        if cid:
            if not isinstance(cid, str) or len(cid) > 64:
                raise ValueError("call_id must be a string")
            states.expire_if_stale(store, cid)
            call = store.get_call(cid)
            if call is None:
                return _error("Unknown call id.")
            return _json(call=_public_call(call, get_service()))
        try:
            limit = max(1, min(50, int(args.get("limit", 10))))
        except (TypeError, ValueError):
            limit = 10
        svc = get_service()
        return _json(calls=[_public_call(c, svc) for c in store.recent_calls(limit)])
    except ValueError as exc:
        return _error(str(exc))
    except Exception:  # noqa: BLE001
        return _error("phone_status failed internally.")


def phone_transcript(args: Any, **kwargs: Any) -> str:
    try:
        cid = _call_id(args)
        svc = get_service()
        call = svc.store.get_call(cid)
        if call is None:
            return _error("Unknown call id.")
        done = call["state"] in states.TERMINAL
        if not svc.config("store_transcripts", True):
            return _json(call_id=cid, state=call["state"], transcript=[],
                         note="Transcripts are disabled (store_transcripts: false); only the "
                              "summary is kept.",
                         summary=results.result_message(call, svc.config,
                                                        store_transcripts=False) if done else "")
        rows = svc.store.transcript(cid)
        lines = [{"role": t["role"], "text": t["text"]} for t in rows]
        note = "" if done else "Call is not finished; transcript is partial."
        return _json(call_id=cid, state=call["state"], transcript=lines, note=note,
                     text="\n".join(results.transcript_lines(rows)))
    except ValueError as exc:
        return _error(str(exc))
    except Exception:  # noqa: BLE001
        return _error("phone_transcript failed internally.")


# ---- schemas ----------------------------------------------------------------------------
_S = {"type": "string"}
SCHEMAS: dict[str, dict] = {
    "phone_prepare": {
        "name": "phone_prepare",
        "description": ("Prepare an outbound phone call and show the user an approval card. "
                        "Never places a call. Use before phone_call."),
        "parameters": {"type": "object", "properties": {
            "callee_name": {"type": "string",
                            "description": "Who is called, including any person the user "
                                           "named, e.g. \"Dr. Smith's dental office\""},
            "number": {"type": "string", "description": "E.164, e.g. +14155550123"},
            "number_source": {"type": "string",
                              "description": "'user' or the URL where the number was found"},
            "goal": {"type": "string",
                     "description": "One full sentence: what to do, with whom, when."},
            "facts_to_share": {"type": "object",
                               "description": "Include EVERY non-sensitive detail from the "
                                              "user's request that the callee may need: "
                                              "person_to_see (the doctor, stylist or contact "
                                              "the user named), service, date, time window, "
                                              "party size, booking name. The voice agent "
                                              "knows nothing else. e.g. {\"person_to_see\":"
                                              "\"Dr. Smith\",\"service\":\"teeth "
                                              "whitening\",\"day\":\"tomorrow\","
                                              "\"time_window\":\"11:00-14:00\","
                                              "\"name\":\"Winch\"}"},
            "sensitive_fields": {"type": "array", "items": {"type": "string", "enum": list(
                consent.SENSITIVE_FIELDS)}, "description": "Fields that need per-call consent"},
            "language": {"type": "string", "default": "en"},
            "max_duration_s": {"type": "integer", "default": 300},
            "record": {"type": "boolean", "default": False},
            "leave_voicemail": {"type": "boolean", "default": False},
            "override_quiet_hours": {
                "type": "boolean", "default": False,
                "description": "Shown on the approval card; only use if the user asked for a "
                               "call outside 08:00-21:00 callee time"},
        }, "required": ["callee_name", "number", "goal"]},
    },
    "phone_call": {
        "name": "phone_call",
        "description": "Place a call that the user already approved. Fails if not approved.",
        "parameters": {"type": "object", "properties": {"call_id": _S}, "required": ["call_id"]},
    },
    "phone_status": {
        "name": "phone_status",
        "description": "Status of one call or the recent call log.",
        "parameters": {"type": "object", "properties": {
            "call_id": _S, "limit": {"type": "integer", "default": 10}}},
    },
    "phone_transcript": {
        "name": "phone_transcript",
        "description": "Full transcript of a finished call. Only when the user asks.",
        "parameters": {"type": "object", "properties": {"call_id": _S}, "required": ["call_id"]},
    },
}
HANDLERS: dict[str, Callable[..., str]] = {
    "phone_prepare": phone_prepare, "phone_call": phone_call,
    "phone_status": phone_status, "phone_transcript": phone_transcript,
}
