"""Shared multi-tenant bridge: the ``/v1`` API (docs/BRIDGE_API.md), Telnyx webhooks and the
Telnyx media stream into ``CallSession``.

Every workspace row lives in ``bridge_calls``. The internal call id (``b`` + random hex) keys the
shared ``calls``/``events``/``questions`` tables, so a workspace can only reach a call through its
own ``(workspace, call_id)`` pair; any other id is a 404, exactly like a missing one.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from .. import brief as briefmod
from .. import consent, safety, states
from ..store import Store
from . import auth, business, telnyx_io
from . import realtime as rt
from .session import CallSession
from .settings import BridgeSettings
from .status import apply_status

log = logging.getLogger(__name__)

DISCLOSURE = "Hi, this is an AI assistant calling on behalf of {business_name}."
BUSINESS_MAX = 80
_URL_RE = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://\S*|www\.\S*|[\w-]+(?:\.[\w-]+)*"
                     r"\.(?:com|net|org|io|ai|co|app|dev|biz|info|us|me|xyz|link|ly)\b\S*)")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_NAME_DROP_RE = re.compile(r"[^A-Za-z0-9 &'.,-]")  # spoken and prompted names: allowlist only
CALL_OK_STATES = frozenset({states.DIALING, states.RINGING, states.IN_PROGRESS})
ALLOWANCE_FLOOR_S = 30  # less than this much monthly allowance left is no call
DURATION_MARGIN_S = 60  # carrier-side time limit = our cap + this, as a backstop only
DIALED_ABOUT_MAX = 200  # characters of the call goal sent with a dialed report (teamshift#2907)
PAGE = 500
CLIENT_PER_MIN = 60  # per client IP, before auth
WORKSPACE_PER_MIN = 60  # per verified workspace, after auth
AUTH_WORKERS = 8  # concurrent usage-token lookups; more get 503
SWEEP_S = 30.0
REAP_MARGIN_S = 30
RETENTION_S = 30 * 86400
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_APPROVAL_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_VENDOR_RE = re.compile(r"twilio|telnyx|openai|realtime|linq|hermes|gpt", re.I)

ERRORS: dict[str, tuple[int, str]] = {
    "unauthorized": (401, "Unknown or missing workspace credential."),
    "bad_signature": (403, "Invalid signature."),
    "workspace_disabled": (403, "Calling is turned off for this workspace."),
    "auth_unavailable": (503, "Sign-in check is unavailable. Try again shortly."),
    "rate_limited": (429, "Too many requests. Try again shortly."),
    "not_found": (404, "Not found."),
    "bad_request": (400, "The request is not valid."),
    "approval_required": (403, "A consumed, unexpired approval is required."),
    "conflict": (409, "This call or approval was already used."),
    "closed": (409, "That question is already closed."),
    "ended": (409, "The call is already over."),
    "not_configured": (503, "The phone service is not set up yet."),
    "blocked_number": (422, "This number cannot be called."),
    "not_allowlisted": (422, "This number is not on the allowed list."),
    "quiet_hours": (422, "It is outside calling hours (08:00 to 21:00) for the callee."),
    "active_call": (422, "Another call is already active for this workspace."),
    "daily_cap": (422, "The daily call limit for this workspace is reached."),
    "month_cap": (422, "This workspace has used this month's call time."),
    "allowance_unavailable": (503, "Call time for this workspace could not be checked right now."),
    "redial_too_soon": (422, "This number was just called. Automatic redial is not allowed."),
    "dial_failed": (502, "The call could not be placed."),
    "name_required": (422, "Say who the call is for: the person's or the business's name."),
    "hangup_unavailable": (503, "The call could not be ended right now. Try again shortly."),
}

LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS bridge_calls (
  internal_id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  call_id TEXT NOT NULL,
  approval_id TEXT NOT NULL,
  hook_secret_hash TEXT NOT NULL,
  store_transcripts INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  answered_at REAL,
  media_started_at REAL,
  ended_at REAL,
  stream_requested_at REAL,
  disclosure_requested_at REAL,
  handoff_at REAL,
  result_at REAL,
  UNIQUE(workspace, call_id),
  UNIQUE(workspace, approval_id)
);
CREATE INDEX IF NOT EXISTS idx_bridge_calls_ws ON bridge_calls(workspace, created_at);
"""
_LEDGER_COLUMNS = {"answered_at", "media_started_at", "ended_at", "stream_requested_at",
                   "disclosure_requested_at", "handoff_at", "result_at"}
_ADDED_COLUMNS = ("handoff_at", "result_at")  # business-call handoff (teamshift#2981): added to older ledgers


def error(code: str) -> JSONResponse:
    status, message = ERRORS[code]
    retry = {"Retry-After": "10"} if code in ("rate_limited", "auth_unavailable") else None
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers=retry)


def disclosure(business_name: str, record: bool = False) -> str:
    line = DISCLOSURE.format(business_name=business_name)
    return f"{line} {briefmod.RECORDED}" if record else line


def business_name(raw: Any) -> str:
    """Workspace-attested name the disclosure speaks: no URLs, only [A-Za-z0-9 &'.,-], <= 80."""
    if not isinstance(raw, str):
        return ""
    text = _NAME_DROP_RE.sub(" ", _URL_RE.sub(" ", _CTRL_RE.sub(" ", raw)))
    return " ".join(text.split())[:BUSINESS_MAX].strip()


def spoken_name(body: dict) -> str:
    """Who the disclosure says the call is for: the workspace's business_name, the brief's user_name, then a name fact.
    Placeholders ("my user", "a local business") never count; "" means the call cannot be made (#2872)."""
    b = body.get("brief") if isinstance(body.get("brief"), dict) else {}
    facts = b.get("facts") if isinstance(b.get("facts"), dict) else {}
    cands = [body.get("business_name"), b.get("user_name"), *(facts.get(k) for k in briefmod.NAME_FACTS)]
    return briefmod.resolve_name(*(business_name(c) for c in cands))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 - bad JSON, too deep, wrong encoding: a 400, never a 500
        return None


