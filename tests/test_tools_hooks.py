"""SPEC acceptance 1, 2, 3, 7 plus approval lifecycle, hooks and command flow."""
from __future__ import annotations

import json

import pytest
from conftest import OTHER_NUMBER, TEST_NUMBER, approve, prepare

from hermes_phone import commands, hooks, states, tools


def call_tool(name, args, **kw):
    return json.loads(tools.HANDLERS[name](args, **kw))


# -- acceptance 1: phone_call without approval is blocked by hook and handler --------------
def test_hook_blocks_without_approval(svc):
    cid = prepare()["call_id"]
    r = hooks.pre_tool_call(tool_name="phone_call", args={"call_id": cid}, task_id="t")
    assert r["action"] == "block" and r["message"]


def test_handler_refuses_without_approval(svc):
    cid = prepare()["call_id"]
    out = call_tool("phone_call", {"call_id": cid})
    assert out["approved"] is False and "error" in out
    assert svc.dialer.calls == []
    assert svc.store.get_call(cid)["state"] == states.AWAITING_APPROVAL


def test_hook_blocks_unknown_and_garbage(svc):
    for args in ({"call_id": "nope"}, {}, None, "x", {"call_id": 5}, {"call_id": ""}):
        r = hooks.pre_tool_call(tool_name="phone_call", args=args)
        assert r["action"] == "block", args


def test_hook_ignores_other_tools(svc):
    assert hooks.pre_tool_call(tool_name="web_search", args={}, task_id="t") is None
    assert hooks.pre_tool_call(tool_name="phone_status", args={"call_id": "x"}) is None


def test_hook_passes_with_approval_and_handler_dials(svc):
    cid = prepare()["call_id"]
    approve(svc, cid)
    assert hooks.pre_tool_call(tool_name="phone_call", args={"call_id": cid}, task_id="t") is None
    out = call_tool("phone_call", {"call_id": cid})
    assert out == {"call_id": cid, "state": "dialing"}
    assert len(svc.dialer.calls) == 1
    assert svc.store.get_call(cid)["dialed_at"] is not None


