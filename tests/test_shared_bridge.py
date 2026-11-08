"""Shared multi-tenant bridge (/v1, Telnyx): tenancy, limits, signatures, AMD, media, usage."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from bridge_support import MULAW_20MS, FakeRealtime, wait_for
from conftest import OTHER_NUMBER, TEST_NUMBER
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_phone import states
from hermes_phone.bridge import BridgeSettings, create_app, status, telnyx_io, tenants, v1
from hermes_phone.bridge import realtime as rt

BASE = "https://voice.example.test"
TOKEN_A, TOKEN_B = "workspace-a-token-" + "a" * 20, "workspace-b-token-" + "b" * 20
VENDORS = re.compile(r"twilio|telnyx|openai|linq|hermes|photon|gpt|realtime|anthropic|claude",
                     re.I)
ROOT = Path(__file__).resolve().parent.parent / "hermes_phone" / "bridge"


def sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def tenant_config(cap_a: int = 5) -> str:
    return json.dumps([
        {"token_sha256": sha(TOKEN_A), "workspace": "wa", "business_name": "Acme Plumbing",
         "daily_cap": cap_a, "tz": "America/New_York"},
        {"token_sha256": sha(TOKEN_B), "workspace": "wb", "business_name": "Bolt Dental",
         "daily_cap": 5, "tz": "Asia/Tokyo"}])


class FakeTelnyx:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.fail_dial = False

    def post(self, path: str, body: dict) -> dict:
        self.posts.append((path, body))
        if path == "/calls":
            if self.fail_dial:
                raise telnyx_io.TelnyxError("HTTP 500")
            return {"data": {"call_control_id": f"v3:cc{len(self.posts)}"}}
        return {"data": {"result": "ok"}}

    def actions(self, name: str) -> list[tuple[str, dict]]:
        return [(p, b) for p, b in self.posts if p.endswith("/actions/" + name)]


class Shared:
    def __init__(self, store, clock, cap_a: int = 5) -> None:
        self.store, self.clock = store, clock
        self.key = Ed25519PrivateKey.generate()
        pub = base64.b64encode(self.key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)).decode()
        self.rt, self.tx = FakeRealtime(), FakeTelnyx()
        self.settings = BridgeSettings(
            public_base_url=BASE, openai_api_key="sk-test", tenants_raw=tenant_config(cap_a),
            telnyx_api_key="KEY-test", telnyx_connection_id="conn-1", telnyx_public_key=pub,
            caller_number="+17175550100", tick_s=0.01, token_ttl_s=60.0, start_timeout_s=3.0,
            telephony_usd_per_min=0.012, voice_usd_per_min=0.06,
            environ={"HERMES_PHONE_TEST_ALLOWLIST": f"{TEST_NUMBER},{OTHER_NUMBER}"})
        self.app = create_app(self.settings, store=store, realtime_connect=self.rt.connect,
                              telnyx_client=self.tx, clock=clock)
        self.client = TestClient(self.app)
        self._n = 0

    def body(self, cid: str, number: str = TEST_NUMBER, aid: str = "", **brief) -> dict:
        self._n += 1
        now = self.clock()
        b = {"call_id": cid, "callee_name": "Bella Napoli", "goal": "Book a table for 4",
             "opening_line": "Hi, this is an AI assistant calling on behalf of Winch.",
             "facts": {"name": "Winch", "party_size": 4}, "language": "en",
             "max_duration_s": 300, "record": False, "leave_voicemail": False,
             "user_name": "Winch", **brief}
        return {"call_id": cid, "number": number,
                "approval": {"id": aid or f"ap_{self._n:032x}", "approved_at": now - 10,
                             "expires_at": now + 890, "consumed_at": now - 1},
                "brief": b, "limits": {"max_duration_s": 300, "question_timeout_s": 75},
                "options": {"store_transcripts": True}}

    def put(self, cid: str, token: str = TOKEN_A, body: dict | None = None, **kw):
        return self.client.put(f"/v1/calls/{cid}", json=body or self.body(cid, **kw),
                               headers={"Authorization": f"Bearer {token}"})

    def get(self, path: str, token: str = TOKEN_A):
        return self.client.get(path, headers={"Authorization": f"Bearer {token}"})

    def post(self, path: str, body: dict, token: str = TOKEN_A):
        return self.client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})

    def dial(self, cid: str = "c1", **kw) -> tuple[str, str]:
        r = self.put(cid, **kw)
        assert r.status_code == 201, r.text
        return r.json()["bridge_call_id"], self.hook_path(r.json()["bridge_call_id"])

    def hook_path(self, iid: str) -> str:
        for path, body in self.tx.posts:
            if path == "/calls" and f"/{iid}/" in body["webhook_url"]:
                return urlsplit(body["webhook_url"]).path
        raise AssertionError("no dial for " + iid)

    def signed(self, path: str, etype: str, stamp: int | None = None, sig: str | None = None,
               **payload):
        raw = json.dumps({"data": {"event_type": etype, "payload": payload}}).encode()
        stamp = int(self.clock()) if stamp is None else stamp
        good = base64.b64encode(self.key.sign(str(stamp).encode() + b"|" + raw)).decode()
        return self.client.post(path, content=raw, headers={
            "telnyx-timestamp": str(stamp), "telnyx-signature-ed25519": sig or good,
            "Content-Type": "application/json"})

    def event(self, iid: str, etype: str, **payload):
        cc = self.store.get_call(iid)["provider_sid"]
        r = self.signed(self.hook_path(iid), etype, call_control_id=cc, **payload)
        assert r.status_code == 200, r.text
        return r

    def end(self, iid: str, cause: str = "normal_clearing") -> None:
        self.event(iid, "call.hangup", hangup_cause=cause)

    def stream_token(self) -> str:
        url = self.tx.actions("streaming_start")[-1][1]["stream_url"]
        return parse_qs(urlsplit(url).query)["token"][0]


@pytest.fixture
def sb(store, clock):
    return Shared(store, clock)


def start_frames(cc: str) -> list[str]:
    return [json.dumps({"event": "connected", "version": "1.0.0"}),
            json.dumps({"event": "start", "sequence_number": "1", "stream_id": "st-1",
                        "start": {"call_control_id": cc, "media_format": {
                            "encoding": "PCMU", "sample_rate": 8000, "channels": 1}}})]


def answered_human(sb: Shared, iid: str) -> None:
    sb.event(iid, "call.answered")
    sb.event(iid, "call.machine.premium.detection.ended", result="human_residence")


# ---- tenancy ----------------------------------------------------------------------------
def test_tenant_config_hashes_only_and_constant_time_lookup():
    ts = tenants.load(tenant_config())
    assert [t.workspace for t in ts] == ["wa", "wb"]
    assert all(TOKEN_A not in repr(t) for t in ts)
    assert tenants.find(ts, TOKEN_B).workspace == "wb"
    assert tenants.find(ts, "nope") is None and tenants.find(ts, "") is None
    assert tenants.load('[{"token_sha256": "xyz", "workspace": "w"}]') == []  # fail closed


def test_workspace_b_gets_404_on_workspace_a_calls(sb):
    iid, _ = sb.dial("c1")
    sb.store.add_question("qa1", iid, "Name?", "name", "Winch")
    assert sb.get("/v1/calls/c1/updates").status_code == 200
    assert sb.get("/v1/calls/c1/updates", TOKEN_B).status_code == 404
    assert sb.post("/v1/calls/c1/hangup", {}, TOKEN_B).status_code == 404
    assert sb.post("/v1/calls/c1/questions/qa1/answer", {"status": "declined"},
                   TOKEN_B).status_code == 404
    assert sb.get(f"/v1/calls/{iid}/updates", TOKEN_B).status_code == 404  # internal id too
    assert sb.store.get_question("qa1")["status"] == "open"
    assert sb.store.get_call(iid)["hangup_requested_at"] is None
    assert sb.get("/v1/calls/c1/updates", "bogus").status_code == 401
    assert sb.client.get("/v1/health").status_code == 401
    assert sb.get("/v1/health").json() == {"ok": True}


def test_question_from_another_call_is_404_even_for_owner(sb):
    sb.dial("c1")
    sb.store.add_question("qx", "some-other-call", "Name?")
    assert sb.post("/v1/calls/c1/questions/qx/answer", {"status": "declined"}).status_code == 404


# ---- approval and idempotency ------------------------------------------------------------
def test_approval_must_be_consumed_and_unexpired(sb):
    body = sb.body("c1")
    del body["approval"]["consumed_at"]
    assert sb.put("c1", body=body).json()["error"]["code"] == "approval_required"
    body = sb.body("c1")
    body["approval"]["expires_at"] = body["approval"]["consumed_at"] - 1
    assert sb.put("c1", body=body).status_code == 403
    body = sb.body("c1")
    body["approval"]["expires_at"] = sb.clock() - 1
    assert sb.put("c1", body=body).status_code == 403
    assert not sb.tx.posts


def test_idempotent_by_call_id_and_single_use_approval(sb):
    body = sb.body("c1", aid="ap_" + "1" * 32)
    first = sb.put("c1", body=body)
    again = sb.put("c1", body=body)
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["bridge_call_id"] == first.json()["bridge_call_id"]
    assert len([p for p, _ in sb.tx.posts if p == "/calls"]) == 1
    assert sb.put("c1", aid="ap_" + "2" * 32).status_code == 409
    assert sb.put("c2", body=sb.body("c2", aid="ap_" + "1" * 32)).status_code == 409


# ---- bridge-side limits ------------------------------------------------------------------
def test_blocklist_and_allowlist(sb):
    for number in ("911", "+19005551234", "988"):
        assert sb.put("cx", number=number).json()["error"]["code"] == "blocked_number"
    assert sb.put("cx", number="+12125550999").json()["error"]["code"] == "not_allowlisted"
    assert not sb.tx.posts


def test_one_active_call_per_workspace(sb):
    iid, _ = sb.dial("c1")
    r = sb.put("c2", number=OTHER_NUMBER)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "active_call")
    assert sb.put("c3", TOKEN_B, number=OTHER_NUMBER).status_code in (201, 422)  # B is separate
    sb.end(iid, "timeout")
    assert sb.store.get_call(iid)["state"] == states.NO_ANSWER
    assert sb.put("c4", number=OTHER_NUMBER).status_code == 201


def test_daily_cap(store, clock):
    sb = Shared(store, clock, cap_a=2)
    for n in range(2):
        iid, _ = sb.dial(f"c{n}")
        sb.end(iid, "user_busy")
        clock.advance(120)
    r = sb.put("c9")
    assert (r.status_code, r.json()["error"]["code"]) == (422, "daily_cap")
    clock.advance(86400)
    assert sb.put("c10").status_code == 201


def test_no_auto_redial(sb):
    iid, _ = sb.dial("c1")
    sb.end(iid, "user_busy")
    sb.clock.advance(30)
    assert sb.put("c2").json()["error"]["code"] == "redial_too_soon"
    sb.tx.fail_dial = True
    sb.clock.advance(60)
    body = sb.body("c3")
    r = sb.put("c3", body=body)
    assert (r.status_code, r.json()["error"]["code"]) == (502, "dial_failed")
    assert sb.put("c3", body=body).json()["state"] == states.FAILED  # repeat: no second dial
    assert len([p for p, _ in sb.tx.posts if p == "/calls"]) == 2


def test_quiet_hours_callee_time(sb):
    sb.clock.t -= 9 * 3600  # 01:00 in New York
    r = sb.put("c1")
    assert (r.status_code, r.json()["error"]["code"]) == (422, "quiet_hours")


def test_quiet_hours_unknown_zone_uses_workspace_tz(sb, monkeypatch):
    from phonenumbers import timezone as pn_tz

    monkeypatch.setattr(pn_tz, "time_zones_for_number", lambda _n: ("Etc/Unknown",))
    # 15:00 UTC: 10:00 for workspace A (New York), 00:00 for workspace B (Tokyo)
    assert sb.put("c1").status_code == 201
    assert sb.put("c1", TOKEN_B).json()["error"]["code"] == "quiet_hours"


def test_duration_capped(sb):
    iid, _ = sb.dial("c1", max_duration_s=5000)
    call = sb.store.get_call(iid)
    assert call["brief"]["limits"]["max_duration_s"] == 300  # the plugin limit (300) wins
    body = sb.body("c2", number=OTHER_NUMBER, max_duration_s=5000)
    body["limits"]["max_duration_s"] = 5000
    sb.end(iid)
    r = sb.put("c2", body=body)
    assert sb.store.get_call(r.json()["bridge_call_id"])["brief"]["limits"][
        "max_duration_s"] == 900
    body = sb.body("c3", max_duration_s=None)
    del body["limits"]["max_duration_s"]
    sb.end(r.json()["bridge_call_id"])
    sb.clock.advance(120)
    r3 = sb.put("c3", body=body)
    assert sb.store.get_call(r3.json()["bridge_call_id"])["brief"]["limits"][
        "max_duration_s"] == 300


# ---- signatures --------------------------------------------------------------------------
def test_webhook_signature_pass_fail_and_stale(sb):
    iid, path = sb.dial("c1")
    assert sb.signed(path, "call.initiated").status_code == 200
    assert sb.signed(path, "call.answered", sig=base64.b64encode(b"x" * 64).decode()
                     ).status_code == 403
    assert sb.signed(path, "call.answered", stamp=int(sb.clock()) - 301).status_code == 403
    assert sb.client.post(path, content=b"{}").status_code == 403  # no headers
    assert sb.store.get_call(iid)["state"] == states.DIALING  # nothing applied
    wrong = path.rsplit("/", 1)[0] + "/" + "x" * 43
    assert sb.signed(wrong, "call.answered").status_code == 404
    assert not telnyx_io.verify_signature("", b"{}", {"telnyx-timestamp": "1",
                                                      "telnyx-signature-ed25519": "AA=="})


# ---- dial ----------------------------------------------------------------------------------
def test_dial_request_shape(sb):
    iid, _ = sb.dial("c1")
    path, body = sb.tx.posts[0]
    secret = body["webhook_url"].rsplit("/", 1)[1]
    assert path == "/calls" and len(secret) == 43
    assert body == {"connection_id": "conn-1", "to": TEST_NUMBER, "from": "+17175550100",
                    "webhook_url": f"{BASE}/v1/telnyx/calls/{iid}/{secret}",
                    "webhook_url_method": "POST", "timeout_secs": 30, "time_limit_secs": 360,
                    "answering_machine_detection": "premium"}
    row = sb.app.state.ledger.by_internal(iid)
    assert secret not in json.dumps(row)  # only its hash is stored
    assert sb.store.get_call(iid)["provider_sid"] == "v3:cc1"


# ---- answering machine detection ------------------------------------------------------------
def test_stream_starts_only_after_human_result(sb):
    iid, _ = sb.dial("c1")
    sb.event(iid, "call.answered")
    assert not sb.tx.actions("streaming_start")
    sb.event(iid, "call.machine.premium.detection.ended", result="human_business")
    (path, body), = sb.tx.actions("streaming_start")
    assert path == "/calls/v3%3Acc1/actions/streaming_start"
    url = urlsplit(body["stream_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"wss://voice.example.test/v1/media/{iid}"
    assert {k: body[k] for k in body if k != "stream_url"} == {
        "stream_track": "inbound_track", "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "PCMU"}
    sb.event(iid, "call.machine.premium.detection.ended", result="human_business")
    assert len(sb.tx.actions("streaming_start")) == 1  # duplicate webhook


def test_machine_without_message_hangs_up(sb):
    iid, _ = sb.dial("c1")
    sb.event(iid, "call.answered")
    sb.event(iid, "call.machine.premium.detection.ended", result="machine")
    assert not sb.tx.actions("streaming_start")
    assert len(sb.tx.actions("hangup")) == 1
    call = sb.store.get_call(iid)
    assert call["state"] == states.VOICEMAIL and call["result"]["outcome"] == "voicemail"


def test_machine_with_message_streams_after_greeting(sb):
    iid, _ = sb.dial("c1", leave_voicemail=True)
    sb.event(iid, "call.answered")
    sb.event(iid, "call.machine.premium.detection.ended", result="machine")
    assert not sb.tx.actions("hangup") and not sb.tx.actions("streaming_start")
    sb.event(iid, "call.machine.premium.greeting.ended", result="beep_detected")
    assert len(sb.tx.actions("streaming_start")) == 1
    cc = sb.store.get_call(iid)["provider_sid"]
    with sb.client.websocket_connect(f"/v1/media/{iid}?token={sb.stream_token()}") as ws:
        for f in start_frames(cc):
            ws.send_text(f)
        wait_for(lambda: sb.rt.of("response.create"))
        instr = sb.rt.of("response.create")[0]["response"]["instructions"]
        assert "Hi, this is an AI assistant calling on behalf of Acme Plumbing." in instr
        assert "answering machine" in instr
        ws.send_text(json.dumps({"event": "stop"}))


# ---- media -------------------------------------------------------------------------------
def test_media_frames_map_both_ways(sb):
    iid, _ = sb.dial("c1")
    answered_human(sb, iid)
    cc = sb.store.get_call(iid)["provider_sid"]
    with sb.client.websocket_connect(f"/v1/media/{iid}?token={sb.stream_token()}") as ws:
        for f in start_frames(cc):
            ws.send_text(f)
        ws.send_text(json.dumps({"event": "media", "sequence_number": "3", "stream_id": "st-1",
                                 "media": {"track": "inbound", "chunk": "1", "timestamp": "20",
                                           "payload": MULAW_20MS}}))
        wait_for(lambda: sb.rt.of("input_audio_buffer.append"))
        assert sb.rt.of("input_audio_buffer.append")[0]["audio"] == MULAW_20MS
        out = base64.b64encode(b"\xff" * 320).decode()
        sb.rt.push({"type": "response.output_audio.delta", "item_id": "it1", "delta": out})
        assert json.loads(ws.receive_text()) == {"event": "media", "media": {"payload": out}}
        assert json.loads(ws.receive_text())["event"] == "mark"
        sb.rt.push({"type": "input_audio_buffer.speech_started"})  # barge-in
        assert json.loads(ws.receive_text()) == {"event": "clear"}
        ws.send_text(json.dumps({"event": "stop", "stream_id": "st-1"}))
    wait_for(lambda: sb.store.get_call(iid)["state"] == states.COMPLETED)
    sess = sb.rt.of("session.update")[0]["session"]
    assert sess["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert "on behalf of Acme Plumbing" in json.dumps(sb.rt.sent)


def test_one_time_media_token_reuse_refused(sb):
    iid, _ = sb.dial("c1")
    answered_human(sb, iid)
    cc, token = sb.store.get_call(iid)["provider_sid"], sb.stream_token()
    with sb.client.websocket_connect(f"/v1/media/{iid}?token={token}") as ws:
        for f in start_frames(cc):
            ws.send_text(f)
        wait_for(lambda: sb.rt.connect_calls == 1)
        with sb.client.websocket_connect(f"/v1/media/{iid}?token={token}") as ws2:
            for f in start_frames(cc):
                ws2.send_text(f)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws2.receive_text()
            assert exc.value.code == 1008
        ws.send_text(json.dumps({"event": "stop"}))
    assert sb.rt.connect_calls == 1
    kinds = [e["kind"] for e in sb.store.events(iid)]
    assert "media_refused" in kinds


# ---- usage, updates, answers, hangup ---------------------------------------------------------
def test_usage_math(sb):
    iid, _ = sb.dial("c1")
    assert sb.get("/v1/calls/c1/updates").json()["call"]["usage"] == {
        "seconds": 0, "telephony_usd": 0.0, "voice_usd": 0.0}
    sb.event(iid, "call.answered")
    sb.app.state.ledger.stamp(iid, "media_started_at", sb.clock() + 10)
    sb.clock.advance(90)
    sb.end(iid)
    call = sb.get("/v1/calls/c1/updates").json()["call"]
    assert call["usage"] == {"seconds": 90, "telephony_usd": 0.018, "voice_usd": 0.08}
    assert call["state"] == states.COMPLETED and call["duration_s"] == 90
    sb.clock.advance(600)  # usage is frozen once the call ended
    assert sb.get("/v1/calls/c1/updates").json()["call"]["usage"]["seconds"] == 90


def test_updates_cursor_questions_and_answers(sb):
    iid, _ = sb.dial("c1")
    sb.store.add_question("q1", iid, "Name for the booking?", "name", "Winch")
    sb.store.add_transcript_line(iid, "agent", "Hi there")
    sb.clock.advance(1)  # a change in the very instant of a poll is sent twice, never lost
    first = sb.get("/v1/calls/c1/updates").json()
    assert [q["id"] for q in first["questions"]] == ["q1"] and first["transcript"]
    sb.clock.advance(1)
    second = sb.get("/v1/calls/c1/updates?cursor=" + first["cursor"]).json()
    assert second["events"] == [] and second["transcript"] == [] and second["questions"] == []
    assert sb.post("/v1/calls/c1/questions/q1/answer",
                   {"status": "answered", "answer": "Winch"}).json() == {"ok": True}
    assert sb.post("/v1/calls/c1/questions/q1/answer", {"status": "declined"}).status_code == 409
    third = sb.get("/v1/calls/c1/updates?cursor=" + second["cursor"]).json()
    assert third["questions"][0]["status"] == "answered"


def test_consent_answer_grants_consent_on_the_bridge(sb):
    iid, _ = sb.dial("c1")
    sb.store.add_question("q2", iid, "Card number?", "card", "", kind="consent",
                          consent_field="card")
    sb.post("/v1/calls/c1/questions/q2/answer", {"status": "answered", "answer": "4111"})
    assert "card" in sb.store.consents(iid)


def test_transcripts_withheld_when_option_off(sb):
    body = sb.body("c1")
    body["options"]["store_transcripts"] = False
    iid = sb.put("c1", body=body).json()["bridge_call_id"]
    sb.store.add_transcript_line(iid, "agent", "Hi")
    assert sb.get("/v1/calls/c1/updates").json()["transcript"] == []


def test_hangup_before_answer(sb):
    iid, _ = sb.dial("c1")
    assert sb.post("/v1/calls/c1/hangup", {}).status_code == 200
    assert sb.store.get_call(iid)["state"] == states.ABORTED
    assert len(sb.tx.actions("hangup")) == 1
    assert sb.post("/v1/calls/c1/hangup", {}).status_code == 409


def test_inbound_callback_says_line_and_hangs_up(sb):
    path = "/v1/telnyx/inbound"
    assert sb.signed(path, "call.initiated", call_control_id="in1",
                     direction="incoming").status_code == 200
    sb.signed(path, "call.answered", call_control_id="in1")
    sb.signed(path, "call.speak.ended", call_control_id="in1")
    assert [p.rsplit("/", 1)[1] for p, _ in sb.tx.posts] == ["answer", "speak", "hangup"]
    assert sb.tx.actions("speak")[0][1]["payload"].startswith("Thanks for calling back")
    assert sb.signed(path, "call.initiated", stamp=1, call_control_id="in2").status_code == 403
    assert len(sb.tx.posts) == 3


# ---- white-label ---------------------------------------------------------------------------
def _result_strings(path: Path) -> list[str]:
    """String constants passed to _finish / set_result_once (they become customer text)."""
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in (
                "_finish", "set_result_once"):
            out += [n.value for a in node.args for n in ast.walk(a)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return out


def test_no_vendor_names_in_spoken_or_customer_text(sb):
    payload = {"callee_name": "Bella", "user_name": "Winch", "goal": "Book",
               "opening_line": v1.disclosure("Acme Plumbing", True), "facts": {},
               "record": True, "leave_voicemail": True}
    spoken = [rt.build_instructions(payload), json.dumps(rt.TOOLS), telnyx_io.INBOUND_LINE,
              payload["opening_line"],
              *(getattr(rt, n) for n in dir(rt) if n.endswith(("_LINE", "_INSTRUCTIONS")))]
    customer = [m for _, m in v1.ERRORS.values()] + list(status.REASONS.values())
    customer += _result_strings(ROOT / "session.py") + _result_strings(ROOT / "v1.py")
    # and what the API actually returned across a full call and a failed dial
    iid, _ = sb.dial("c1")
    answered_human(sb, iid)
    sb.end(iid)
    customer.append(sb.get("/v1/calls/c1/updates").text)
    sb.tx.fail_dial = True
    sb.clock.advance(120)
    customer += [sb.put("c2").text, sb.get("/v1/calls/c2/updates").text]
    hits = [t for t in spoken + customer if VENDORS.search(t)]
    assert not hits, hits
    assert v1.disclosure("Acme Plumbing") == (
        "Hi, this is an AI assistant calling on behalf of Acme Plumbing.")
