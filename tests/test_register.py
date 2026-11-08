"""register(): no sockets, matches plugin.yaml, hook contract, Hermes-style path loading."""
from __future__ import annotations

import importlib.util
import inspect
import re
import socket
import sys
import types
from pathlib import Path

import pytest
import yaml
from conftest import FakeCtx

import hermes_phone
from hermes_phone import tools

ROOT = Path(__file__).resolve().parent.parent
VALID_HOOKS = {"pre_tool_call", "pre_llm_call", "transform_llm_output"}


@pytest.fixture
def manifest():
    return yaml.safe_load((ROOT / "plugin.yaml").read_text())


@pytest.fixture
def registered(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PHONE_DB", str(tmp_path / "reg.db"))
    ctx = FakeCtx()
    hermes_phone.register(ctx)
    yield ctx
    tools._service = None


def test_register_opens_no_sockets(tmp_path, monkeypatch):
    def deny(*a, **k):
        raise RuntimeError("network access is disabled while Plugin Doctor runs")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setenv("HERMES_PHONE_DB", str(tmp_path / "reg.db"))
    ctx = FakeCtx()
    hermes_phone.register(ctx)
    assert not (tmp_path / "reg.db").exists()  # store is lazy: register touches no database
    tools._service = None


def test_register_does_not_import_heavy_modules():
    code = ("import sys, hermes_phone\n"
            "class C:\n"
            "  def __getattr__(self, n): return lambda *a, **k: None\n"
            "hermes_phone.register(C())\n"
            "bad = [m for m in ('phonenumbers','twilio','fastapi','uvicorn','websockets')"
            " if m in sys.modules]\n"
            "print(bad)\n")
    import subprocess

    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                         cwd=ROOT, env={"PYTHONPATH": str(ROOT)}, check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_registrations_match_manifest(registered, manifest):
    assert set(registered.tools) == set(manifest["provides_tools"]) and len(registered.tools) == 4
    assert [n for n, _ in registered.hooks] and sorted(n for n, _ in registered.hooks) == sorted(
        manifest["provides_hooks"])
    assert set(n for n, _ in registered.hooks) <= VALID_HOOKS
    assert set(registered.commands) == {"phone"}
    for name, t in registered.tools.items():
        assert t["toolset"] and t["schema"]["name"] == name and callable(t["handler"])
        assert t["schema"]["parameters"]["type"] == "object"


def test_skill_registered_and_exists(registered):
    assert len(registered.skills) == 1
    skill = registered.skills[0]
    assert skill["name"] == "phone-calls" and skill["path"].exists()
    text = skill["path"].read_text()
    assert text.startswith("---\nname: phone-calls\n") and "phone_prepare" in text
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", skill["name"])


def test_hook_callbacks_accept_kwargs(registered):
    for name, cb in registered.hooks:
        kinds = [p.kind for p in inspect.signature(cb).parameters.values()]
        assert inspect.Parameter.VAR_KEYWORD in kinds, name


def test_handlers_and_command_accept_kwargs(registered):
    for t in registered.tools.values():
        kinds = [p.kind for p in inspect.signature(t["handler"]).parameters.values()]
        assert inspect.Parameter.VAR_KEYWORD in kinds
    cmd = registered.commands["phone"]["handler"]
    assert inspect.Parameter.VAR_KEYWORD in [
        p.kind for p in inspect.signature(cmd).parameters.values()]


def test_hooks_survive_extra_kwargs(registered, tmp_path):
    pre = registered.hook("pre_tool_call")
    assert pre(tool_name="x", args={}, task_id="t", session_id="s", tool_call_id="1",
               turn_id="2", api_request_id="3", middleware_trace=[], future_kw=1) is None
    llm = registered.hook("pre_llm_call")
    assert llm(session_id="s", task_id="t", turn_id="1", user_message="hi",
               conversation_history=[], is_first_turn=True, model="m", platform="cli",
               parent_session_id="", sender_id="") is None


def test_manifest_fields(manifest):
    for key in ("name", "version", "description"):
        assert manifest[key]
    assert manifest["name"] == "hermes-phone"
    assert manifest["homepage"] == "https://github.com/winchxyz/hermes-phone"
    for entry in manifest["requires_env"]:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", entry["name"])
    # developer-only switches must not be declared as user-configurable env
    declared = {e["name"] for e in manifest["requires_env"]}
    assert not {"HERMES_PHONE_TEST_ALLOWLIST", "HERMES_PHONE_ALLOW_ALL"} & declared
    allowed_types = {"str", "string", "int", "integer", "float", "number", "bool", "boolean"}
    for spec in manifest["config_schema"].values():
        assert spec["type"] in allowed_types


def test_all_dependencies_upper_bounded(manifest):
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    deps = (list(manifest["python_dependencies"]) + pyproject["dependencies"]
            + [d for group in pyproject.get("optional-dependencies", {}).values() for d in group])
    assert deps
    # the only extra is the shared bridge's; the plugin inside Hermes never installs it
    assert set(pyproject.get("optional-dependencies", {})) <= {"bridge"}
    for dep in deps:
        assert re.search(r"<\s*\d|==|~=", dep), f"no upper bound: {dep}"
    assert sorted(manifest["python_dependencies"]) == sorted(pyproject["dependencies"])


def test_version_consistent(manifest):
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert manifest["version"] == pyproject["project"]["version"] == hermes_phone.__version__


def test_loads_like_hermes_by_path(tmp_path, monkeypatch):
    """Replicates plugins_loader._load_directory_module: root __init__.py as a package."""
    monkeypatch.setenv("HERMES_PHONE_DB", str(tmp_path / "ld.db"))
    ns = "hermes_plugins"
    if ns not in sys.modules:
        pkg = types.ModuleType(ns)
        pkg.__path__ = []
        sys.modules[ns] = pkg
    name = f"{ns}.hermes_phone_loadtest"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        ctx = FakeCtx()
        module.register(ctx)
        assert len(ctx.tools) == 4 and len(ctx.hooks) == 3 and "phone" in ctx.commands
        assert ctx.skills and ctx.skills[0]["path"].exists()
        # the tools registered through this load work end to end
        out = ctx.tools["phone_status"]["handler"]({})
        assert '"calls"' in out
    finally:
        for m in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
            del sys.modules[m]
        tools._service = None
