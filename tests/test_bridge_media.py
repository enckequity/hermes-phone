"""Media stream: Twilio <-> fake Realtime (SPEC criteria 4, 7, 9 and the P2 lifecycle)."""
from __future__ import annotations

import base64
import json
import time

import pytest
from bridge_support import MULAW_20MS, FakeTwilio, make_settings, wait_for
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_phone import states
from hermes_phone.bridge import create_app
from hermes_phone.bridge import realtime as rt

CARD = "4111 1111 1111 1111"
ADDRESS = "221B Baker Street"
SECRET_FACTS = {"name": "Winch", "party_size": 4, "card_number": CARD, "home_address": ADDRESS}
OUTBOUND = base64.b64encode(bytes(range(256)) * 4).decode()  # 1024 bytes


def open_call(h, cid):
    token = h.token(cid)
    return token, h.client.websocket_connect(f"/media/{cid}")


def start(h, ws, cid, token):
    for m in h.start_msg(cid, token):
        ws.send_text(m)


def first_event(h, etype):
    wait_for(lambda: h.rt.of(etype))
    return h.rt.of(etype)[0]


def drain(ws):
    out = []
    while True:
        try:
            out.append(json.loads(ws.receive_text()))
        except WebSocketDisconnect:
            return out


def test_session_update_is_pcmu_both_ways_with_tools(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        sess = first_event(harness, "session.update")["session"]
    assert sess["type"] == "realtime" and sess["model"] == "gpt-realtime-2.1"
    assert sess["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert sess["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    assert sess["audio"]["input"]["turn_detection"]["type"] == "server_vad"
    assert [t["name"] for t in sess["tools"]] == ["ask_user", "end_call"]
    assert harness.store.get_call(cid)["state"] in (states.IN_PROGRESS, states.COMPLETED)


def responses(h):
    return h.rt.of("response.create")


@pytest.mark.parametrize("record", [False, True])
def test_first_response_forces_ai_disclosure_after_greeting(harness, record):
    cid = harness.dialing_call(record=record)
    line = harness.store.get_call(cid)["brief"]["opening_line"]
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        time.sleep(0.1)
        assert not responses(harness)  # waits for the callee's "Hello"
        harness.rt.push({"type": "input_audio_buffer.speech_started"})
        harness.rt.push({"type": "input_audio_buffer.speech_stopped"})
        wait_for(lambda: responses(harness))
        harness.rt.push({"type": "input_audio_buffer.speech_stopped"})  # second utterance
        time.sleep(0.1)
        assert len(harness.rt.of("session.update")) == 1  # VAD still locked during the opening
        harness.rt.push({"type": "response.created", "response": {"id": "rO"}})
        harness.rt.push({"type": "response.done", "response": {"id": "rO"}})
        wait_for(lambda: len(harness.rt.of("session.update")) == 2)
    kinds = [e["type"] for e in harness.rt.sent]
    assert kinds[0] == "session.update" and "response.create" in kinds
    assert len(responses(harness)) == 1
    first = responses(harness)[0]["response"]["instructions"]
    assert "an AI assistant calling on behalf of" in first and line in first
    step = first.split("CURRENT STEP")[-1]
    assert "goal" not in step.lower() and "brief" not in step.lower()
    # Per-response instructions replace the session ones in Realtime: the opening must carry
    # the call context, or the agent says "I don't have the details yet" (live bug, Nov 5).
    assert "WHAT YOU ARE CALLING FOR: Book a table for 4" in first
    assert "FACTS YOU MAY SHARE" in first and '"party_size": 4' in first
    assert ("may be recorded" in step) is record
    sess = harness.rt.sent[0]["session"]
    assert "You are an AI" in sess["instructions"]
    assert sess["audio"]["input"]["turn_detection"]["create_response"] is False
    assert sess["audio"]["input"]["turn_detection"]["interrupt_response"] is False
    updates = harness.rt.of("session.update")
    assert len(updates) == 2 and kinds.index("response.create") < kinds.index(
        "session.update", 1)
    td = updates[1]["session"]["audio"]["input"]["turn_detection"]
    assert td["create_response"] is True and td["type"] == "server_vad"
    assert td["interrupt_response"] is True


def test_silent_answerer_gets_the_opening_after_2_5_seconds(harness, clock):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        clock.advance(2.0)
        time.sleep(0.1)
        assert not responses(harness)
        clock.advance(0.6)
        wait_for(lambda: responses(harness))
        harness.rt.push({"type": "input_audio_buffer.speech_stopped"})  # late hello: no 2nd
        harness.rt.push({"type": "response.created", "response": {"id": "rO"}})
        harness.rt.push({"type": "response.done", "response": {"id": "rO"}})
        wait_for(lambda: len(harness.rt.of("session.update")) == 2)
    assert len(responses(harness)) == 1
    assert harness.rt.of("session.update")[1]["session"]["audio"]["input"][
        "turn_detection"]["create_response"] is True


def test_unconsented_sensitive_values_never_reach_realtime(harness):
    cid = harness.dialing_call(args=None, facts_to_share=SECRET_FACTS,
                               goal=f"Book a table, my card is {CARD}")
    token, cm = open_call(harness, cid)
    harness.rt.reactor = lambda ev: ([
        {"type": "response.function_call_arguments.done", "name": "ask_user", "call_id": "f1",
         "arguments": json.dumps({"question": "Which card?", "field": "card"})}
    ] if ev.get("type") == "response.create" and "response" in ev else [])
    with cm as ws:
        start(harness, ws, cid, token)
        harness.rt.push({"type": "input_audio_buffer.speech_stopped"})
        wait_for(lambda: harness.store.open_questions())  # consent card: waits, no output yet
    blob = json.dumps(harness.rt.sent)
    assert not harness.rt.of("conversation.item.create")
    for secret in (CARD, "4111", ADDRESS, "Baker"):
        assert secret not in blob, secret
    assert "Winch" in blob and "party_size" in blob  # allowed facts do go through


def test_consented_value_is_shared_but_cvv_never(harness):
    cid = harness.dialing_call(consent=("address",), facts_to_share=SECRET_FACTS,
                               sensitive_fields=["address"])
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
    blob = json.dumps(harness.rt.sent)
    assert ADDRESS in blob and CARD not in blob


def test_audio_passthrough_both_ways_without_transcoding(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        ws.send_text(harness.media(20))
        wait_for(lambda: harness.rt.of("input_audio_buffer.append"))
        assert harness.rt.of("input_audio_buffer.append")[0]["audio"] == MULAW_20MS
        harness.rt.push({"type": "response.output_audio.delta", "item_id": "i1",
                         "delta": OUTBOUND})
        media = json.loads(ws.receive_text())
        mark = json.loads(ws.receive_text())
    assert media == {"event": "media", "streamSid": "MZ1", "media": {"payload": OUTBOUND}}
    assert mark["event"] == "mark" and mark["streamSid"] == "MZ1"


def test_bad_token_and_token_reuse_are_refused(harness):
    cid = harness.dialing_call()
    with harness.client.websocket_connect(f"/media/{cid}") as ws:
        start(harness, ws, cid, "forged")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert harness.rt.connect_calls == 0
    token = harness.token(cid)
    with harness.client.websocket_connect(f"/media/{cid}") as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
    with harness.client.websocket_connect(f"/media/{cid}") as ws:  # same token again
        start(harness, ws, cid, token)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert harness.rt.connect_calls == 1


def test_expired_token_refused(harness, clock):
    cid = harness.dialing_call()
    token = harness.token(cid)
    clock.advance(harness.settings.token_ttl_s + 1)
    with harness.client.websocket_connect(f"/media/{cid}") as ws:
        start(harness, ws, cid, token)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert harness.rt.connect_calls == 0


def test_barge_in_clears_cancels_and_truncates(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        for ts in (20, 40, 60, 80, 100):
            ws.send_text(harness.media(ts))
        wait_for(lambda: len(harness.rt.of("input_audio_buffer.append")) == 5)
        big = base64.b64encode(bytes(8000)).decode()  # 1000 ms of audio
        harness.rt.push({"type": "response.created", "response": {"id": "r1"}})
        harness.rt.push({"type": "response.output_audio.delta", "item_id": "i9", "delta": big})
        assert json.loads(ws.receive_text())["event"] == "media"
        assert json.loads(ws.receive_text())["event"] == "mark"
        ws.send_text(harness.media(500))  # 400 ms of the item have been played
        wait_for(lambda: len(harness.rt.of("input_audio_buffer.append")) == 6)
        harness.rt.push({"type": "input_audio_buffer.speech_started"})
        assert json.loads(ws.receive_text()) == {"event": "clear", "streamSid": "MZ1"}
        wait_for(lambda: harness.rt.of("conversation.item.truncate"))
    assert harness.rt.of("response.cancel")
    trunc = harness.rt.of("conversation.item.truncate")[0]
    assert trunc["item_id"] == "i9" and trunc["audio_end_ms"] == 400


def test_end_call_stores_result_and_hangs_up(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        harness.rt.push({"type": "input_audio_buffer.speech_stopped"})
        wait_for(lambda: responses(harness))
        harness.rt.push({"type": "response.output_audio_transcript.done",
                         "transcript": "Perfect, thank you so much. Goodbye!"})
        harness.rt.push({"type": "response.function_call_arguments.done", "name": "end_call",
                         "call_id": "f9", "arguments": json.dumps({
                             "outcome": "success",
                             "agreed": {"date": "Friday", "time": "20:00", "party_size": 4},
                             "next_steps": "none"})})
        harness.rt.push({"type": "response.done", "response": {"id": "rEnd"}})
        wait_for(lambda: harness.tw.updates)
        drain(ws)
    assert harness.tw.updates[-1] == {"sid": "CA" + "1" * 32, "status": "completed"}
    call = harness.store.get_call(cid)
    assert call["state"] == states.COMPLETED and call["outcome"] == "success"
    assert call["result"]["agreed"]["time"] == "20:00" and call["result"]["next_steps"] == "none"
    out = harness.rt.of("conversation.item.create")[-1]["item"]
    assert out["type"] == "function_call_output" and out["call_id"] == "f9"
    assert len(harness.rt.of("response.create")) == 1  # nothing after end_call but the opener


def test_end_call_waits_for_marks_then_grace(harness, clock):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        harness.rt.push({"type": "response.output_audio.delta", "item_id": "i1",
                         "delta": OUTBOUND})
        ws.receive_text()
        mark = json.loads(ws.receive_text())
        harness.rt.push({"type": "response.output_audio_transcript.done",
                         "transcript": "Thanks so much, goodbye!"})
        harness.rt.push({"type": "response.function_call_arguments.done", "name": "end_call",
                         "call_id": "f9", "arguments": json.dumps({"outcome": "partial"})})
        harness.rt.push({"type": "response.done", "response": {"id": "rEnd"}})
        wait_for(lambda: harness.rt.of("conversation.item.create"))
        assert harness.tw.updates == []  # goodbye audio not yet played
        clock.advance(harness.settings.end_grace_s + 1)  # lost mark: grace expires
        wait_for(lambda: harness.tw.updates)
        drain(ws)
    assert mark["event"] == "mark"
    assert harness.store.get_call(cid)["state"] == states.COMPLETED


def test_end_call_in_same_response_as_goodbye_waits_for_its_marks(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        harness.rt.push({"type": "response.created", "response": {"id": "r1"}})
        harness.rt.push({"type": "response.output_audio.delta", "item_id": "i1",
                         "delta": OUTBOUND})
        ws.receive_text()
        mark = json.loads(ws.receive_text())
        harness.rt.push({"type": "response.output_audio_transcript.done",
                         "transcript": "Thanks so much, goodbye!"})
        harness.rt.push({"type": "response.function_call_arguments.done", "name": "end_call",
                         "call_id": "f9", "arguments": json.dumps({"outcome": "success"})})
        harness.rt.push({"type": "response.done", "response": {"id": "r1"}})
        wait_for(lambda: harness.rt.of("conversation.item.create"))
        time.sleep(0.2)
        assert harness.tw.updates == []  # response is done but the goodbye is still playing
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MZ1",
                                 "mark": {"name": mark["mark"]["name"]}}))
        wait_for(lambda: harness.tw.updates)  # no grace needed: last mark played
        drain(ws)
    assert harness.tw.updates[-1]["status"] == "completed"


def test_instructions_require_confirming_before_end_call():
    from hermes_phone.bridge import realtime as rtm

    text = rtm.build_instructions({"callee_name": "Luigi", "user_name": "Winch", "goal": "x",
                                   "facts": {"name": "Winch"}, "opening_line": "Hi."})
    for needle in ("confirm", "confirmation number", "booking name", "thank", "goodbye",
                   "book it", "partial or needs_followup"):
        assert needle in text.lower(), needle
    desc = next(t for t in rtm.TOOLS if t["name"] == "end_call")["description"]
    assert "confirmation number" in desc and "success only if" in desc
    opening = rtm.opening_response_instructions("Hi, I'm an AI assistant.")
    assert "Hi, I'm an AI assistant." in opening
    assert "goal" not in opening.lower() and "word for word" in opening


def test_invalid_tool_arguments_do_not_crash(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        harness.rt.push({"type": "response.function_call_arguments.done", "name": "end_call",
                         "call_id": "f3", "arguments": "{not json"})
        wait_for(lambda: harness.rt.of("conversation.item.create"))
    assert json.loads(harness.rt.of("conversation.item.create")[0]["item"]["output"]
                      )["error"]


def test_transcripts_collected_both_sides(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        harness.rt.push({"type": "response.output_audio_transcript.done",
                         "transcript": "Hi, this is an AI assistant calling on behalf of Winch."})
        harness.rt.push({"type": "conversation.item.input_audio_transcription.completed",
                         "transcript": "Bella Napoli, how can I help?"})
        wait_for(lambda: len(harness.store.transcript(cid)) == 2)
    lines = harness.store.transcript(cid)
    assert [(t["role"], t["text"][:8]) for t in lines] == [("agent", "Hi, this"),
                                                           ("callee", "Bella Na")]


def test_max_duration_wraps_up_then_hangs_up(harness, clock):
    cid = harness.dialing_call(max_duration_s=120)
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        clock.advance(89)
        import time

        time.sleep(0.1)
        assert len(harness.rt.of("response.create")) == 1  # not yet at T-30
        clock.advance(2)  # 91 s >= 120 - 30
        wait_for(lambda: len(harness.rt.of("response.create")) == 2)
        assert "almost reached" in harness.rt.of("response.create")[1]["response"]["instructions"]
        assert harness.tw.updates == []
        clock.advance(30)
        wait_for(lambda: harness.tw.updates)
        drain(ws)
    call = harness.store.get_call(cid)
    assert call["state"] == states.ABORTED and call["reason"] == "max_duration"
    assert call["result"]["reason"] == "max_duration"
    assert harness.tw.updates[-1]["status"] == "completed"


def test_realtime_connection_loss_apologises_and_fails(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        harness.rt.push(None)  # connection closed
        wait_for(lambda: harness.tw.updates)
        drain(ws)
    call = harness.store.get_call(cid)
    assert call["state"] == states.FAILED and call["reason"] == "realtime_connection_lost"
    assert "Say" in harness.tw.updates[0]["twiml"] and "Hangup" in harness.tw.updates[0]["twiml"]


def test_realtime_connect_failure_fails_call_and_hangs_up(harness):
    async def boom(settings, call_id):
        raise OSError("no route")

    tw = FakeTwilio()
    app = create_app(make_settings(), store=harness.store, realtime_connect=boom,
                     twilio_client=tw, clock=harness.clock)
    cl = TestClient(app)
    harness.client = cl
    cid = harness.dialing_call()
    token = harness.token(cid)
    with cl.websocket_connect(f"/media/{cid}") as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: tw.updates)
        drain(ws)
    assert harness.store.get_call(cid)["state"] == states.FAILED


def test_callee_hangs_up_mid_call_finalises_once(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MZ1"}))
        wait_for(lambda: harness.store.get_call(cid)["state"] == states.COMPLETED)
    assert harness.tw.updates == []  # already hung up by the callee
    assert harness.store.get_call(cid)["reason"] == "callee_hung_up"


def test_end_call_without_goodbye_forces_a_closing_line_then_hangs_up(harness):
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        harness.rt.push({"type": "input_audio_buffer.speech_stopped"})
        wait_for(lambda: responses(harness))
        harness.rt.push({"type": "response.output_audio_transcript.done",
                         "transcript": "Great, 3 pm it is."})
        harness.rt.push({"type": "response.function_call_arguments.done", "name": "end_call",
                         "call_id": "f9", "arguments": json.dumps({"outcome": "success"})})
        harness.rt.push({"type": "response.done", "response": {"id": "rEnd"}})
        wait_for(lambda: any(r.get("response", {}).get("instructions", "").endswith(
            rt.GOODBYE_INSTRUCTIONS) for r in harness.rt.of("response.create")))
        assert not harness.tw.updates  # no hangup before the goodbye played
        harness.rt.push({"type": "response.created", "response": {"id": "rBye"}})
        harness.rt.push({"type": "response.output_audio_transcript.done",
                         "transcript": "Thank you so much, goodbye!"})
        harness.rt.push({"type": "response.done", "response": {"id": "rBye"}})
        wait_for(lambda: harness.tw.updates)
        drain(ws)
    assert harness.store.get_call(cid)["state"] == states.COMPLETED
