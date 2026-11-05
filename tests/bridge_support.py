"""Fakes for bridge tests: scripted Realtime connection, fake Twilio REST, signing, waiting."""
from __future__ import annotations

import base64
import json
import queue
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from conftest import TEST_NUMBER, approve, prepare
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from hermes_phone import tools
from hermes_phone.bridge import BridgeSettings, create_app

BASE = "https://bridge.example.test"
AUTH = "test-auth-token"
MULAW_20MS = base64.b64encode(bytes([0xFF, 0x7F, 0x00, 0x80] * 40)).decode()  # 160 bytes = 20 ms


def wait_for(pred, timeout: float = 5.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


class FakeRealtime:
    """Records every client event; the test pushes server events."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.inbox: queue.Queue = queue.Queue()
        self.closed = False
        self.connect_calls = 0
        self.reactor = None  # optional fn(event) -> list[dict] of server events

    async def connect(self, settings, call_id):
        self.connect_calls += 1
        return self

    async def send(self, event: dict) -> None:
        self.sent.append(event)
        if self.reactor:
            for e in self.reactor(event) or []:
                self.inbox.put(e)

    async def recv(self):
        import asyncio

        while True:
            try:
                return self.inbox.get_nowait()
            except queue.Empty:
                if self.closed:
                    return None
                await asyncio.sleep(0.005)

    async def close(self) -> None:
        self.closed = True

    def push(self, event: dict | None) -> None:
        self.inbox.put(event) if event is not None else setattr(self, "closed", True)

    def of(self, etype: str) -> list[dict]:
        return [e for e in self.sent if e.get("type") == etype]


class _CallCtx:
    def __init__(self, owner, sid):
        self.owner, self.sid = owner, sid

    def update(self, **kw):
        self.owner.updates.append({"sid": self.sid, **kw})
        return SimpleNamespace(sid=self.sid)


class FakeTwilio:
    """Mimics ``Client.calls`` (callable for update, ``.create`` for dialing)."""

    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.created: list[dict] = []
        self.calls = self

    def __call__(self, sid):
        return _CallCtx(self, sid)

    def create(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(sid="CA" + "1" * 32)


def sign(path: str, params: dict[str, str], token: str = AUTH) -> str:
    return RequestValidator(token).compute_signature(BASE + path, params)


def post_signed(client: TestClient, path: str, params: dict[str, str], token: str = AUTH,
                signature: str | None = None, headers: dict | None = None):
    h = {"Content-Type": "application/x-www-form-urlencoded"}
    sig = signature if signature is not None else sign(path, params, token)
    if sig:
        h["X-Twilio-Signature"] = sig
    h.update(headers or {})
    return client.post(path, content=urlencode(params), headers=h)


def make_settings(**kw) -> BridgeSettings:
    base = dict(public_base_url=BASE, twilio_auth_token=AUTH, twilio_account_sid="ACtest",
                twilio_from_number="+15555550100", openai_api_key="sk-test", tick_s=0.01,
                token_ttl_s=60.0, start_timeout_s=3.0,
                environ={"HERMES_PHONE_TEST_ALLOWLIST": TEST_NUMBER})
    base.update(kw)
    return BridgeSettings(**base)


class Harness:
    def __init__(self, store, clock, **settings_kw) -> None:
        self.store, self.clock = store, clock
        self.rt, self.tw = FakeRealtime(), FakeTwilio()
        self.settings = make_settings(**settings_kw)
        self.app = create_app(self.settings, store=store, realtime_connect=self.rt.connect,
                              twilio_client=self.tw, clock=clock)
        self.client = TestClient(self.app)

    def dialing_call(self, consent: tuple[str, ...] = (), **prep) -> str:
        """prepare -> approve -> phone_call with the NullDialer: a call in ``dialing``."""
        self.clock.advance(120)  # clear the 60 s same-number gap between calls in one test
        cid = prepare(prep or None)["call_id"]
        from hermes_phone import commands

        for field in consent:
            assert "recorded" in commands.phone_command(f"consent {cid} {field}", user_id="u1")
        approve(None, cid)
        res = json.loads(tools.phone_call({"call_id": cid}))
        assert res.get("state") == "dialing", res
        return cid

    def token(self, cid: str) -> str:
        r = post_signed(self.client, f"/twiml/{cid}", {"CallSid": "CA" + "1" * 32})
        assert r.status_code == 200, r.text
        import re

        return re.search(r'name="token" value="([^"]+)"', r.text).group(1)

    @staticmethod
    def start_msg(cid: str, token: str, sid: str = "CA" + "1" * 32) -> list[str]:
        return [json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}),
                json.dumps({"event": "start", "sequenceNumber": "1", "streamSid": "MZ1",
                            "start": {"accountSid": "ACtest", "streamSid": "MZ1",
                                      "callSid": sid, "tracks": ["inbound"],
                                      "mediaFormat": {"encoding": "audio/x-mulaw",
                                                      "sampleRate": 8000, "channels": 1},
                                      "customParameters": {"token": token}}})]

    @staticmethod
    def media(ts: int, payload: str = MULAW_20MS) -> str:
        return json.dumps({"event": "media", "streamSid": "MZ1",
                           "media": {"track": "inbound", "chunk": str(ts // 20),
                                     "timestamp": str(ts), "payload": payload}})
