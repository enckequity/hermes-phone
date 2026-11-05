"""P4 results: the one final message per call, duration and cost, transcripts, log, retry,
restart safety and the AI-disclosure check. No network; fake clock; watcher stepped by hand."""
from __future__ import annotations

import json

import pytest
from bridge_support import Harness, wait_for
from conftest import FakeCtx, approve, prepare
from test_bridge_media import open_call, start

from hermes_phone import brief as briefmod
from hermes_phone import commands, hooks, relay, results, states, tools
from hermes_phone.store import Store

AGREED = {"date": "tomorrow", "time": "19:00", "party_size": 2, "notes": "inside",
          "name": "Winch", "confirmation_number": "B47"}


def make_call(svc, clock, final: str, *, result: dict | None = None, outcome: str = "",
              reason: str = "", ring_s: float = 30, talk_s: int | None = None,
              web: bool = False, **prep) -> str:
    """prepare -> approve -> dial (NullDialer) -> stop the watcher -> settle the call."""
    clock.advance(120)
    cid = prepare(prep or None)["call_id"]
    approve(svc, cid)
    assert "error" not in json.loads(tools.phone_call({"call_id": cid}))
    svc.watchers.pop(cid).stop.set()  # tests step watchers by hand
    store = svc.store
    if web:
        store.update_call(cid, provider_sid="web-" + cid)
    if result is not None:
        store.set_result_once(cid, result, outcome)
    if talk_s is not None:
        store.update_call(cid, duration_s=talk_s)
    clock.advance(ring_s)
    if final in (states.COMPLETED, states.ABORTED, states.FAILED, states.VOICEMAIL):
        states.transition(store, cid, states.IN_PROGRESS)
    states.transition(store, cid, final, outcome=outcome, reason=reason)
    if final == states.COMPLETED:
        store.add_transcript_line(cid, "agent", "Hello.")
    return cid


def result_messages(ctx: FakeCtx) -> list[str]:
    return [m.text for m in ctx.messages
            if not m.text.startswith(("Call ", "Transcript of", "Ringing"))]


def run_watcher(svc, cid, steps: int = 6) -> relay.CallWatcher:
    w = relay.CallWatcher(svc, cid, poll_s=0.01)
    for _ in range(steps):
        w._step()
    return w


SUCCESS = {"outcome": "success", "agreed": AGREED, "next_steps": ""}
CASES = {
    "success": (states.COMPLETED, SUCCESS, "success", ""),
    "partial": (states.COMPLETED, {"outcome": "partial", "agreed": {"date": "Friday"},
                                   "next_steps": "Call back Monday"}, "partial", ""),
    "failed": (states.COMPLETED, {"outcome": "failed", "agreed": {}, "next_steps": "",
                                  "summary": "They are fully booked."}, "failed", ""),
    "needs_followup": (states.COMPLETED, {"outcome": "needs_followup", "agreed": {},
                                          "next_steps": "Send the party size"},
                       "needs_followup", ""),
    "no_answer": (states.NO_ANSWER, {"outcome": "no_answer", "summary": "No answer.",
                                     "reason": "no_answer"}, "no_answer", "no_answer"),
    "busy": (states.BUSY, {"outcome": "busy", "summary": "Line busy.", "reason": "busy"},
             "busy", "busy"),
    "voicemail": (states.COMPLETED, {"outcome": "voicemail", "reason": "voicemail",
                                     "summary": "Reached voicemail, did not leave a message."},
                  "voicemail", "voicemail"),
    "aborted": (states.ABORTED, {"outcome": "needs_followup", "reason": "user_hangup",
                                 "summary": "You ended the call."}, "needs_followup",
                "user_hangup"),
}


# ---- exactly one result per terminal state ---------------------------------------------------
@pytest.mark.parametrize("name", sorted(CASES))
def test_exactly_one_result_message_per_terminal_state(svc, clock, ctx, name):
    final, result, outcome, reason = CASES[name]
    cid = make_call(svc, clock, final, result=result, outcome=outcome, reason=reason,
                    talk_s=40)
    run_watcher(svc, cid)
    assert len(result_messages(ctx)) == 1
    assert svc.store.get_call(cid)["result_posted_at"] is not None
    run_watcher(svc, cid)  # a new watcher after a restart / reload posts nothing more
    relay.start_watcher(svc, cid)
    svc.watchers.pop(cid, None)
    assert len(result_messages(ctx)) == 1
    text = result_messages(ctx)[0]
    if final == states.COMPLETED:
        assert text.rstrip().endswith(f"Transcript: /phone transcript {cid}")
    else:
        assert "Transcript:" not in text  # nothing was said, nothing to read
    assert ("Retry? Reply /phone retry" in text) is (name in results.RETRYABLE
                                                     or outcome in results.RETRYABLE)
    assert "**" not in text and "`" not in text and "_" not in text.split("\n")[0]