class Ledger:
    """Workspace-scoped rows on the store's connection and lock."""

    def __init__(self, store: Store) -> None:
        self.store = store
        with store._lock:
            store.conn.executescript(LEDGER_SQL)
            have = {r[1] for r in store.conn.execute("PRAGMA table_info(bridge_calls)")}
            for col in _ADDED_COLUMNS:
                if col not in have:
                    store.conn.execute(f"ALTER TABLE bridge_calls ADD COLUMN {col} REAL")
            store.conn.commit()

    def by_call(self, workspace: str, call_id: str) -> dict | None:
        return self.store._one("SELECT * FROM bridge_calls WHERE workspace=? AND call_id=?",
                               (workspace, call_id))

    def by_internal(self, internal_id: str) -> dict | None:
        return self.store._one("SELECT * FROM bridge_calls WHERE internal_id=?", (internal_id,))

    def approval_used(self, workspace: str, approval_id: str) -> bool:
        return self.store._one("SELECT 1 AS x FROM bridge_calls WHERE workspace=? AND "
                               "approval_id=?", (workspace, approval_id)) is not None

    def active(self, workspace: str) -> int:
        live = sorted(states.ACTIVE)
        marks = ",".join("?" * len(live))
        row = self.store._one(
            "SELECT COUNT(*) AS n FROM bridge_calls b JOIN calls c ON c.id=b.internal_id "
            f"WHERE b.workspace=? AND c.state IN ({marks})", (workspace, *live))
        return int(row["n"]) if row else 0

    def count_since(self, workspace: str, since: float) -> int:
        row = self.store._one("SELECT COUNT(*) AS n FROM bridge_calls WHERE workspace=? AND "
                              "created_at>=?", (workspace, since))
        return int(row["n"]) if row else 0

    def last_to(self, workspace: str, number: str) -> float | None:
        row = self.store._one(
            "SELECT MAX(b.created_at) AS t FROM bridge_calls b JOIN calls c ON "
            "c.id=b.internal_id WHERE b.workspace=? AND c.number=?", (workspace, number))
        return row["t"] if row and row["t"] is not None else None

    def insert(self, **row: Any) -> None:
        cols = ",".join(row)
        self.store._exec(f"INSERT INTO bridge_calls({cols}) VALUES({','.join('?' * len(row))})",
                         tuple(row.values()))

    def stamp(self, internal_id: str, column: str, value: float, *, once: bool = True) -> bool:
        assert column in _LEDGER_COLUMNS
        cond = f" AND {column} IS NULL" if once else ""
        cur = self.store._exec(f"UPDATE bridge_calls SET {column}=? WHERE internal_id=?{cond}",
                               (value, internal_id))
        return cur.rowcount == 1


def usage(row: dict, call: dict, now: float, settings: BridgeSettings) -> dict:
    """Answered seconds (telephony) and streamed seconds (voice), priced per started second."""
    end = row.get("ended_at") or (call.get("ended_at") if call["state"] in states.TERMINAL
                                  else None) or now

    def secs(start: float | None) -> int:
        return max(0, int(round(end - start))) if start is not None else 0

    tel, voice = secs(row.get("answered_at")), secs(row.get("media_started_at"))
    tel_usd = round(tel / 60 * settings.telephony_usd_per_min, 4)
    voice_usd = round(voice / 60 * settings.voice_usd_per_min, 4)
    return {"cost_usd": round(tel_usd + voice_usd, 4), "seconds": tel,
            "telephony_usd": tel_usd, "voice_usd": voice_usd}


