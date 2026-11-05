"""TwilioDialer, PhoneService wiring, call-time bridge check and CLI (no network)."""
from __future__ import annotations

import json

from bridge_support import BASE, FakeTwilio
from conftest import OTHER_NUMBER, TEST_NUMBER, FakeCtx, approve, prepare

from hermes_phone import cli, tools
from hermes_phone.dialer import TwilioDialer, missing_env

ENV = {"TWILIO_ACCOUNT_SID": "ACx", "TWILIO_AUTH_TOKEN": "sekret-token",
       "TWILIO_FROM_NUMBER": "+15555550100", "PUBLIC_BASE_URL": BASE + "/",
       "OPENAI_API_KEY": "sk-secret", "HERMES_PHONE_TEST_ALLOWLIST": TEST_NUMBER}


def call_row(number=TEST_NUMBER, max_s=300):
    return {"id": "c1", "number": number, "provider_sid": "",
            "brief": {"limits": {"max_duration_s": max_s}}}


def test_place_call_parameters():
    tw = FakeTwilio()
    res = TwilioDialer(ENV, client=tw).place_call(call_row(max_s=120), {})
    assert res.ok and res.provider_sid.startswith("CA")
    kw = tw.created[0]
    assert kw["to"] == TEST_NUMBER and kw["from_"] == "+15555550100"
    assert kw["url"] == f"{BASE}/twiml/c1" and kw["status_callback"] == f"{BASE}/status/c1"
    assert set(kw["status_callback_event"]) == {"initiated", "ringing", "answered", "completed"}
    assert kw["async_amd"] == "true" and kw["machine_detection"] == "Enable"
    assert kw["time_limit"] == 120 + 15 and kw["method"] == "POST"


def test_dialer_refuses_non_allowlisted_and_blocked_even_when_called_directly():
    tw = FakeTwilio()
    d = TwilioDialer(ENV, client=tw)
    assert not d.place_call(call_row(OTHER_NUMBER), {}).ok
    assert not d.place_call(call_row("+1911"), {}).ok
    assert not d.place_call(call_row("+19005551234"), {}).ok
    open_env = {**ENV, "HERMES_PHONE_ALLOW_ALL": "1"}
    assert not TwilioDialer(open_env, client=tw).place_call(call_row("+112"), {}).ok
    no_list = {k: v for k, v in ENV.items() if k != "HERMES_PHONE_TEST_ALLOWLIST"}
    assert not TwilioDialer(no_list, client=tw).place_call(call_row(), {}).ok
    assert tw.created == []


def test_dialer_reports_twilio_errors_and_hangup():
    class Boom(FakeTwilio):
        def create(self, **kw):
            raise RuntimeError("401")

    res = TwilioDialer(ENV, client=Boom()).place_call(call_row(), {})
    assert not res.ok and "RuntimeError" in res.error
    tw = FakeTwilio()
    assert TwilioDialer(ENV, client=tw).hangup({"provider_sid": "CA9"}) is True
    assert tw.updates == [{"sid": "CA9", "status": "completed"}]
    assert TwilioDialer(ENV, client=tw).hangup({"provider_sid": ""}) is False


def test_preflight_health():
    ok = TwilioDialer(ENV, http_get=lambda url, t: 200 if url == BASE + "/healthz" else 404)
    assert ok.preflight() == ""

    def down(url, t):
        raise ConnectionError("refused")

    assert "not reachable" in TwilioDialer(ENV, http_get=down).preflight()
    assert "503" in TwilioDialer(ENV, http_get=lambda u, t: 503).preflight()