def test_hook_fails_closed_on_internal_error(svc, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(svc, "check_approval", boom)
    r = hooks.pre_tool_call(tool_name="phone_call", args={"call_id": "c"})
    assert r["action"] == "block"


def test_approval_is_bound_to_exact_call_id(svc):
    a, b = prepare()["call_id"], prepare()["call_id"]
    approve(svc, a)
    assert hooks.pre_tool_call(tool_name="phone_call", args={"call_id": b})["action"] == "block"
    appr = svc.store.get_approval(a)
    assert appr["user_id"] == "u1" and appr["session_id"] == "s1" and appr["approved_at"]


def test_approval_one_use(svc, clock):
    cid = prepare()["call_id"]
    approve(svc, cid)
    assert "error" not in call_tool("phone_call", {"call_id": cid})
    again = call_tool("phone_call", {"call_id": cid})
    assert "error" in again and len(svc.dialer.calls) == 1
    assert hooks.pre_tool_call(tool_name="phone_call", args={"call_id": cid})["action"] == "block"


def test_approval_ttl_expiry(svc, clock):
    cid = prepare()["call_id"]
    approve(svc, cid)
    clock.advance(states.APPROVAL_TTL_S + 1)
    assert hooks.pre_tool_call(tool_name="phone_call", args={"call_id": cid})["action"] == "block"
    assert "error" in call_tool("phone_call", {"call_id": cid})
    assert svc.store.get_call(cid)["state"] == states.EXPIRED
    assert svc.dialer.calls == []


def test_unapproved_card_expires(svc, clock):
    cid = prepare()["call_id"]
    clock.advance(states.APPROVAL_TTL_S + 1)
    out = commands.phone_command(f"approve {cid} confirm")
    assert "expired" in out
    assert svc.store.get_call(cid)["state"] == states.EXPIRED


# -- acceptance 2: blocklisted number refused at prepare and at dial -----------------------
@pytest.mark.parametrize(
    "num", ["911", "+1 (911)", "9-1-1", "tel:911", "112", "999", "+1 900 555 0123"])
def test_prepare_refuses_blocked(svc, num):
    out = prepare({"number": num})
    assert out.get("error") and out.get("blocked")
    assert svc.store.recent_calls() == []


def test_dial_refuses_blocked_even_with_approval(svc):
    """Defense in depth: a corrupted row with a blocked number still cannot dial."""
    cid = prepare()["call_id"]
    approve(svc, cid)
    svc.store._exec("UPDATE calls SET number='+911' WHERE id=?", (cid,))
    out = call_tool("phone_call", {"call_id": cid})
    assert out.get("blocked") and svc.dialer.calls == []


# -- acceptance 3: allowlist -----------------------------------------------------------------
def test_prepare_refuses_non_allowlisted(svc):
    out = prepare({"number": OTHER_NUMBER})
    assert out.get("blocked") and "allowlist" in out["error"]


def test_prepare_refuses_all_when_allowlist_unset(svc):
    svc._environ = {}
    out = prepare()
    assert out.get("blocked") and "HERMES_PHONE_TEST_ALLOWLIST" in out["error"]


def test_dial_refuses_when_allowlist_removed_after_approval(svc):
    cid = prepare()["call_id"]
    approve(svc, cid)
    svc._environ = {"HERMES_PHONE_TEST_ALLOWLIST": OTHER_NUMBER}
    out = call_tool("phone_call", {"call_id": cid})
    assert out.get("blocked") and svc.dialer.calls == []


def test_allow_all_env(svc):
    svc._environ = {"HERMES_PHONE_ALLOW_ALL": "1"}
    assert "call_id" in prepare({"number": OTHER_NUMBER})


# -- quiet hours and rate limits through the tools -------------------------------------------
def test_quiet_hours_refused_then_override(svc, clock):
    clock.advance(-10 * 3600)  # 05:00 New York
    out = prepare()
    assert out.get("quiet_hours")
    ok = prepare({"override_quiet_hours": True})
    assert "call_id" in ok and ok["brief"]["quiet_hours_override"] is True


def test_quiet_hours_rechecked_at_dial(svc, clock):
    cid = prepare()["call_id"]
    approve(svc, cid)
    clock.advance(12 * 3600)  # 03:00 UTC next day = 22:00 in New York: past quiet-hours start
    svc.store._exec("UPDATE approvals SET expires_at=? WHERE call_id=?", (clock() + 100, cid))
    out = call_tool("phone_call", {"call_id": cid})
    assert out.get("quiet_hours") and svc.dialer.calls == []


def test_one_active_call_and_min_gap(svc, clock):
    a = prepare()["call_id"]
    approve(svc, a)
    assert "error" not in call_tool("phone_call", {"call_id": a})
    b = prepare()["call_id"]
    approve(svc, b)
    out = call_tool("phone_call", {"call_id": b})
    assert out.get("rate_limited") and "active" in out["error"]
    states.transition(svc.store, a, states.NO_ANSWER)
    out = call_tool("phone_call", {"call_id": b})
    assert out.get("rate_limited") and "moments ago" in out["error"]
    assert svc.store.get_call(b)["state"] == states.APPROVED  # approval not burned by a refusal
    clock.advance(61)
    assert "error" not in call_tool("phone_call", {"call_id": b})


def test_daily_limit_from_config(svc, ctx, clock):
    ctx.config["daily_call_limit"] = 1
    a = prepare()["call_id"]
    approve(svc, a)
    call_tool("phone_call", {"call_id": a})
    states.transition(svc.store, a, states.NO_ANSWER)
    clock.advance(120)
    b = prepare()["call_id"]
    approve(svc, b)
    out = call_tool("phone_call", {"call_id": b})
    assert out.get("rate_limited") and "Daily" in out["error"]


# -- prepare behaviour ----------------------------------------------------------------------
def test_prepare_card_and_opening_line(svc, ctx):
    out = prepare()
    assert out["state"] == "awaiting_approval" and out["approve_hint"].startswith("/phone approve")
    assert out["brief"]["opening_line"] == (
        "Hi, this is an AI assistant calling on behalf of Winch.")
    assert out["card_posted"] and ctx.messages and out["call_id"] in ctx.messages[0].text
    assert svc.dialer.calls == []


def test_prepare_record_adds_notice(svc):
    out = prepare({"record": True})
    assert out["brief"]["opening_line"].endswith("This call may be recorded.")
    assert out["brief"]["limits"]["recording"] is True


def test_prepare_clamps_duration(svc):
    assert prepare({"max_duration_s": 99999})["brief"]["limits"]["max_duration_s"] == 900
    assert prepare({"max_duration_s": 1})["brief"]["limits"]["max_duration_s"] == 30


@pytest.mark.parametrize("bad", [
    {"callee_name": ""}, {"goal": ""}, {"number": ""}, {"number": 5}, {"facts_to_share": "x"},
    {"sensitive_fields": ["cvv"]}, {"sensitive_fields": ["nope"]}, {"sensitive_fields": "card"},
    {"facts_to_share": {"cvv": "123"}}, {"facts_to_share": {"note": {"otp": "999999"}}},
])
def test_prepare_rejects_bad_input(svc, bad):
    assert "error" in prepare(bad)


# -- acceptance 7: sensitive values never reach the voice model without consent --------------
SECRET_CARD = "4111 1111 1111 1111"
SECRET_ADDR = "221B Baker Street"


def _voice(svc, cid):
    from hermes_phone import brief as briefmod

    call = svc.store.get_call(cid)
    return briefmod.voice_payload(call, svc.store.consents(cid))


def test_sensitive_withheld_without_consent(svc):
    out = prepare({
        "facts_to_share": {"name": "Winch", "card_number": SECRET_CARD,
                           "home_address": SECRET_ADDR, "note": f"pay with {SECRET_CARD}",
                           "contact": "winch@example.com"},
        "goal": f"Book a table, card {SECRET_CARD}", "sensitive_fields": ["card", "address"]})
    cid = out["call_id"]
    payload = json.dumps(_voice(svc, cid))
    public = json.dumps(out) + json.dumps(call_tool("phone_status", {"call_id": cid}))
    for secret in (SECRET_CARD, "4111", SECRET_ADDR, "winch@example.com"):
        assert secret not in payload, secret
        assert secret not in public, secret
    assert "Winch" in payload
    assert set(out["brief"]["needs_consent"]) == {"card", "address"}


def test_consent_is_per_field_and_per_call(svc):
    facts = {"name": "Winch", "card_number": SECRET_CARD, "home_address": SECRET_ADDR}
    a = prepare({"facts_to_share": facts, "sensitive_fields": ["card", "address"]})["call_id"]
    b = prepare({"facts_to_share": facts, "sensitive_fields": ["card", "address"]})["call_id"]
    msg = commands.phone_command(f"consent {a} card")
    assert "recorded" in msg
    assert SECRET_ADDR not in msg  # the refreshed card never prints values of withheld facts
    pa = json.dumps(_voice(svc, a))
    assert SECRET_CARD in pa and SECRET_ADDR not in pa
    pb = json.dumps(_voice(svc, b))
    assert SECRET_CARD not in pb and SECRET_ADDR not in pb


@pytest.mark.parametrize("field", ["cvv", "password", "otp"])
def test_forbidden_never_granted(svc, field):
    cid = prepare()["call_id"]
    assert "never shared" in commands.phone_command(f"consent {cid} {field}")
    assert svc.store.consents(cid) == set()


# -- status / transcript ----------------------------------------------------------------------
def test_status_and_transcript(svc):
    cid = prepare()["call_id"]
    st = call_tool("phone_status", {"call_id": cid})
    assert st["call"]["state"] == "awaiting_approval" and "facts_raw" not in json.dumps(st)
    assert len(call_tool("phone_status", {})["calls"]) == 1
    svc.store.add_transcript_line(cid, "agent", "Hi")
    tr = call_tool("phone_transcript", {"call_id": cid})
    assert tr["transcript"] == [{"role": "agent", "text": "Hi"}] and "partial" in tr["note"]


def test_transcripts_can_be_disabled(svc, ctx):
    cid = prepare()["call_id"]
    svc.store.add_transcript_line(cid, "agent", "Hi")
    ctx.config["store_transcripts"] = False
    assert call_tool("phone_transcript", {"call_id": cid})["transcript"] == []


# -- garbage input never raises, always valid JSON ---------------------------------------------
GARBAGE = [None, "x", 5, [], {}, {"call_id": None}, {"call_id": ["a"]}, {"number": {"a": 1}},
           {"callee_name": 1, "number": 2, "goal": 3}, {"limit": "abc"}, {"call_id": "x" * 500},
           {"facts_to_share": None, "sensitive_fields": None}]


@pytest.mark.parametrize("name", sorted(tools.HANDLERS))
@pytest.mark.parametrize("args", GARBAGE)
def test_handlers_never_raise(svc, name, args):
    out = tools.HANDLERS[name](args, task_id="t", session_id="s", unexpected=1)
    assert isinstance(out, str)
    assert isinstance(json.loads(out), dict)


def test_handlers_survive_broken_store(svc, monkeypatch):
    monkeypatch.setattr(tools.PhoneService, "store", property(lambda self: 1 / 0))
    for h in tools.HANDLERS.values():
        assert "error" in json.loads(h({"call_id": "x", "callee_name": "a", "number": TEST_NUMBER,
                                        "goal": "g"}))


# -- pre_llm_call --------------------------------------------------------------------------------
def test_pre_llm_call_nothing_pending(svc):
    assert hooks.pre_llm_call(session_id="s", user_message="hi") is None


def test_pre_llm_call_reports_result_once_and_questions(svc):
    cid = prepare()["call_id"]
    approve(svc, cid)
    call_tool("phone_call", {"call_id": cid})
    svc.watchers.pop(cid).stop.set()  # Hermes restarted: no watcher will post the result
    states.transition(svc.store, cid, states.NO_ANSWER, outcome="no_answer")
    svc.store.add_question("q1", cid, "terrace or inside?", "seating", "inside")
    out = hooks.pre_llm_call(session_id="s")
    assert "No answer" in out["context"] and "terrace or inside?" in out["context"]
    again = hooks.pre_llm_call(session_id="s")  # result delivered; question is still open
    assert "No answer" not in again["context"] and "q1" in again["context"]


# -- /phone command ---------------------------------------------------------------------------
def test_approve_via_ask_choice_call(svc, ctx):
    cid = prepare()["call_id"]
    ctx.choices = ["Call"]
    out = commands.phone_command(f"approve {cid}", user_id="u9", session_id="sx")
    assert out.startswith("Approved") and ctx.asked[0][1] == ["Call", "Edit", "Cancel"]
    assert svc.store.get_approval(cid)["user_id"] == "u9"


def test_approve_via_ask_choice_cancel_and_edit(svc, ctx):
    a, b = prepare()["call_id"], prepare()["call_id"]
    ctx.choices = ["Cancel", "Edit"]
    assert "canceled" in commands.phone_command(f"approve {a}")
    assert "edited" in commands.phone_command(f"approve {b}")
    assert svc.store.get_call(a)["state"] == "canceled"
    assert svc.store.get_call(b)["reason"] == "edit_requested"
    assert svc.store.get_approval(a) is None


def test_approve_requires_confirm_without_buttons(svc, ctx):
    cid = prepare()["call_id"]  # FakeCtx.ask_choice returns None when nothing scripted
    out = commands.phone_command(f"approve {cid}")
    assert f"/phone approve {cid} confirm" in out
    assert svc.store.get_approval(cid) is None
    assert commands.phone_command(f"approve {cid} confirm").startswith("Approved")


def test_approve_without_ask_choice_attribute(svc):
    svc.ctx = object()
    cid = prepare()["call_id"]
    assert "confirm" in commands.phone_command(f"approve {cid}")


def test_cannot_approve_twice_or_unknown(svc):
    cid = prepare()["call_id"]
    approve(svc, cid)
    # Re-approving an approved-but-undialed call dials it once; after that it cannot repeat.
    assert commands.phone_command(f"approve {cid} confirm").startswith("Approved. Dialing")
    assert "only a call awaiting approval" in commands.phone_command(f"approve {cid} confirm")
    assert "Unknown" in commands.phone_command("approve nope confirm")


def test_cannot_approve_if_allowlist_lost(svc):
    cid = prepare()["call_id"]
    svc._environ = {}
    assert commands.phone_command(f"approve {cid} confirm").startswith("Cannot approve")


def test_cancel_status_log_transcript(svc):
    cid = prepare()["call_id"]
    assert cid in commands.phone_command("status")
    assert cid in commands.phone_command(f"status {cid}")
    assert cid in commands.phone_command("log 5")
    assert "canceled" in commands.phone_command(f"cancel {cid}")
    assert svc.store.get_call(cid)["state"] == "canceled"
    assert "already canceled" in commands.phone_command(f"cancel {cid}")
    assert commands.phone_command(f"transcript {cid}")
    assert hooks.pre_tool_call(tool_name="phone_call", args={"call_id": cid})["action"] == "block"


def test_commands_usage_and_unknowns(svc):
    assert "Unknown question" in commands.phone_command("answer q1 yes")
    assert "No active call" in commands.phone_command("hangup")
    assert commands.phone_command("").startswith("Usage")
    assert commands.phone_command("bogus").startswith("Usage")
    for raw in ("approve", "cancel", "consent", "consent x", "transcript", "status zzz"):
        assert isinstance(commands.phone_command(raw), str)
