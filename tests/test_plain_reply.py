"""Plain-text replies through pre_llm_call: answer a question, approve or cancel a card."""
from __future__ import annotations

import json

from conftest import prepare

from hermes_phone import commands, hooks, states, tools


def hook(msg, session="S1", **kw):
    return hooks.pre_llm_call(user_message=msg, session_id=session, sender_id="u1",
                              platform="telegram", **kw)


def live_call(store, clock, session="S1", **prep):
    clock.advance(120)
    cid = prepare(prep or None, session_id=session)["call_id"]
    commands.record_user_approval(cid, {"user_id": "u1", "session_id": session})
    assert json.loads(tools.phone_call({"call_id": cid})).get("state") == "dialing"
    store.update_call(cid, state=states.ON_HOLD_FOR_USER)
    return cid


def question(store, cid, qid="q1", text="How many patients?", suggestion="", **kw):
    store.add_question(qid, cid, text, kw.pop("field", "party_size"), "", **kw)
    store.mark_card_posted(qid, suggestion)
    return qid


def blocked(res):
    assert res and res["action"] == "block"
    return res["message"]


# ---- answers ----------------------------------------------------------------------------------
def test_plain_text_answers_the_one_open_question_and_blocks_the_turn(svc, store, clock):
    cid = live_call(store, clock)
    question(store, cid)
    msg = blocked(hook("Doctor Retard"))
    assert msg == 'Sent to Bella Napoli: "Doctor Retard"'
    q = store.get_question("q1")
    assert q["status"] == "answered" and q["answer"] == "Doctor Retard"


def test_affirmative_sends_the_suggestion_the_user_saw(svc, store, clock):
    cid = live_call(store, clock)
    question(store, cid, suggestion="1")
    assert blocked(hook("okay")) == 'Sent to Bella Napoli: "1"'
    assert store.get_question("q1")["answer"] == "1"


def test_affirmative_without_suggestion_is_not_an_answer(svc, store, clock):
    cid = live_call(store, clock)
    question(store, cid)
    assert "What should I tell them" in blocked(hook("yes"))
    assert store.get_question("q1")["status"] == "open"


def test_decline_words_decline(svc, store, clock):
    cid = live_call(store, clock)
    for i, word in enumerate(["decline", "no", "не говори", "Don't share"]):
        qid = question(store, cid, qid=f"q{i}")
        assert "nothing was shared" in blocked(hook(word))
        assert store.get_question(qid)["status"] == "declined"


def test_other_session_cannot_answer(svc, store, clock):
    cid = live_call(store, clock, session="S1")
    question(store, cid)
    assert hook("Doctor Retard", session="S2") is None
    assert hook("yes", session="") is None
    assert store.get_question("q1")["status"] == "open"


def test_slash_and_long_text_pass_through(svc, store, clock):
    cid = live_call(store, clock)
    question(store, cid)
    assert hook("/phone status") is None
    assert hook("x" * 400) is None
    assert store.get_question("q1")["status"] == "open"


def test_consent_question_is_never_auto_answered(svc, store, clock):
    cid = live_call(store, clock)
    question(store, cid, text="Which address?", field="address", kind="consent",
             consent_field="address")
    for word in ("yes", "ok", "221B Baker Street"):
        assert "/phone consent" in blocked(hook(word))
    assert store.get_question("q1")["status"] == "open"
    assert store.consents(cid) == set()
    assert "nothing was shared" in blocked(hook("decline"))


def test_sensitive_text_in_a_plain_answer_needs_consent(svc, store, clock):
    cid = live_call(store, clock)
    question(store, cid)
    assert "consent" in blocked(hook("my card is 4111 1111 1111 1111"))
    assert store.get_question("q1")["status"] == "open"


def test_two_open_questions_fall_back_to_the_command(svc, store, clock):
    cid = live_call(store, clock)
    question(store, cid, qid="q1")
    question(store, cid, qid="q2", text="Insurance?")
    msg = blocked(hook("yes"))
    assert "/phone answer <id>" in msg and "q1" in msg and "q2" in msg
    assert {store.get_question(q)["status"] for q in ("q1", "q2")} == {"open"}


def test_no_pending_question_passes_through_and_commands_still_work(svc, store, clock):
    cid = live_call(store, clock)
    assert hook("hello there") is None
    question(store, cid)
    assert "Sent." in commands.phone_command("answer q1 two")


# ---- approve / cancel by reply ----------------------------------------------------------------
def prepared(session="S1", **prep):
    return prepare(prep or None, session_id=session)["call_id"]


def test_call_reply_approves_and_dials_exactly_once(svc, store, clock):
    cid = prepared()
    msg = blocked(hook("Call"))
    assert "Dialing Bella Napoli" in msg and cid in msg
    call = store.get_call(cid)
    assert call["state"] == states.DIALING and len(svc.dialer.calls) == 1
    assert hook("call") is None  # nothing waiting any more: goes to the model
    assert len(svc.dialer.calls) == 1


def test_each_approval_word_works(svc, store, clock):
    for i, word in enumerate(["yes", "go", "approve", "да", "звони", "позвони"]):
        clock.advance(120)
        cid = prepared(session=f"S{i}")
        assert "Dialing" in blocked(hook(word, session=f"S{i}"))
        store.update_call(cid, state=states.COMPLETED)


def test_cancel_reply_cancels(svc, store):
    cid = prepared()
    assert "canceled" in blocked(hook("cancel"))
    assert store.get_call(cid)["state"] == states.CANCELED and not svc.dialer.calls
    cid = prepared()
    assert "canceled" in blocked(hook("отмена"))
    assert store.get_call(cid)["state"] == states.CANCELED


def test_other_text_passes_through_to_the_model(svc, store):
    cid = prepared()
    assert hook("what time is it?") is None
    assert hook("call them back later maybe") is None
    assert store.get_call(cid)["state"] == states.AWAITING_APPROVAL


def test_other_sessions_pending_call_is_untouched(svc, store):
    cid = prepared(session="S1")
    assert hook("call", session="S2") is None
    assert store.get_call(cid)["state"] == states.AWAITING_APPROVAL and not svc.dialer.calls


def test_expired_card_is_not_approved(svc, store, clock):
    cid = prepared()
    clock.advance(states.APPROVAL_TTL_S + 1)
    assert hook("call") is None
    assert store.get_call(cid)["state"] == states.EXPIRED and not svc.dialer.calls


def test_two_waiting_cards_need_the_explicit_command(svc, store, clock):
    prepared()
    clock.advance(120)
    prepared()
    assert "More than one call" in blocked(hook("call"))
    assert not svc.dialer.calls


def test_reply_cannot_dial_a_blocked_number(svc, store, clock):
    # allowlist is re-checked at approval time even for replies
    cid = prepared()
    svc.environ["HERMES_PHONE_TEST_ALLOWLIST"] = "+12125550999"
    assert "Cannot approve" in blocked(hook("call"))
    assert not svc.dialer.calls
    assert store.get_call(cid)["state"] == states.AWAITING_APPROVAL


def test_reply_cannot_dial_an_emergency_number(svc, store):
    cid = prepared()
    store.conn.execute("UPDATE calls SET number='+911' WHERE id=?", (cid,))
    store.conn.commit()
    assert "Cannot approve" in blocked(hook("call"))
    assert not svc.dialer.calls


def test_card_text_mentions_reply(svc):
    res = prepare(None, session_id="S1")
    assert 'Reply "call" to place it, "cancel" to drop it' in res["card"]
