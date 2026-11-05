"""Shared fixtures: fake Hermes ctx, tmp store, fake clock. No network, no Hermes."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):  # importlib mode: make helpers and the package importable
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hermes_phone import tools  # noqa: E402
from hermes_phone.store import Store  # noqa: E402

TEST_NUMBER = "+12125550123"  # New York style number
OTHER_NUMBER = "+12125550188"


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.edits: list[str] = []
        self.ok = True

    def edit(self, text: str) -> dict:
        self.edits.append(text)
        return {"ok": True}


class FakeCtx:
    """Records register_* calls like Hermes' PluginContext, with scripted ask_choice."""

    def __init__(self, config: dict | None = None, choices: list | None = None) -> None:
        self.tools: dict[str, dict] = {}
        self.hooks: list[tuple[str, object]] = []
        self.commands: dict[str, dict] = {}
        self.skills: list[dict] = []
        self.messages: list[FakeMessage] = []
        self.asked: list[tuple[str, list]] = []
        self.config = dict(config or {})
        self.choices = list(choices or [])

    def register_tool(self, name, toolset, schema, handler, check_fn=None, requires_env=None,
                      is_async=False, description="", emoji="", override=False):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

    def register_hook(self, hook_name, callback, *, hold_stream=False):
        self.hooks.append((hook_name, callback))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = {"handler": handler, "description": description,
                               "args_hint": args_hint}

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append({"name": name, "path": Path(path), "description": description})

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli = getattr(self, "cli", {})
        self.cli[name] = {"setup": setup_fn, "handler": handler_fn}

    def post_message(self, text):
        msg = FakeMessage(text)
        self.messages.append(msg)
        return msg

    def ask_choice(self, question, choices):
        self.asked.append((question, list(choices)))
        return self.choices.pop(0) if self.choices else None

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def hook(self, name):
        return next(cb for n, cb in self.hooks if n == name)


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> Clock:
    # 2026-11-04 15:00:00 UTC == 10:00 in New York: inside quiet-hours window.
    from datetime import UTC, datetime

    return Clock(datetime(2026, 11, 4, 15, 0, tzinfo=UTC).timestamp())


@pytest.fixture
def store(tmp_path, clock) -> Store:
    s = Store(path=tmp_path / "phone.db", clock=clock)
    yield s
    s.close()


@pytest.fixture
def ctx() -> FakeCtx:
    return FakeCtx(config={"user_name": "Winch"})


@pytest.fixture
def env() -> dict:
    return {"HERMES_PHONE_TEST_ALLOWLIST": TEST_NUMBER}


@pytest.fixture
def svc(store, ctx, env):
    dialer = tools.NullDialer()
    service = tools.configure(ctx=ctx, store=store, dialer=dialer, environ=env)
    yield service
    for watcher in list(service.watchers.values()):
        watcher.stop.set()
    tools._service = None


def prepare(args: dict | None = None, **kwargs) -> dict:
    import json

    base = {"callee_name": "Bella Napoli", "number": TEST_NUMBER, "goal": "Book a table for 4",
            "facts_to_share": {"name": "Winch", "party_size": 4}}
    base.update(args or {})
    return json.loads(tools.phone_prepare(base, **kwargs))


def approve(svc, call_id: str, user: str = "u1") -> None:
    from hermes_phone import commands

    # Record the approval only; `/phone approve` itself also dials (see test_approve_dials.py).
    commands.record_user_approval(call_id, {"user_id": user, "session_id": "s1"})


@pytest.fixture
def harness(store, clock, svc):
    from bridge_support import Harness

    return Harness(store, clock)
