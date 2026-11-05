"""Twilio trial accounts reject some calls.create parameters; the dialer steps down."""
from hermes_phone import dialer as dialer_mod


class _Err(Exception):
    def __init__(self):
        super().__init__("x")
        self.code, self.status = None, 400
        self.msg = ("Unable to create record: Invalid or disallowed parameters provided - "
                    "trial accounts have limited parameter access")


class _Calls:
    def __init__(self, reject):
        self.reject, self.seen = reject, []

    def create(self, **params):
        self.seen.append(params)
        if any(k in params for k in self.reject):
            raise _Err()
        return type("C", (), {"sid": "CA" + "1" * 32})()


def _dialer(reject):
    env = {"TWILIO_ACCOUNT_SID": "AC" + "1" * 32, "TWILIO_AUTH_TOKEN": "t",
           "TWILIO_FROM_NUMBER": "+12125550100", "PUBLIC_BASE_URL": "https://h.example",
           "HERMES_PHONE_TEST_ALLOWLIST": "+12125550123"}
    calls = _Calls(reject)
    d = dialer_mod.TwilioDialer(environ=env, client=type("Cl", (), {"calls": calls})())
    return d, calls


CALL = {"id": "c1", "number": "+12125550123", "brief": {"limits": {"max_duration_s": 300}}}


def test_full_params_when_allowed():
    d, calls = _dialer(reject=())
    assert d.place_call(CALL, {}).ok
    assert len(calls.seen) == 1 and "machine_detection" in calls.seen[0]


def test_steps_down_when_trial_rejects_amd_and_time_limit():
    d, calls = _dialer(reject=("machine_detection", "time_limit"))
    res = d.place_call(CALL, {})
    assert res.ok and res.provider_sid.startswith("CA")
    assert len(calls.seen) == 2 and "status_callback_event" in calls.seen[1]


def test_bare_minimum_as_last_resort():
    d, calls = _dialer(reject=("machine_detection", "status_callback_event"))
    assert d.place_call(CALL, {}).ok and len(calls.seen) == 3


def test_reports_error_when_even_minimum_fails():
    d, calls = _dialer(reject=("to",))
    res = d.place_call(CALL, {})
    assert not res.ok and "disallowed parameters" in res.error and len(calls.seen) == 3
