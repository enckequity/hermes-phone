"""Bridge HTTP surface: signatures, /twiml gating, status callbacks (SPEC criteria 1, 8)."""
from __future__ import annotations

import json

from bridge_support import BASE, FakeTwilio, post_signed, sign
from conftest import prepare

from hermes_phone import states

SID = "CA" + "1" * 32


def test_healthz(harness):
    assert harness.client.get("/healthz").json() == {"ok": True}


def test_twiml_rejects_unsigned_and_bad_signature(harness):
    cid = harness.dialing_call()
    assert post_signed(harness.client, f"/twiml/{cid}", {"CallSid": SID},
                       signature="").status_code == 403
    assert post_signed(harness.client, f"/twiml/{cid}", {"CallSid": SID},
                       token="wrong").status_code == 403
    # signature computed for another path must not validate either
    bad = sign("/twiml/other", {"CallSid": SID})
    assert post_signed(harness.client, f"/twiml/{cid}", {"CallSid": SID},
                       signature=bad).status_code == 403
    assert harness.store.media_token_used(cid) is False


def test_twiml_returns_stream_with_token_parameter(harness):
    cid = harness.dialing_call()
    r = post_signed(harness.client, f"/twiml/{cid}", {"CallSid": SID})
    assert r.status_code == 200
    assert f'<Stream url="wss://bridge.example.test/media/{cid}">' in r.text
    assert "?" not in r.text.split("<Stream url=")[1].split(">")[0]  # no query string allowed
    assert '<Parameter name="token" value="' in r.text


def test_twiml_refused_for_unapproved_call(harness):
    cid = prepare()["call_id"]  # awaiting_approval, approval never recorded
    r = post_signed(harness.client, f"/twiml/{cid}", {"CallSid": SID})
    assert r.status_code == 403 and "Stream" not in r.text


def test_twiml_refused_when_approval_not_consumed(harness):
    from conftest import approve

    cid = prepare()["call_id"]
    approve(None, cid)  # approved, but phone_call never consumed it
    assert post_signed(harness.client, f"/twiml/{cid}", {"CallSid": SID}).status_code == 403


def test_twiml_refused_unknown_call_and_not_allowlisted(harness):
    assert post_signed(harness.client, "/twiml/nope", {"CallSid": SID}).status_code == 403
    cid = harness.dialing_call()
    harness.settings.environ = {}  # allowlist gone: bridge refuses on its own
    assert post_signed(harness.client, f"/twiml/{cid}", {"CallSid": SID}).status_code == 403


def test_twiml_second_request_after_stream_connected_refused(harness):
    cid = harness.dialing_call()
    token = harness.token(cid)
    assert harness.store.consume_media_token(cid, token)
    assert post_signed(harness.client, f"/twiml/{cid}", {"CallSid": SID}).status_code == 403


# ---- status callbacks ---------------------------------------------------------------------
def status(h, cid, **p):
    params = {"CallSid": SID, **p}
    return post_signed(h.client, f"/status/{cid}", params)


def test_status_unsigned_is_403_and_changes_nothing(harness):
    cid = harness.dialing_call()
    r = post_signed(harness.client, f"/status/{cid}", {"CallSid": SID, "CallStatus": "failed"},
                    signature="")
    assert r.status_code == 403
    r = post_signed(harness.client, f"/status/{cid}", {"CallSid": SID, "CallStatus": "failed"},
                    token="bad")
    assert r.status_code == 403
    assert harness.store.get_call(cid)["state"] == states.DIALING


def test_status_happy_path_and_idempotent(harness):
    cid = harness.dialing_call()
    status(harness, cid, CallStatus="initiated")
    status(harness, cid, CallStatus="ringing")
    assert harness.store.get_call(cid)["state"] == states.RINGING
    status(harness, cid, CallStatus="in-progress")
    assert harness.store.get_call(cid)["state"] == states.IN_PROGRESS
    status(harness, cid, CallStatus="ringing")  # late duplicate must not go backwards
    assert harness.store.get_call(cid)["state"] == states.IN_PROGRESS
    for _ in range(3):  # duplicate completed callbacks
        r = status(harness, cid, CallStatus="completed", CallDuration="42")
        assert r.status_code == 200
    call = harness.store.get_call(cid)
    assert call["state"] == states.COMPLETED and call["duration_s"] == 42
    assert call["result"]["outcome"] == "needs_followup"
    done = [e for e in harness.store.events(cid)
            if e["kind"] == "state" and json.loads(e["data_json"])["to"] == states.COMPLETED]
    assert len(done) == 1


def test_status_unknown_call_404(harness):
    assert status(harness, "nope", CallStatus="ringing").status_code == 404


def test_terminal_no_answer_busy_failed_exactly_one_result_no_redial(harness):
    for twilio_status, state in (("no-answer", states.NO_ANSWER), ("busy", states.BUSY),
                                 ("failed", states.FAILED)):
        cid = harness.dialing_call()
        status(harness, cid, CallStatus="ringing")
        before = len(harness.store.events(cid))
        for _ in range(2):
            status(harness, cid, CallStatus=twilio_status)
        call = harness.store.get_call(cid)
        assert call["state"] == state and call["outcome"] == state
        assert call["result"]["outcome"] == state and call["result"]["summary"]
        assert call["ended_at"] is not None
        to_terminal = [e for e in harness.store.events(cid)[before:] if e["kind"] == "state"]
        assert len(to_terminal) == 1
        harness.store.update_call(cid, result_delivered=1)  # next call needs a free slot
    # no redial: no Twilio REST call was ever made by the bridge
    assert harness.tw.created == [] and harness.tw.updates == []


def test_voicemail_without_message_hangs_up_once_result(harness):
    cid = harness.dialing_call()
    status(harness, cid, CallStatus="in-progress")
    status(harness, cid, AnsweredBy="machine_start")
    status(harness, cid, AnsweredBy="machine_end_beep")  # duplicate AMD callback
    call = harness.store.get_call(cid)
    assert call["state"] == states.VOICEMAIL and call["outcome"] == "voicemail"
    assert harness.tw.updates and harness.tw.updates[0]["status"] == "completed"
    status(harness, cid, CallStatus="completed", CallDuration="9")
    call = harness.store.get_call(cid)
    assert call["state"] == states.COMPLETED and call["outcome"] == "voicemail"
    assert call["result"]["summary"].startswith("Reached voicemail")


def test_amd_human_does_nothing(harness):
    cid = harness.dialing_call()
    status(harness, cid, CallStatus="in-progress")
    status(harness, cid, AnsweredBy="human")
    assert harness.store.get_call(cid)["state"] == states.IN_PROGRESS


def test_status_ignores_other_call_sid(harness):
    cid = harness.dialing_call()
    harness.store.update_call(cid, provider_sid=SID)
    post_signed(harness.client, f"/status/{cid}", {"CallSid": "CA" + "9" * 32,
                                                   "CallStatus": "failed"})
    assert harness.store.get_call(cid)["state"] == states.DIALING


def test_fake_twilio_unused_import():  # keeps the helper import honest
    assert FakeTwilio().created == [] and BASE.startswith("https://")
