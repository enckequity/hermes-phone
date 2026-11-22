"""Opt-in call playbooks (#76 new-patient check, #91 hold wait): never dial, approval per call."""
from __future__ import annotations

import json
import stat

import pytest
from conftest import TEST_NUMBER

from hermes_phone import brief as briefmod
from hermes_phone import commands, states, tools
from hermes_phone import playbooks as pb
from hermes_phone.bridge import business, realtime


@pytest.fixture
def home(tmp_path):
    return {"HERMES_HOME": str(tmp_path / "hh")}


DOCS = [{"name": "Dr. Ada Example", "office": "Sample Family Medicine", "number": TEST_NUMBER}]


def test_both_playbooks_are_off_by_default(home):
    for slug in pb.SLUGS:
        assert pb.opted_in(slug, home) is False
    with pytest.raises(pb.PlaybookError, match="opt-in"):
        pb.plan_new_patient(DOCS, "Example PPO", "Test Owner", home)
    with pytest.raises(pb.PlaybookError, match="opt-in"):
        pb.plan_hold_wait(TEST_NUMBER, "Example Co", "billing", "a bill", "Test Owner",
                          environ=home)


def test_opt_in_needs_the_users_words_and_opt_out_turns_it_off(home):
    with pytest.raises(pb.PlaybookError):
        pb.opt_in(pb.NEW_PATIENT, "  ", home)
    pb.opt_in(pb.NEW_PATIENT, "yes, turn on doctor calls", home)
    assert pb.opted_in(pb.NEW_PATIENT, home)
    assert not pb.opted_in(pb.HOLD_WAIT, home)  # per playbook
    pb.opt_out(pb.NEW_PATIENT, home)
    assert not pb.opted_in(pb.NEW_PATIENT, home)


def test_state_is_one_private_jsonl_per_slug(home):
    pb.opt_in(pb.HOLD_WAIT, "yes", home)
    path = pb.state_path(pb.HOLD_WAIT, home)
    assert path.name == "hold-wait.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text().splitlines()[0])["kind"] == "opt_in"
    with pytest.raises(pb.PlaybookError):
        pb.state_path("../x", home)


def test_new_patient_brief_asks_only_and_shares_no_health_details():
    args = pb.new_patient_args({"name": "Dr. A", "number": TEST_NUMBER,
                                "condition": "zz-diagnosis"}, "Example PPO", "Test Owner")
    assert "zz-diagnosis" not in json.dumps(args)
    assert "do not book" in args["goal"].lower()
    assert "Do not book" in args["facts_to_share"]["rules"]
    assert args["max_duration_s"] == 300 and args["record"] is False


@pytest.mark.parametrize("bad", ["911", "+19005551234", "my user"])
def test_refuses_unsafe_numbers_and_placeholder_names(bad):
    with pytest.raises(pb.PlaybookError):
        if bad == "my user":
            pb.new_patient_args({"name": "Dr. A", "number": TEST_NUMBER}, "P", bad)
        else:
            pb.new_patient_args({"name": "Dr. A", "number": bad}, "P", "Test Owner")


def test_batch_lists_everything_skips_answered_and_caps_size(home):
    pb.opt_in(pb.NEW_PATIENT, "yes", home)
    first = pb.plan_new_patient(DOCS, "Example PPO", "Test Owner", home)
    assert len(first["entries"]) == 1 and "ONE call at a time" in first["approval"]
    eid = first["entries"][0]["entry_id"]
    pb.log_answer(pb.NEW_PATIENT, eid, "c1", "yes", "yes", "takes it", home)
    again = pb.plan_new_patient(DOCS, "Example PPO", "Test Owner", home)
    assert again["entries"] == [] and again["skipped"][0]["entry_id"] == eid
    with pytest.raises(pb.PlaybookError, match="at most"):
        pb.plan_new_patient(DOCS * (pb.MAX_BATCH + 1), "Example PPO", "Test Owner", home)


def test_answers_need_a_real_call_id_and_known_values(home):
    with pytest.raises(pb.PlaybookError):
        pb.log_answer(pb.NEW_PATIENT, "e1", "", "yes", environ=home)
    with pytest.raises(pb.PlaybookError):
        pb.log_answer(pb.NEW_PATIENT, "e1", "c1", "probably", environ=home)
    with pytest.raises(pb.PlaybookError):
        pb.log_answer(pb.HOLD_WAIT, "e1", "c1", "yes", environ=home)