def register(app: FastAPI, *, settings: BridgeSettings, store: Store,
             clock: Callable[[], float], connect: rt.RealtimeConnect, telnyx: Any,
             sessions: dict[str, CallSession]) -> None:
    try:
        overrides, config_error = auth.load_overrides(settings.overrides_raw), ""
    except ValueError as exc:
        overrides, config_error = {}, str(exc)  # fail closed: no new calls until fixed
        log.error("HERMES_PHONE_BRIDGE_OVERRIDES rejected: %s", exc)
    authn = auth.Authenticator(signing_key=settings.signing_key, auth_url=settings.auth_url,
                               overrides=overrides, clock=clock,
                               timeout_s=settings.auth_timeout_s)
    app.state.authenticator = authn
    ledger = Ledger(store)
    app.state.ledger = ledger
    # Separate pools: a slow auth service can never delay a dial or a hangup.
    auth_pool = ThreadPoolExecutor(AUTH_WORKERS, thread_name_prefix="bridge-auth")
    carrier_pool = ThreadPoolExecutor(8, thread_name_prefix="bridge-carrier")
    auth_slots = asyncio.Semaphore(AUTH_WORKERS)
    by_client = auth.Window(CLIENT_PER_MIN, clock)
    by_workspace = auth.Window(WORKSPACE_PER_MIN, clock)

    def client_ip(request: Request) -> str:
        if settings.trust_proxy:
            first = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            if first:
                return first
        return request.client.host if request.client else ""

    async def tenant_of(request: Request) -> tuple[auth.Workspace | None, Response | None]:
        # The per-IP limit counts only requests that cost work: a lookup for a token that is
        # neither cached nor known-good, or a failed auth. Verified traffic counts against its
        # workspace only, so workspaces sharing an egress IP cannot lock each other out.
        authz = request.headers.get("authorization") or ""
        claimed = request.headers.get(auth.WORKSPACE_HEADER) or ""
        counted = False
        if authz.partition(" ")[2].strip().startswith(auth.USAGE_PREFIX):  # network lookup
            if not authn.trusted(authz, claimed):
                counted = True
                if not by_client.hit(client_ip(request)):
                    return None, error("rate_limited")
            if auth_slots.locked():
                return None, error("auth_unavailable")
            async with auth_slots:
                ws, code = await asyncio.get_running_loop().run_in_executor(
                    auth_pool, authn.check, authz, claimed)
        else:
            ws, code = authn.check(authz, claimed)  # HMAC: no I/O
        if ws is None:
            if not counted and not by_client.hit(client_ip(request)):
                return None, error("rate_limited")
            return None, error(code)
        if not by_workspace.hit(ws.name):  # keyed on the verified name, never the claimed one
            return None, error("rate_limited")
        return ws, None

    async def in_carrier_pool(fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(carrier_pool, fn, *args)

    def not_ready() -> bool:
        return bool(config_error or settings.missing_env())

    async def owned(request: Request, call_id: str) -> tuple[Any, dict | None]:
        t, err = await tenant_of(request)
        if t is None:
            return err, None
        row = ledger.by_call(t.name, call_id) if _ID_RE.match(call_id) else None
        if row is None:
            return error("not_found"), None
        return t, row

    async def carrier(ccid: str, name: str, body: dict | None = None) -> bool:
        try:
            await in_carrier_pool(telnyx_io.action, telnyx, ccid, name, body)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("call action %s failed: %s", name, exc)
            return False

    async def send_digits(ccid: str, keys: str, command_id: str) -> bool:
        # command_id makes a retried command a no-op at the carrier (one press per tool call).
        return await carrier(ccid, "send_dtmf", {"digits": keys, "duration_millis": 250,
                                                 "command_id": command_id})

    def quiet_hangup(ccid: str) -> None:
        try:
            telnyx_io.action(telnyx, ccid, "hangup", None)
        except Exception as exc:  # noqa: BLE001
            log.warning("call action hangup failed: %s", exc)

    def verified(body: bytes, request: Request) -> dict | None:
        if not telnyx_io.verify_signature(settings.telnyx_public_key, body, request.headers,
                                          now=clock()):
            return None
        try:
            data = json.loads(body).get("data")
        except (ValueError, AttributeError):
            return None
        return data if isinstance(data, dict) else None

    # ---- plugin-facing API ------------------------------------------------------------
    @app.get("/v1/health")
    async def health(request: Request) -> Response:
        t, err = await tenant_of(request)
        if t is None:
            return err
        return error("not_configured") if not_ready() else JSONResponse({"ok": True})

    @app.put("/v1/calls/{call_id}")
    async def submit(call_id: str, request: Request) -> Response:
        t, err = await tenant_of(request)
        if t is None:
            return err
        if not_ready():
            return error("not_configured")
        body = await _json(request)
        if (not isinstance(body, dict) or not _ID_RE.match(call_id)
                or body.get("call_id", call_id) != call_id
                or not isinstance(body.get("brief"), dict)):
            return error("bad_request")
        appr = body.get("approval") if isinstance(body.get("approval"), dict) else {}
        aid = appr.get("id")
        consumed, expires = appr.get("consumed_at"), appr.get("expires_at")
        now = clock()
        if not isinstance(aid, str) or not _APPROVAL_RE.match(aid):
            return error("approval_required")
        with store._lock:  # check-then-create is atomic for this process
            row = ledger.by_call(t.name, call_id)
            if row is not None:  # a repeat answers even after the approval expired
                if row["approval_id"] != aid:
                    return error("conflict")
                call = store.get_call(row["internal_id"])
                return JSONResponse({"call_id": call_id, "state": call["state"],
                                     "bridge_call_id": row["internal_id"]})
            if (not _num(consumed) or not _num(expires) or expires <= consumed
                    or now >= expires):
                return error("approval_required")
            if ledger.approval_used(t.name, aid):
                return error("conflict")
            if not spoken_name(body):  # input, checked before any limit
                return error("name_required")
            code, e164 = _limits(t, body.get("number"), now)
            if code:
                return error(code)
            iid, secret, max_d = _create(t, call_id, aid, e164, body, now)
        if settings.business_call_url:
            return await hand_off(t, request, call_id, iid, secret, e164, max_d)
        if code := await dial_native(t, iid, secret, e164, max_d):
            return error(code)
        return JSONResponse({"call_id": call_id, "state": states.DIALING,
                             "bridge_call_id": iid}, status_code=201)

    async def dial_native(t: auth.Workspace, iid: str, secret: str, e164: str, max_d: int) -> str:
        """Dial the call on the bridge itself (Telnyx + Realtime): "" when placed, else the error code it was settled with."""
        key = ""
        if settings.room_url:
            # One monthly allowance per workspace, shared with its inbound calls. The key is the gw id the auth service returned (a
            # workspace cannot name another's); no answer, no gw id or no time left means no call. Fail closed.
            # A usage-token workspace (t-<10 hex>) is keyed by its gw id and has none until the auth service returns one; an
            # operator-minted token workspace is keyed by its name.
            key = t.gateway or ("" if auth.USAGE_WORKSPACE_RE.match(t.name) else t.name)
            granted = None
            if key:
                out = await in_carrier_pool(room_call, {"action": "admit", "workspace": key, "call_id": iid})
                granted = out.get("seconds") if isinstance(out, dict) and isinstance(out.get("seconds"), int) else None
            code = "allowance_unavailable" if granted is None else ("month_cap" if granted < ALLOWANCE_FLOOR_S else "")
            if code:
                store.set_result_once(iid, {"outcome": "failed", "reason": code, "summary": ERRORS[code][1]}, "failed")
                if states.can_transition(store.get_call(iid)["state"], states.FAILED):
                    states.transition(store, iid, states.FAILED, reason=code, outcome="failed")
                return code
            max_d = min(max_d, granted)
        hook = f"{settings.public_base_url}/v1/telnyx/calls/{iid}/{secret}"
        # The workspace's own verified number when it has one (enckequity/teamshift#2908), else the shared line.
        req = telnyx_io.dial_body(connection_id=settings.telnyx_connection_id, to=e164,
                                  from_=t.caller or settings.caller_number, webhook_url=hook,
                                  time_limit_s=max_d + DURATION_MARGIN_S)
        try:
            try:
                data = await in_carrier_pool(telnyx.post, "/calls", req)
            except telnyx_io.TelnyxError as exc:
                # A 403 D51 (caller ID not verified) is refused before anything rings, so one redial on the shared line is safe.
                # Every other error, and a timeout above all, may have placed the call and is never redialed.
                if req["from"] == settings.caller_number or not telnyx_io.unverified_caller(exc):
                    raise
                log.warning("workspace caller ID refused as unverified; dialing on the shared line")
                req = {**req, "from": settings.caller_number}
                data = await in_carrier_pool(telnyx.post, "/calls", req)
            ccid = str(data["data"]["call_control_id"])
        except Exception as exc:  # noqa: BLE001 - never redialed
            log.warning("dial failed: %s", exc)
            store.set_result_once(iid, {"outcome": "failed", "reason": "dial_failed",
                                        "summary": ERRORS["dial_failed"][1]}, "failed")
            if settings.room_url and isinstance(exc, telnyx_io.TelnyxError):
                # The carrier answered with an error, so nothing was dialed: give the reserved time back. A timeout may still have
                # placed the call, so that stays charged at the grant until it is reported.
                await in_carrier_pool(report_close, iid, t.name, 0)
            if states.can_transition(store.get_call(iid)["state"], states.FAILED):  # not aborted
                states.transition(store, iid, states.FAILED, reason="dial_failed",
                                  outcome="failed")
            return "dial_failed"
        store.update_call(iid, provider_sid=ccid)
        if settings.room_url and key:
            # A business that calls the shared line back reaches the workspace that called it (enckequity/teamshift#2907).
            # Sent once the carrier took the dial (even an unanswered call shows up as a missed call); best effort, off the loop.
            call = store.get_call(iid)
            about = " ".join(str(call.get("goal") or "").split())[:DIALED_ABOUT_MAX]
            in_background(report_dialed, {"action": "dialed", "call_id": iid, "workspace": key, "number": e164,
                                          "callee": str(call.get("callee_name") or "")[:200], "about": about})
        if store.hangup_requested(iid):  # the user hung up while we were dialing
            await carrier(ccid, "hangup")
        return ""

    def fail_call(iid: str, code: str) -> None:
        store.set_result_once(iid, {"outcome": "failed", "reason": code, "summary": ERRORS[code][1]}, "failed")
        if states.can_transition(store.get_call(iid)["state"], states.FAILED):  # not aborted, not already settled
            states.transition(store, iid, states.FAILED, reason=code, outcome="failed")

    async def hand_off(t: auth.Workspace, request: Request, call_id: str, iid: str, secret: str, e164: str,
                       max_d: int) -> Response:
        """Place the call on linq-voice's GPT-Live stack (teamshift#2981). linq-voice meters the allowance (out_<iid>) and
        posts exactly one result to the URL below; the request is never retried."""
        key = t.gateway or ("" if auth.USAGE_WORKSPACE_RE.match(t.name) else t.name)  # same key as the allowance
        if not key:
            fail_call(iid, "allowance_unavailable")
            return error("allowance_unavailable")
        call = store.get_call(iid)
        payload = briefmod.voice_payload(call, store.consents(iid))
        # The workspace's own number only with its own usage token: linq-voice checks both against the portal (#2992).
        token = (request.headers.get("authorization") or "").partition(" ")[2].strip()
        caller_auth = ({"name": t.name, "token": token} if t.caller and auth.USAGE_TOKEN_RE.match(token)
                       and auth.USAGE_WORKSPACE_RE.match(t.name) else None)
        req = business.request(iid=iid, workspace=key, number=e164, payload=payload, max_seconds=max_d,
                               result_url=f"{settings.business_result_base}/v1/business/results/{iid}/{secret}",
                               caller_number=t.caller if caller_auth else "", caller_auth=caller_auth)
        ledger.stamp(iid, "handoff_at", clock())  # before the post: the result can arrive before its reply
        dialed = {"action": "dialed", "call_id": iid, "workspace": key, "number": e164,
                  "callee": str(call.get("callee_name") or "")[:200],
                  "about": " ".join(str(call.get("goal") or "").split())[:DIALED_ABOUT_MAX]}
        # The voice service answers only once the dial is placed (up to ~25 s), longer than the plugin waits for this PUT, so it
        # is asked in the background; a refusal becomes this call's failed result in updates.
        task = asyncio.ensure_future(deliver(t, iid, secret, e164, max_d, req, dialed))
        background.add(task)
        task.add_done_callback(background.discard)
        return JSONResponse({"call_id": call_id, "state": states.DIALING, "bridge_call_id": iid}, status_code=201)

    async def deliver(t: auth.Workspace, iid: str, secret: str, e164: str, max_d: int, req: dict,
                      dialed: dict) -> None:
        status, reply = await in_carrier_pool(business.post, settings.business_call_url, settings.room_key, req,
                                              settings.business_timeout_s)
        if business.unavailable(status, reply):
            # Business calls are off at the voice service (no `business` block: 404, or an explicit "disabled"). Nothing
            # was dialed there, so this call is dialed here instead: the voice config is the one on/off switch (#2872).
            store._exec("UPDATE bridge_calls SET handoff_at=NULL WHERE internal_id=?", (iid,))
            store.add_event(iid, "handoff_unavailable", {"status": status})
            if store.get_call(iid)["state"] in states.TERMINAL:  # the user hung up meanwhile: settled, never dialed
                return
            await dial_native(t, iid, secret, e164, max_d)  # settles its own failures
            return
        hung = store.hangup_requested(iid)  # the user hung up while the voice service had the request
        if status == 0:
            # No answer: the call may still have been placed, and it is never re-sent. It stays dialing until its result
            # arrives or the reaper ends it.
            log.warning("business call %s: no reply from the voice service; waiting for its result", iid)
            store.add_event(iid, "handoff_unconfirmed", {})
            if hung:
                await in_carrier_pool(cancel_there, iid)  # it may still dial: a 404 there is harmless
            return
        if code := business.refusal(status, reply):
            log.warning("business call %s refused (%s %s)", iid, status, str(reply.get("state") or "")[:20])
            if hung:  # cancelled there before it dialed: the user's hangup, not a failure
                settle_hangup(iid)
            else:
                fail_call(iid, code)
            ledger.stamp(iid, "ended_at", clock())
            return
        store.add_event(iid, "handoff", {})
        if hung:  # accepted after the hangup: cancel it now
            await in_carrier_pool(cancel_there, iid)
        await in_carrier_pool(report_dialed, dialed)

    def cancel_there(iid: str) -> int:
        status = 0
        for _ in range(2):  # once more after no answer or a server error
            status = business.cancel(settings.business_call_url, settings.room_key, iid, settings.business_timeout_s)
            if status in (200, 404, 409, 400):
                break
        if status != 200:
            log.warning("business call %s: cancel answered %s", iid, status)
        store.add_event(iid, "cancel_sent", {"status": status})
        return status

    def settle_hangup(iid: str) -> None:
        store.set_result_once(iid, {"outcome": "needs_followup", "reason": "user_hangup",
                                    "summary": "The user ended the call."}, "needs_followup")
        if states.can_transition(store.get_call(iid)["state"], states.ABORTED):
            states.transition(store, iid, states.ABORTED, reason="user_hangup")

    @app.post("/v1/business/results/{iid}/{secret}")
    async def business_result(iid: str, secret: str, request: Request) -> Response:
        """linq-voice's one result for a handed-off call. No credential: the per-call secret in the path is the proof.
        A repeated call_id (re-sent after a restart) is acknowledged and ignored."""
        row = ledger.by_internal(iid) if _ID_RE.match(iid) else None
        if (row is None or not row.get("handoff_at")
                or not hmac.compare_digest(_sha(secret), row["hook_secret_hash"])):
            return error("not_found")
        body = await _json(request)
        res = business.parse_result(body)
        if res is None or (isinstance(body, dict) and body.get("call_id") not in (None, iid)):
            return error("bad_request")
        now = clock()
        if not ledger.stamp(iid, "result_at", now):
            return JSONResponse({"ok": True, "duplicate": True})
        kind = business.outcome_of(res)
        result: dict[str, Any] = {"outcome": kind, "summary": res["outcome"], "reason": f"business_{res['state']}",
                                  "next_steps": res["next_steps"][:1000], "duration_s": res["seconds"]}
        if res["agreed"].strip():
            result["agreed"] = {"notes": res["agreed"]}
        store.set_result_once(iid, result, kind)
        if res["seconds"] and res["state"] in ("completed", "machine"):
            ledger.stamp(iid, "answered_at", now - res["seconds"])
            ledger.stamp(iid, "media_started_at", now - res["seconds"])
        store.update_call(iid, duration_s=res["seconds"])
        if row["store_transcripts"] and settings.store_transcripts:
            for role, text in business.transcript_lines(res["transcript_tail"]):
                store.add_transcript_line(iid, role, text)
        for step in business.path_of(res):
            cur = store.get_call(iid)
            if states.can_transition(cur["state"], step):
                fields: dict[str, Any] = {"reason": f"business_{res['state']}"} if step in states.TERMINAL else {}
                if step in states.TERMINAL and not cur.get("outcome"):
                    fields["outcome"] = kind
                states.transition(store, iid, step, **fields)
        ledger.stamp(iid, "ended_at", now)
        store.add_event(iid, "business_result", {"state": res["state"]})
        return JSONResponse({"ok": True})

    background: set[Any] = set()  # keeps allowance reports alive until they finish

    def room_call(body: dict) -> dict | None:
        """POST to the allowance service (loopback, bearer = member_pin_key); the parsed reply, or None when it could not answer."""
        req = urllib.request.Request(settings.room_url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {settings.room_key}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=settings.room_timeout_s) as resp:  # noqa: S310
                out = json.loads(resp.read(4096).decode("utf-8", "replace"))
            return out if isinstance(out, dict) else None
        except Exception:  # noqa: BLE001 - any failure to ask or parse means no answer
            return None

    def in_background(fn: Callable[..., Any], *args: Any) -> None:
        task = asyncio.ensure_future(in_carrier_pool(fn, *args))
        background.add(task)
        task.add_done_callback(background.discard)

    def report_dialed(body: dict) -> None:
        if room_call(body) is None:
            log.warning("dialed report for %s not delivered; a callback reaches the public line only", body["call_id"])

    def report_close(call_id: str, workspace: str, seconds: int) -> None:
        if settings.room_url:
            if room_call({"action": "close", "call_id": call_id, "seconds": max(0, seconds)}) is None:
                log.warning("allowance close for %s not delivered; it stays charged at what was granted", workspace)

    def _limits(t: auth.Workspace, number: Any, now: float) -> tuple[str, str]:
        e164, _ = safety.validate_number(number)
        if e164 is None:
            return "blocked_number", ""
        if not safety.check_allowlist(e164, settings.environ).ok:
            return "not_allowlisted", ""
        if not safety.check_quiet_hours(e164, datetime.fromtimestamp(now, UTC),
                                        fallback_tz=t.tz).ok:
            return "quiet_hours", ""
        if ledger.active(t.name):
            return "active_call", ""
        if ledger.count_since(t.name, now - 86400) >= t.daily_cap:
            return "daily_cap", ""
        last = ledger.last_to(t.name, e164)
        if last is not None and now - last < safety.MIN_GAP_SAME_NUMBER_S:
            return "redial_too_soon", ""
        return "", e164

    def _create(t: auth.Workspace, call_id: str, aid: str, e164: str, body: dict,
                now: float) -> tuple[str, str, int]:
        b = body["brief"]
        lim = body.get("limits") if isinstance(body.get("limits"), dict) else {}
        max_d = min(briefmod.clamp_duration(lim.get("max_duration_s")),
                    briefmod.clamp_duration(b.get("max_duration_s")))
        facts = b.get("facts") if isinstance(b.get("facts"), dict) else {}
        record = bool(b.get("record"))
        business = spoken_name(body)  # checked non-empty by submit
        stored = {
            "opening_line": disclosure(business, record),  # the bridge's, never the plugin's
            "facts_raw": facts,
            "limits": {"max_duration_s": max_d, "recording": record,
                       "voicemail_message": bool(b.get("leave_voicemail")),
                       "question_timeout_s": briefmod.clamp_question_timeout(
                           lim.get("question_timeout_s"))},
            "language": str(b.get("language") or "en")[:16],
            "user_name": briefmod.resolve_name(business_name(b.get("user_name"))) or business,
        }
        iid = "b" + secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)  # 43 chars
        store.create_call(iid, state=states.APPROVED,
                          callee_name=str(b.get("callee_name") or "the business")[:200],
                          number=e164, number_source="bridge", goal=str(b.get("goal") or "")[:2000],
                          brief=stored, user_id=t.name)
        # The plugin sends consented facts only; record those consents so the voice payload
        # keeps them. Forbidden fields stay dropped by build_safe_facts.
        for key, value in facts.items():
            field = consent.field_for_fact(key, value)
            if field in consent.SENSITIVE_FIELDS:
                store.grant_consent(iid, field, t.name)
        opts = body.get("options") if isinstance(body.get("options"), dict) else {}
        ledger.insert(internal_id=iid, workspace=t.name, call_id=call_id, approval_id=aid,
                      hook_secret_hash=_sha(secret),
                      store_transcripts=0 if opts.get("store_transcripts") is False else 1,
                      created_at=now)
        states.transition(store, iid, states.DIALING, dialed_at=now)
        return iid, secret, max_d

    @app.get("/v1/calls/{call_id}/updates")
    async def updates(call_id: str, request: Request, cursor: str = "") -> Response:
        t, row = await owned(request, call_id)
        if row is None:
            return t
        iid = row["internal_id"]
        ev0, tr0, q0 = _parse_cursor(cursor)
        now = clock()
        events = store._all("SELECT id,ts,kind,data_json FROM events WHERE call_id=? AND id>? "
                            "ORDER BY id LIMIT ?", (iid, ev0, PAGE))
        lines = store._all("SELECT id,ts,role,text FROM transcripts WHERE call_id=? AND id>? "
                           "ORDER BY id LIMIT ?", (iid, tr0, PAGE))
        qs = [q for q in store.questions_for_call(iid)
              if q["created_at"] >= q0 or (q.get("answered_at") or 0) >= q0]
        call = store.get_call(iid)
        assert call is not None
        result = call["result"] if call["state"] in states.TERMINAL else None
        out = {
            "cursor": f"{events[-1]['id'] if events else ev0}."
                      f"{lines[-1]['id'] if lines else tr0}.{now!r}",
            "call": {"state": call["state"], "outcome": call.get("outcome"),
                     "reason": call.get("reason"),
                     "duration_s": call.get("duration_s"),
                     "disclosure_spoken": (None if call.get("disclosure_spoken") is None
                                           else bool(call["disclosure_spoken"])),
                     "result": result,
                     "usage": usage(row, call, now, settings)},
            "events": [{"id": f"e{e['id']}", "kind": e["kind"], "ts": e["ts"],
                        "data": json.loads(e["data_json"] or "{}")}
                       for e in events if not _VENDOR_RE.search(e["kind"])],
            "questions": [{"id": q["id"], "question": q["question"], "field": q["field"] or "",
                           "suggested": q["suggested"] or "", "kind": q["kind"],
                           "consent_field": q["consent_field"] or None,
                           "status": "timeout" if q["status"] == "late" else q["status"]}
                          for q in qs],
            "transcript": ([{"id": f"t{x['id']}", "role": x["role"], "text": x["text"],
                             "ts": x["ts"]} for x in lines]
                           if row["store_transcripts"] else []),
        }
        return JSONResponse(out)

    @app.post("/v1/calls/{call_id}/questions/{question_id}/answer")
    async def answer(call_id: str, question_id: str, request: Request) -> Response:
        t, row = await owned(request, call_id)
        if row is None:
            return t
        q = store.get_question(question_id) if _ID_RE.match(question_id) else None
        if q is None or q["call_id"] != row["internal_id"]:
            return error("not_found")
        body = await _json(request)
        status = body.get("status") if isinstance(body, dict) else None
        text = body.get("answer") if isinstance(body, dict) else None
        if status not in ("answered", "declined") or (
                status == "answered" and (not isinstance(text, str) or len(text) > 2000)):
            return error("bad_request")
        if q["status"] != "open":
            return error("closed")
        if status == "answered" and q.get("consent_field"):
            store.grant_consent(q["call_id"], q["consent_field"], t.name)  # before resolve
        got = store.resolve_question(question_id, status, text if status == "answered" else None)
        return JSONResponse({"ok": True}) if got == status else error("closed")

    @app.post("/v1/calls/{call_id}/hangup")
    async def hangup(call_id: str, request: Request) -> Response:
        t, row = await owned(request, call_id)
        if row is None:
            return t
        iid = row["internal_id"]
        call = store.get_call(iid)
        if call is None or call["state"] in states.TERMINAL:
            return error("ended")
        if row.get("handoff_at"):  # driven by linq-voice: it ends the call and its result says cancelled
            store.request_hangup(iid)  # a hand-off still in flight cancels as soon as it is accepted
            status = await in_carrier_pool(business.cancel, settings.business_call_url, settings.room_key, iid,
                                           settings.business_timeout_s)
            if status == 200:
                store.add_event(iid, "cancel_requested", {})
                return JSONResponse({"ok": True})
            if status == 409:  # already ended or settled there: its result is on the way
                return error("ended")
            if status == 404 and ledger.stamp(iid, "result_at", clock()):  # never placed there: no result will come
                settle_hangup(iid)
                ledger.stamp(iid, "ended_at", clock())
                return JSONResponse({"ok": True})
            if status == 404:
                return error("ended")
            return error("hangup_unavailable")
        store.request_hangup(iid)
        if iid not in sessions:  # no voice session ticking yet: end it here
            store.set_result_once(iid, {"outcome": "needs_followup", "reason": "user_hangup",
                                        "summary": "The user ended the call."}, "needs_followup")
            if states.can_transition(call["state"], states.ABORTED):
                states.transition(store, iid, states.ABORTED, reason="user_hangup")
            if call.get("provider_sid"):
                await carrier(call["provider_sid"], "hangup")
        return JSONResponse({"ok": True})

    # ---- carrier webhooks -------------------------------------------------------------
    @app.post("/v1/telnyx/calls/{iid}/{secret}")
    async def call_event(iid: str, secret: str, request: Request) -> Response:
        data = verified(await request.body(), request)
        if data is None:
            return error("bad_signature")
        row = ledger.by_internal(iid) if _ID_RE.match(iid) else None
        if row is None or not hmac.compare_digest(row["hook_secret_hash"], _sha(secret)):
            return error("not_found")
        call = store.get_call(iid)
        assert call is not None
        etype = str(data.get("event_type") or "")
        p = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        ccid = str(p.get("call_control_id") or "")
        if call.get("provider_sid") and ccid and ccid != call["provider_sid"]:
            return JSONResponse({"ok": True, "result": "ignored"})
        if ccid and not call.get("provider_sid"):  # the dial response was lost or is late
            store.update_call(iid, provider_sid=ccid)
        store.add_event(iid, "carrier_event", {"type": etype[:64]})
        now = clock()
        ended = call["state"] in states.TERMINAL or call.get("hangup_requested_at") is not None
        if ended and etype != "call.hangup" and iid not in sessions:
            if ccid:
                await carrier(ccid, "hangup")  # we wrote this call off: it must not stay live
            return JSONResponse({"ok": True, "result": "ended"})
        if etype == "call.answered":
            ledger.stamp(iid, "answered_at", now)
            apply_status(store, iid, {"CallStatus": "in-progress", "CallSid": ccid})
        elif etype in telnyx_io.AMD_EVENTS:
            await _on_amd(iid, call, ccid, str(p.get("result") or "").lower())
        elif etype in telnyx_io.GREETING_EVENTS:
            fresh = store.get_call(iid)
            if fresh and fresh["state"] == states.VOICEMAIL and _leaves(fresh):
                await _disclose(iid, ccid)
        elif etype == "call.speak.ended":
            if (ledger.by_internal(iid) or row).get("disclosure_requested_at") is None:
                pass  # no carrier disclosure was requested on this call (a person hears the model's)
            elif str(p.get("status") or "") == "completed":
                store.set_disclosure(iid, True)  # only a fully played disclosure counts
                await _start_stream(iid, ccid)
            else:  # cut off: the voice model never talks to someone who was not told
                store.add_event(iid, "disclosure_incomplete", {})
                await carrier(ccid, "hangup")
        elif etype == "streaming.stopped" and iid in sessions:
            sessions[iid].notify_stream_lost()
            await carrier(ccid, "hangup")
        elif etype == "call.hangup":
            ledger.stamp(iid, "ended_at", now)
            fresh = ledger.by_internal(iid) or row
            answered = fresh.get("answered_at") is not None
            secs = int(round(now - fresh["answered_at"])) if answered else 0
            if settings.room_url:  # meter this call against the workspace's allowance (never blocks the event loop)
                in_background(report_close, iid, str(fresh.get("workspace")), secs)
            apply_status(store, iid, {
                "CallStatus": telnyx_io.hangup_status(str(p.get("hangup_cause") or ""), answered),
                "CallSid": ccid, "CallDuration": str(secs)})
        return JSONResponse({"ok": True})

    def _listens(row: dict, call: dict) -> bool:
        """A stream that was started without a spoken disclosure (menu or machine): listening.
        From the ledger, so a restart or another worker gets the same answer."""
        fresh = ledger.by_internal(row["internal_id"]) or row
        return (fresh.get("disclosure_requested_at") is not None
                and not call.get("disclosure_spoken") and call["state"] != states.VOICEMAIL)

    def _leaves(call: dict) -> bool:
        return bool(call["brief"]["limits"].get("voicemail_message"))

    async def _on_amd(iid: str, call: dict, ccid: str, result: str) -> None:
        store.add_event(iid, "answering_machine", {"result": result[:32]})
        if result in telnyx_io.MACHINE_RESULTS and not _leaves(call):
            # A business's phone menu sounds like a machine. Listen silently (no disclosure yet,
            # the model's audio is dropped): it presses keys through the menu, and a person who
            # picks up hears the forced opening with the disclosure first. A voicemail greeting
            # ends the call without a message.
            if ledger.stamp(iid, "disclosure_requested_at", clock()):  # media sees: not spoken
                store.add_event(iid, "listen_start", {})
                await _start_stream(iid, ccid)
            return
        if result in telnyx_io.MACHINE_RESULTS or result.startswith("fax"):
            apply_status(store, iid, {"AnsweredBy": "machine_start", "CallSid": ccid})
            if not (_leaves(call) and result in telnyx_io.MACHINE_RESULTS):
                await carrier(ccid, "hangup")  # a fax line: nothing to say to it
            return  # leaving a message: the stream starts once the greeting ended
        # A person (or cannot tell): the stream starts now and the model's forced opening says the
        # disclosure word for word in its own voice, the first audio the person hears. The carrier's
        # robotic speech plus the stream start after it added about 6.5 s of mismatched audio and
        # silence (#2872); the carrier still speaks it only before a voicemail message.
        await _start_stream(iid, ccid)

    async def _disclose(iid: str, ccid: str) -> None:
        """Play the fixed disclosure with the carrier's own speech before any model audio."""
        if not ledger.stamp(iid, "disclosure_requested_at", clock()):
            return  # duplicate webhook
        line = store.get_call(iid)["brief"]["opening_line"]
        if not await carrier(ccid, "speak", {"payload": line, "voice": "female",
                                             "language": "en-US"}):
            store.add_event(iid, "disclosure_failed", {})
            await carrier(ccid, "hangup")

    async def _start_stream(iid: str, ccid: str) -> None:
        if not ledger.stamp(iid, "stream_requested_at", clock()):
            return  # duplicate webhook
        token = store.create_media_token(iid, settings.token_ttl_s)
        url = f"{settings.ws_base_url()}/v1/media/{iid}?token={token}"
        if not await carrier(ccid, "streaming_start", telnyx_io.stream_body(url)):
            store.add_event(iid, "stream_failed", {})
            await carrier(ccid, "hangup")

    async def inbound(request: Request) -> Response:
        data = verified(await request.body(), request)
        if data is None:
            return error("bad_signature")
        # shortcut: every callback hears one fixed line and is hung up. Routing a callback to
        # the workspace that called that business (or the concierge) is enckequity/teamshift#1089.
        etype = str(data.get("event_type") or "")
        p = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        ccid = str(p.get("call_control_id") or "")
        if not ccid:
            return JSONResponse({"ok": True})
        if etype == "call.initiated" and p.get("direction", "incoming") == "incoming":
            await carrier(ccid, "answer")
        elif etype == "call.answered":
            await carrier(ccid, "speak", {"payload": telnyx_io.INBOUND_LINE, "voice": "female",
                                          "language": "en-US"})
        elif etype == "call.speak.ended":
            await carrier(ccid, "hangup")
        return JSONResponse({"ok": True})

    # Off by default: inbound calls on our number belong to the existing voice service.
    if settings.inbound_enabled:
        app.add_api_route("/v1/telnyx/inbound", inbound, methods=["POST"])

    # ---- media ------------------------------------------------------------------------
    @app.websocket("/v1/media/{iid}")
    async def media(ws: WebSocket, iid: str) -> None:
        from .app import _await_start, _close  # lazy: app imports this module

        token = ws.query_params.get("token") or ""
        row = ledger.by_internal(iid) if _ID_RE.match(iid) else None
        if row is None or not token or len(token) > 200 or not store.consume_media_token(
                iid, token):  # refused before the socket is accepted
            if row is not None:
                store.add_event(iid, "media_refused", {"reason": "bad_token"})
            await _close(ws, 1008)
            return
        await ws.accept()
        start = await _await_start(ws, settings.start_timeout_s)
        reason = _media_refusal(row, iid, start)
        if reason:
            if row is not None:
                store.add_event(iid, "media_refused", {"reason": reason})
            await _close(ws, 1008)
            return
        assert start is not None and row is not None
        call = store.get_call(iid)
        assert call is not None
        if call["state"] in (states.DIALING, states.RINGING):
            states.transition(store, iid, states.IN_PROGRESS)
            call = store.get_call(iid)
        ledger.stamp(iid, "media_started_at", clock())
        info = start.get("start") or {}
        ccid = str(info.get("call_control_id") or call.get("provider_sid") or "")
        per_call = dataclasses.replace(
            settings, store_transcripts=settings.store_transcripts and bool(
                row["store_transcripts"]))
        session = CallSession(ws=telnyx_io.MediaSocket(ws), call=call, store=store,
                              settings=per_call, connect=connect,
                              twilio_client_factory=lambda: telnyx_io.Hangup(telnyx),
                              clock=clock, disclosed=bool(call.get("disclosure_spoken")),
                              press_keys=(lambda keys, cid: send_digits(ccid, keys, cid))
                              if ccid else None,
                              listening=_listens(row, call))
        if call["state"] == states.VOICEMAIL:
            session.notify_voicemail("machine_end_beep")  # stream began after the greeting
        sessions[iid] = session
        try:
            await session.run({"streamSid": str(start.get("stream_id") or "stream"),
                               "callSid": ccid})
        finally:
            sessions.pop(iid, None)
            if ccid:  # a dropped stream must not leave the line open (hangup is idempotent);
                carrier_pool.submit(quiet_hangup, ccid)  # submitted, so even cancellation sends it
            await _close(ws, 1000)

    def _media_refusal(row: dict | None, iid: str, start: dict | None) -> str:
        if row is None:
            return "unknown_call"
        if not start or not isinstance(start.get("start"), dict):
            return "no_start"
        call = store.get_call(iid)
        if call is None:
            return "unknown_call"
        ok = CALL_OK_STATES | ({states.VOICEMAIL} if _leaves(call) else set())
        if call["state"] not in ok:
            return f"state_{call['state']}"
        info = start["start"]
        fmt = info.get("media_format") or {}
        try:
            if fmt and (str(fmt.get("encoding", "")).upper() != "PCMU"
                        or int(fmt.get("sample_rate", 8000)) != 8000):
                return "bad_media_format"
        except (AttributeError, TypeError, ValueError):
            return "bad_media_format"
        ccid = str(info.get("call_control_id") or "")
        if call.get("provider_sid") and ccid and ccid != call["provider_sid"]:
            return "call_mismatch"
        return ""

    # ---- sweeps -----------------------------------------------------------------------
    def reap(*, restart: bool = False) -> int:
        """End live calls nobody is driving: all of them after a restart, otherwise those past
        cap + ring time + margin. Hangs up at the carrier and marks them failed."""
        now = clock()
        live = sorted(states.ACTIVE)
        rows = store._all(
            "SELECT b.internal_id, b.created_at, b.handoff_at FROM bridge_calls b JOIN calls c ON "
            f"c.id=b.internal_id WHERE c.state IN ({','.join('?' * len(live))})", tuple(live))
        n = 0
        for r in rows:
            iid = r["internal_id"]
            if not restart and iid in sessions:
                continue  # driven by this process: its own ticker enforces the cap
            call = store.get_call(iid)
            limit = int(call["brief"]["limits"]["max_duration_s"])
            if r["handoff_at"]:  # linq-voice caps at 300 s and may still ring, say goodbye and post after that
                limit = min(limit, business.MAX_SECONDS) + business.TAIL_S - telnyx_io.DIAL_TIMEOUT_S - REAP_MARGIN_S
            stale = now >= r["created_at"] + limit + telnyx_io.DIAL_TIMEOUT_S + REAP_MARGIN_S
            if (not restart or r["handoff_at"]) and not stale:
                continue  # a handed-off call is driven by linq-voice, across our restarts too
            reason = "bridge_restart" if restart else "stale_call"
            if call.get("provider_sid"):
                try:
                    telnyx_io.action(telnyx, call["provider_sid"], "hangup", None)
                except Exception as exc:  # noqa: BLE001 - mark it anyway: never stuck active
                    log.warning("reaper hangup failed: %s", exc)
            store.set_result_once(iid, {"outcome": "failed", "reason": reason,
                                        "summary": "The call was interrupted."}, "failed")
            fresh = store.get_call(iid)
            if states.can_transition(fresh["state"], states.FAILED):
                states.transition(store, iid, states.FAILED, reason=reason, outcome="failed")
            elif states.can_transition(fresh["state"], states.ABORTED):  # approved, never dialed
                states.transition(store, iid, states.ABORTED, reason=reason)
            ledger.stamp(iid, "ended_at", now)
            n += 1
        return n

    def retention() -> None:
        """Daily: carrier/other events, media tokens and transcripts older than their keep."""
        now = clock()
        keep_tr = (settings.transcript_retention_days * 86400 if settings.store_transcripts
                   else RETENTION_S)
        ids = "SELECT internal_id FROM bridge_calls"
        store._exec(f"DELETE FROM events WHERE ts<? AND call_id IN ({ids})", (now - RETENTION_S,))
        store._exec(f"DELETE FROM media_tokens WHERE created_at<? AND call_id IN ({ids})",
                    (now - RETENTION_S,))
        store._exec(f"DELETE FROM transcripts WHERE ts<? AND call_id IN ({ids})",
                    (now - keep_tr,))

    async def sweeper() -> None:
        last_retention = -math.inf
        while True:
            try:
                await in_carrier_pool(lambda: reap())
                if clock() - last_retention >= 86400:
                    last_retention = clock()
                    retention()
            except Exception:  # noqa: BLE001 - keep sweeping
                log.exception("bridge sweep failed")
            await asyncio.sleep(SWEEP_S)

    app.state.reap, app.state.retention, app.state.sweeper = reap, retention, sweeper
    restarted = reap(restart=True)  # nothing from before this process is being driven
    if restarted:
        log.warning("ended %d calls left live by a previous bridge process", restarted)


def _parse_cursor(cursor: str) -> tuple[int, int, float]:
    try:
        ev, tr, ts = cursor.split(".", 2)
        out = int(ev), int(tr), float(ts)
    except (ValueError, AttributeError):
        return 0, 0, 0.0
    return out if max(out[0], out[1]) < 2**62 and math.isfinite(out[2]) else (0, 0, 0.0)