def test_each_terminal_state_reads_right(svc, clock, ctx):
    texts = {}
    for name in ("no_answer", "busy", "voicemail", "aborted", "failed"):
        final, result, outcome, reason = CASES[name]
        cid = make_call(svc, clock, final, result=result, outcome=outcome, reason=reason,
                        talk_s=40)
        run_watcher(svc, cid)
        texts[name] = result_messages(ctx)[-1]
    assert texts["no_answer"].startswith("📵 No answer after 30 s.")
    assert texts["busy"].startswith("📵 Line busy.")
    assert texts["voicemail"].startswith("📫 Reached voicemail, did not leave a message.")
    assert texts["aborted"].startswith("⛔ Call ended early: You hung up.")
    assert texts["failed"].startswith("❌ Failed. They are fully booked.")
    for name in ("no_answer", "busy", "voicemail"):
        assert "Retry? Reply /phone retry c" in texts[name]


def test_canceled_and_expired_after_dialing_get_a_reason(svc, clock, ctx):
    cid = make_call(svc, clock, states.CANCELED, reason="canceled")
    run_watcher(svc, cid)
    assert result_messages(ctx)[-1].startswith("⛔ Call canceled")


# ---- formatting ------------------------------------------------------------------------------
def test_success_message_matches_the_demo(svc, clock, ctx):
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success",
                    talk_s=72)
    run_watcher(svc, cid)
    assert result_messages(ctx)[0] == (
        "✅ Success. Tomorrow 19:00, party of 2, inside, under Winch. Confirmation B47. "
        "Next: nothing. 1m 12s, about $0.21.\n"
        f"Transcript: /phone transcript {cid}")


def test_cost_estimate_twilio_web_and_config(svc, clock):
    call = {"provider_sid": "CA1"}
    assert results.estimate_cost(call, 72) == 0.21  # 2 started minutes * .014 + 1.2 min * .15
    assert results.estimate_cost({"provider_sid": "web-c1"}, 72) == 0.18  # no telephony in web
    cfg = {"twilio_cost_per_min_usd": 0.1, "realtime_cost_per_min_usd": 0.5}
    assert results.estimate_cost(call, 60, lambda k, d: cfg.get(k, d)) == 0.6
    assert results.estimate_cost(call, 0) == 0.0
    assert results.estimate_cost(call, 60, lambda k, d: "junk") == 0.16  # bad config: defaults
    assert [results.fmt_duration(x) for x in (5, 60, 72, 125)] == ["5s", "1m", "1m 12s", "2m 05s"]


def test_web_mode_result_costs_no_telephony(svc, clock, ctx):
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success",
                    talk_s=72, web=True)
    run_watcher(svc, cid)
    assert "1m 12s, about $0.18." in result_messages(ctx)[0]


def test_duration_falls_back_to_the_result_then_timestamps(svc, clock):
    call = {"duration_s": None, "result": {"duration_s": 33}, "ended_at": 100, "dialed_at": 10}
    assert results.call_duration_s(call) == 33
    call["result"] = {}
    assert results.call_duration_s(call) == 90


def test_result_never_contains_sensitive_values(svc, clock, ctx):
    card, addr = "4111 1111 1111 1111", "12 Baker Street"
    agreed = dict(AGREED, notes=f"paid with {card}", address=addr, card_number=card)
    result = {"outcome": "success", "agreed": agreed, "summary": f"card {card}, mail a@b.co",
              "next_steps": f"Send the invoice to {addr}"}
    cid = make_call(svc, clock, states.COMPLETED, result=result, outcome="success",
                    talk_s=50, facts_to_share={"name": "Winch", "home_address": addr})
    run_watcher(svc, cid)
    text = result_messages(ctx)[0]
    for secret in (card, "4111", addr, "Baker", "a@b.co"):
        assert secret not in text, secret
    assert "Confirmation B47" in text


