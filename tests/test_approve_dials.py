"""Tapping Call dials right away; it does not depend on the model calling phone_call."""
from conftest import prepare

from hermes_phone import commands, states, tools


def test_approve_confirm_dials_immediately(svc):
    cid = prepare()["call_id"]
    out = commands.phone_command(f"approve {cid} confirm", user_id="u1", session_id="s1")
    assert out == "", out
    assert tools.get_service().store.get_call(cid)["state"] == states.DIALING
    assert len(tools.get_service().dialer.calls) == 1


def test_approve_again_on_approved_call_dials_instead_of_erroring(svc):
    cid = prepare()["call_id"]
    commands.record_user_approval(cid, {"user_id": "u1", "session_id": "s1"})
    out = commands.phone_command(f"approve {cid}", user_id="u1", session_id="s1")
    assert out == "", out
    again = commands.phone_command(f"approve {cid}", user_id="u1", session_id="s1")
    assert "only a call awaiting approval" in again  # dialing now; never a second dial
    assert len(tools.get_service().dialer.calls) == 1


def test_approve_reports_dial_failure_with_status_hint(svc):
    cid = prepare()["call_id"]

    class Failing:
        calls: list = []

        def place_call(self, call, payload):
            return tools.DialResult(ok=False, error="twilio error: code 21219 unverified")

    tools.get_service().dialer = Failing()
    out = commands.phone_command(f"approve {cid} confirm", user_id="u1", session_id="s1")
    assert "dialing failed" in out and "21219" in out and f"/phone status {cid}" in out


def test_approval_falls_back_to_a_text_reply_when_no_status_message_can_be_posted(svc, monkeypatch):
    monkeypatch.setattr(commands, "_status_posted", lambda call_id: False)
    cid = prepare()["call_id"]
    out = commands.phone_command(f"approve {cid} confirm", user_id="u1", session_id="s1")
    assert out.startswith("Approved. Dialing"), out
