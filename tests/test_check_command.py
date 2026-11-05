"""`/phone check` reports readiness without leaking secret values."""
from hermes_phone import commands, tools


def _run(env):
    tools.configure(environ=env)
    return commands.phone_command("check")


def test_check_reports_masked_allowlist_and_names_only():
    out = _run({"HERMES_PHONE_TEST_ALLOWLIST": "+12125550123, junk",
                "TWILIO_AUTH_TOKEN": "supersecret", "PUBLIC_BASE_URL": "https://x"})
    assert "1 valid number(s): +1••••••0123" in out
    assert "1 entry not valid E.164" in out
    assert "TWILIO_AUTH_TOKEN: set" in out and "TWILIO_ACCOUNT_SID: MISSING" in out
    assert "supersecret" not in out and "2125550123" not in out


def test_check_when_allowlist_not_visible():
    out = _run({})
    assert "NOT visible to the plugin" in out


def test_check_flags_env_example_placeholders():
    out = _run({"HERMES_PHONE_TEST_ALLOWLIST": "+15555550199",
                "TWILIO_ACCOUNT_SID": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "TWILIO_FROM_NUMBER": "+15555550100",
                "PUBLIC_BASE_URL": "https://your-tunnel.trycloudflare.com"})
    assert "(PLACEHOLDER)" in out
    assert out.count("PLACEHOLDER from .env.example") == 3


def test_status_shows_verbatim_failure_reason(tmp_path):
    from hermes_phone import states
    from hermes_phone.store import Store
    store = Store(path=tmp_path / "d.db")
    tools.configure(store=store, environ={})
    cid = "c1"
    store.create_call(cid, state=states.FAILED, callee_name="R", number="+12125550123",
                      number_source="user", goal="g", brief={"goal": "g"})
    store.update_call(cid, state=states.FAILED, reason="twilio error: code 21219 unverified")
    out = commands.phone_command(f"status {cid}")
    assert "reason: twilio error: code 21219" in out
