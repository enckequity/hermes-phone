"""Web phone mode: the callee is a browser page. No network, fake Realtime, fake clock."""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest
from bridge_support import BASE, MULAW_20MS, Harness, wait_for
from conftest import TEST_NUMBER, approve, prepare
from starlette.websockets import WebSocketDisconnect

from hermes_phone import commands, mode, states, tools, webphone
from hermes_phone.bridge import webpage
from hermes_phone.dialer import TwilioDialer, WebPhoneDialer

OUTBOUND = base64.b64encode(bytes(range(256)) * 4).decode()
TWILIO = {"TWILIO_ACCOUNT_SID": "ACx", "TWILIO_AUTH_TOKEN": "t", "TWILIO_FROM_NUMBER": "+1",
          "PUBLIC_BASE_URL": BASE}
WEB_BASE = {"OPENAI_API_KEY": "sk-x", "PUBLIC_BASE_URL": BASE}


# ---- selection and /phone check ----
@pytest.mark.parametrize("env,name,how", [
    ({**TWILIO, "OPENAI_API_KEY": "k"}, "twilio", "default"),
    ({**TWILIO, "OPENAI_API_KEY": "k", "HERMES_PHONE_MODE": "twilio"}, "twilio", "explicit"),
    ({**TWILIO, "OPENAI_API_KEY": "k", "HERMES_PHONE_MODE": "web"}, "web", "explicit"),
    ({**WEB_BASE, "HERMES_PHONE_MODE": "web"}, "web", "explicit"),
    (dict(WEB_BASE), "web", "auto"),  # Twilio incomplete, OpenAI + base url present
    ({"OPENAI_API_KEY": "k"}, "twilio", "default"),  # nothing usable: stays twilio (missing)
])
def test_mode_selection(env, name, how):
    chosen = mode.resolve(env)
    assert (chosen.name, chosen.how) == (name, how)


def test_explicit_twilio_never_falls_back_to_web():
    chosen = mode.resolve({**WEB_BASE, "HERMES_PHONE_MODE": "twilio"})
    assert chosen.name == "twilio" and "TWILIO_ACCOUNT_SID" in chosen.missing


def test_explicit_web_reports_missing_names():
    chosen = mode.resolve({"HERMES_PHONE_MODE": "web", "PUBLIC_BASE_URL": BASE})
    assert chosen.name == "web" and chosen.missing == ["OPENAI_API_KEY"]


def test_service_picks_the_dialer_from_env(store):
    web = tools.PhoneService(store=store, environ=dict(WEB_BASE))
    assert isinstance(web.dialer, WebPhoneDialer) and web.mode == "web"
    tw = tools.PhoneService(store=store, environ={**TWILIO, "OPENAI_API_KEY": "k"})
    assert isinstance(tw.dialer, TwilioDialer) and not isinstance(tw.dialer, WebPhoneDialer)
    off = tools.PhoneService(store=store, environ={"HERMES_PHONE_MODE": "web"})
    assert off.dialer.configured is False and "OPENAI_API_KEY" in off.dialer.missing


def test_check_shows_the_active_mode():
    tools.configure(environ={**WEB_BASE, "HERMES_PHONE_MODE": "web"})
    out = commands.phone_command("check")
    assert "- mode: web (set by HERMES_PHONE_MODE)" in out
    assert "TWILIO_ACCOUNT_SID" not in out and "OPENAI_API_KEY: set" in out
    tools.configure(environ=dict(WEB_BASE))
    assert "- mode: web (automatic" in commands.phone_command("check")
    tools.configure(environ={**TWILIO, "OPENAI_API_KEY": "k"})
    out = commands.phone_command("check")
    assert "- mode: twilio (default)" in out and "TWILIO_AUTH_TOKEN: set" in out


def test_bridge_settings_need_no_twilio_in_web_mode():
    from hermes_phone.bridge import BridgeSettings

    s = BridgeSettings.from_env({**WEB_BASE, "HERMES_PHONE_MODE": "web"})
    assert s.missing_env() == []
    assert "TWILIO_ACCOUNT_SID" in BridgeSettings.from_env(
        {"OPENAI_API_KEY": "k", "PUBLIC_BASE_URL": BASE, "HERMES_PHONE_MODE": "twilio"}
    ).missing_env()


