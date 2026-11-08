"""Client for a shared remote bridge (docs/BRIDGE_API.md), used when HERMES_PHONE_BRIDGE_URL is set.

The workspace only makes outbound HTTPS requests. The plugin keeps its local store as the source
for tools, hooks and cards: ``place_call`` submits the approved call, and ``sync`` (run by the
watcher every poll) pushes answers and hangups and mirrors bridge updates into the store.
Stdlib only (``urllib``). No retries: a failed submit fails the call and is never redialed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from . import consent, safety, states
from . import mode as modemod
from .store import Store
from .tools import DialResult

log = logging.getLogger(__name__)

API = "/v1"
TIMEOUT_S = 10.0
POLL_S = 2.0  # watcher interval for remote calls (bridge allows 60 requests a minute)
SID_PREFIX = "bridge-"  # provider_sid of every remote call (results.py shows bridge cost only)
UNREACHABLE_FAIL_S = 120.0  # bridge silent this long during a live call: fail it locally
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MIRROR_STATES = states.LIVE | states.TERMINAL  # the bridge may never set pre-dial states
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # never resend the token
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def approval_id(appr: Mapping[str, Any]) -> str:
    """Stable id of one recorded approval: the same approval always gives the same id."""
    raw = f"{appr['call_id']}:{appr['approved_at']!r}"
    return "ap_" + hashlib.sha256(raw.encode()).hexdigest()[:32]


class RemoteBridge:
    configured = True
    remote = True
    poll_s = POLL_S

    def __init__(self, environ: Mapping[str, str], *, store: Store | None,
                 config: Callable[[str, Any], Any] | None = None,
                 timeout_s: float = TIMEOUT_S) -> None:
        self.environ = environ
        self.store = store
        self.config = config or (lambda key, default=None: default)
        self.timeout_s = timeout_s
        self._pushed: set[str] = set()  # question ids whose answer the bridge accepted
        self._hung: set[str] = set()
        self._down_since: dict[str, float] = {}

    # -- HTTP --
    @property
    def base_url(self) -> str:
        return (self.environ.get(modemod.BRIDGE_URL_ENV) or "").strip().rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        base = self.base_url
        parts = urllib.parse.urlsplit(base)
        if parts.scheme != "https" and not (parts.scheme == "http"
                                            and parts.hostname in _LOCAL_HOSTS):
            raise BridgeError(f"{modemod.BRIDGE_URL_ENV} must be an https URL")
        token, workspace = modemod.remote_credentials(self.environ)
        if not token:
            raise BridgeError(f"{modemod.BRIDGE_TOKEN_ENV} is not set")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
                   "Content-Type": "application/json", "User-Agent": "hermes-phone"}
        if workspace:  # usage-token fallback: the bridge verifies the pair
            headers["X-TeamShift-Workspace"] = workspace
        req = urllib.request.Request(
            base + API + path, method=method,
            data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with _OPENER.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise BridgeError(f"HTTP {exc.code}: {_error_text(exc)}", exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise BridgeError(f"not reachable ({type(exc).__name__})") from None
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise BridgeError("answered with invalid JSON") from None
        if not isinstance(data, dict):
            raise BridgeError("answered with invalid JSON")
        return data

    @staticmethod
    def _path(call_id: str, *rest: str) -> str:
        return "/calls/" + "/".join(urllib.parse.quote(p, safe="") for p in (call_id, *rest))

    # -- dialer interface (same shape as TwilioDialer) --
    def preflight(self) -> str:
        try:
            self._request("GET", "/health")
        except BridgeError as exc:
            return f"The phone service is not reachable ({exc}). Nothing was dialed."
        return ""

    def place_call(self, call: dict, payload: dict) -> DialResult:
        number, cid = call["number"], call["id"]
        e164, reason = safety.validate_number(number)
        if e164 is None or e164 != number:
            return DialResult(ok=False, error=reason or "number failed validation")
        allow = safety.check_allowlist(number, self.environ)
        if not allow.ok:
            return DialResult(ok=False, error=allow.reason)
        appr = self.store.get_approval(cid)
        if appr is None or appr.get("used_at") is None:
            return DialResult(ok=False, error="no consumed approval for this call")
        granted = self.store.consents(cid)
        facts, _ = consent.build_safe_facts(payload.get("facts") or {}, granted)  # defence again
        brief = dict(payload, facts=facts,
                     goal=consent.redact_text(str(payload.get("goal") or ""), granted))
        limits = call["brief"].get("limits", {})
        body = {
            "call_id": cid, "number": number,
            "approval": {"id": approval_id(appr), "approved_at": appr["approved_at"],
                         "expires_at": appr["expires_at"], "consumed_at": appr["used_at"]},
            "brief": brief,
            "limits": {"max_duration_s": int(payload.get("max_duration_s") or 300),
                       "question_timeout_s": int(limits.get("question_timeout_s", 75))},
            "options": {"store_transcripts": self._keep_transcripts()},
        }
        try:
            self._request("PUT", self._path(cid), body)
        except BridgeError as exc:
            if not exc.status:  # outcome unknown (timeout): make sure nothing stays live
                self.hangup(call)
            return DialResult(ok=False, error=f"phone service error: {exc}")
        return DialResult(ok=True, provider_sid=SID_PREFIX + cid)

    def hangup(self, call: dict) -> bool:
        cid = call.get("id") or ""
        if not cid:
            return False
        try:
            self._request("POST", self._path(cid, "hangup"), {})
        except BridgeError as exc:
            if exc.status != 409:  # 409: already ended
                log.warning("bridge hangup failed: %s", exc)
                return False
        self._hung.add(cid)
        return True

    # -- watcher hook --
    def sync(self, store: Store, call_id: str) -> None:
        """Push local answers/hangup, then mirror bridge updates. Never raises."""
        call = store.get_call(call_id)
        if call is None or call["state"] in states.TERMINAL:
            return
        try:
            self._push(store, call)
            path = self._path(call_id, "updates")
            if call.get("bridge_cursor"):
                path += "?cursor=" + urllib.parse.quote(call["bridge_cursor"], safe="")
            data = self._request("GET", path)
        except BridgeError as exc:
            self._unreachable(store, call, exc)
            return
        except Exception:  # noqa: BLE001 - a watcher must keep going
            log.exception("bridge sync failed for %s", call_id)
            return
        self._down_since.pop(call_id, None)
        mirror(store, call_id, data, keep_transcripts=self._keep_transcripts())

    def _keep_transcripts(self) -> bool:
        return bool(self.config("store_transcripts", True))

    def _push(self, store: Store, call: dict) -> None:
        cid = call["id"]
        if call.get("hangup_requested_at") is not None and cid not in self._hung:
            self.hangup(call)
        granted = store.consents(cid)
        for q in store.questions_for_call(cid):
            if q["status"] not in ("answered", "declined") or q["id"] in self._pushed:
                continue
            status, answer = q["status"], q.get("answer") or ""
            asks = consent.classify_question(q.get("field") or "", q.get("question") or "")
            allowed = granted | ({q["consent_field"]} if q.get("consent_field") else set())
            if status == "answered" and (asks in consent.FORBIDDEN_FIELDS
                                         or consent.text_fields(answer, allowed)):
                status, answer = "declined", ""  # never leaves the workspace
            body = {"status": status, **({"answer": answer} if status == "answered" else {})}
            try:
                self._request("POST", self._path(cid, "questions", q["id"], "answer"), body)
            except BridgeError as exc:
                if exc.status != 409:  # 409: question already closed on the bridge
                    raise
            self._pushed.add(q["id"])

    def _unreachable(self, store: Store, call: dict, exc: BridgeError) -> None:
        cid = call["id"]
        log.warning("bridge sync failed for %s: %s", cid, exc)
        gone = exc.status in (401, 403, 404)
        first = self._down_since.setdefault(cid, time.monotonic())
        if not gone and time.monotonic() - first < UNREACHABLE_FAIL_S:
            return
        reason = "bridge_rejected" if gone else "bridge_unreachable"
        store.set_result_once(cid, {"outcome": "failed", "reason": reason,
                                    "summary": "Lost contact with the phone service."}, "failed")
        _set_state(store, cid, states.FAILED, reason=reason, outcome="failed")
        self._down_since.pop(cid, None)


def _error_text(exc: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(exc.read() or b"{}")
        err = data.get("error") if isinstance(data, dict) else None
        text = err.get("message") if isinstance(err, dict) else err
    except Exception:  # noqa: BLE001
        text = ""
    return str(text or exc.reason or "error").replace("\n", " ")[:200]


def _set_state(store: Store, cid: str, new: str, **fields: Any) -> None:
    """Mirror the bridge's state. The bridge is authoritative once dialed, so intermediate states
    the local machine would require (e.g. dialing -> in_progress -> completed) may be skipped."""
    call = store.get_call(cid)
    if call is None or call["state"] == new or call["state"] in states.TERMINAL:
        return
    if new in states.TERMINAL:
        fields.setdefault("ended_at", store.clock())
    if store.update_state_if(cid, call["state"], new, **fields):
        store.add_event(cid, "state", {"from": call["state"], "to": new, "source": "bridge"})


def _usd(value: Any) -> float | None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e6
    return float(value) if ok else None


def _str(value: Any, limit: int = 2000) -> str:
    return value[:limit] if isinstance(value, str) else ""


def mirror(store: Store, cid: str, data: Mapping[str, Any], *, keep_transcripts: bool = True
           ) -> None:
    """Apply one ``updates`` response to the local store. Bridge text is untrusted data."""
    # Idempotent: replaying a page (crash before the cursor update) changes nothing. Questions,
    # events and transcript lines are keyed by bridge ids; state, result and usage are set-once
    # or compare-and-set.
    for q in data.get("questions") or []:
        if not isinstance(q, dict) or not _ID_RE.match(str(q.get("id") or "")):
            continue
        qid = q["id"]
        local = store.get_question(qid)
        if local is None:
            kind = "consent" if q.get("kind") == "consent" else "question"
            store.add_question(qid, cid, _str(q.get("question"), 500), _str(q.get("field"), 64),
                               _str(q.get("suggested"), 500), kind=kind,
                               consent_field=_str(q.get("consent_field"), 32))
            local = store.get_question(qid)
        elif local["call_id"] != cid:
            continue  # an id from another call never overwrites it
        if q.get("status") == "timeout" and local and local["status"] == "open":
            store.resolve_question(qid, "timeout")
    for line in (data.get("transcript") or []) if keep_transcripts else []:
        if (isinstance(line, dict) and _ID_RE.match(str(line.get("id") or ""))
                and line.get("role") in ("agent", "callee") and line.get("text")):
            store.add_transcript_line(cid, line["role"], _str(line["text"]), line["id"])
    for ev in data.get("events") or []:
        if (isinstance(ev, dict) and _ID_RE.match(str(ev.get("id") or ""))
                and _str(ev.get("kind"), 64)):
            store.add_event(cid, "bridge." + ev["kind"][:64],
                            ev.get("data") if isinstance(ev.get("data"), dict) else {}, ev["id"])
    info = data.get("call") if isinstance(data.get("call"), dict) else {}
    if info.get("disclosure_spoken") is not None:
        store.set_disclosure(cid, bool(info["disclosure_spoken"]))
    if isinstance(info.get("duration_s"), int) and info["duration_s"] >= 0:
        store.update_call(cid, duration_s=info["duration_s"])
    usage = info.get("usage")
    if isinstance(usage, dict) and _usd(usage.get("cost_usd")) is not None:
        store.update_call(cid, usage_json=json.dumps({"cost_usd": _usd(usage["cost_usd"])}))
    if isinstance(info.get("result"), dict):
        store.set_result_once(cid, info["result"], _str(info.get("outcome"), 64))
    state = info.get("state")
    if state in _MIRROR_STATES:
        fields = {k: _str(info.get(k), 500) for k in ("outcome", "reason") if info.get(k)}
        _set_state(store, cid, state, **(fields if state in states.TERMINAL else {}))
    if isinstance(data.get("cursor"), str) and len(data["cursor"]) <= 512:
        store.update_call(cid, bridge_cursor=data["cursor"])
