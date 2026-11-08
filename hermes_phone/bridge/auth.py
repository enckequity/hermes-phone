"""Workspace auth for the shared bridge. No tenant list to sync; two ways in:

1. HMAC token ``<workspace>.<sig>``, ``sig = base64url_nopad(HMAC-SHA256(signing_key,
   "hermes-bridge-v1|" + workspace))``. The bridge recomputes it (constant-time compare).
2. A workspace's TeamShift usage token (``Bearer ts_usg_...`` plus ``X-TeamShift-Workspace``),
   checked against the auth service. Only successes are cached, for 10 minutes.

Per-workspace overrides (``HERMES_PHONE_BRIDGE_OVERRIDES``, a JSON object or a path to one):
``{"<workspace>": {"daily_cap": 10, "timezone": "America/New_York", "disabled": true}}``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CONTEXT = "hermes-bridge-v1|"
GHOST_CONTEXT = "ghost-bridge-v1|"  # the ghost-call bridge uses the same scheme
WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
USAGE_TOKEN_RE = re.compile(r"^ts_usg_[0-9a-f]{64}$")
USAGE_WORKSPACE_RE = re.compile(r"^t-[0-9a-f]{10}$")
USAGE_PREFIX = "ts_usg_"
WORKSPACE_HEADER = "x-teamshift-workspace"
CACHE_TTL_S = 600.0
DEFAULT_DAILY_CAP = 10
DEFAULT_TZ = "America/New_York"


@dataclass(frozen=True)
class Workspace:
    name: str
    daily_cap: int = DEFAULT_DAILY_CAP
    tz: str = DEFAULT_TZ


def _sig(key: str, workspace: str, context: str) -> str:
    mac = hmac.new(key.encode(), (context + workspace).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def mint(key: str, workspace: str, context: str = CONTEXT) -> str:
    if not key or not WORKSPACE_RE.match(workspace):
        raise ValueError("need a signing key and a workspace matching " + WORKSPACE_RE.pattern)
    return f"{workspace}.{_sig(key, workspace, context)}"


def verify_hmac(key: str, token: str, context: str = CONTEXT) -> str | None:
    """Workspace name for a valid token, else None (malformed, wrong key, swapped name)."""
    if not key or not token or len(token) > 200 or token.count(".") != 1:
        return None
    workspace, sig = token.split(".")
    if not WORKSPACE_RE.match(workspace):
        return None
    return workspace if hmac.compare_digest(sig, _sig(key, workspace, context)) else None


def load_overrides(raw: str) -> dict[str, dict]:
    """Raises ValueError on a bad config: the caller then refuses new calls (fail closed)."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw if raw.startswith("{") else Path(raw).read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"overrides unreadable ({type(exc).__name__})") from None
    if not isinstance(data, dict):
        raise ValueError("overrides must be a JSON object")
    for name, spec in data.items():
        if not WORKSPACE_RE.match(str(name)) or not isinstance(spec, dict):
            raise ValueError("override keys must be workspace names with object values")
        if "timezone" in spec:
            ZoneInfo(str(spec["timezone"]))  # raises on an unknown zone (a ValueError subclass)
        if "daily_cap" in spec and (not isinstance(spec["daily_cap"], int)
                                    or spec["daily_cap"] < 0):
            raise ValueError("daily_cap must be a non-negative integer")
    return data


def default_http_get(url: str, headers: dict[str, str], timeout_s: float) -> int:
    """Status code of a GET; 0 when there was no HTTP answer (timeout, connection error)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


class Authenticator:
    def __init__(self, *, signing_key: str, auth_url: str, overrides: dict[str, dict],
                 clock: Callable[[], float], timeout_s: float = 5.0,
                 http_get: Callable[[str, dict[str, str], float], int] | None = None) -> None:
        self.signing_key = signing_key
        self.auth_url = auth_url.strip()
        self.overrides = overrides
        self.clock = clock
        self.timeout_s = timeout_s
        self.http_get = http_get or default_http_get
        self._cache: dict[str, float] = {}  # sha256(name, token) -> expiry; successes only

    def check(self, authorization: str, workspace_header: str) -> tuple[Workspace | None, str]:
        """``(workspace, "")`` or ``(None, code)`` with code ``unauthorized``,
        ``workspace_disabled`` or ``auth_unavailable``. Blocking (may call the auth service)."""
        if not authorization.lower().startswith("bearer "):
            return None, "unauthorized"
        token = authorization[7:].strip()
        if token.startswith(USAGE_PREFIX):
            name, code = self._usage(token, workspace_header.strip())
        else:
            name = verify_hmac(self.signing_key, token)
            code = "" if name else "unauthorized"
        if name is None:
            return None, code
        spec = self.overrides.get(name, {})
        if spec.get("disabled") is True:
            return None, "workspace_disabled"
        return Workspace(name, int(spec.get("daily_cap", DEFAULT_DAILY_CAP)),
                         str(spec.get("timezone") or DEFAULT_TZ)), ""

    def _usage(self, token: str, name: str) -> tuple[str | None, str]:
        if not self.auth_url or not USAGE_TOKEN_RE.match(token) or not USAGE_WORKSPACE_RE.match(
                name):
            return None, "unauthorized"
        key = hashlib.sha256(f"{name}\0{token}".encode()).hexdigest()
        now = self.clock()
        if self._cache.get(key, 0) > now:
            return name, ""
        status = self.http_get(self.auth_url, {"Authorization": f"Bearer {token}",
                                               "x-teamshift-workspace": name,
                                               "Accept": "application/json"}, self.timeout_s)
        if status == 200:
            if len(self._cache) > 10000:  # bound memory: drop expired entries
                self._cache = {k: v for k, v in self._cache.items() if v > now}
            self._cache[key] = now + CACHE_TTL_S
            return name, ""
        if status == 401:
            return None, "unauthorized"
        log.warning("workspace auth service unavailable (status %s)", status or "none")
        return None, "auth_unavailable"  # fail closed, never cached
