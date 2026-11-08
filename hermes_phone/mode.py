"""Which kind of call the plugin places: a Twilio PSTN call or a browser "web phone" call.

Pure env logic, no imports of heavy packages, shared by the plugin, the bridge and the CLI.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

MODE_ENV = "HERMES_PHONE_MODE"
TWILIO = "twilio"
WEB = "web"
REMOTE = "remote"  # a shared bridge reached over HTTPS (docs/BRIDGE_API.md)
BRIDGE_URL_ENV = "HERMES_PHONE_BRIDGE_URL"
BRIDGE_TOKEN_ENV = "HERMES_PHONE_BRIDGE_TOKEN"
REMOTE_ENV = (BRIDGE_URL_ENV, BRIDGE_TOKEN_ENV)
TWILIO_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL")
WEB_ENV = ("OPENAI_API_KEY", "PUBLIC_BASE_URL")


@dataclass
class Mode:
    name: str  # "twilio", "web" or "remote"
    missing: list[str] = field(default_factory=list)  # env names still needed for this mode
    how: str = ""  # "explicit", "default", "auto" or "bridge"


def _unset(environ: Mapping[str, str], names: tuple[str, ...]) -> list[str]:
    return [k for k in names if not (environ.get(k) or "").strip()]


def resolve(environ: Mapping[str, str]) -> Mode:
    """``HERMES_PHONE_MODE=web|twilio`` wins. Unset: Twilio when fully configured, web when
    Twilio is incomplete but OPENAI_API_KEY and PUBLIC_BASE_URL are set, else Twilio (missing).
    ``HERMES_PHONE_BRIDGE_URL`` beats all of them: calls go to the shared remote bridge."""
    if (environ.get(BRIDGE_URL_ENV) or "").strip():
        return Mode(REMOTE, _unset(environ, REMOTE_ENV), "bridge")
    explicit = (environ.get(MODE_ENV) or "").strip().lower()
    if explicit == WEB:
        return Mode(WEB, _unset(environ, WEB_ENV), "explicit")
    twilio_missing = _unset(environ, TWILIO_ENV)
    if explicit == TWILIO:
        return Mode(TWILIO, twilio_missing, "explicit")
    if not twilio_missing:
        return Mode(TWILIO, [], "default")
    if not _unset(environ, WEB_ENV):
        return Mode(WEB, [], "auto")
    return Mode(TWILIO, twilio_missing, "default")
