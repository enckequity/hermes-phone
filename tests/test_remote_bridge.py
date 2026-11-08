"""Remote bridge client (docs/BRIDGE_API.md) against a fake bridge on 127.0.0.1 (stdlib
http.server in a thread). No real providers, no network beyond loopback."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from bridge_support import wait_for
from conftest import TEST_NUMBER, approve, prepare

from hermes_phone import cli, relay, remote, states, tools

TOKEN = "ws-token-123"


class FakeBridge:
    """Minimal bridge: auth, one-use approval, idempotent PUT, scripted update pages."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.calls: dict[str, dict] = {}
        self.pages: list[dict] = []  # served in order by GET .../updates
        self.fail: dict[str, tuple[int, dict]] = {}  # "PUT" / "updates" -> forced response
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                status, data = bridge.handle(self.command, self.path, self.headers, body)
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_PUT = do_POST = _go

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def of(self, method: str, suffix: str = "") -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"].endswith(suffix)]

    def handle(self, method, path, headers, body):
        url = urlsplit(path)
        self.requests.append({"method": method, "path": url.path, "query": parse_qs(url.query),
                              "auth": headers.get("Authorization"),
                              "ws": headers.get("X-TeamShift-Workspace"), "body": body})
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"error": {"code": "unauthorized", "message": "bad credential"}}
        parts = url.path.split("/")[2:]  # after /v1
        if parts == ["health"]:
            return 200, {"ok": True}
        cid = parts[1]
        if method == "PUT":
            if "PUT" in self.fail:
                return self.fail["PUT"]
            appr = (body or {}).get("approval") or {}
            if not appr.get("id") or not appr.get("consumed_at"):
                return 403, {"error": {"code": "approval_required", "message": "no approval"}}
            if cid in self.calls:
                if self.calls[cid]["approval_id"] != appr["id"]:
                    return 409, {"error": {"code": "approval_mismatch", "message": "conflict"}}
                return 200, self.calls[cid]["resp"]
            resp = {"call_id": cid, "state": "dialing", "bridge_call_id": "b-" + cid}
            self.calls[cid] = {"approval_id": appr["id"], "resp": resp}
            return 201, resp
        if parts[-1] == "updates":
            if "updates" in self.fail:
                return self.fail["updates"]
            if self.pages:
                return 200, self.pages.pop(0)
            return 200, {"cursor": (parse_qs(url.query).get("cursor") or [""])[0]}
        return 200, {"ok": True}  # answer, hangup


@pytest.fixture
def bridge():
    b = FakeBridge()
    yield b
    b.close()


@pytest.fixture
def rsvc(store, ctx, bridge):
    env = {"HERMES_PHONE_TEST_ALLOWLIST": TEST_NUMBER, "HERMES_PHONE_BRIDGE_URL": bridge.url,
           "HERMES_PHONE_BRIDGE_TOKEN": TOKEN}
    service = tools.configure(ctx=ctx, store=store, environ=env)
    service.watch_poll_s = 0.02
    assert service.dialer.poll_s == 1.0  # remote default
    service.dialer.poll_s = 0.02
    yield service
    for w in list(service.watchers.values()):
        w.stop.set()
    tools._service = None


def dialed(svc, facts=None) -> dict:
    """A call that went through prepare, approval and the approval spend, without a watcher."""
    cid = prepare({"facts_to_share": facts} if facts else None)["call_id"]
    approve(svc, cid)
    assert svc.store.consume_approval(cid)
    states.transition(svc.store, cid, states.DIALING, dialed_at=svc.store.clock())
    return svc.store.get_call(cid)


def payload(svc, call) -> dict:
    from hermes_phone import brief

    return brief.voice_payload(call, svc.store.consents(call["id"]))