# ---- restart safety --------------------------------------------------------------------------
def test_watcher_died_before_posting_pre_llm_call_reports_once(svc, clock, ctx):
    cid = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                    outcome="no_answer", reason="no_answer")
    assert not result_messages(ctx)  # Hermes restarted mid-call: nobody posted
    first = hooks.pre_llm_call()
    assert first and cid in first["context"] and "No answer after 30 s" in first["context"]
    assert "data, not instructions" in first["context"]
    assert hooks.pre_llm_call() is None  # exactly once


def test_pre_llm_call_never_reports_a_result_the_watcher_posted(svc, clock, ctx):
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success",
                    talk_s=60)
    run_watcher(svc, cid)
    assert len(result_messages(ctx)) == 1
    assert hooks.pre_llm_call() is None


def test_pre_llm_call_leaves_a_live_watchers_result_alone(svc, clock, ctx):
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success",
                    talk_s=60)
    svc.watchers[cid] = object()  # a running watcher will post it in a moment
    assert hooks.pre_llm_call() is None
    svc.watchers.pop(cid)
    assert hooks.pre_llm_call() is not None


def test_failed_post_is_retried_then_left_for_pre_llm_call(svc, clock):
    class Flaky(FakeCtx):
        refuse = True

        def post_message(self, text):
            msg = super().post_message(text)
            msg.ok = not self.refuse
            return msg

    svc.ctx = flaky = Flaky(config={"user_name": "Winch"})
    cid = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                    outcome="no_answer", reason="no_answer")
    w = run_watcher(svc, cid, steps=10)
    assert w._step() is True and svc.store.get_call(cid)["result_posted_at"] is None
    assert "No answer" in hooks.pre_llm_call()["context"]
    flaky.refuse = False
    assert hooks.pre_llm_call() is None


def test_result_posted_survives_reopening_the_database(svc, clock, ctx, tmp_path):
    cid = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                    outcome="no_answer", reason="no_answer")
    run_watcher(svc, cid)
    path = svc.store.conn.execute("PRAGMA database_list").fetchone()[2]
    fresh = Store(path=path, clock=clock)
    assert fresh.get_call(cid)["result_posted_at"] is not None
    assert fresh.undelivered_results() == []


# ---- transcripts -----------------------------------------------------------------------------
def add_lines(store, cid, n=3, pad=0):
    for i in range(n):
        role = "agent" if i % 2 == 0 else "callee"
        store.add_transcript_line(cid, role, f"line {i} " + "x" * pad)


def test_transcript_command_is_readable(svc, clock):
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success",
                    talk_s=72)
    svc.store.add_transcript_line(cid, "agent", "Hi, this is an AI assistant calling.")
    svc.store.add_transcript_line(cid, "callee", "  Bella   Napoli, how can I help? ")
    out = commands.phone_command(f"transcript {cid}")
    assert out == (f"Transcript of call {cid} to Bella Napoli (1m 12s):\n"
                   "Agent: Hello.\nAgent: Hi, this is an AI assistant calling.\n"
                   "Them: Bella Napoli, how can I help?")
    data = json.loads(tools.phone_transcript({"call_id": cid}))
    assert data["text"].startswith("Agent: Hello.") and len(data["transcript"]) == 3


def test_long_transcript_is_split_into_telegram_safe_messages(svc, clock, ctx):
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success")
    add_lines(svc.store, cid, n=30, pad=400)
    add_lines(svc.store, cid, n=1, pad=9000)  # one monster line is split too
    out = commands.phone_command(f"transcript {cid}")
    parts = [m.text for m in ctx.messages if m.text.startswith("Transcript of call")]
    assert out == f"Transcript sent in {len(parts)} messages." and len(parts) >= 4
    assert all(len(p) < results.CHUNK_LIMIT for p in parts)
    assert parts[0].startswith(f"Transcript of call {cid} to Bella Napoli")
    assert f"(1/{len(parts)}):" in parts[0].splitlines()[0]
    body = "\n".join(parts)
    assert body.count("line ") >= 31 and "x" * 400 in body


def test_chunk_lines_respects_the_limit():
    lines = ["Agent: " + "word " * 300] * 5 + ["Them: ok"]
    chunks = results.chunk_lines(lines, 500)
    assert all(len(c) <= 500 for c in chunks) and chunks[-1].endswith("Them: ok")