# ---- fixtures ----
@pytest.fixture
def web(store, clock, ctx):
    env = {"HERMES_PHONE_TEST_ALLOWLIST": TEST_NUMBER, "HERMES_PHONE_MODE": "web", **WEB_BASE}
    dialer = WebPhoneDialer(env, store=store, http_get=lambda url, t: 200)
    service = tools.configure(ctx=ctx, store=store, dialer=dialer, environ=env)
    h = Harness(store, clock)
    h.svc = service
    yield h
    for watcher in list(service.watchers.values()):
        watcher.stop.set()
    tools._service = None


def link_of(h, cid) -> tuple[str, str]:
    url = h.svc.dialer.links[cid]
    parsed = urlparse(url)
    assert url == f"{BASE}/phone/{cid}?t={parse_qs(parsed.query)['t'][0]}"
    return parsed.path, parse_qs(parsed.query)["t"][0]


def answer(h, cid, token):
    return h.client.post(f"/phone/{cid}/answer", json={"t": token})


def start_ws(h, cid, info):
    sid = "MWtest"
    return [json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}),
            json.dumps({"event": "start", "sequenceNumber": "1", "streamSid": sid, "start": {
                "streamSid": sid, "callSid": info["call_sid"], "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {"token": info["media_token"]}}})]


def media(ts, payload=MULAW_20MS):
    return json.dumps({"event": "media", "streamSid": "MWtest", "media": {
        "track": "inbound", "chunk": str(ts // 20), "timestamp": str(ts), "payload": payload}})


def connect(h, cid):
    """Answer in the 'page' and open the media socket exactly as the page JS does."""
    _, token = link_of(h, cid)
    info = answer(h, cid, token).json()
    cm = h.client.websocket_connect(f"/media/{cid}")
    return info, cm


def drain(ws):
    out = []
    while True:
        try:
            out.append(json.loads(ws.receive_text()))
        except WebSocketDisconnect:
            return out


# ---- approve posts the link ----
def test_approve_in_web_mode_dials_nobody_and_posts_the_link(web, ctx):
    cid = prepare()["call_id"]
    out = commands.phone_command(f"approve {cid} confirm", user_id="u1", session_id="s1")
    assert out == ""  # the live status message is the only reply
    call = web.store.get_call(cid)
    assert call["state"] == states.DIALING and call["provider_sid"] == f"web-{cid}"
    path, token = link_of(web, cid)
    wait_for(lambda: any(token in m.text for m in ctx.messages))
    msg = next(m.text for m in ctx.messages if token in m.text)
    assert "tap Answer" in msg and f"{BASE}/phone/{cid}?t={token}" in msg.split("\n")[1]
    assert web.svc.answer_links == {} or cid not in web.svc.answer_links
    # only the hash is at rest
    rows = web.store.conn.execute("SELECT token_hash FROM web_tokens").fetchall()
    assert rows and all(r[0] != token and len(r[0]) == 64 for r in rows)


def test_phone_call_tool_returns_the_link_for_the_model(web):
    cid = prepare()["call_id"]
    approve(None, cid)
    res = json.loads(tools.phone_call({"call_id": cid}))
    assert res["mode"] == "web" and res["answer_url"].startswith(f"{BASE}/phone/{cid}?t=")


def test_unapproved_call_is_refused_and_creates_no_token(web):
    cid = prepare()["call_id"]
    res = json.loads(tools.phone_call({"call_id": cid}))
    assert "Not approved" in res["error"]
    assert web.store.conn.execute("SELECT COUNT(*) FROM web_tokens").fetchone()[0] == 0
    assert web.store.get_call(cid)["state"] == states.AWAITING_APPROVAL


def test_safety_checks_still_apply_in_web_mode(web):
    res = prepare({"number": "+12125550188"})  # not on the allowlist
    assert "error" in res and res.get("blocked")


# ---- page and answer ----
def test_page_requires_a_valid_token_and_sets_security_headers(web):
    cid = web.dialing_call()
    _, token = link_of(web, cid)
    ok = web.client.get(f"/phone/{cid}", params={"t": token})
    assert ok.status_code == 200 and "Answer" in ok.text
    assert ok.headers["cache-control"] == "no-store"
    assert ok.headers["referrer-policy"] == "no-referrer"
    csp = ok.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in ok.text and "wss://bridge.example.test" in csp
    assert "default-src 'none'" in csp and "unsafe-inline" not in csp
    # viewing alone does not consume the token or change state
    assert web.store.get_call(cid)["state"] == states.DIALING
    bad = [web.client.get(f"/phone/{cid}", params={"t": "wrong"}),
           web.client.get(f"/phone/{cid}"),
           web.client.get("/phone/cdoesnotexist", params={"t": token}),
           web.client.get(f"/phone/{cid}", params={"t": token + "x"})]
    assert {r.status_code for r in bad} == {404}
    assert len({r.text for r in bad}) == 1  # identical generic page: no existence oracle
    assert web.store.get_call(cid)["state"] == states.DIALING


def test_expired_token_gives_error_page_and_no_state_change(web, clock):
    cid = web.dialing_call()
    _, token = link_of(web, cid)
    web.store.conn.execute("UPDATE web_tokens SET expires_at=?", (clock() - 1,))
    web.store.conn.commit()
    assert web.client.get(f"/phone/{cid}", params={"t": token}).status_code == 404
    assert answer(web, cid, token).status_code == 404
    assert web.store.get_call(cid)["state"] == states.DIALING


def test_twilio_calls_and_non_dialing_calls_have_no_web_page(harness):
    cid = harness.dialing_call()  # NullDialer: no web- sid
    assert harness.client.get(f"/phone/{cid}", params={"t": "x"}).status_code == 404


def test_answer_consumes_token_mints_media_token_and_goes_in_progress(web):
    cid = web.dialing_call()
    _, token = link_of(web, cid)
    r = answer(web, cid, token)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    info = r.json()
    assert info["call_sid"] == f"web-{cid}" and len(info["media_token"]) >= 24
    assert web.store.get_call(cid)["state"] == states.IN_PROGRESS
    assert answer(web, cid, token).status_code == 404  # single use
    assert web.client.get(f"/phone/{cid}", params={"t": token}).status_code == 404  # reload
    assert web.store.get_call(cid)["state"] == states.IN_PROGRESS


def test_answer_with_wrong_token_changes_nothing(web):
    cid = web.dialing_call()
    assert answer(web, cid, "nope").status_code == 404
    assert web.client.post(f"/phone/{cid}/answer", content=b"not json").status_code == 404
    assert web.store.get_call(cid)["state"] == states.DIALING
    _, token = link_of(web, cid)
    assert answer(web, cid, token).status_code == 200  # the real token still works


def test_routes_are_rate_limited(web):
    cid = web.dialing_call()
    codes = [web.client.get(f"/phone/{cid}", params={"t": "x"}).status_code for _ in range(40)]
    assert 429 in codes and codes[0] == 404


# ---- served page ----
def test_served_page_speaks_the_media_protocol_and_has_no_external_urls(web):
    cid = web.dialing_call()
    _, token = link_of(web, cid)
    html = web.client.get(f"/phone/{cid}", params={"t": token}).text
    for needle in ("'connected'", "'start'", "'media'", "'mark'", "'stop'", "'clear'",
                   "audio/x-mulaw", "customParameters", "getUserMedia", "AudioContext",
                   "/answer", "Answer", "Hang up"):
        assert needle in html, needle
    assert "http://" not in html and "https://" not in html and "src=" not in html
    assert "<link" not in html and "@import" not in html


def test_callee_name_is_escaped_in_the_page():
    html = webpage.render_page(call_id="c1", callee="<script>x</script>", nonce="n")
    assert "<script>x</script>" not in html and "&lt;script&gt;" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_codec_round_trips_under_node(tmp_path):
    script = webpage.CODEC_JS + r"""
const assert = require('assert');
// mu-law: every code decodes then re-encodes to itself (except the duplicate negative zero)
for (let u = 0; u < 256; u++) {
  if (u === 0x7f) continue;
  assert.strictEqual(CODEC.lin2ulaw(CODEC.ulaw2lin(u)), u, 'code ' + u);
}
assert.strictEqual(CODEC.lin2ulaw(0), 0xff);                // G.711 silence
assert.strictEqual(CODEC.lin2ulaw(32767), 0x80);
assert.strictEqual(CODEC.lin2ulaw(-32768), 0x00);
// 48 kHz sine -> 8 kHz -> 160 byte frames
const d = new CODEC.Downsampler(48000), frames = [];
const f = new CODEC.Framer((b) => frames.push(b));
const x = new Float32Array(48000);
for (let i = 0; i < x.length; i++) x[i] = 0.5 * Math.sin(2 * Math.PI * 440 * i / 48000);
f.push(d.push(x));
assert.strictEqual(frames.length, 50);                       // 1 s = 50 x 20 ms
assert.ok(frames.every((b) => b.length === 160));
const back = CODEC.decode(frames[10]);
let peak = 0; for (const v of back) peak = Math.max(peak, Math.abs(v));
assert.ok(peak > 0.4 && peak < 0.6, 'peak ' + peak);
// 44.1 kHz works too (non-integer ratio)
const d2 = new CODEC.Downsampler(44100); const n2 = d2.push(new Float32Array(44100)).length;
assert.ok(Math.abs(n2 - 8000) <= 1, 'n2 ' + n2);
// base64 + upsample
const bytes = new Uint8Array([1, 2, 250, 255]);
assert.deepStrictEqual(Array.from(CODEC.b64ToBytes(CODEC.bytesToB64(bytes))), [1, 2, 250, 255]);
assert.strictEqual(CODEC.upsample(new Float32Array(160), 48000).length, 960);
console.log('ok');
"""
    path = tmp_path / "codec.js"
    path.write_text(script)
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


# ---- full media round trip ----
def test_round_trip_disclosure_audio_barge_in_and_end_call(web):
    cid = web.dialing_call()
    line = web.store.get_call(cid)["brief"]["opening_line"]
    info, cm = connect(web, cid)
    with cm as ws:
        for m in start_ws(web, cid, info):
            ws.send_text(m)
        wait_for(lambda: web.rt.of("session.update"))
        assert not web.rt.of("response.create")
        web.rt.push({"type": "input_audio_buffer.speech_stopped"})
        wait_for(lambda: web.rt.of("response.create"))
        first = web.rt.of("response.create")[0]["response"]["instructions"]
        assert "an AI assistant calling on behalf of" in first and line in first
        for ts in (0, 20, 40):
            ws.send_text(media(ts))
        wait_for(lambda: len(web.rt.of("input_audio_buffer.append")) == 3)
        assert web.rt.of("input_audio_buffer.append")[0]["audio"] == MULAW_20MS
        web.rt.push({"type": "response.created", "response": {"id": "r1"}})
        web.rt.push({"type": "response.output_audio.delta", "item_id": "i1", "delta": OUTBOUND})
        got = json.loads(ws.receive_text())
        mark = json.loads(ws.receive_text())
        assert got["event"] == "media" and got["media"]["payload"] == OUTBOUND
        assert mark["event"] == "mark"
        ws.send_text(json.dumps({"event": "mark", "streamSid": "MWtest",
                                 "mark": {"name": mark["mark"]["name"]}}))
        # the opening is done: barge-in is allowed from here on (not during the opening)
        web.rt.push({"type": "response.done", "response": {"id": "r1"}})
        wait_for(lambda: len(web.rt.of("session.update")) == 2)
        # barge-in
        web.rt.push({"type": "response.output_audio.delta", "item_id": "i2", "delta": OUTBOUND})
        ws.receive_text(), ws.receive_text()
        web.rt.push({"type": "input_audio_buffer.speech_started"})
        assert json.loads(ws.receive_text())["event"] == "clear"
        # live transcript line for the page
        web.rt.push({"type": "response.output_audio_transcript.done", "transcript": "Hello!"})
        assert json.loads(ws.receive_text()) == {"event": "transcript", "role": "agent",
                                                 "text": "Hello!"}
        # end_call closes the socket and records one result
        web.rt.push({"type": "response.function_call_arguments.done", "name": "end_call",
                     "call_id": "f9", "arguments": json.dumps({"outcome": "success"})})
        events = drain(ws)
    assert events[-1]["event"] == "ended"
    call = web.store.get_call(cid)
    assert call["state"] == states.COMPLETED and call["outcome"] == "success"
    assert call["duration_s"] is not None and call["reason"] == "end_call"
    assert web.tw.updates == []  # no Twilio REST in web mode
    assert len([e for e in web.store.events(cid) if e["kind"] == "bridge_finish"]) == 1


def test_browser_stop_finalises_once(web):
    cid = web.dialing_call()
    info, cm = connect(web, cid)
    with cm as ws:
        for m in start_ws(web, cid, info):
            ws.send_text(m)
        wait_for(lambda: web.rt.of("session.update"))
        ws.send_text(json.dumps({"event": "stop", "streamSid": "MWtest"}))
        wait_for(lambda: web.store.get_call(cid)["state"] == states.COMPLETED)
    assert web.store.get_call(cid)["reason"] == "callee_hung_up"


def test_phone_hangup_command_closes_the_browser_socket(web):
    cid = web.dialing_call()
    info, cm = connect(web, cid)
    with cm as ws:
        for m in start_ws(web, cid, info):
            ws.send_text(m)
        wait_for(lambda: web.rt.of("session.update"))
        out = commands.phone_command(f"hangup {cid}", user_id="u1")
        assert "ended" in out
        events = drain(ws)
    assert events[-1]["event"] == "ended"
    call = web.store.get_call(cid)
    assert call["state"] == states.ABORTED and call["reason"] == "user_hangup"
    assert web.tw.updates == []


def test_max_duration_closes_the_browser_socket(web, clock):
    cid = web.dialing_call(max_duration_s=60)
    info, cm = connect(web, cid)
    with cm as ws:
        for m in start_ws(web, cid, info):
            ws.send_text(m)
        wait_for(lambda: web.rt.of("session.update"))
        clock.advance(61)
        events = drain(ws)
    assert events[-1]["event"] == "ended"
    assert web.store.get_call(cid)["reason"] == "max_duration"


def test_media_socket_with_a_wrong_token_is_refused(web):
    cid = web.dialing_call()
    info, cm = connect(web, cid)
    info["media_token"] = "forged"
    with cm as ws:
        for m in start_ws(web, cid, info):
            ws.send_text(m)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert not web.rt.connect_calls


# ---- nobody answers ----
def test_no_answer_after_60s_gives_one_result_and_no_redial(web, clock, ctx):
    cid = web.dialing_call()
    _, token = link_of(web, cid)
    clock.advance(webphone.RING_TIMEOUT_S - 5)
    assert web.client.get(f"/phone/{cid}", params={"t": token}).status_code == 200
    clock.advance(10)
    assert web.client.get(f"/phone/{cid}", params={"t": token}).status_code == 404
    call = web.store.get_call(cid)
    assert call["state"] == states.NO_ANSWER and call["outcome"] == "no_answer"
    assert call["result"]["reason"] == "no_answer"
    assert answer(web, cid, token).status_code == 404
    assert webphone.expire_stale(web.store, cid) is False  # exactly once
    assert len(web.svc.dialer.links) == 1  # no redial, no second link


def test_watcher_expires_an_unopened_link(web, clock, ctx):
    cid = web.dialing_call()
    clock.advance(webphone.RING_TIMEOUT_S + 1)
    wait_for(lambda: web.store.get_call(cid)["state"] == states.NO_ANSWER)
    wait_for(lambda: any("finished" in e for m in ctx.messages for e in m.edits + [m.text]))
    assert web.store.get_call(cid)["result"]["reason"] == "no_answer"


def test_page_that_answered_but_never_connected_fails_the_call(web, clock):
    cid = web.dialing_call()
    _, token = link_of(web, cid)
    assert answer(web, cid, token).status_code == 200
    clock.advance(webphone.CONNECT_TIMEOUT_S + 1)
    assert webphone.expire_stale(web.store, cid) is True
    assert web.store.get_call(cid)["state"] == states.FAILED
    assert webphone.expire_stale(web.store, cid) is False
