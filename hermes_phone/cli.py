"""``hermes phone serve`` and ``hermes phone doctor``. Heavy imports stay inside the handlers."""
from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from typing import Any


def setup(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="phone_cmd")
    sub.add_parser("serve", help="Run the media bridge (Twilio <-> OpenAI Realtime)")
    sub.add_parser("doctor", help="Check env vars, allowlist and bridge health (names only)")


def make_handler(ctx: Any = None) -> Callable[[argparse.Namespace], int]:
    def handler(args: argparse.Namespace) -> int:
        cmd = getattr(args, "phone_cmd", None)
        if cmd == "serve":
            return serve(ctx)
        if cmd == "doctor":
            return doctor()
        print("usage: hermes phone {serve|doctor}")
        return 2

    return handler


def _config(ctx: Any, key: str) -> Any:
    try:
        return ctx.get_config(key, None)
    except Exception:  # noqa: BLE001
        return None


def serve(ctx: Any = None) -> int:
    import uvicorn

    from .bridge import BridgeSettings, create_app
    from .store import Store

    settings = BridgeSettings.from_env()
    for key, attr in (("model", "model"), ("voice", "voice")):
        value = _config(ctx, key)
        if value and not os.environ.get(f"HERMES_PHONE_{key.upper()}"):
            setattr(settings, attr, str(value))
    if _config(ctx, "store_transcripts") is False:
        settings.store_transcripts = False
    missing = settings.missing_env()
    if missing:
        print("hermes-phone: cannot start the bridge, missing env: " + ", ".join(missing))
        return 2
    store = Store(path=settings.db_path) if settings.db_path else Store()
    print(f"hermes-phone bridge on {settings.host}:{settings.port} (model {settings.model})")
    uvicorn.run(create_app(settings, store=store), host=settings.host, port=settings.port)
    return 0


def run_checks(environ: Mapping[str, str], http_get: Callable[[str, float], int] | None = None
               ) -> list[tuple[str, bool, str]]:
    """List of (check, ok, detail). Details name variables, never values."""
    from . import dialer, safety
    from . import mode as modemod

    out: list[tuple[str, bool, str]] = []
    chosen = modemod.resolve(environ)
    out.append(("mode", True, chosen.name))
    needed = (modemod.WEB_ENV if chosen.name == modemod.WEB
              else (*dialer.TWILIO_ENV, "OPENAI_API_KEY"))
    for name in needed:
        ok = bool((environ.get(name) or "").strip())
        out.append((f"env {name}", ok, "set" if ok else "MISSING"))
    allow = safety.parse_allowlist(environ.get(safety.ALLOWLIST_ENV))
    allow_all = environ.get(safety.ALLOW_ALL_ENV) == "1"
    out.append(("allowlist", bool(allow) or allow_all,
                f"{len(allow)} number(s)" if allow else
                ("ALLOW_ALL developer opt-out is ON" if allow_all
                 else f"{safety.ALLOWLIST_ENV} empty: all dialing refused")))
    base = (environ.get("PUBLIC_BASE_URL") or "").strip()
    if base:
        d = dialer.TwilioDialer(environ, http_get=http_get)
        problem = d.preflight()
        out.append(("bridge health", not problem, "reachable" if not problem else problem))
    else:
        out.append(("bridge health", False, "PUBLIC_BASE_URL not set"))
    return out


def doctor() -> int:
    results = run_checks(os.environ)
    for name, ok, detail in results:
        print(f"[{'ok' if ok else 'FAIL'}] {name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1