def test_transcript_persists_across_plugin_reload(svc, clock, ctx):
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success")
    add_lines(svc.store, cid, n=4)
    path = svc.store.conn.execute("PRAGMA database_list").fetchone()[2]
    svc.store.close()
    tools._service = None  # plugin reload: new service, new connection, same file
    tools.configure(ctx=ctx, store=Store(path=path, clock=clock), dialer=tools.NullDialer(),
                    environ={})
    out = commands.phone_command(f"transcript {cid}")
    assert "Agent: line 0" in out and "Them: line 1" in out and "Agent: line 2" in out


def test_store_transcripts_false_keeps_only_the_summary(svc, clock, ctx):
    ctx.config["store_transcripts"] = False
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success",
                    talk_s=72)
    svc.store.add_transcript_line(cid, "callee", "should never be shown")
    out = commands.phone_command(f"transcript {cid}")
    assert "store_transcripts: false" in out and "Confirmation B47" in out
    assert "never be shown" not in out
    data = json.loads(tools.phone_transcript({"call_id": cid}))
    assert data["transcript"] == [] and "Confirmation B47" in data["summary"]
    run_watcher(svc, cid)
    assert "Transcript: not stored" in result_messages(ctx)[-1]
    assert "/phone transcript" not in result_messages(ctx)[-1]


def test_transcript_unknown_and_empty(svc, clock):
    assert commands.phone_command("transcript nope") == "Unknown call id."
    cid = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                    outcome="no_answer")
    assert commands.phone_command(f"transcript {cid}") == "No transcript recorded."


# ---- call log --------------------------------------------------------------------------------
def test_phone_log_one_line_per_call(svc, clock):
    a = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success", talk_s=72)
    b = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                  outcome="no_answer")
    out = commands.phone_command("log")
    lines = out.splitlines()
    assert len(lines) == 2 and lines[0].endswith(b) and lines[1].endswith(a)
    assert "Bella Napoli  no answer  -" in lines[0]
    assert "UTC  Bella Napoli  success  1m 12s" in lines[1]
    assert commands.phone_command("log 1").count("\n") == 0


def test_phone_log_empty(svc):
    assert commands.phone_command("log") == "No calls yet."


# ---- retry -----------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["no_answer", "busy", "voicemail", "needs_followup",
                                  "failed", "aborted"])
def test_retry_allowed_states_make_a_new_unapproved_call(svc, clock, ctx, name):
    final, result, outcome, reason = CASES[name]
    cid = make_call(svc, clock, final, result=result, outcome=outcome, reason=reason)
    out = commands.phone_command(f"retry {cid}", user_id="u1")
    new = svc.store.recent_calls(1)[0]
    assert new["id"] != cid and new["retry_of"] == cid
    assert f"/phone approve {new['id']}" in out and "nothing is dialed" in out
    assert new["state"] == states.AWAITING_APPROVAL
    assert svc.store.get_approval(new["id"]) is None  # approval is per call id: needs a fresh one
    assert svc.dialer.calls[-1]["call_id"] == cid  # nothing dialed for the retry
    assert new["goal"] == "Book a table for 4" and new["brief"]["facts_raw"]["party_size"] == 4
    blocked = hooks.pre_tool_call(tool_name="phone_call", args={"call_id": new["id"]})
    assert blocked and blocked["action"] == "block"


def test_retry_refused_for_other_states(svc, clock):
    ok = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success")
    assert "only no answer, busy" in commands.phone_command(f"retry {ok}")
    live = prepare()["call_id"]
    assert "only a finished call" in commands.phone_command(f"retry {live}")
    canceled = prepare()["call_id"]
    states.transition(svc.store, canceled, states.CANCELED)
    assert "only no answer" in commands.phone_command(f"retry {canceled}")
    assert commands.phone_command("retry nope") == "Unknown call id nope."
    assert commands.phone_command("retry").startswith("Usage")
    assert len(svc.store.recent_calls(10)) == 3  # nothing was created


