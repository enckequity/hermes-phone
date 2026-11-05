from __future__ import annotations

from datetime import UTC, datetime

import pytest
from conftest import OTHER_NUMBER, TEST_NUMBER

from hermes_phone import safety, states
from hermes_phone.safety import check_allowlist, check_quiet_hours, check_rate_limits, is_blocked

BLOCKED = [
    "911", "+911", "112", "999", "000", "110", "119", "100", "101", "102", "103",
    "+1 (911)", "9-1-1", "9 1 1", "tel:911", "tel:+911", "0911", "00911", "+1 911", "1-911",
    "(911)", "911;ext=5", "9l1", "+44 999", "+61 000", "+1 900 555 0123", "1-900-555-0123",
    "+1 976 555 0123", "1-976-555-0123", "12345", "+12345", "", "abc", "   ", "+1 4",
]


@pytest.mark.parametrize("raw", BLOCKED)
def test_blocklist_bypass_attempts(raw):
    assert not is_blocked(raw).ok, raw
    e164, reason = safety.validate_number(raw)
    assert e164 is None and reason


@pytest.mark.parametrize("raw", [None, 911, 0, [], {}])
def test_blocklist_handles_garbage_types(raw):
    e164, _ = safety.validate_number(raw)
    assert e164 is None


@pytest.mark.parametrize("raw,expected", [
    ("+12125550123", "+12125550123"),
    ("+1 (212) 555-0123", "+12125550123"),
    ("tel:+12125550123", "+12125550123"),
    ("+44 20 7946 0958", "+442079460958"),
    ("0044 20 7946 0958", "+442079460958"),
])
def test_normalizes_to_e164(raw, expected):
    assert safety.validate_number(raw) == (expected, "")


def test_rejects_non_international_input():
    e164, reason = safety.validate_number("212 555 0123")
    assert e164 is None and "E.164" in reason


def test_allowlist_unset_refuses_everything():
    assert not check_allowlist(TEST_NUMBER, {}).ok
    assert not check_allowlist(TEST_NUMBER, {"HERMES_PHONE_TEST_ALLOWLIST": "  , "}).ok


def test_allowlist_only_listed_numbers():
    env = {"HERMES_PHONE_TEST_ALLOWLIST": f"{TEST_NUMBER}, +442079460958"}
    assert check_allowlist(TEST_NUMBER, env).ok
    assert check_allowlist("+442079460958", env).ok
    assert not check_allowlist(OTHER_NUMBER, env).ok


def test_allow_all_requires_explicit_env_one():
    assert check_allowlist(OTHER_NUMBER, {"HERMES_PHONE_ALLOW_ALL": "1"}).ok
    assert not check_allowlist(OTHER_NUMBER, {"HERMES_PHONE_ALLOW_ALL": "true"}).ok
    assert not check_allowlist(OTHER_NUMBER, {"HERMES_PHONE_ALLOW_ALL": "0"}).ok


def _utc(h, m=0):
    return datetime(2026, 11, 4, h, m, tzinfo=UTC)


def test_quiet_hours_boundaries_new_york():
    # NY is UTC-5 on 2026-11-04. 08:00 local = 13:00 UTC, 21:00 local = 02:00 UTC next day.
    assert not check_quiet_hours(TEST_NUMBER, _utc(12, 59)).ok
    assert check_quiet_hours(TEST_NUMBER, _utc(13, 0)).ok
    assert check_quiet_hours(TEST_NUMBER, _utc(23, 59)).ok
    late = datetime(2026, 11, 5, 2, 0, tzinfo=UTC)
    assert not check_quiet_hours(TEST_NUMBER, late).ok
    assert check_quiet_hours(TEST_NUMBER, datetime(2026, 11, 5, 1, 59, tzinfo=UTC)).ok


def test_quiet_hours_override():
    assert check_quiet_hours(TEST_NUMBER, _utc(7), override=True).ok


def test_quiet_hours_naive_datetime_treated_as_utc():
    assert check_quiet_hours(TEST_NUMBER, datetime(2026, 11, 4, 15, 0)).ok


def _dial(store, cid, number, state=states.IN_PROGRESS):
    store.create_call(cid, state=states.DRAFT, callee_name="x", number=number, number_source="",
                      goal="g", brief={})
    store.update_call(cid, state=state, dialed_at=store.clock())


def test_rate_limit_one_active_call(store):
    _dial(store, "a", TEST_NUMBER)
    r = check_rate_limits(store, OTHER_NUMBER)
    assert not r.ok and "active" in r.reason
    assert check_rate_limits(store, OTHER_NUMBER, exclude_call_id="a").ok


def test_rate_limit_min_gap_same_number(store, clock):
    _dial(store, "a", TEST_NUMBER, state=states.COMPLETED)
    clock.advance(30)
    r = check_rate_limits(store, TEST_NUMBER)
    assert not r.ok and "moments ago" in r.reason
    assert check_rate_limits(store, OTHER_NUMBER).ok
    clock.advance(31)
    assert check_rate_limits(store, TEST_NUMBER).ok


def test_rate_limit_daily(store, clock):
    for i in range(3):
        _dial(store, f"c{i}", f"+1212555010{i}", state=states.COMPLETED)
    assert not check_rate_limits(store, OTHER_NUMBER, daily_limit=3).ok
    assert check_rate_limits(store, OTHER_NUMBER, daily_limit=4).ok
    clock.advance(86401)  # rolling 24h window
    assert check_rate_limits(store, OTHER_NUMBER, daily_limit=3).ok
