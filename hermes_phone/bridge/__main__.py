"""``python -m hermes_phone.bridge``: serve the bridge with uvicorn. Config from env only."""
from __future__ import annotations

import sys

from .app import create_app
from .settings import BridgeSettings


def main() -> int:
    import uvicorn

    settings = BridgeSettings.from_env()
    missing = settings.missing_env()
    if missing:
        # Fail safe, not fatal: /healthz stays up (so a supervisor does not crash-loop) while every
        # Twilio webhook is refused and phone_call fails closed. Names only, never values.
        print("hermes-phone bridge: WARNING missing env (calls will be refused): "
              + ", ".join(missing), file=sys.stderr, flush=True)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
