"""Business calls handed to linq-voice's GPT-Live stack (enckequity/teamshift#2981, #2982, #2992)."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
from test_shared_bridge import Shared

from hermes_phone import states
from hermes_phone.bridge import business
from hermes_phone.bridge.settings import BridgeSettings

URL = "http://127.0.0.1:8770/_internal/business-call"
BASE = "http://172.22.0.1:8790"
PIN = "pin-key-test"
NAME, TOKEN = "t-0123456789", "ts_usg_" + "a" * 64
GW, OWN = "gw-0123456789ab", "+17175550142"


class Voice:
    """linq-voice's /_internal/business-call: records each request, answers with the scripted reply."""

    def __init__(self, status: int = 202, reply: dict | None = None) -> None:
        self.status, self.reply, self.calls = status, reply if reply is not None else {"state": "dialing"}, []

    def __call__(self, url: str, key: str, body: dict, timeout_s: float) -> tuple[int, dict]:
        self.calls.append((url, key, body))
        return self.status, self.reply


@pytest.fixture
def voice(monkeypatch):
    v = Voice()
    monkeypatch.setattr(business, "post", v)
    return v


@pytest.fixture
def hb(store, clock, voice):
    return Shared(store, clock, business_call_url=URL, business_result_base=BASE, room_key=PIN)


def result_path(voice: Voice) -> str:
    return urlsplit(voice.calls[-1][2]["result_url"]).path


def send_result(sb: Shared, path: str, **kw):
    body = {"call_id": path.split("/")[4], "state": "completed", "outcome": "Booked for Friday 7 pm.",
            "agreed": "Friday 7 pm, table for 4, name Winch", "next_steps": "", "seconds": 84.4,
            "transcript_tail": "You: Hi, this is an AI assistant calling on behalf of Acme Plumbing.\n"
                               "Them: Sure, Friday at 7 works.", **kw}
    return sb.client.post(path, json=body)


def test_hands_off_instead_of_dialing(hb, voice):
    r = hb.put("c1")
    assert r.status_code == 201, r.text
    iid = r.json()["bridge_call_id"]
    assert not [p for p, _ in hb.tx.posts if p == "/calls"]  # the bridge placed no call itself
    url, key, body = voice.calls[0]
    assert (url, key) == (URL, PIN)
    assert set(body) == {"call_id", "workspace", "number", "brief", "result_url"}  # no caller ID asked for
    assert body["call_id"] == iid and body["workspace"] == "wa"  # an operator token workspace is keyed by name
    assert body["result_url"].startswith(f"{BASE}/v1/business/results/{iid}/")
    b = body["brief"]
    assert set(b) == {"goal", "facts", "disclosure", "max_seconds", "rules"}
    assert b["disclosure"].startswith("Hi, this is an AI assistant calling on behalf of Acme Plumbing")
    assert b["max_seconds"] == 300 and b["goal"] == "Book a table for 4"
    assert "name: Winch" in b["facts"] and "party_size: 4" in b["facts"]
    assert "never put the business on hold" in b["rules"] and "ask_user" not in b["rules"]
    assert hb.store.get_call(iid)["state"] == states.DIALING


def test_own_number_goes_only_with_the_workspaces_own_usage_token(store, clock, voice):
    sb = Shared(store, clock, business_call_url=URL, business_result_base=BASE, room_key=PIN,
                auth_url="https://auth.invalid/usage")
    reply = {"front_door_instance": GW, "caller_number": OWN}
    sb.app.state.authenticator.http_get = lambda _u, _h, _t: (200, json.dumps(reply))
    r = sb.client.put("/v1/calls/c1", json=sb.body("c1"),
                      headers={"Authorization": f"Bearer {TOKEN}", "X-TeamShift-Workspace": NAME})
    assert r.status_code == 201, r.text
    body = voice.calls[0][2]
    assert body["workspace"] == GW  # linq-voice matches it against the view's front_door_instance
    assert body["caller_number"] == OWN and body["caller_auth"] == {"name": NAME, "token": TOKEN}


def test_no_verified_number_sends_neither_field(store, clock, voice):
    sb = Shared(store, clock, business_call_url=URL, business_result_base=BASE, room_key=PIN,
                auth_url="https://auth.invalid/usage")
    sb.app.state.authenticator.http_get = lambda _u, _h, _t: (200, json.dumps({"front_door_instance": GW}))
    r = sb.client.put("/v1/calls/c1", json=sb.body("c1"),
                      headers={"Authorization": f"Bearer {TOKEN}", "X-TeamShift-Workspace": NAME})
    assert r.status_code == 201
    assert "caller_number" not in voice.calls[0][2] and "caller_auth" not in voice.calls[0][2]


