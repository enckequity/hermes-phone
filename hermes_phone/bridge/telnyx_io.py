"""Telnyx Call Control helpers: webhook signatures, REST client (stdlib urllib), request bodies and
the media-stream frame mapping. Nothing here is spoken to a callee except ``INBOUND_LINE``."""
from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

API_BASE = "https://api.telnyx.com/v2"
SIGNATURE_TOLERANCE_S = 300
DIAL_TIMEOUT_S = 30
INBOUND_LINE = "Thanks for calling back, we'll pass on your message. Goodbye."
# Premium AMD results that mean a person (or nobody we can tell apart from one) answered.
HUMAN_RESULTS = frozenset({"human", "human_residence", "human_business", "not_sure", "silence"})
MACHINE_RESULTS = frozenset({"machine"})
AMD_EVENTS = frozenset({"call.machine.premium.detection.ended", "call.machine.detection.ended"})
GREETING_EVENTS = frozenset({"call.machine.premium.greeting.ended",
                             "call.machine.greeting.ended"})


class TelnyxError(Exception):
    pass


def verify_signature(public_key_b64: str, body: bytes, headers: Mapping[str, str],
                     now: float | None = None) -> bool:
    """Telnyx signs ``{timestamp}|{body}`` with Ed25519. Fail closed: no key, no header, a stale
    timestamp or any parse error is invalid."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        stamp = headers.get("telnyx-timestamp") or ""
        sig = headers.get("telnyx-signature-ed25519") or ""
        if not stamp or not sig:
            return False
        if abs((time.time() if now is None else now) - int(stamp)) > SIGNATURE_TOLERANCE_S:
            return False
        key.verify(base64.b64decode(sig, validate=True), stamp.encode() + b"|" + body)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class TelnyxClient:
    """Minimal Call Control client. ``post`` raises ``TelnyxError`` with no secrets in it."""

    def __init__(self, api_key: str, base_url: str = API_BASE, timeout_s: float = 10.0) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def post(self, path: str, body: dict) -> dict:
        if not self.api_key:
            raise TelnyxError("not configured")
        req = urllib.request.Request(
            self.base_url + path, method="POST", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise TelnyxError(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise TelnyxError(type(exc).__name__) from None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise TelnyxError("invalid JSON") from None
        return data if isinstance(data, dict) else {}


def action(client: Any, ccid: str, name: str, body: dict | None = None) -> dict:
    from urllib.parse import quote

    return client.post(f"/calls/{quote(ccid, safe='')}/actions/{name}", body or {})


def dial_body(*, connection_id: str, to: str, from_: str, webhook_url: str,
              time_limit_s: int) -> dict:
    return {"connection_id": connection_id, "to": to, "from": from_,
            "webhook_url": webhook_url, "webhook_url_method": "POST",
            "timeout_secs": DIAL_TIMEOUT_S, "time_limit_secs": int(time_limit_s),
            "answering_machine_detection": "premium"}


def stream_body(stream_url: str) -> dict:
    return {"stream_url": stream_url, "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU"}


def hangup_status(cause: str, answered: bool) -> str:
    """Telnyx ``hangup_cause`` -> the Twilio-style CallStatus that ``status.apply_status`` takes."""
    if answered:
        return "completed"
    cause = (cause or "").lower()
    if cause in ("user_busy", "busy", "call_rejected"):
        return "busy"
    if cause in ("timeout", "no_answer", "originator_cancel"):
        return "no-answer"
    return "failed"


class Hangup:
    """Twilio-shaped ``client.calls(sid).update(...)`` that hangs up a Telnyx call, so
    ``CallSession`` stays provider-neutral. Any update (status or apology TwiML) hangs up."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def calls(self, ccid: str) -> Hangup._Call:
        return Hangup._Call(self.client, ccid)

    class _Call:
        def __init__(self, client: Any, ccid: str) -> None:
            self.client, self.ccid = client, ccid

        def update(self, **_: Any) -> None:
            action(self.client, self.ccid, "hangup")


class MediaSocket:
    """Wraps the Telnyx media websocket for ``CallSession``. Inbound ``media``/``stop``/``mark``
    frames already have the shape the session reads. Outbound frames drop ``streamSid``:
    ``{"event":"media","media":{"payload":...}}``, ``{"event":"clear"}`` and ``mark``."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws

    async def receive_text(self) -> str:
        return await self.ws.receive_text()

    async def send_text(self, text: str) -> None:
        msg = json.loads(text)
        kind = msg.get("event")
        if kind == "media":
            out: dict = {"event": "media", "media": {"payload": msg["media"]["payload"]}}
        elif kind == "clear":
            out = {"event": "clear"}
        elif kind == "mark":
            out = {"event": "mark", "mark": msg.get("mark") or {}}
        else:
            return  # nothing else is part of the Telnyx stream protocol
        await self.ws.send_text(json.dumps(out))

    async def close(self, code: int = 1000) -> None:
        await self.ws.close(code=code)