def test_service_picks_dialer_from_env(store, ctx):
    full = tools.PhoneService(ctx=ctx, store=store, environ=ENV)
    assert isinstance(full.dialer, TwilioDialer)
    partial = tools.PhoneService(ctx=ctx, store=store,
                                 environ={"TWILIO_ACCOUNT_SID": "x"})
    assert isinstance(partial.dialer, tools.NullDialer) and partial.dialer.configured is False
    assert "PUBLIC_BASE_URL" in partial.dialer.missing
    assert missing_env({}) == list(("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                                    "TWILIO_FROM_NUMBER", "PUBLIC_BASE_URL"))


def test_phone_call_not_configured_fails_closed(store, ctx, clock):
    tools.configure(ctx=ctx, store=store, environ={"HERMES_PHONE_TEST_ALLOWLIST": TEST_NUMBER})
    try:
        cid = prepare()["call_id"]
        approve(None, cid)
        out = json.loads(tools.phone_call({"call_id": cid}))
        assert out["configured"] is False and "not configured" in out["error"]
        assert "TWILIO_AUTH_TOKEN" in out["error"] and "sekret" not in out["error"]
        assert store.get_call(cid)["state"] == "approved"  # approval not burnt
        assert store.get_approval(cid)["used_at"] is None
    finally:
        tools._service = None


def test_phone_call_fails_closed_when_bridge_unhealthy_then_dials_when_healthy(store, ctx):
    tw = FakeTwilio()
    state = {"up": False}

    def http_get(url, timeout):
        if not state["up"]:
            raise ConnectionError("down")
        return 200

    dialer = TwilioDialer(ENV, client=tw, http_get=http_get)
    tools.configure(ctx=ctx, store=store, dialer=dialer, environ=ENV)
    try:
        cid = prepare()["call_id"]
        approve(None, cid)
        out = json.loads(tools.phone_call({"call_id": cid}))
        assert out["bridge_unreachable"] is True and "Nothing was dialed" in out["error"]
        assert tw.created == [] and store.get_call(cid)["state"] == "approved"
        state["up"] = True
        out = json.loads(tools.phone_call({"call_id": cid}))
        assert out["state"] == "dialing" and len(tw.created) == 1
        assert store.get_call(cid)["provider_sid"].startswith("CA")
    finally:
        tools._service = None


def test_dial_failure_marks_failed(store, ctx):
    class Boom(FakeTwilio):
        def create(self, **kw):
            raise RuntimeError("x")

    tools.configure(ctx=ctx, store=store, dialer=TwilioDialer(
        ENV, client=Boom(), http_get=lambda u, t: 200), environ=ENV)
    try:
        cid = prepare()["call_id"]
        approve(None, cid)
        out = json.loads(tools.phone_call({"call_id": cid}))
        assert "error" in out and store.get_call(cid)["state"] == "failed"
    finally:
        tools._service = None


# ---- CLI --------------------------------------------------------------------------------
def test_doctor_prints_names_only_never_secrets(capsys, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("hermes_phone.dialer._http_status", lambda url, t: 200)
    assert cli.doctor() == 0
    out = capsys.readouterr().out
    assert "TWILIO_AUTH_TOKEN" in out and "sekret-token" not in out and "sk-secret" not in out
    assert "[FAIL]" not in out


def test_doctor_flags_missing_and_empty_allowlist():
    res = {n: (ok, d) for n, ok, d in cli.run_checks(
        {"TWILIO_ACCOUNT_SID": "x"}, http_get=lambda u, t: 200)}
    assert res["env TWILIO_AUTH_TOKEN"][0] is False
    assert res["allowlist"][0] is False and "refused" in res["allowlist"][1]
    assert res["bridge health"][0] is False


def test_register_adds_cli_command_and_serve_requires_env(capsys, monkeypatch):
    import argparse

    import hermes_phone

    ctx = FakeCtx()
    hermes_phone.register(ctx)
    assert set(ctx.cli) == {"phone"}
    parser = argparse.ArgumentParser()
    ctx.cli["phone"]["setup"](parser)
    args = parser.parse_args(["serve"])
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "OPENAI_API_KEY",
              "PUBLIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert ctx.cli["phone"]["handler"](args) == 2
    assert "missing env" in capsys.readouterr().out
