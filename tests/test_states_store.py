from __future__ import annotations

import sqlite3

import pytest

from hermes_phone import states
from hermes_phone.store import Store


def _mk(store, cid="c1", state=states.DRAFT):
    store.create_call(cid, state=state, callee_name="B", number="+12125550123", number_source="u",
                      goal="g", brief={"x": 1})


SPEC_PATHS = [
    ["awaiting_approval", "approved", "dialing", "ringing", "in_progress", "on_hold_for_user",
     "in_progress", "completed"],
    ["awaiting_approval", "approved", "dialing", "in_progress", "voicemail", "completed"],
    ["awaiting_approval", "approved", "dialing", "no_answer"],
    ["awaiting_approval", "approved", "dialing", "ringing", "busy"],
    ["awaiting_approval", "approved", "dialing", "failed"],
    ["awaiting_approval", "canceled"],
    ["awaiting_approval", "expired"],
    ["awaiting_approval", "approved", "dialing", "in_progress", "aborted"],
]


@pytest.mark.parametrize("path", SPEC_PATHS)
def test_spec_paths(store, path):
    _mk(store)
    for new in path:
        call = states.transition(store, "c1", new)
        assert call["state"] == new
    assert path[-1] in states.TERMINAL
    assert call["ended_at"] is not None


@pytest.mark.parametrize("old", sorted(states.TERMINAL))
def test_terminal_states_have_no_exit(store, old):
    _mk(store, state=old)
    for new in states.ALLOWED:
        with pytest.raises(states.IllegalTransition):
            states.transition(store, "c1", new)


@pytest.mark.parametrize("old,new", [
    (states.DRAFT, states.APPROVED), (states.DRAFT, states.DIALING),
    (states.AWAITING_APPROVAL, states.DIALING), (states.AWAITING_APPROVAL, states.IN_PROGRESS),
    (states.APPROVED, states.COMPLETED), (states.DIALING, states.COMPLETED),
    (states.IN_PROGRESS, states.DIALING), (states.IN_PROGRESS, states.NO_ANSWER),
    (states.ON_HOLD_FOR_USER, states.VOICEMAIL), (states.RINGING, states.EXPIRED),
])
def test_illegal_transitions(store, old, new):
    _mk(store, state=old)
    with pytest.raises(states.IllegalTransition):
        states.transition(store, "c1", new)
    assert store.get_call("c1")["state"] == old


def test_unknown_call(store):
    with pytest.raises(states.UnknownCall):
        states.transition(store, "nope", states.CANCELED)


def test_every_active_state_can_abort():
    for s in states.LIVE:
        assert states.can_transition(s, states.ABORTED)


def test_transition_logs_event(store):
    _mk(store)
    states.transition(store, "c1", states.AWAITING_APPROVAL)
    ev = store.events("c1")
    assert [e["kind"] for e in ev] == ["state"]


def test_stale_awaiting_expires(store, clock):
    _mk(store, state=states.AWAITING_APPROVAL)
    clock.advance(states.APPROVAL_TTL_S - 1)
    assert states.expire_if_stale(store, "c1")["state"] == states.AWAITING_APPROVAL
    clock.advance(2)
    assert states.expire_if_stale(store, "c1")["state"] == states.EXPIRED


def test_stale_approved_expires(store, clock):
    _mk(store, state=states.APPROVED)
    store.record_approval("c1", user_id="u", session_id="s", ttl_s=900)
    clock.advance(901)
    assert states.expire_if_stale(store, "c1")["state"] == states.EXPIRED


def test_approval_one_use_and_ttl(store, clock):
    _mk(store)
    store.record_approval("c1", user_id="u", session_id="s", ttl_s=900)
    appr = store.get_approval("c1")
    assert appr["user_id"] == "u" and appr["session_id"] == "s"
    assert store.consume_approval("c1") is True
    assert store.consume_approval("c1") is False
    store.record_approval("c1", user_id="u", session_id="s", ttl_s=10)
    clock.advance(11)
    assert store.consume_approval("c1") is False


def test_migrations_and_wal(tmp_path):
    path = tmp_path / "m.db"
    s = Store(path=path)
    assert s.schema_version() == 5
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"calls", "approvals", "consents", "questions", "events", "transcripts"} <= tables
    s.close()
    s2 = Store(path=path)  # reopen: idempotent migration, second process may open by path
    assert s2.schema_version() == 5
    s2.close()


def test_injected_connection(tmp_path):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    s = Store(conn)
    _mk(s)
    assert s.get_call("c1")["callee_name"] == "B"


def test_persists_across_reopen(tmp_path):
    path = tmp_path / "p.db"
    s = Store(path=path)
    _mk(s)
    s.add_transcript_line("c1", "agent", "hello")
    s.close()
    s2 = Store(path=path)
    assert s2.get_call("c1") is not None
    assert s2.transcript("c1") == [{"ts": s2.transcript("c1")[0]["ts"], "role": "agent",
                                    "text": "hello"}]


def test_update_call_rejects_unknown_column(store):
    _mk(store)
    with pytest.raises(ValueError):
        store.update_call("c1", number="+1")