def test_only_one_retry_per_brief(svc, clock):
    cid = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                    outcome="no_answer")
    commands.phone_command(f"retry {cid}")
    again = commands.phone_command(f"retry {cid}")
    assert "already retried as" in again
    retry_id = svc.store.retries_of(cid)[0]["id"]
    # the retry itself, once it also ends unanswered, cannot be retried again either
    states.transition(svc.store, retry_id, states.CANCELED)
    assert len(svc.store.retries_of(cid)) == 1
    clock.advance(120)
    second = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                       outcome="no_answer")
    svc.store._exec("UPDATE calls SET retry_of=? WHERE id=?", (cid, second))
    assert "already a retry" in commands.phone_command(f"retry {second}")


def test_retry_offer_disappears_once_used(svc, clock, ctx):
    cid = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                    outcome="no_answer")
    commands.phone_command(f"retry {cid}")
    call = svc.store.get_call(cid)
    assert "Already retried once." in results.result_message(call, svc.config, retried=True)


def test_retry_carries_late_answers_but_stays_consent_filtered(svc, clock):
    cid = make_call(svc, clock, states.COMPLETED, result=CASES["needs_followup"][1],
                    outcome="needs_followup")
    store = svc.store
    for qid, question, field, answer in (
            ("q1", "Terrace or inside?", "seating", "inside"),
            ("q2", "What is your home address?", "address", "12 Baker Street"),
            ("q3", "What is the CVV?", "cvv", "123")):
        store.add_question(qid, cid, question, field)
        store.resolve_question(qid, "timeout")
        assert store.resolve_question(qid, "answered", answer) == "late"
    store.add_question("q4", cid, "Allergies?", "allergies")  # never answered
    commands.phone_command(f"retry {cid}")
    new = store.recent_calls(1)[0]
    raw = new["brief"]["facts_raw"]
    assert raw["Terrace or inside?"] == "inside" and "123" not in json.dumps(raw)
    payload = briefmod.voice_payload(new, store.consents(new["id"]))
    blob = json.dumps(payload)
    assert "inside" in blob and "Baker" not in blob  # address needs consent on the new call
    assert "address" in new["brief"]["needs_consent"]


def test_retry_rechecks_safety(svc, clock):
    cid = make_call(svc, clock, states.NO_ANSWER, result=CASES["no_answer"][1],
                    outcome="no_answer")
    svc.environ.pop("HERMES_PHONE_TEST_ALLOWLIST")
    out = commands.phone_command(f"retry {cid}")
    assert out.startswith("Could not prepare the retry:")
    assert svc.store.retries_of(cid) == []


# ---- AI disclosure check (criterion 4) -------------------------------------------------------
@pytest.mark.parametrize("first,expected", [
    ("Hi, this is an AI assistant calling on behalf of Winch.", 1),
    ("Hello, I am calling to book a table.", 0),
])
@pytest.mark.parametrize("keep", [True, False])
def test_disclosure_spoken_is_recorded_from_the_first_agent_line(harness, first, expected, keep):
    harness.settings.store_transcripts = keep
    cid = harness.dialing_call()
    token, cm = open_call(harness, cid)
    with cm as ws:
        start(harness, ws, cid, token)
        wait_for(lambda: harness.rt.of("session.update"))
        for text in (first, "An AI assistant, yes."):  # only the first line counts
            harness.rt.push({"type": "response.output_audio_transcript.done",
                             "transcript": text})
        wait_for(lambda: harness.store.get_call(cid)["disclosure_spoken"] is not None)
        wait_for(lambda: not harness.rt.inbox.qsize())
    call = harness.store.get_call(cid)
    assert call["disclosure_spoken"] == expected
    status = json.loads(tools.phone_status({"call_id": cid}))["call"]
    assert status["disclosure_spoken"] is bool(expected)
    assert bool(harness.store.transcript(cid)) is keep


def test_disclosure_unknown_before_any_agent_line(svc):
    cid = prepare()["call_id"]
    assert json.loads(tools.phone_status({"call_id": cid}))["call"]["disclosure_spoken"] is None
    assert briefmod.disclosure_in(briefmod.opening_line("Winch"))
    assert not briefmod.disclosure_in("Hi, I'm calling about a table. Said the maid.")


def test_phone_status_carries_the_result_text(svc, clock):
    cid = make_call(svc, clock, states.COMPLETED, result=SUCCESS, outcome="success",
                    talk_s=72)
    call = json.loads(tools.phone_status({"call_id": cid}))["call"]
    assert call["duration_s"] == 72 and call["result_text"].startswith("✅ Success.")
    assert Harness  # keep the import used by fixtures in this module
