"""Business calls on linq-voice's GPT-Live stack (enckequity/teamshift#2872, #2981, #2992).

With ``HERMES_PHONE_BUSINESS_CALL_URL`` set, the shared bridge keeps the tool, the brief, the
approval, quiet hours and the daily cap, but does not dial: it hands the call to linq-voice's
loopback ``POST /_internal/business-call`` (bearer = the member_pin_key credential) and waits for
exactly one result POST on ``<HERMES_PHONE_BUSINESS_RESULT_BASE>/v1/business/results/<iid>/<secret>``.

linq-voice meters the member allowance itself (``out_<call_id>``), so the bridge does not admit
or close allowance for these calls. The result POST carries no credential: the per-call secret in
its path is the only proof, compared in constant time against its stored hash. linq-voice sends an
unconfirmed result again after a restart, so a repeated call_id is acknowledged and ignored.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .. import states

MIN_SECONDS, MAX_SECONDS = 30, 300  # linq-voice refuses anything outside this
REFUSED = -1  # post(): the connection was refused, so nothing was sent
# After its cap linq-voice may still: wait for its request (~25 s), ring (30 s), say goodbye, hang up (15 s), post (15 s).
TAIL_S = 150
RESULT_HOSTS = frozenset({"127.0.0.1", "172.22.0.1"})  # linq-voice posts results only here
GOAL_MAX, FACTS_MAX, DISCLOSURE_MAX, RULES_MAX = 2000, 4000, 300, 2000
RESULT_TEXT_MAX = 2000
TAIL_MAX = 1500
_CALL_URL_RE = re.compile(r"http://(127\.0\.0\.1|localhost|\[::1\])(:\d{2,5})?/_internal/business-call")

# linq-voice result state -> (path through the bridge state machine, outcome when none is derived)
_PATHS: dict[str, tuple[tuple[str, ...], str]] = {
    "completed": ((states.IN_PROGRESS, states.COMPLETED), ""),
    "no_answer": ((states.NO_ANSWER,), "no_answer"),
    "machine": ((states.IN_PROGRESS, states.VOICEMAIL, states.COMPLETED), "voicemail"),
    "failed": ((states.FAILED,), "failed"),
    # the customer hung up on the task (cancel, teamshift#3000): like a hangup before the bridge's own session ran
    "cancelled": ((states.ABORTED,), "needs_followup"),
}


def call_url(raw: str | None) -> str:
    """The linq-voice endpoint, loopback only (the bearer never leaves this host). Unset = off."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not _CALL_URL_RE.fullmatch(raw):
        raise ValueError("HERMES_PHONE_BUSINESS_CALL_URL must be http loopback .../_internal/business-call")
    return raw


def result_base(raw: str | None) -> str:
    """Where linq-voice reaches this bridge: http on 127.0.0.1 or 172.22.0.1, nothing else."""
    raw = (raw or "").strip().rstrip("/")
    if not raw:
        return ""
    u = urlsplit(raw)
    if (u.scheme != "http" or u.hostname not in RESULT_HOSTS or u.username or u.password
            or u.query or u.fragment or u.path):
        raise ValueError("HERMES_PHONE_BUSINESS_RESULT_BASE must be http://127.0.0.1:<port> or http://172.22.0.1:<port>")
    return raw


def _facts_text(facts: dict) -> str:
    """Consent-filtered facts as one ``key: value`` line each, cut to linq-voice's limit."""
    lines = []
    for key in sorted(facts):
        value = facts[key]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {' '.join(str(text).split())}")
    out = "\n".join(lines)
    return out[:FACTS_MAX]


def rules_text(user_name: str) -> str:
    return (f"You call on behalf of {user_name}. There is no one to check with during the call: never put "
            "the business on hold. Negotiate: if an offer is outside the preference, ask for alternatives first, "
            "then accept the best offer that fits the preference or anything the facts call acceptable. "
            f"The booking name is {user_name} unless the facts give another name: give it, never ask. "
            "If nothing offered fits, ask them to pencil it in, say you will confirm and call back, and end with "
            "the held option in agreed and the choice to make in next_steps. Never share anything that is not in "
            "the facts. Before ending, confirm the key details back and ask for a confirmation number.")[:RULES_MAX]


def caller_ok(number: str) -> bool:
    """linq-voice refuses (400, failing the call) a caller_number outside US/Canada: send only +1 numbers."""
    return bool(re.fullmatch(r"\+1[2-9][0-9]{2}[2-9][0-9]{6}", number or ""))