def test_usage_workspace_without_a_gateway_is_never_handed_off(store, clock, voice):
    sb = Shared(store, clock, business_call_url=URL, business_result_base=BASE, room_key=PIN,
                auth_url="https://auth.invalid/usage")
    sb.app.state.authenticator.http_get = lambda _u, _h, _t: (200, "{}")
    r = sb.client.put("/v1/calls/c1", json=sb.body("c1"),
                      headers={"Authorization": f"Bearer {TOKEN}", "X-TeamShift-Workspace": NAME})
    assert r.status_code == 503 and r.json()["error"]["code"] == "allowance_unavailable"
    assert not voice.calls


def test_result_settles_the_call_once_and_a_repeat_is_ignored(hb, voice):
    iid = hb.put("c1").json()["bridge_call_id"]
    path = result_path(voice)
    r = send_result(hb, path)
    assert r.status_code == 200 and r.json() == {"ok": True}
    call = hb.store.get_call(iid)
    assert call["state"] == states.COMPLETED and call["outcome"] == "success"
    assert call["result"]["agreed"] == {"notes": "Friday 7 pm, table for 4, name Winch"}
    assert call["result"]["summary"] == "Booked for Friday 7 pm." and call["duration_s"] == 84
    up = hb.get("/v1/calls/c1/updates").json()
    assert up["call"]["state"] == "completed" and up["call"]["usage"]["seconds"] == 84
    assert [x["role"] for x in up["transcript"]] == ["agent", "callee"]
    # linq-voice re-sends an unconfirmed result after a restart: acknowledged, nothing changes
    again = send_result(hb, path, state="failed", outcome="The call was interrupted by a restart.")
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert hb.store.get_call(iid)["outcome"] == "success"
    assert len(hb.get("/v1/calls/c1/updates").json()["transcript"]) == 2


def test_result_needs_the_calls_own_secret_and_a_valid_body(hb, voice):
    iid = hb.put("c1").json()["bridge_call_id"]
    path = result_path(voice)
    assert send_result(hb, path.rsplit("/", 1)[0] + "/wrong").status_code == 404
    assert send_result(hb, path.replace(iid, "b" + "0" * 24)).status_code == 404
    assert send_result(hb, path, state="rescheduled").status_code == 400
    assert send_result(hb, path, call_id="b-other").status_code == 400
    assert hb.store.get_call(iid)["state"] == states.DIALING  # none of those counted
    assert send_result(hb, path).status_code == 200


def test_a_dialed_call_is_never_reachable_through_the_result_route(store, clock):
    sb = Shared(store, clock)  # no handoff: the bridge dials, so its webhook secret must not settle a result
    iid, hook = sb.dial("c1")
    secret = hook.rsplit("/", 1)[1]
    assert send_result(sb, f"/v1/business/results/{iid}/{secret}").status_code == 404


@pytest.mark.parametrize("state,final,outcome", [
    ("no_answer", states.NO_ANSWER, "no_answer"),
    ("machine", states.COMPLETED, "voicemail"),
    ("failed", states.FAILED, "failed"),
])
def test_other_results(hb, voice, state, final, outcome):
    iid = hb.put("c1").json()["bridge_call_id"]
    assert send_result(hb, result_path(voice), state=state, agreed="", seconds=0).status_code == 200
    call = hb.store.get_call(iid)
    assert (call["state"], call["outcome"]) == (final, outcome)


def test_agreed_with_an_open_choice_is_partial(hb, voice):
    iid = hb.put("c1").json()["bridge_call_id"]
    send_result(hb, result_path(voice), next_steps="Confirm 9 am instead of 10 am.")
    assert hb.store.get_call(iid)["outcome"] == "partial"


def test_nothing_agreed_is_followup(hb, voice):
    iid = hb.put("c1").json()["bridge_call_id"]
    send_result(hb, result_path(voice), agreed="", outcome="They will call back with prices.")
    assert hb.store.get_call(iid)["outcome"] == "needs_followup"


