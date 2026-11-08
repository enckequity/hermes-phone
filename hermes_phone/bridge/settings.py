"""Bridge settings. Nothing here is logged. The co-located bridge reads secrets from the
environment; the shared bridge (``from_shared_env``) reads them from credential files."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .. import mode as modemod
from .. import secrets as secretsmod
from ..safety import ALLOW_ALL_ENV, ALLOWLIST_ENV

DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TWILIO_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
SHARED_DB_PATH = "/data/phone-bridge.db"
SHARED_PORT = 8790
# Shared bridge secrets: one file each under $CREDENTIALS_DIRECTORY, never plain env.
CRED_FILES = {"openai_api_key": "openai_api_key", "telnyx_api_key": "telnyx_api_key",
              "telnyx_public_key": "telnyx_public_key", "signing_key": "bridge_signing_key"}


def _read_creds(directory: str) -> dict[str, str]:
    """Missing directory or file -> "" (reported by missing_env by name, never by value)."""
    out = {}
    for attr, fname in CRED_FILES.items():
        try:
            out[attr] = (Path(directory) / fname).read_text().strip() if directory else ""
        except OSError:
            out[attr] = ""
    return out


def _rate(raw: str | None) -> float:
    try:
        return max(0.0, float(raw or 0))
    except ValueError:
        return 0.0


@dataclass
class BridgeSettings:
    public_base_url: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    openai_api_key: str = ""
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    db_path: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    store_transcripts: bool = True
    token_ttl_s: float = 60.0
    start_timeout_s: float = 10.0
    tick_s: float = 0.5
    wrapup_lead_s: float = 30.0
    end_grace_s: float = 4.0
    opening_wait_s: float = 2.5  # silent answerer: speak first after this long
    question_timeout_s: int = 75  # fallback; the brief's limits.question_timeout_s wins
    question_poll_s: float = 0.25  # how often the bridge looks for the user's answer (<= 0.5)
    filler_every_s: float = 15.0
    closing_max_s: float = 15.0  # longest wait for the spoken callback line before hanging up
    # shared multi-tenant bridge (docs/BRIDGE_API.md), placing calls through Telnyx
    shared: bool = False  # multi-tenant /v1 surface only (see shared.py)
    signing_key: str = ""  # HMAC key for <workspace>.<sig> tokens
    auth_url: str = ""  # HERMES_PHONE_BRIDGE_AUTH_URL: usage-token check; unset = path off
    overrides_raw: str = ""  # HERMES_PHONE_BRIDGE_OVERRIDES: JSON object or a path to one
    inbound_enabled: bool = False  # HERMES_PHONE_BRIDGE_INBOUND=1 mounts the inbound stub
    auth_timeout_s: float = 5.0
    telnyx_api_key: str = ""
    telnyx_connection_id: str = ""
    telnyx_public_key: str = ""  # base64 Ed25519 key that signs webhooks
    caller_number: str = ""  # HERMES_PHONE_CALLER_NUMBER, the caller ID on every call
    telephony_usd_per_min: float = 0.0
    voice_usd_per_min: float = 0.0
    environ: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> BridgeSettings:
        env = os.environ if environ is None else environ  # non-secret config only
        secret = lambda name: secretsmod.get(name, environ)  # noqa: E731  profile-scoped route
        bind_all = bool(env.get("PORT")) and not env.get("HERMES_PHONE_BRIDGE_HOST")
        port_raw = env.get("HERMES_PHONE_BRIDGE_PORT") or env.get("PORT") or str(DEFAULT_PORT)
        try:
            port = int(port_raw)
        except ValueError:
            port = DEFAULT_PORT
        return cls(
            public_base_url=secret("PUBLIC_BASE_URL").strip().rstrip("/"),
            twilio_account_sid=secret("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=secret("TWILIO_AUTH_TOKEN"),
            twilio_from_number=secret("TWILIO_FROM_NUMBER"),
            openai_api_key=secret("OPENAI_API_KEY"),
            model=env.get("HERMES_PHONE_MODEL") or DEFAULT_MODEL,
            voice=env.get("HERMES_PHONE_VOICE") or DEFAULT_VOICE,
            db_path=env.get("HERMES_PHONE_DB_PATH") or env.get("HERMES_PHONE_DB") or "",
            host=env.get("HERMES_PHONE_BRIDGE_HOST") or ("0.0.0.0" if bind_all else DEFAULT_HOST),  # noqa: S104
            port=port,
            store_transcripts=(env.get("HERMES_PHONE_STORE_TRANSCRIPTS", "1").strip().lower()
                               not in ("0", "false", "no", "off")),
            environ={**env, **{n: secret(n) for n in secretsmod.SECRET_NAMES}},
        )

    @classmethod
    def from_shared_env(cls, environ: Mapping[str, str] | None = None) -> BridgeSettings:
        """Shared bridge: config from env, secrets only from $CREDENTIALS_DIRECTORY files."""
        env = os.environ if environ is None else environ
        creds = _read_creds(env.get("CREDENTIALS_DIRECTORY") or "")
        public = env.get("HERMES_PHONE_BRIDGE_PUBLIC_URL") or env.get("PUBLIC_BASE_URL") or ""
        return cls(
            shared=True,
            public_base_url=public.strip().rstrip("/"),
            openai_api_key=creds["openai_api_key"],
            telnyx_api_key=creds["telnyx_api_key"],
            telnyx_public_key=creds["telnyx_public_key"],
            signing_key=creds["signing_key"],
            auth_url=(env.get("HERMES_PHONE_BRIDGE_AUTH_URL") or "").strip(),
            overrides_raw=env.get("HERMES_PHONE_BRIDGE_OVERRIDES") or "",
            inbound_enabled=(env.get("HERMES_PHONE_BRIDGE_INBOUND") or "").strip() == "1",
            telnyx_connection_id=env.get("TELNYX_CONNECTION_ID") or "",
            caller_number=env.get("HERMES_PHONE_CALLER_NUMBER") or "",
            telephony_usd_per_min=_rate(env.get("HERMES_PHONE_TELEPHONY_USD_PER_MIN")),
            voice_usd_per_min=_rate(env.get("HERMES_PHONE_VOICE_USD_PER_MIN")),
            model=env.get("HERMES_PHONE_MODEL") or DEFAULT_MODEL,
            voice=env.get("HERMES_PHONE_VOICE") or DEFAULT_VOICE,
            db_path=env.get("HERMES_PHONE_DB_PATH") or SHARED_DB_PATH,
            host=env.get("HERMES_PHONE_BRIDGE_HOST") or DEFAULT_HOST,
            port=SHARED_PORT,
            store_transcripts=(env.get("HERMES_PHONE_STORE_TRANSCRIPTS", "1").strip().lower()
                               not in ("0", "false", "no", "off")),
            environ={k: env[k] for k in (ALLOWLIST_ENV, ALLOW_ALL_ENV) if k in env},
        )

    def missing_env(self) -> list[str]:
        """Names (never values) of required env vars that are unset."""
        if self.shared:
            need = {"credential telnyx_api_key": self.telnyx_api_key,
                    "credential telnyx_public_key": self.telnyx_public_key,
                    "credential openai_api_key": self.openai_api_key,
                    "credential bridge_signing_key or HERMES_PHONE_BRIDGE_AUTH_URL":
                        self.signing_key or self.auth_url,
                    "TELNYX_CONNECTION_ID": self.telnyx_connection_id,
                    "HERMES_PHONE_CALLER_NUMBER": self.caller_number,
                    "HERMES_PHONE_BRIDGE_PUBLIC_URL": self.public_base_url}
            return [k for k, v in need.items() if not v]
        pairs = {
            "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
            "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
            "TWILIO_FROM_NUMBER": self.twilio_from_number,
            "OPENAI_API_KEY": self.openai_api_key,
            "PUBLIC_BASE_URL": self.public_base_url,
        }
        chosen = modemod.resolve({**{k: v for k, v in pairs.items() if v},
                                  modemod.MODE_ENV: self.environ.get(modemod.MODE_ENV, "")})
        if chosen.name == modemod.WEB:  # web phone mode needs no Twilio at all
            return [k for k in modemod.WEB_ENV if not pairs[k]]
        return [k for k, v in pairs.items() if not v]

    def ws_base_url(self) -> str:
        base = self.public_base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base