# ---- mode switch --------------------------------------------------------------------------
def test_bridge_url_selects_remote_and_missing_token_is_named(rsvc, bridge):
    assert isinstance(rsvc.dialer, remote.RemoteBridge) and rsvc.mode == "remote"
    no_token = tools.PhoneService(store=rsvc.store,
                                  environ={"HERMES_PHONE_BRIDGE_URL": bridge.url})
    assert not no_token.dialer.configured
    assert no_token.dialer.missing == ["HERMES_PHONE_BRIDGE_TOKEN"]
    upstream = tools.PhoneService(store=rsvc.store, environ={"OPENAI_API_KEY": "k",
                                                             "PUBLIC_BASE_URL": "https://x"})
    assert upstream.mode == "web"  # unset bridge URL: upstream behaviour unchanged


def test_health_sends_bearer_and_doctor_names_no_vendor(rsvc, bridge):
    assert rsvc.dialer.preflight() == ""
    assert bridge.requests[-1]["auth"] == f"Bearer {TOKEN}"
    checks = cli.run_checks(rsvc.environ)
    assert all(ok for _, ok, _ in checks)
    assert not any(v in str(checks).lower() for v in ("twilio", "openai", "telnyx"))
    bad = remote.RemoteBridge({**rsvc.environ, "HERMES_PHONE_BRIDGE_TOKEN": "wrong"}, store=None)
    assert "HTTP 401" in bad.preflight()


def test_plain_http_to_a_remote_host_is_refused(store):
    b = remote.RemoteBridge({"HERMES_PHONE_BRIDGE_URL": "http://bridge.example",
                             "HERMES_PHONE_BRIDGE_TOKEN": TOKEN}, store=store)
    assert "https" in b.preflight()


# ---- submit -------------------------------------------------------------------------------
def test_phone_call_submits_approved_call_with_consented_fields_only(rsvc, bridge, ctx):
    cid = prepare({"facts_to_share": {"name": "Winch", "party_size": 4,
                                      "home_address": "1 Main St, Springfield"}})["call_id"]
    approve(rsvc, cid)
    out = json.loads(tools.phone_call({"call_id": cid}))
    assert out == {"call_id": cid, "state": "dialing"}
    put = bridge.of("PUT")[0]
    body = put["body"]
    assert put["path"] == f"/v1/calls/{cid}" and put["auth"] == f"Bearer {TOKEN}"
    assert body["approval"]["id"].startswith("ap_") and body["approval"]["consumed_at"]
    assert body["brief"]["facts"] == {"name": "Winch", "party_size": 4}
    assert "1 Main St" not in json.dumps(body) and "facts_raw" not in json.dumps(body)
    assert "workspace" not in json.dumps(body)  # the bridge derives it from the token


def test_place_call_strips_unconsented_and_forbidden_even_if_payload_has_them(rsvc, bridge):
    call = dialed(rsvc)
    p = payload(rsvc, call)
    p["facts"] = {**p["facts"], "card_number": "4111 1111 1111 1111", "cvv": "123",
                  "password": "hunter2"}
    assert rsvc.dialer.place_call(call, p).ok
    sent = json.dumps(bridge.of("PUT")[0]["body"])
    assert "4111" not in sent and "hunter2" not in sent and '"cvv"' not in sent


def test_no_consumed_approval_never_reaches_the_bridge(rsvc, bridge):
    cid = prepare()["call_id"]
    approve(rsvc, cid)  # recorded but not spent
    call = rsvc.store.get_call(cid)
    res = rsvc.dialer.place_call(call, payload(rsvc, call))
    assert not res.ok and "approval" in res.error and bridge.of("PUT") == []


def test_bridge_refusing_the_approval_fails_the_call(rsvc, bridge):
    bridge.fail["PUT"] = (403, {"error": {"code": "approval_required", "message": "no approval"}})
    cid = prepare()["call_id"]
    approve(rsvc, cid)
    out = json.loads(tools.phone_call({"call_id": cid}))
    assert out["state"] == states.FAILED and "HTTP 403: no approval" in out["error"]
    assert len(bridge.of("PUT")) == 1


