"""Profile-scoped secret lookup, no bare os.environ reads of secrets, rate-limit proxy trust."""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

from bridge_support import Harness, make_settings

from hermes_phone import secrets as sec
from hermes_phone import tools
from hermes_phone.bridge import BridgeSettings

ROOT = Path(__file__).resolve().parent.parent
NAMES = sorted(sec.SECRET_NAMES)


def fake_scope(monkeypatch, get_secret):
    agent = types.ModuleType("agent")
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = get_secret
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)


def no_hermes(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # import raises ImportError


def test_injected_mapping_wins_and_ignores_hermes(monkeypatch):
    fake_scope(monkeypatch, lambda n, d=None: "from-hermes")
    assert sec.get("OPENAI_API_KEY", {"OPENAI_API_KEY": "from-test"}) == "from-test"
    assert sec.get("OPENAI_API_KEY", {}) == ""


def test_falls_back_to_process_env_without_hermes(monkeypatch):
    no_hermes(monkeypatch)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert sec.get("TWILIO_AUTH_TOKEN") == "tok"
    assert sec.get("OPENAI_API_KEY") == ""


def test_uses_hermes_secret_scope_not_process_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "launch-profile-key")
    fake_scope(monkeypatch, lambda n, d=None: {"OPENAI_API_KEY": "profile-b-key"}.get(n))
    assert sec.get("OPENAI_API_KEY") == "profile-b-key"
    assert sec.get("PUBLIC_BASE_URL") == ""  # None / missing -> ""


def test_unscoped_error_fails_closed(monkeypatch):
    class UnscopedSecretError(RuntimeError):
        pass

    def boom(name, default=None):
        raise UnscopedSecretError(name)

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    fake_scope(monkeypatch, boom)
    assert sec.get("OPENAI_API_KEY") == ""
    assert tools.PhoneService().environ.get("OPENAI_API_KEY") is None
    assert "OPENAI_API_KEY" not in tools.PhoneService().environ


def test_environ_view_routes_only_secret_names(monkeypatch):
    monkeypatch.setenv("HERMES_PHONE_MODE", "web")
    monkeypatch.setenv("OPENAI_API_KEY", "launch")
    fake_scope(monkeypatch, lambda n, d=None: "scoped" if n == "OPENAI_API_KEY" else None)
    env = tools.PhoneService().environ
    assert env.get("OPENAI_API_KEY") == "scoped"
    assert env.get("HERMES_PHONE_MODE") == "web"  # non-secret config stays on os.environ
    assert env["OPENAI_API_KEY"] == "scoped"


def test_service_and_dialer_follow_the_active_profile(monkeypatch):
    current = {}
    fake_scope(monkeypatch, lambda n, d=None: current.get(n))
    monkeypatch.delenv("HERMES_PHONE_MODE", raising=False)
    svc = tools.PhoneService()
    assert svc.dialer.configured is False
    current.update(OPENAI_API_KEY="k", PUBLIC_BASE_URL="https://x.example")
    assert svc.mode == "web" and svc.dialer.configured
    current.clear()
    assert svc.dialer.configured is False


def test_bridge_settings_read_through_the_helper(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "launch")
    fake_scope(monkeypatch, lambda n, d=None: {"OPENAI_API_KEY": "scoped",
                                               "PUBLIC_BASE_URL": "https://b.example/"}.get(n))
    s = BridgeSettings.from_env()
    assert s.openai_api_key == "scoped" and s.public_base_url == "https://b.example"
    assert s.twilio_auth_token == ""
    injected = BridgeSettings.from_env({"OPENAI_API_KEY": "t", "PORT": "9"})
    assert injected.openai_api_key == "t" and injected.port == 9


def test_no_bare_environ_reads_of_secret_names():
    files = [*(ROOT / "hermes_phone").glob("*.py"), *(ROOT / "hermes_phone/bridge").glob("*.py")]
    pat = re.compile(r"os\.(environ|getenv)")
    names = re.compile("|".join(NAMES))
    offenders = []
    for path in files:
        if path.name == "secrets.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if pat.search(line) and names.search(line):
                offenders.append(f"{path.name}:{n}")
        text = path.read_text()
        # os.environ passed wholesale to something that reads secrets
        assert "run_checks(os.environ)" not in text
    assert not offenders, offenders
    # secret names are only read via .get / helper on injected or wrapped mappings
    for path in files:
        if path.name in ("secrets.py", "settings.py"):
            continue
        assert not re.search(r"os\.environ\b[^\n]*\b(" + "|".join(NAMES) + ")", path.read_text())


# ---- rate limiter keying ----
def hammer(h, n=40, xff=None):
    codes = []
    for i in range(n):
        headers = {"x-forwarded-for": xff(i)} if xff else {}
        codes.append(h.client.get("/phone/nope", params={"t": "x"}, headers=headers).status_code)
    return codes


def test_spoofed_forwarded_for_does_not_bypass_limit_by_default(store, clock):
    h = Harness(store, clock)
    assert 429 in hammer(h, xff=lambda i: f"10.0.0.{i}")


def test_forwarded_for_honoured_only_with_trust_proxy(store, clock):
    env = {"HERMES_PHONE_TRUST_PROXY": "1"}
    h = Harness(store, clock, environ=env)
    assert 429 not in hammer(h, n=20, xff=lambda i: f"10.0.1.{i}")  # distinct real clients
    assert 429 in hammer(h, n=40, xff=lambda i: "10.9.9.9")  # one client still limited
    assert make_settings(environ=env).environ["HERMES_PHONE_TRUST_PROXY"] == "1"
