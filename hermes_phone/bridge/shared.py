"""Uvicorn factory for the shared multi-tenant bridge:

    uvicorn --factory hermes_phone.bridge.shared:create_app --host 127.0.0.1 --port 8790

Config comes from env and secrets from ``$CREDENTIALS_DIRECTORY`` (see settings.CRED_FILES). The
app answers both with and without the proxy's ``/bridge/phone`` prefix, so it works whether or
not the proxy strips it. Writes go only to the SQLite file (default ``/data``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from starlette.applications import Starlette
from starlette.routing import Mount

from . import app as appmod
from .settings import BridgeSettings

PREFIX = "/bridge/phone"
log = logging.getLogger(__name__)


def create_app() -> Starlette:
    settings = BridgeSettings.from_shared_env()
    missing = settings.missing_env()
    if missing:  # names only, never values; /healthz stays up and new calls get 503
        log.warning("phone bridge missing config (calls refused): %s", ", ".join(missing))
    inner = appmod.create_app(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):  # mounted apps get no lifespan: run the sweeps here
        task = asyncio.create_task(inner.state.sweeper())
        try:
            yield
        finally:
            task.cancel()

    return Starlette(routes=[Mount(PREFIX, app=inner), Mount("/", app=inner)],
                     lifespan=lifespan)
