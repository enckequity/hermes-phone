"""Opening is never interrupted; unknown details go straight to ask_user; hold stays terse."""
from __future__ import annotations

import time

import pytest
from bridge_support import Harness, wait_for
from test_question_relay import ask, count, live, outputs, question_id

from hermes_phone import commands
from hermes_phone.bridge import realtime as rt


@pytest.fixture
def qh(store, clock, svc):
    return Harness(store, clock, question_poll_s=0.01)


def td_updates(h):
    return [e["session"]["audio"]["input"]["turn_detection"] for e in h.rt.of("session.update")
            if "audio" in e["session"] and "turn_detection" in e["session"]["audio"].get(
                "input", {})]


def finish_opening(h):
    h.rt.push({"type": "input_audio_buffer.speech_stopped"})
    wait_for(lambda: h.rt.of("response.create"))
    h.rt.push({"type": "response.created", "response": {"id": "rO"}})
    h.rt.push({"type": "response.done", "response": {"id": "rO"}})
    wait_for(lambda: len(td_updates(h)) == 2)


# ---- A: the opening cannot be interrupted or restarted ----------------------------------------
def test_barge_in_during_the_opening_is_ignored_and_never_reopens(qh):
    with live(qh) as ws:
        qh.rt.push({"type": "input_audio_buffer.speech_started"})
        qh.rt.push({"type": "input_audio_buffer.speech_stopped"})  # "Hi" -> opening
        wait_for(lambda: qh.rt.of("response.create"))
        qh.rt.push({"type": "response.created", "response": {"id": "rO"}})
        time.sleep(0.05)
        qh.rt.push({"type": "input_audio_buffer.speech_started"})  # "Hey." barges in
        qh.rt.push({"type": "input_audio_buffer.speech_stopped"})
        time.sleep(0.15)
        assert not qh.rt.of("response.cancel")
        assert not qh.rt.of("conversation.item.truncate")
        assert len(qh.rt.of("response.create")) == 1
        assert len(td_updates(qh)) == 1 and td_updates(qh)[0]["interrupt_response"] is False
        qh.rt.push({"type": "response.done", "response": {"id": "rO"}})
        wait_for(lambda: len(td_updates(qh)) == 2)
    assert td_updates(qh)[1] == {"type": "server_vad", "create_response": True,
                                 "interrupt_response": True}
    assert len(qh.rt.of("response.create")) == 1
    assert ws is not None


def test_opening_lock_has_a_timeout_if_response_done_never_comes(qh, clock):
    with live(qh):
        qh.rt.push({"type": "input_audio_buffer.speech_stopped"})
        wait_for(lambda: qh.rt.of("response.create"))
        clock.advance(16)
        wait_for(lambda: len(td_updates(qh)) == 2)


def test_instructions_forbid_repeating_the_introduction_and_claiming_ignorance(qh):
    with live(qh):
        sess = qh.rt.of("session.update")[0]["session"]
    text = sess["instructions"]
    assert "Never repeat the introduction" in text
    assert "rephrase just the request in one short sentence" in text
    assert "do NOT say you are unable" in text and "ask_user tool immediately" in text
    assert "not first say that you cannot" in sess["tools"][0]["description"]


# ---- C: terse hold ----------------------------------------------------------------------------
def test_callee_speaking_during_hold_gets_one_short_line_not_an_auto_reply(qh):
    with live(qh):
        finish_opening(qh)
        ask(qh, "Which doctor?", field="doctor")
        qid = question_id(qh)
        wait_for(lambda: len(td_updates(qh)) == 3)
        assert td_updates(qh)[2]["create_response"] is False  # no VAD auto-reply during hold
        qh.rt.push({"type": "input_audio_buffer.speech_started"})
        qh.rt.push({"type": "input_audio_buffer.speech_stopped"})
        wait_for(lambda: count(qh, rt.HOLD_REPLY_LINE) == 1)
        hold = [e for e in qh.rt.of("response.create") if e.get("response", {}).get(
            "instructions", "").endswith(rt.HOLD_REPLY_INSTRUCTIONS)][0]
        assert hold["response"]["tool_choice"] == "none"
        low = rt.HOLD_REPLY_INSTRUCTIONS.lower()
        assert "do not explain" in low and "keep them on the line" in low
        assert rt.HOLD_REPLY_LINE.count(".") == 1  # one sentence
        # the user answers: auto replies come back
        commands.phone_command(f"answer {qid} Doctor Retard")
        wait_for(lambda: outputs(qh))
        wait_for(lambda: len(td_updates(qh)) == 4)
        assert td_updates(qh)[3]["create_response"] is True


def test_hold_reply_not_sent_while_a_response_is_in_flight(qh):
    with live(qh):
        finish_opening(qh)
        ask(qh, "Which doctor?", field="doctor")
        question_id(qh)
        wait_for(lambda: len(td_updates(qh)) == 3)
        qh.rt.push({"type": "response.created", "response": {"id": "rX"}})
        time.sleep(0.05)
        qh.rt.push({"type": "input_audio_buffer.speech_stopped"})
        time.sleep(0.15)
        assert count(qh, rt.HOLD_REPLY_LINE) == 0


def test_filler_is_one_of_two_short_lines_and_waits_15s_after_a_hold_reply(qh, clock):
    assert rt.FILLER_LINE in rt.FILLER_INSTRUCTIONS and rt.FILLER_LINE_2 in rt.FILLER_INSTRUCTIONS
    assert "nothing else" in rt.FILLER_INSTRUCTIONS
    with live(qh):
        finish_opening(qh)
        ask(qh, "Which doctor?", field="doctor")
        question_id(qh)
        wait_for(lambda: len(td_updates(qh)) == 3)
        clock.advance(10)
        qh.rt.push({"type": "input_audio_buffer.speech_stopped"})
        wait_for(lambda: count(qh, rt.HOLD_REPLY_LINE) == 1)
        clock.advance(10)  # 20 s after the question but only 10 s after the hold reply
        time.sleep(0.15)
        assert count(qh, rt.FILLER_LINE) == 0
        clock.advance(6)
        wait_for(lambda: count(qh, rt.FILLER_LINE) == 1)


# ---- Problem 1: who is being called reaches the voice model -----------------------------------
def test_callee_name_and_goal_reach_the_realtime_instructions(qh):
    with live(qh, callee_name="Doctor Retard dental office",
              goal="Book a teeth whitening with Doctor Retard tomorrow between 11 and 2",
              facts_to_share={"person_to_see": "Doctor Retard",
                              "card_number": "4111 1111 1111 1111"}):
        text = qh.rt.of("session.update")[0]["session"]["instructions"]
    assert "YOU ARE CALLING: Doctor Retard dental office" in text
    assert "Book a teeth whitening with Doctor Retard tomorrow between 11 and 2" in text
    assert "Doctor Retard" in text.split("FACTS YOU MAY SHARE")[1]
    assert "4111" not in text  # sensitive filtering unchanged
