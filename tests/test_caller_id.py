"""A workspace's own verified caller ID (enckequity/teamshift#2908): used when the auth reply carries it, one redial on the shared
line after a 403 D51, and never a redial for any other error or a timeout."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest
from test_shared_bridge import Shared

from hermes_phone.bridge import auth, telnyx_io

NAME, TOKEN = "t-0123456789", "ts_usg_" + "a" * 64
OWN, SHARED = "+17175550142", "+17175550100"
# The body Telnyx returns for an unverified `from` on POST /v2/calls (HTTP 403).
D51_BODY = json.dumps({"errors": [{
    "code": "D51", "title": "Unverified origination number",
    "detail": "The 'from' number must be a number on your account or a verified number.",
    "source": {"pointer": "/from"},
    "meta": {"url": "https://developers.telnyx.com/docs/overview/errors/D51"}}]}).encode()
OTHER_403 = json.dumps({"errors": [{"code": "D38", "title": "Destination not allowed",
                                    "detail": "Outbound calls to this destination are not enabled."}]}).encode()


def http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.telnyx.com/v2/calls", status, "x", {}, io.BytesIO(body))


def carrier_error(status: int, body: bytes) -> telnyx_io.TelnyxError:
    """The exact error the real client raises for this HTTP answer."""
    client = telnyx_io.TelnyxClient("KEY-test")
    with pytest.MonkeyPatch.context() as mp:
        def refuse(*_a, **_k):
            raise http_error(status, body)
        mp.setattr(telnyx_io.urllib.request, "urlopen", refuse)
        with pytest.raises(telnyx_io.TelnyxError) as info:
            client.post("/calls", {})
    return info.value


class Scripted:
    """Carrier fake: each dial takes the next scripted answer (an exception to raise, or None to accept)."""

    def __init__(self, *answers) -> None:
        self.answers, self.posts = list(answers), []

    def post(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        if path == "/calls":
            answer = self.answers.pop(0) if self.answers else None
            if answer is not None:
                raise answer
            return {"data": {"call_control_id": f"v3:cc{len(self.posts)}"}}
        return {"data": {"result": "ok"}}

    def dials(self) -> list[str]:
        return [b["from"] for p, b in self.posts if p == "/calls"]


def bridge(store, clock, reply: dict, *answers) -> tuple[Shared, Scripted]:
    sb = Shared(store, clock, auth_url="https://auth.invalid/usage")
    sb.app.state.authenticator.http_get = lambda _u, _h, _t: (200, json.dumps(reply))
    tx = Scripted(*answers)
    sb.tx.post = tx.post  # the app holds this client; route its posts through the script
    return sb, tx


def put(sb: Shared, cid: str = "c1"):
    return sb.client.put(f"/v1/calls/{cid}", json=sb.body(cid),
                         headers={"Authorization": f"Bearer {TOKEN}", "X-TeamShift-Workspace": NAME})


def test_the_real_client_reads_the_d51_code_without_putting_the_body_in_the_message():
    exc = carrier_error(403, D51_BODY)
    assert (exc.status, exc.codes, str(exc)) == (403, ("D51",), "HTTP 403")
    assert telnyx_io.unverified_caller(exc)
    assert not telnyx_io.unverified_caller(carrier_error(403, OTHER_403))
    assert not telnyx_io.unverified_caller(carrier_error(422, D51_BODY))  # D51 only counts on a 403
    assert not telnyx_io.unverified_caller(carrier_error(403, b"<html>forbidden</html>"))
    assert not telnyx_io.unverified_caller(telnyx_io.TelnyxError("TimeoutError"))


def test_caller_of_takes_only_an_e164_number():
    assert auth.caller_of(json.dumps({"caller_number": OWN})) == OWN
    for bad in ({"caller_number": "7175550142"}, {"caller_number": 17175550142}, {"caller_number": "+1 717 555 0142"}, {}):
        assert auth.caller_of(json.dumps(bad)) == ""
    assert auth.caller_of("nope") == "" and auth.caller_of("[]") == ""


def test_a_workspace_with_a_verified_number_calls_from_it(store, clock):
    sb, tx = bridge(store, clock, {"caller_number": OWN})
    assert put(sb).status_code == 201
    assert tx.dials() == [OWN]


def test_without_one_the_shared_line_is_used(store, clock):
    sb, tx = bridge(store, clock, {"caller_number": None})
    assert put(sb).status_code == 201
    assert tx.dials() == [SHARED]


def test_a_d51_redials_once_on_the_shared_line(store, clock):
    sb, tx = bridge(store, clock, {"caller_number": OWN}, carrier_error(403, D51_BODY))
    r = put(sb)
    assert r.status_code == 201, r.text
    assert tx.dials() == [OWN, SHARED]
    assert store.get_call(r.json()["bridge_call_id"])["provider_sid"] == "v3:cc2"


def test_a_d51_on_the_shared_line_too_is_not_redialed_again(store, clock):
    sb, tx = bridge(store, clock, {"caller_number": OWN}, carrier_error(403, D51_BODY), carrier_error(403, D51_BODY))
    r = put(sb)
    assert (r.status_code, r.json()["error"]["code"]) == (502, "dial_failed")
    assert tx.dials() == [OWN, SHARED]


@pytest.mark.parametrize("make", [
    lambda: telnyx_io.TelnyxError("TimeoutError"),  # no answer: the call may have been placed
    lambda: telnyx_io.TelnyxError("URLError"),
    lambda: TimeoutError("read timed out"),
    lambda: carrier_error(403, OTHER_403),
    lambda: carrier_error(500, D51_BODY)])
def test_a_timeout_or_any_other_error_is_never_redialed(store, clock, make):
    error = make()
    sb, tx = bridge(store, clock, {"caller_number": OWN}, error)
    r = put(sb)
    assert (r.status_code, r.json()["error"]["code"]) == (502, "dial_failed")
    assert tx.dials() == [OWN]


def test_without_a_workspace_number_a_d51_is_not_redialed(store, clock):
    sb, tx = bridge(store, clock, {}, carrier_error(403, D51_BODY))
    assert put(sb).json()["error"]["code"] == "dial_failed"
    assert tx.dials() == [SHARED]
