"""Bridge settings. Secrets come only from the environment; nothing here is logged."""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from .. import mode as modemod
from .. import secrets as secretsmod

DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TWILIO_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")


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

    def missing_env(self) -> list[str]:
        """Names (never values) of required env vars that are unset."""
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