def test_hold_wait_brief_never_commits_and_alerts_on_a_person():
    args = pb.hold_wait_args(TEST_NUMBER, "Example Insurance", "claims", "a denied claim",
                             "Test Owner", "press 2")
    f = args["facts_to_share"]
    assert "AI assistant" in f["on_reaching_a_person"]
    assert "end the call" in f["on_reaching_a_person"]
    assert "Do not answer account, identity or security questions" in f["rules"]
    assert args["max_duration_s"] == pb.HOLD_MAX_S == 900
    assert f["menu_hints"] == "press 2"


@pytest.mark.parametrize("slug", pb.SLUGS)
def test_dry_run_never_dials(slug, home):
    out = pb.dry_run(slug, home)
    assert out["dialed"] is False and out["summary"]["answers"]
    assert out["summary"]["pending"] == []


@pytest.mark.parametrize("slug", pb.SLUGS)
def test_playbook_args_go_through_the_approval_card_and_dial_only_on_approval(slug, svc, home):
    pb.opt_in(slug, "yes", home)
    if slug == pb.NEW_PATIENT:
        args = pb.plan_new_patient(DOCS, "Example PPO", "Test Owner", home)["entries"][0]
    else:
        args = pb.plan_hold_wait(TEST_NUMBER, "Example Co", "billing", "a bill", "Test Owner",
                                 environ=home)["entries"][0]
    out = json.loads(tools.phone_prepare(args["phone_prepare"], session_id="s1"))
    assert out["state"] == states.AWAITING_APPROVAL, out
    assert out["card_posted"] is True
    dialer = tools.get_service().dialer
    assert dialer.calls == []  # preparing never dials
    refused = json.loads(tools.phone_call({"call_id": out["call_id"]}, session_id="s1"))
    assert "error" in refused and dialer.calls == []
    commands.phone_command(f"approve {out['call_id']} confirm", user_id="u1", session_id="s1")
    assert len(dialer.calls) == 1
    # the voice agent gets the inquiry-only rules, never the booking/negotiation rules
    call = tools.get_service().store.get_call(out["call_id"])
    payload = briefmod.voice_payload(call, set())
    assert payload["inquiry_only"] is True
    for hold in (True, False):
        text = realtime.build_instructions(payload, hold)
        assert "INQUIRY ONLY" in text
        assert "ACCEPT any offer" not in text and "NEGOTIATE" not in text
        assert "pencil it in" not in text and "confirm the key details" not in text
    assert "Inquiry only" in business.rules_text("Test Owner", True)
    assert "Negotiate" not in business.rules_text("Test Owner", True)


def test_ordinary_calls_keep_the_booking_rules():
    payload = {"callee_name": "X", "user_name": "Test Owner", "goal": "book", "opening_line": "",
               "facts": {}}
    assert "ACCEPT any offer" in realtime.build_instructions(payload, True)
    assert "NEGOTIATE" in realtime.build_instructions(payload, False)
    assert "INQUIRY ONLY" not in realtime.build_instructions(payload, True)


def test_a_corrected_answer_reopens_the_office(home):
    pb.opt_in(pb.NEW_PATIENT, "yes", home)
    eid = pb.plan_new_patient(DOCS, "Example PPO", "Test Owner", home)["entries"][0]["entry_id"]
    pb.log_answer(pb.NEW_PATIENT, eid, "c1", "yes", "yes", "", home)
    pb.log_answer(pb.NEW_PATIENT, eid, "c2", "unknown", "unknown", "correction", home)
    assert pb.summary(pb.NEW_PATIENT, home)["answers"][0]["answer"] == "unknown"
    again = pb.plan_new_patient(DOCS, "Example PPO", "Test Owner", home)
    assert [e["entry_id"] for e in again["entries"]] == [eid] and again["skipped"] == []


def test_cli_status_and_error_exit(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", home["HERMES_HOME"])
    assert pb.main(["status", "hold-wait"]) == 0
    assert json.loads(capsys.readouterr().out)["opted_in"] is False
    assert pb.main(["plan", "hold-wait", "--on-behalf-of", "Test Owner", "--number",
                    TEST_NUMBER, "--callee", "X", "--reach", "y", "--reason", "z"]) == 2
    assert "opt-in" in json.loads(capsys.readouterr().out)["error"]