def test_submit_is_idempotent_by_call_id_and_approval(rsvc, bridge):
    call = dialed(rsvc)
    p = payload(rsvc, call)
    first, second = rsvc.dialer.place_call(call, p), rsvc.dialer.place_call(call, p)
    assert first.ok and second.ok and len(bridge.calls) == 1
    ids = {r["body"]["approval"]["id"] for r in bridge.of("PUT")}
    assert len(ids) == 1  # same approval -> same id, so the bridge dedupes


def test_bridge_error_is_a_clear_failure_with_no_retry(rsvc, bridge, ctx):
    bridge.fail["PUT"] = (503, {"error": {"code": "busy", "message": "line busy"}})
    cid = prepare()["call_id"]
    approve(rsvc, cid)
    out = json.loads(tools.phone_call({"call_id": cid}))
    assert out["state"] == states.FAILED and "HTTP 503: line busy" in out["error"]
    assert len(bridge.of("PUT")) == 1 and cid not in rsvc.watchers
    assert rsvc.store.get_call(cid)["state"] == states.FAILED
    assert not any(v in out["error"].lower() for v in ("twilio", "openai", "telnyx"))


def test_unreachable_bridge_fails_the_submit(store):
    b = remote.RemoteBridge({"HERMES_PHONE_BRIDGE_URL": "http://127.0.0.1:9",
                             "HERMES_PHONE_BRIDGE_TOKEN": TOKEN,
                             "HERMES_PHONE_TEST_ALLOWLIST": TEST_NUMBER}, store=store,
                            timeout_s=1)
    store.create_call("c1", state="dialing", callee_name="X", number=TEST_NUMBER,
                      number_source="user", goal="g", brief={"limits": {}})
    store.record_approval("c1", user_id="u", session_id="s", ttl_s=60)
    store.consume_approval("c1")
    res = b.place_call(store.get_call("c1"), {"facts": {}})
    assert not res.ok and "not reachable" in res.error


# ---- updates, questions, answers -----------------------------------------------------------
def test_cursor_polling_mirrors_state_questions_transcript_and_result(rsvc, bridge):
    call = dialed(rsvc)
    cid = call["id"]
    bridge.pages = [
        {"cursor": "c1", "call": {"state": "ringing"}, "events": [{"id": "e1", "kind": "ringing"}]},
        {"cursor": "c2", "call": {"state": "on_hold_for_user", "disclosure_spoken": True},
         "questions": [{"id": "q1", "question": "Name for the booking?", "field": "name",
                        "status": "open"}],
         "transcript": [{"id": "t1", "role": "agent",
                         "text": "Hi, this is an AI assistant calling."}]},
        {"cursor": "c3", "call": {"state": "completed", "outcome": "success", "duration_s": 42,
                                  "usage": {"cost_usd": 0.31},
                                  "result": {"outcome": "success", "agreed": {"time": "20:00"}}}},
    ]
    for _ in range(4):
        rsvc.dialer.sync(rsvc.store, cid)
    cursors = [r["query"].get("cursor", [""])[0] for r in bridge.of("GET", "/updates")]
    assert cursors == ["", "c1", "c2"]  # 4th sync skipped: call is terminal locally
    got = rsvc.store.get_call(cid)
    assert got["state"] == states.COMPLETED and got["outcome"] == "success"
    assert got["duration_s"] == 42 and got["disclosure_spoken"] == 1 and got["ended_at"]
    assert got["result"]["agreed"] == {"time": "20:00"} and got["bridge_cursor"] == "c3"
    assert json.loads(got["usage_json"]) == {"cost_usd": 0.31}
    assert rsvc.store.get_question("q1")["call_id"] == cid
    assert rsvc.store.transcript(cid)[0]["role"] == "agent"
    assert any(e["kind"] == "bridge.ringing" for e in rsvc.store.events(cid))