def request(*, iid: str, workspace: str, number: str, payload: dict, max_seconds: int, result_url: str,
            caller_number: str = "", caller_auth: dict | None = None) -> dict:
    """The /_internal/business-call body. ``caller_number`` goes only together with ``caller_auth``."""
    body: dict[str, Any] = {
        "call_id": iid, "workspace": workspace, "number": number, "result_url": result_url,
        "brief": {
            "goal": (" ".join(str(payload.get("goal") or "").split()) or "Speak with the business.")[:GOAL_MAX],
            "facts": _facts_text(payload.get("facts") or {}),
            "disclosure": str(payload["opening_line"])[:DISCLOSURE_MAX],
            "max_seconds": max(MIN_SECONDS, min(MAX_SECONDS, int(max_seconds))),
            "rules": rules_text(str(payload.get("user_name") or "the customer")),
        },
    }
    if caller_number and caller_auth and caller_ok(caller_number):
        body["caller_number"], body["caller_auth"] = caller_number, caller_auth
    return body


def post(url: str, key: str, body: dict, timeout_s: float) -> tuple[int, dict]:
    """``(status, reply)``; ``(0, {})`` when there was no HTTP answer. Never retried by the caller."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - loopback only
            status, raw = int(resp.status), resp.read(4096)
    except urllib.error.HTTPError as exc:  # 4xx/5xx: the body still says why (429 allowance vs busy)
        try:
            status, raw = int(exc.code), exc.read(4096)
        except OSError:
            status, raw = int(exc.code), b""
    except urllib.error.URLError as exc:
        # Refused: nothing listened, so nothing was placed. Anything else (a timeout above all) may have placed the call.
        return (REFUSED if isinstance(exc.reason, ConnectionRefusedError) else 0), {}
    except ConnectionRefusedError:
        return REFUSED, {}
    except (OSError, ValueError):
        return 0, {}
    try:
        reply = json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        reply = {}
    return status, reply if isinstance(reply, dict) else {}


def cancel(url: str, key: str, iid: str, timeout_s: float) -> int:
    """Ask linq-voice to end a handed-off call (teamshift#3000); its HTTP status, or 0 without an answer."""
    return post(url, key, {"call_id": iid, "action": "cancel"}, timeout_s)[0]


def refusal(status: int, reply: dict) -> str:
    """The bridge error code for a reply that did not start a call, or "" for 202 dialing."""
    if status == 202:
        return ""
    if status == 429:
        return "month_cap" if reply.get("state") == "allowance" else "rate_limited"
    return "dial_failed"  # 400, 409, a 200 {state: failed} (its result was posted too), no answer


def parse_result(body: Any) -> dict | None:
    """A valid linq-voice result as ``{state, outcome, agreed, next_steps, seconds, transcript_tail}``."""
    if not isinstance(body, dict) or body.get("state") not in _PATHS:
        return None
    text = {k: str(body.get(k) or "")[:RESULT_TEXT_MAX] for k in ("outcome", "agreed", "next_steps")}
    try:
        seconds = max(0, min(MAX_SECONDS + 120, int(round(float(body.get("seconds") or 0)))))
    except (TypeError, ValueError):
        seconds = 0
    return {"state": body["state"], **text, "seconds": seconds,
            "transcript_tail": str(body.get("transcript_tail") or "")[-TAIL_MAX:]}


def outcome_of(res: dict) -> str:
    """One of the bridge's END_OUTCOMES (or no_answer) for the plugin."""
    path_outcome = _PATHS[res["state"]][1]
    if path_outcome:
        return path_outcome
    # linq-voice's end_call fills next_steps even for a done deal ("arrive 10 min early"), so agreed alone means success.
    return "success" if res["agreed"].strip() else "needs_followup"


def path_of(res: dict) -> tuple[str, ...]:
    return _PATHS[res["state"]][0]


def transcript_lines(tail: str) -> list[tuple[str, str]]:
    """``(role, text)`` pairs from linq-voice's tail (``You: ...`` / ``Them: ...`` lines; the first may be cut)."""
    out = []
    for line in tail.splitlines():
        who, sep, text = line.partition(":")
        if not sep or not text.strip():
            continue
        role = {"you": "agent", "them": "callee"}.get(who.strip().lower())
        if role is None:
            continue
        out.append((role, text.strip()[:2000]))
    return out
