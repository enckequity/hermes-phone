"""Usage-token auth against a fake auth service, and the shared deploy factory."""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from hermes_phone.bridge import auth, shared

TOK = "ts_usg_" + "a" * 64
WS = "t-0123456789"


class FakeAuth:
    def __init__(self) -> None:
        self.status, self.delay, self.requests = 200, 0.0, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append({"auth": self.headers.get("Authorization"),
                                       "ws": self.headers.get("x-teamshift-workspace")})
                time.sleep(outer.delay)
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/verify"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = FakeAuth()
    yield f
    f.server.shutdown()


def authn(url: str, now: list[float], **kw) -> auth.Authenticator:
    return auth.Authenticator(signing_key="k", auth_url=url, overrides={}, clock=lambda: now[0],
                              **kw)


def test_usage_token_valid_and_cached(fake):
    now = [1000.0]
    a = authn(fake.url, now)
    assert a.check(f"Bearer {TOK}", WS) == (auth.Workspace(WS), "")
    assert fake.requests == [{"auth": f"Bearer {TOK}", "ws": WS}]
    assert a.check(f"Bearer {TOK}", WS)[1] == ""
    assert len(fake.requests) == 1  # cache hit
    now[0] += auth.CACHE_TTL_S + 1
    assert a.check(f"Bearer {TOK}", WS)[1] == "" and len(fake.requests) == 2


def test_usage_token_401_5xx_timeout_not_cached(fake):
    now = [0.0]
    a = authn(fake.url, now, timeout_s=0.2)
    fake.status = 401
    assert a.check(f"Bearer {TOK}", WS) == (None, "unauthorized")
    fake.status = 200
    assert a.check(f"Bearer {TOK}", WS) == (None, "unauthorized")  # 401 negative-cached
    assert len(fake.requests) == 1
    now[0] += auth.DENY_TTL_S + 1
    fake.status = 503
    assert a.check(f"Bearer {TOK}", WS) == (None, "auth_unavailable")
    fake.status, fake.delay = 200, 1.0
    assert a.check(f"Bearer {TOK}", WS) == (None, "auth_unavailable")
    fake.delay = 0.0
    assert a.check(f"Bearer {TOK}", WS)[1] == ""  # failures were never cached
    assert len(fake.requests) == 4


def test_usage_token_bad_shapes_never_reach_service(fake):
    a = authn(fake.url, [0.0])
    for header, ws in ((f"Bearer {TOK}", "acme"), (f"Bearer {TOK}", ""),
                       ("Bearer ts_usg_" + "A" * 64, WS), ("Bearer ts_usg_abc", WS),
                       (f"Basic {TOK}", WS), ("", WS)):
        assert a.check(header, ws) == (None, "unauthorized")
    assert authn("", [0.0]).check(f"Bearer {TOK}", WS) == (None, "unauthorized")  # path off
    assert fake.requests == []


def test_usage_token_disabled_override(fake):
    a = auth.Authenticator(signing_key="", auth_url=fake.url, clock=lambda: 0.0,
                           overrides={WS: {"disabled": True}})
    assert a.check(f"Bearer {TOK}", WS) == (None, "workspace_disabled")


def test_bridge_routes_usage_token(fake, tmp_path, monkeypatch):
    client = factory_client(tmp_path, monkeypatch, HERMES_PHONE_BRIDGE_AUTH_URL=fake.url)
    h = {"Authorization": f"Bearer {TOK}", "X-TeamShift-Workspace": WS}
    assert client.get("/bridge/phone/v1/calls/nope/updates", headers=h).status_code == 404
    fake.status = 401
    h2 = {**h, "Authorization": "Bearer ts_usg_" + "b" * 64}
    assert client.get("/bridge/phone/v1/calls/nope/updates", headers=h2).status_code == 401
    fake.status = 500
    h3 = {**h, "Authorization": "Bearer ts_usg_" + "c" * 64}
    r = client.get("/bridge/phone/v1/calls/nope/updates", headers=h3)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "auth_unavailable")


def factory_client(tmp_path, monkeypatch, **env) -> TestClient:
    creds = tmp_path / "creds"
    creds.mkdir()
    for name in ("openai_api_key", "telnyx_api_key", "telnyx_public_key", "bridge_signing_key"):
        (creds / name).write_text(f"value-of-{name}\n")
    base = {"CREDENTIALS_DIRECTORY": str(creds), "HERMES_PHONE_DB_PATH": str(tmp_path / "b.db"),
            "HERMES_PHONE_BRIDGE_PUBLIC_URL": "https://voice.example/bridge/phone",
            "TELNYX_CONNECTION_ID": "conn", "HERMES_PHONE_CALLER_NUMBER": "+15555550100"}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    return TestClient(shared.create_app())


def test_factory_prefix_and_credential_files(tmp_path, monkeypatch):
    client = factory_client(tmp_path, monkeypatch)
    assert client.get("/healthz").status_code == 200
    assert client.get("/bridge/phone/healthz").status_code == 200
    tok = auth.mint("value-of-bridge_signing_key", "acme")
    r = client.get("/bridge/phone/v1/health", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert client.post("/bridge/phone/v1/telnyx/inbound").status_code == 404  # off by default


def test_factory_ignores_secrets_in_plain_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-plain-env")
    monkeypatch.setenv("TELNYX_API_KEY", "plain")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "empty"))
    from hermes_phone.bridge.settings import BridgeSettings

    s = BridgeSettings.from_shared_env()
    assert s.openai_api_key == "" and s.telnyx_api_key == ""
    assert "credential openai_api_key" in s.missing_env()