def test_bridge_cannot_move_a_call_back_to_approved_or_hijack_a_question(rsvc, bridge):
    a, b = dialed(rsvc), dialed(rsvc)
    rsvc.store.add_question("qx", a["id"], "other call?", kind="question")
    remote.mirror(rsvc.store, b["id"], {"call": {"state": "approved"},
                                        "questions": [{"id": "qx", "status": "timeout"},
                                                      {"id": "bad id!", "question": "x"}]})
    assert rsvc.store.get_call(b["id"])["state"] == states.DIALING
    assert rsvc.store.get_question("qx")["status"] == "open"
    assert rsvc.store.get_question("bad id!") is None


def test_answer_round_trip_is_pushed_once(rsvc, bridge):
    call = dialed(rsvc)
    cid = call["id"]
    bridge.pages = [{"cursor": "c1", "questions": [
        {"id": "q1", "question": "Name for the booking?", "field": "name", "status": "open"}]}]
    rsvc.dialer.sync(rsvc.store, cid)
    assert "Sent" in relay.answer_question(rsvc, "q1", "Winch")
    rsvc.dialer.sync(rsvc.store, cid)
    rsvc.dialer.sync(rsvc.store, cid)
    posts = bridge.of("POST", "/answer")
    assert len(posts) == 1 and posts[0]["path"] == f"/v1/calls/{cid}/questions/q1/answer"
    assert posts[0]["body"] == {"status": "answered", "answer": "Winch"}


def test_forbidden_answers_never_leave_the_workspace(rsvc, bridge):
    call = dialed(rsvc)
    cid = call["id"]
    bridge.pages = [{"cursor": "c1", "questions": [
        {"id": "q1", "question": "What's the CVV on the card?", "field": "cvv",
         "status": "open"}]}]
    rsvc.dialer.sync(rsvc.store, cid)
    rsvc.store.resolve_question("q1", "answered", "987")  # even if something stored it
    rsvc.dialer.sync(rsvc.store, cid)
    post = bridge.of("POST", "/answer")[0]
    assert post["body"] == {"status": "declined"} and "987" not in json.dumps(bridge.requests)


def test_hangup_request_is_pushed(rsvc, bridge):
    call = dialed(rsvc)
    rsvc.store.request_hangup(call["id"])
    rsvc.dialer.sync(rsvc.store, call["id"])
    rsvc.dialer.sync(rsvc.store, call["id"])
    assert len(bridge.of("POST", "/hangup")) == 1


def test_bridge_forgetting_the_call_fails_it_locally(rsvc, bridge):
    call = dialed(rsvc)
    bridge.fail["updates"] = (404, {"error": {"code": "not_found", "message": "unknown call"}})
    rsvc.dialer.sync(rsvc.store, call["id"])
    got = rsvc.store.get_call(call["id"])
    assert got["state"] == states.FAILED and got["reason"] == "bridge_rejected"


def test_transient_errors_keep_polling(rsvc, bridge):
    call = dialed(rsvc)
    bridge.fail["updates"] = (502, {"error": "bad gateway"})
    rsvc.dialer.sync(rsvc.store, call["id"])
    assert rsvc.store.get_call(call["id"])["state"] == states.DIALING


# ---- end to end through the unchanged watcher ----------------------------------------------
def test_watcher_posts_cards_and_one_result_from_remote_updates(rsvc, bridge, ctx):
    bridge.pages = [
        {"cursor": "c1", "call": {"state": "in_progress"}},
        {"cursor": "c2", "call": {"state": "on_hold_for_user"}, "questions": [
            {"id": "q1", "question": "How many people?", "field": "party_size",
             "status": "open"}]},
    ]
    cid = prepare()["call_id"]
    approve(rsvc, cid)
    assert json.loads(tools.phone_call({"call_id": cid}))["state"] == "dialing"
    wait_for(lambda: any(" asks" in m.text for m in ctx.messages))
    assert "Sent" in relay.answer_question(rsvc, "q1", "4")
    wait_for(lambda: bridge.of("POST", "/answer"))
    bridge.pages.append({"cursor": "c3", "call": {
        "state": "completed", "outcome": "success",
        "result": {"outcome": "success", "agreed": {"party_size": 4}}}})
    wait_for(lambda: rsvc.store.get_call(cid)["result_posted_at"] is not None)
    results = [m for m in ctx.messages if "party_size" in m.text or "4" in m.text]
    assert results and rsvc.store.get_call(cid)["state"] == states.COMPLETED


