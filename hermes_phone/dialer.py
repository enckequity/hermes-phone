"""Twilio dialer: the only place a real outbound call is created (lazy ``twilio`` import).

Third layer of defence: it re-runs the blocklist and allowlist itself, so a direct call to
``place_call`` can never reach a number the safety rules refuse.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from . import mode as modemod
from . import safety, webphone
from .store import Store
from .tools import DialResult

log = logging.getLogger(__name__)

TWILIO_ENV = modemod.TWILIO_ENV
TIME_LIMIT_MARGIN_S = 15
RING_TIMEOUT_S = 30
HEALTH_TIMEOUT_S = 3.0


def missing_env(environ: Mapping[str, str]) -> list[str]:
    """Names (never values) of the Twilio env vars that are unset."""
    return [k for k in TWILIO_ENV if not (environ.get(k) or "").strip()]


def _http_status(url: str, timeout: float) -> int:
    import httpx

    return httpx.get(url, timeout=timeout, follow_redirects=False).status_code


class TwilioDialer:
    configured = True

    def __init__(self, environ: Mapping[str, str], *, client: Any = None,
                 http_get: Callable[[str, float], int] | None = None) -> None:
        self.environ = environ
        self._client = client
        self._http_get = http_get or _http_status

    @property
    def base_url(self) -> str:
        return (self.environ.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")

    def client(self) -> Any:
        if self._client is None:
            from twilio.rest import Client  # lazy

            self._client = Client(self.environ["TWILIO_ACCOUNT_SID"],
                                  self.environ["TWILIO_AUTH_TOKEN"])
        return self._client

    def preflight(self) -> str:
        """Empty string when the bridge answers /healthz; otherwise a message. Call-time only."""
        url = f"{self.base_url}/healthz"
        try:
            code = self._http_get(url, HEALTH_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return (f"The media bridge is not reachable at {self.base_url} "
                    f"({type(exc).__name__}). Start it with `hermes phone serve` and check "
                    "your tunnel, then try again. Nothing was dialed.")
        if code != 200:
            return (f"The media bridge answered HTTP {code} at {self.base_url}/healthz. "
                    "Nothing was dialed.")
        return ""

    def place_call(self, call: dict, payload: dict) -> DialResult:
        number = call["number"]
        e164, reason = safety.validate_number(number)
        if e164 is None or e164 != number:
            return DialResult(ok=False, error=reason or "number failed validation")
        allow = safety.check_allowlist(number, self.environ)
        if not allow.ok:
            return DialResult(ok=False, error=allow.reason)
        missing = missing_env(self.environ)
        if missing:
            return DialResult(ok=False, error="not configured: missing " + ", ".join(missing))
        cid = call["id"]
        max_s = int(call["brief"]["limits"]["max_duration_s"])
        status_url = f"{self.base_url}/status/{cid}"
        base = dict(to=number, from_=self.environ["TWILIO_FROM_NUMBER"],
                    url=f"{self.base_url}/twiml/{cid}", method="POST",
                    status_callback=status_url, status_callback_method="POST")
        # Trial accounts reject some parameters ("Invalid or disallowed parameters ... trial
        # accounts have limited parameter access"). Try the full set, then step down. The bridge
        # enforces max duration itself, so dropping time_limit/AMD keeps the call safe; without
        # AMD voicemail is not detected (the call is simply reported as answered).
        attempts = [
            dict(base, status_callback_event=["initiated", "ringing", "answered", "completed"],
                 machine_detection="Enable", async_amd="true",
                 async_amd_status_callback=status_url,
                 async_amd_status_callback_method="POST",
                 time_limit=min(max_s, safety_cap()) + TIME_LIMIT_MARGIN_S,
                 timeout=RING_TIMEOUT_S),
            dict(base, status_callback_event=["initiated", "ringing", "answered", "completed"]),
            base,
        ]
        created = None
        for i, params in enumerate(attempts):
            try:
                created = self.client().calls.create(**params)
                if i:
                    log.warning("twilio calls.create succeeded with reduced parameters (set %d)", i)
                break
            except Exception as exc:  # noqa: BLE001
                detail = describe_twilio_error(exc)
                retryable = "disallowed parameters" in detail.lower() or "trial account" in (
                    detail.lower())
                if retryable and i + 1 < len(attempts):
                    log.warning("twilio calls.create rejected parameters, retrying: %s", detail)
                    continue
                log.warning("twilio calls.create failed: %s", detail)
                return DialResult(ok=False, error=f"twilio error: {detail}")
        return DialResult(ok=True, provider_sid=str(getattr(created, "sid", "") or ""))

    def hangup(self, call: dict) -> bool:
        sid = call.get("provider_sid") or ""
        if not sid:
            return False
        try:
            self.client().calls(sid).update(status="completed")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("twilio hangup failed: %s", type(exc).__name__)
            return False


class WebPhoneDialer(TwilioDialer):
    """Web phone mode: no PSTN call. ``place_call`` mints a single-use link token; the callee is
    the browser page that link opens. Safety checks are the same as for a real number."""

    configured = True

    def __init__(self, environ: Mapping[str, str], *, store: Store,
                 http_get: Callable[[str, float], int] | None = None) -> None:
        super().__init__(environ, http_get=http_get)
        self.store = store
        self.links: dict[str, str] = {}  # call_id -> link (plain token only ever lives here)

    def place_call(self, call: dict, payload: dict) -> DialResult:
        number = call["number"]
        e164, reason = safety.validate_number(number)
        if e164 is None or e164 != number:
            return DialResult(ok=False, error=reason or "number failed validation")
        allow = safety.check_allowlist(number, self.environ)
        if not allow.ok:
            return DialResult(ok=False, error=allow.reason)
        missing = [k for k in modemod.WEB_ENV if not (self.environ.get(k) or "").strip()]
        if missing:
            return DialResult(ok=False, error="not configured: missing " + ", ".join(missing))
        cid = call["id"]
        token = self.store.create_web_token(cid, webphone.LINK_TTL_S)
        url = webphone.answer_url(self.base_url, cid, token)
        self.links[cid] = url
        return DialResult(ok=True, provider_sid=webphone.SID_PREFIX + cid, answer_url=url)

    def hangup(self, call: dict) -> bool:
        """Ask the bridge session to end the call (it polls this flag every tick)."""
        cid = call.get("id") or ""
        if not cid:
            return False
        self.store.request_hangup(cid)
        return True


# Common Twilio REST error codes -> what the user should do. https://www.twilio.com/docs/api/errors
TWILIO_HINTS = {
    20003: "Twilio rejected the credentials: check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.",
    20404: "Twilio resource not found: check TWILIO_ACCOUNT_SID.",
    21210: "TWILIO_FROM_NUMBER is not a number owned by this Twilio account.",
    21212: "TWILIO_FROM_NUMBER is not a valid phone number.",
    21205: "PUBLIC_BASE_URL is not a valid public https URL.",
    21215: "Calls to this country/region are disabled: enable it in Twilio Voice Geo Permissions.",
    13227: "Calls to this country/region are disabled: enable it in Twilio Voice Geo Permissions.",
    21219: "Trial account: verify the destination number in Twilio > Verified Caller IDs.",
    21217: "The destination number is not valid.",
}


def describe_twilio_error(exc: BaseException) -> str:
    """Short, secret-free description: exception type, Twilio code and a fix hint."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    msg = str(getattr(exc, "msg", "") or "").strip().replace("\n", " ")[:200]
    parts = [type(exc).__name__]
    if code:
        parts.append(f"code {code}")
    if status:
        parts.append(f"HTTP {status}")
    text = " ".join(parts)
    if msg:
        text += f": {msg}"
    hint = TWILIO_HINTS.get(code) if isinstance(code, int) else None
    low = msg.lower()
    if "compliance profile" in low or "trust hub" in low or "kyc" in low:
        # Twilio reuses 20003 for this; it is NOT a credentials problem.
        hint = ("Twilio requires an approved Primary Customer Profile (KYC) in Trust Hub "
                "before calling. Complete it in Twilio Console > Trust Hub; approval can take "
                "hours. Meanwhile use HERMES_PHONE_MODE=web.")
    elif "trial account" in low:
        hint = ("Twilio trial accounts cannot run custom call instructions. Upgrade the "
                "account, or use HERMES_PHONE_MODE=web.")
    if hint:
        text += f" Fix: {hint}"
    return text


def safety_cap() -> int:
    from .brief import HARD_MAX_DURATION_S

    return HARD_MAX_DURATION_S
