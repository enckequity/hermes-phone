"""FastAPI app factory for the media bridge. Everything external is injectable."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .. import safety, states
from ..store import Store
from . import realtime as rt
from . import telnyx_io, twilio_io, v1, webroutes
from .session import CallSession
from .settings import BridgeSettings
from .status import apply_status

log = logging.getLogger(__name__)

TWIML_OK_STATES = frozenset({states.DIALING, states.RINGING, states.IN_PROGRESS})


def create_app(settings: BridgeSettings, *, store: Store | None = None,
               realtime_connect: rt.RealtimeConnect | None = None,
               twilio_client: Any = None, clock: Callable[[], float] | None = None,
               telnyx_client: Any = None) -> FastAPI:
    app = FastAPI(title="hermes-phone bridge", docs_url=None, redoc_url=None, openapi_url=None)
    the_store = store if store is not None else Store(path=settings.db_path or None)
    the_clock = clock or the_store.clock
    connect = realtime_connect or rt.connect_openai
    sessions: dict[str, CallSession] = {}
    client_holder: dict[str, Any] = {}

    def get_client() -> Any:
        if twilio_client is not None:
            return twilio_client
        if "c" not in client_holder:
            client_holder["c"] = twilio_io.make_client(settings.twilio_account_sid,
                                                       settings.twilio_auth_token)
        return client_holder["c"]

    app.state.store = the_store
    app.state.sessions = sessions
    app.state.settings = settings

    async def verified_form(request: Request) -> dict[str, str] | None:
        params = twilio_io.parse_form(await request.body())
        url = twilio_io.public_url(settings.public_base_url, request.url.path,
                                   request.url.query)
        sig = request.headers.get("X-Twilio-Signature")
        if twilio_io.valid_signature(settings.twilio_auth_token, url, params, sig):
            return params
        return None

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    if settings.shared:  # multi-tenant: only the workspace-scoped /v1 surface exists
        v1.register(app, settings=settings, store=the_store, clock=the_clock, connect=connect,
                    telnyx=telnyx_client or telnyx_io.TelnyxClient(settings.telnyx_api_key),
                    sessions=sessions)
        return app

    @app.post("/twiml/{call_id}")
    async def twiml(call_id: str, request: Request) -> Response:
        if await verified_form(request) is None:
            return PlainTextResponse("invalid signature", status_code=403)
        refusal = _twiml_refusal(the_store, call_id, settings)
        if refusal:
            if the_store.get_call(call_id):
                the_store.add_event(call_id, "twiml_refused", {"reason": refusal})
            return PlainTextResponse("refused", status_code=403)
        token = the_store.create_media_token(call_id, settings.token_ttl_s)
        ws_url = f"{settings.ws_base_url()}/media/{call_id}"
        return Response(twilio_io.stream_twiml(ws_url, token), media_type="text/xml")

    @app.post("/status/{call_id}")
    async def status(call_id: str, request: Request) -> Response:
        params = await verified_form(request)
        if params is None:
            return PlainTextResponse("invalid signature", status_code=403)
        if the_store.get_call(call_id) is None:
            return PlainTextResponse("unknown call", status_code=404)

        def on_voicemail(answered_by: str) -> None:
            session = sessions.get(call_id)
            if session is not None:
                session.notify_voicemail(answered_by)
            elif not _leaves_voicemail(the_store, call_id) and params.get("CallSid"):
                _bg_hangup(get_client, params["CallSid"])

        note = apply_status(the_store, call_id, params, on_voicemail=on_voicemail)
        return JSONResponse({"ok": True, "result": note})

    webroutes.register(app, settings=settings, store=the_store, clock=the_clock,
                       refusal=lambda cid: _twiml_refusal(the_store, cid, settings))

    @app.websocket("/media/{call_id}")
    async def media(ws: WebSocket, call_id: str) -> None:
        await ws.accept()
        start = await _await_start(ws, settings.start_timeout_s)
        reason = _ws_refusal(the_store, call_id, start)
        if reason:
            if the_store.get_call(call_id):
                the_store.add_event(call_id, "media_refused", {"reason": reason})
            await _close(ws, 1008)
            return
        assert start is not None
        info = start["start"]
        call = the_store.get_call(call_id)
        assert call is not None
        if call["state"] in (states.DIALING, states.RINGING):
            states.transition(the_store, call_id, states.IN_PROGRESS)
        if info.get("callSid") and not call.get("provider_sid"):
            the_store.update_call(call_id, provider_sid=info["callSid"])
            call = the_store.get_call(call_id)
        session = CallSession(ws=ws, call=call, store=the_store, settings=settings,
                              connect=connect, twilio_client_factory=get_client, clock=the_clock)
        sessions[call_id] = session
        try:
            await session.run({**info, "streamSid": info.get("streamSid") or start.get(
                "streamSid", "")})
        finally:
            sessions.pop(call_id, None)
            await _close(ws, 1000)

    return app


# ---- helpers ----------------------------------------------------------------------------
def _twiml_refusal(store: Store, call_id: str, settings: BridgeSettings) -> str:
    call = store.get_call(call_id)
    if call is None:
        return "unknown_call"
    if call["state"] not in TWIML_OK_STATES:
        return f"state_{call['state']}"
    appr = store.get_approval(call_id)
    if appr is None or appr["used_at"] is None:
        return "no_consumed_approval"
    if store.media_token_used(call_id):
        return "stream_already_connected"
    e164, why = safety.validate_number(call["number"])
    if e164 is None:
        return why or "blocked_number"
    if not safety.check_allowlist(e164, settings.environ).ok:
        return "not_allowlisted"
    return ""


def _ws_refusal(store: Store, call_id: str, start: dict | None) -> str:
    if not start or not isinstance(start.get("start"), dict):
        return "no_start"
    info = start["start"]
    token = (info.get("customParameters") or {}).get("token", "")
    call = store.get_call(call_id)
    if call is None:
        return "unknown_call"
    if call["state"] not in TWIML_OK_STATES:
        return f"state_{call['state']}"
    fmt = info.get("mediaFormat") or {}
    if fmt and (str(fmt.get("encoding", "")).lower() != "audio/x-mulaw"
                or int(fmt.get("sampleRate", 8000)) != 8000):
        return "bad_media_format"
    sid = info.get("callSid") or ""
    if call.get("provider_sid") and sid and sid != call["provider_sid"]:
        return "sid_mismatch"
    if not isinstance(token, str) or not token or not store.consume_media_token(call_id, token):
        return "bad_token"
    return ""


async def _await_start(ws: WebSocket, timeout: float) -> dict | None:
    async def _loop() -> dict | None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                return None
            if msg.get("event") == "start":
                return msg
            if msg.get("event") not in ("connected",):
                return None

    try:
        return await asyncio.wait_for(_loop(), timeout)
    except Exception:  # noqa: BLE001 - timeout or disconnect
        return None


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except Exception:  # noqa: BLE001
        pass


def _leaves_voicemail(store: Store, call_id: str) -> bool:
    call = store.get_call(call_id)
    return bool(call and call["brief"]["limits"].get("voicemail_message"))


def _bg_hangup(get_client: Callable[[], Any], sid: str) -> None:
    try:
        get_client().calls(sid).update(status="completed")
    except Exception:  # noqa: BLE001
        log.warning("voicemail hangup failed")