def test_replayed_page_is_idempotent(rsvc, bridge):
    call = dialed(rsvc)
    cid = call["id"]
    page = {"cursor": "c1", "call": {"state": "in_progress"},
            "events": [{"id": "e1", "kind": "answered"}, {"kind": "no id, dropped"}],
            "questions": [{"id": "q1", "question": "Name?", "field": "name", "status": "open"}],
            "transcript": [{"id": "t1", "role": "agent", "text": "Hi, an AI assistant here."},
                           {"id": "t2", "role": "callee", "text": "Hello?"}]}
    remote.mirror(rsvc.store, cid, page)
    before = (rsvc.store.events(cid), rsvc.store.transcript(cid),
              rsvc.store.questions_for_call(cid))
    remote.mirror(rsvc.store, cid, json.loads(json.dumps(page)))  # crash before cursor: replay
    after = (rsvc.store.events(cid), rsvc.store.transcript(cid),
             rsvc.store.questions_for_call(cid))
    assert after == before
    assert [e["kind"] for e in after[0]].count("bridge.answered") == 1
    assert len(after[1]) == 2 and len(after[2]) == 1


def test_remote_cost_comes_only_from_bridge_usage(rsvc, bridge, ctx):
    from hermes_phone import results

    cid = prepare()["call_id"]
    card = next(m.text for m in ctx.messages if "Call brief" in m.text)
    assert "est. cost" not in card  # no plugin-default estimate in remote mode
    approve(rsvc, cid)
    rsvc.store.consume_approval(cid)
    states.transition(rsvc.store, cid, states.DIALING, dialed_at=rsvc.store.clock())
    rsvc.store.update_call(cid, provider_sid=remote.SID_PREFIX + cid)
    end = {"state": "completed", "outcome": "success", "duration_s": 95,
           "result": {"outcome": "success"}}
    remote.mirror(rsvc.store, cid, {"call": end})
    text = results.result_message(rsvc.store.get_call(cid), rsvc.config)
    assert "$" not in text  # no usage: no cost figure at all
    rsvc.store.update_call(cid, usage_json=json.dumps({"cost_usd": 0.42}))
    assert "$0.42" in results.result_message(rsvc.store.get_call(cid), rsvc.config)
    upstream = dict(rsvc.store.get_call(cid), provider_sid="CA1")
    assert "about $" in results.result_message(upstream, rsvc.config)  # other modes unchanged


def test_usage_token_pair_is_the_fallback_credential(bridge):
    base = {"HERMES_PHONE_BRIDGE_URL": bridge.url}
    pair = {"TEAMSHIFT_USAGE_TOKEN": TOKEN, "TEAMSHIFT_WORKSPACE_NAME": "t-0123456789"}
    assert remote.RemoteBridge({**base, **pair}, store=None).preflight() == ""
    assert bridge.requests[-1]["ws"] == "t-0123456789"
    both = {**base, **pair, "HERMES_PHONE_BRIDGE_TOKEN": TOKEN}
    assert remote.RemoteBridge(both, store=None).preflight() == ""
    assert bridge.requests[-1]["ws"] is None  # the bridge token wins; no workspace header
    from hermes_phone import mode as modemod

    assert modemod.resolve({**base, **pair}).missing == []
    assert modemod.resolve({**base, "TEAMSHIFT_USAGE_TOKEN": TOKEN}).missing == [
        "HERMES_PHONE_BRIDGE_TOKEN"]
