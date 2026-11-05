"""Web phone routes: ``GET /phone/{id}?t=`` (the page) and ``POST /phone/{id}/answer``.

No Twilio signature applies here; the single-use link token is the auth. Every failure looks the
same (generic 404) so the routes never reveal whether a call id exists. Tokens are never logged.
"""
from __future__ import annotations

import json
import secrets
from collections import deque
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .. import states, webphone
from ..store import Store
from . import webpage
from .settings import BridgeSettings
from .status import _advance

RATE_LIMIT = 30  # requests per window per client
RATE_WINDOW_S = 60.0
MAX_BODY = 2048


def _security_headers(settings: BridgeSettings, nonce: str = "") -> dict[str, str]:
    connect = "'self'"
    ws_base = settings.ws_base_url()
    if ws_base.startswith(("wss://", "ws://")):
        connect += " " + ws_base  # Safari older than 15.4 does not map 'self' to ws(s)
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    style = f"'nonce-{nonce}'" if nonce else "'unsafe-inline'"  # error page has a tiny inline style
    csp = (f"default-src 'none'; script-src {script}; style-src {style}; "
           f"connect-src {connect}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Permissions-Policy": "microphone=(self)"}


def register(app: FastAPI, *, settings: BridgeSettings, store: Store,
              clock: Callable[[], float], refusal: Callable[[str], str]) -> None:
    hits: dict[str, deque[float]] = {}

    def trust_proxy() -> bool:
        return (settings.environ.get("HERMES_PHONE_TRUST_PROXY") or "").strip() == "1"

    def limited(request: Request) -> bool:
        key = request.client.host if request.client else "?"
        if trust_proxy():  # only behind a proxy that sets/overwrites X-Forwarded-For (Railway)
            fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            key = fwd or key
        now = clock()
        q = hits.setdefault(key, deque())
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        if len(hits) > 1024:  # keep memory bounded
            for k in [k for k, v in hits.items() if not v or now - v[-1] > RATE_WINDOW_S]:
                hits.pop(k, None)
        return False

    def invalid(status: int = 404) -> Response:
        return HTMLResponse(webpage.render_error(), status_code=status,
                            headers=_security_headers(settings))

    def usable(call_id: str, token: str) -> dict | None:
        """The call, if the link may be used right now. Never raises, never says why not."""
        if len(call_id) > 64 or not token:
            return None
        webphone.expire_stale(store, call_id)
        call = store.get_call(call_id)
        if call is None or not webphone.is_web_sid(call.get("provider_sid")):
            return None
        if call["state"] not in (states.DIALING, states.RINGING):
            return None
        if not store.peek_web_token(call_id, token) or refusal(call_id):
            return None
        return call

    @app.get("/phone/{call_id}")
    async def phone_page(call_id: str, request: Request, t: str = "") -> Response:
        if limited(request):
            return invalid(429)
        call = usable(call_id, t)
        if call is None:
            return invalid()
        nonce = secrets.token_urlsafe(16)
        html = webpage.render_page(call_id=call_id, callee=call["callee_name"], nonce=nonce)
        return HTMLResponse(html, headers=_security_headers(settings, nonce))

    @app.post("/phone/{call_id}/answer")
    async def phone_answer(call_id: str, request: Request) -> Response:
        hdr = _security_headers(settings)
        gone = JSONResponse({"error": "unavailable"}, status_code=404, headers=hdr)
        if limited(request):
            return JSONResponse({"error": "unavailable"}, status_code=429, headers=hdr)
        try:
            raw = (await request.body())[:MAX_BODY]
            body = json.loads(raw or b"{}")
            token = body.get("t") if isinstance(body, dict) else ""
        except ValueError:
            return gone
        if not isinstance(token, str) or usable(call_id, token) is None:
            return gone
        if not store.consume_web_token(call_id, token):  # single use; a reload cannot reuse it
            return gone
        cur = store.get_call(call_id)
        if cur is None or not _advance(store, call_id, states.IN_PROGRESS):
            return gone
        store.add_event(call_id, "web_answered", {})
        media_token = store.create_media_token(call_id, settings.token_ttl_s)
        return JSONResponse({"media_token": media_token, "call_sid": cur.get("provider_sid", "")},
                            headers=hdr)