@pytest.mark.parametrize("status,reply,code,http", [
    (429, {"state": "allowance"}, "month_cap", 422),
    (429, {"state": "busy"}, "rate_limited", 429),
    (409, {"state": "duplicate"}, "dial_failed", 502),
    (400, {"error": "invalid business call request"}, "dial_failed", 502),
    (200, {"state": "failed"}, "dial_failed", 502),
])
def test_refusals_fail_the_call(hb, voice, status, reply, code, http):
    voice.status, voice.reply = status, reply
    r = hb.put("c1")
    assert r.status_code == http and r.json()["error"]["code"] == code
    assert len(voice.calls) == 1  # never re-sent
    iid = hb.store._one("SELECT internal_id FROM bridge_calls WHERE call_id='c1'")["internal_id"]
    assert hb.store.get_call(iid)["state"] == states.FAILED


def test_a_result_that_beats_the_reply_wins(hb, voice, monkeypatch):
    def placed_then_failed(url, key, body, timeout_s):  # linq-voice posts its result, then answers 200 failed
        voice.calls.append((url, key, body))
        assert send_result(hb, urlsplit(body["result_url"]).path, state="failed", agreed="",
                           outcome="The call could not be placed.").status_code == 200
        return 200, {"state": "failed"}
    monkeypatch.setattr(business, "post", placed_then_failed)
    assert hb.put("c1").status_code == 502
    iid = hb.store._one("SELECT internal_id FROM bridge_calls WHERE call_id='c1'")["internal_id"]
    call = hb.store.get_call(iid)
    assert call["state"] == states.FAILED and call["result"]["summary"] == "The call could not be placed."


def test_no_reply_keeps_the_call_dialing_for_its_result(hb, voice):
    voice.status, voice.reply = 0, {}
    r = hb.put("c1")
    assert r.status_code == 201 and len(voice.calls) == 1
    iid = r.json()["bridge_call_id"]
    assert hb.store.get_call(iid)["state"] == states.DIALING
    assert send_result(hb, result_path(voice)).status_code == 200
    assert hb.store.get_call(iid)["state"] == states.COMPLETED


def test_hangup_is_refused_and_restart_leaves_the_call_to_linq_voice(hb, voice, clock):
    iid = hb.put("c1").json()["bridge_call_id"]
    r = hb.post("/v1/calls/c1/hangup", {})
    assert r.status_code == 422 and r.json()["error"]["code"] == "hangup_unavailable"  # not 409: that reads as ended
    assert hb.app.state.reap(restart=True) == 0
    assert hb.store.get_call(iid)["state"] == states.DIALING
    clock.advance(300 + 120)  # past its cap, ring time and margin: the reaper ends it
    assert hb.app.state.reap() == 1
    assert hb.store.get_call(iid)["state"] == states.FAILED
    late = send_result(hb, result_path(voice))  # a result after the reaper: acknowledged, the call stays settled
    assert late.status_code == 200 and hb.store.get_call(iid)["state"] == states.FAILED


def test_settings_accept_only_loopback_endpoint_and_the_two_result_hosts():
    env = {"HERMES_PHONE_BUSINESS_CALL_URL": URL, "HERMES_PHONE_BUSINESS_RESULT_BASE": BASE + "/"}
    s = BridgeSettings.from_shared_env(env)
    assert (s.business_call_url, s.business_result_base) == (URL, BASE)
    assert "HERMES_PHONE_BUSINESS_RESULT_BASE" not in s.missing_env()
    assert "credential member_pin_key" in s.missing_env()  # no credentials directory in this test
    assert "HERMES_PHONE_BUSINESS_RESULT_BASE" in BridgeSettings.from_shared_env(
        {"HERMES_PHONE_BUSINESS_CALL_URL": URL}).missing_env()
    for bad in ("http://10.0.0.5:8770/_internal/business-call", "https://127.0.0.1/_internal/business-call",
                "http://127.0.0.1:8770/other"):
        with pytest.raises(ValueError):
            business.call_url(bad)
    for bad in ("http://10.0.0.5:8790", "https://127.0.0.1:8790", "http://172.22.0.1:8790/x",
                "http://u:p@127.0.0.1:8790"):
        with pytest.raises(ValueError):
            business.result_base(bad)


def test_result_parsing_bounds_and_transcript_roles():
    assert business.parse_result({"state": "completed", "seconds": "nan?"})["seconds"] == 0
    assert business.parse_result({"state": "completed", "seconds": 99999})["seconds"] == 420
    assert business.parse_result([]) is None and business.parse_result({"state": "x"}) is None
    assert business.transcript_lines("ou: cut first line\nYou: Hello.\nThem: Hi.\nnoise") == [
        ("agent", "Hello."), ("callee", "Hi.")]
