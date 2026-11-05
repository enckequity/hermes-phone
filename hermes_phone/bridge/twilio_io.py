"""Twilio helpers: signature validation, TwiML, status mapping, lazy REST client."""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl
from xml.sax.saxutils import quoteattr

from .. import states

# Twilio CallStatus -> our state. "initiated"/"queued" carry no information we act on.
STATUS_MAP: dict[str, str] = {
    "ringing": states.RINGING,
    "in-progress": states.IN_PROGRESS,
    "answered": states.IN_PROGRESS,
    "completed": states.COMPLETED,
    "busy": states.BUSY,
    "no-answer": states.NO_ANSWER,
    "failed": states.FAILED,
    "canceled": states.CANCELED,
}
IGNORED_STATUSES = frozenset({"queued", "initiated"})
MACHINE_PREFIXES = ("machine", "fax")  # AnsweredBy values that mean "not a person"


def parse_form(body: bytes) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded body without python-multipart."""
    return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))


def is_machine(answered_by: str) -> bool:
    return (answered_by or "").lower().startswith(MACHINE_PREFIXES)


def machine_message_done(answered_by: str) -> bool:
    """True once the greeting/beep has finished, so a voicemail message may be spoken."""
    return (answered_by or "").lower().startswith("machine_end")


def public_url(base_url: str, path: str, query: str = "") -> str:
    """URL Twilio signed: PUBLIC_BASE_URL + path (+ query), never the internal proxy URL."""
    url = base_url.rstrip("/") + path
    return f"{url}?{query}" if query else url


def valid_signature(auth_token: str, url: str, params: dict[str, str], signature: str | None
                    ) -> bool:
    """Fail closed: no token, no signature or any error means invalid."""
    if not auth_token or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator

        return bool(RequestValidator(auth_token).validate(url, params, signature))
    except Exception:  # noqa: BLE001
        return False


def stream_twiml(ws_url: str, token: str) -> str:
    """Bidirectional stream. Query strings are not allowed in <Stream url>, so the one-time
    token travels as a <Parameter> and arrives in ``start.customParameters``."""
    return ('<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
            f"<Stream url={quoteattr(ws_url)}>"
            f"<Parameter name=\"token\" value={quoteattr(token)}/>"
            "</Stream></Connect></Response>")


def say_and_hangup_twiml(text: str) -> str:
    from xml.sax.saxutils import escape

    return f"<Response><Say>{escape(text)}</Say><Hangup/></Response>"


def make_client(account_sid: str, auth_token: str) -> Any:
    from twilio.rest import Client  # lazy: keeps register() and import light

    return Client(account_sid, auth_token)
