"""Workspaces of the shared bridge, from ``HERMES_PHONE_BRIDGE_TENANTS`` (a JSON file path or the
JSON itself). Only sha256 hashes of the workspace tokens are stored; lookups are constant time.

Format: ``[{"token_sha256": "<64 hex>", "workspace": "w1", "business_name": "Acme",
"daily_cap": 10, "tz": "America/New_York"}]``.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_DAILY_CAP = 10
DEFAULT_TZ = "America/New_York"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Tenant:
    workspace: str
    business_name: str
    daily_cap: int
    tz: str
    token_sha256: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def load(raw: str) -> list[Tenant]:
    """Parse the config. Any error loads nothing (every request is then refused), never a partial
    set. Logs names and positions only, never hashes."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        text = raw if raw[0] in "[{" else Path(raw).read_text()
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError("must be a JSON list")
        out: list[Tenant] = []
        for i, item in enumerate(items):
            h = str(item.get("token_sha256") or "").lower()
            ws = str(item.get("workspace") or "").strip()
            name = str(item.get("business_name") or "").strip()
            tz = str(item.get("tz") or DEFAULT_TZ)
            ZoneInfo(tz)  # raises on an unknown zone
            cap = int(item.get("daily_cap", DEFAULT_DAILY_CAP))
            if not _HEX64.match(h) or not ws or not name or cap < 0:
                raise ValueError(f"entry {i} is incomplete")
            out.append(Tenant(ws, name, cap, tz, h))
        if len({t.workspace for t in out} | {t.token_sha256 for t in out}) != 2 * len(out):
            raise ValueError("duplicate workspace or token")
        return out
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.error("tenant config rejected (%s); every workspace request is refused",
                  type(exc).__name__)
        return []


def find(tenants: list[Tenant], token: str) -> Tenant | None:
    """Compare against every entry (no early exit), so timing does not reveal a match."""
    if not token or len(token) > 512:
        return None
    want = token_hash(token)
    found = None
    for t in tenants:
        if hmac.compare_digest(t.token_sha256, want):
            found = t
    return found
