"""Call results (SPEC 1.6/1.7): the final message, duration and cost estimate, transcripts, log.

Everything here is plain text (no Markdown that Telegram could mangle) and is built only from
what ``end_call`` recorded (agreed fields, next steps) plus the call's own bookkeeping. Sensitive
values are scrubbed even when the user consented to share them on the call: a result message
lives in chat history for good.
"""
from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from . import consent, states

CHUNK_LIMIT = 3500  # Telegram caps a message at 4096; stay well below
DEFAULT_TWILIO_PER_MIN = 0.014  # US outbound to a mobile/landline, Twilio list price
# OpenAI Realtime audio: roughly $0.05/min of input audio plus $0.08/min of output audio at
# typical talk ratios, rounded up for tool and text tokens. An estimate, not a bill.
DEFAULT_REALTIME_PER_MIN = 0.15
RETRYABLE = frozenset({"no_answer", "busy", "voicemail", "needs_followup", "failed"})
AGREED_ORDER = ("date", "time", "party_size", "notes", "name", "price", "confirmation_number")
_SKIP_KEYS = {"confirmation_number"}  # rendered as its own sentence

ICONS = {"success": "✅", "partial": "🟡", "failed": "❌", "needs_followup": "⚠️",
         "no_answer": "📵", "busy": "📵", "voicemail": "📫", "aborted": "⛔",
         "canceled": "⛔", "expired": "⌛"}
REASON_TEXT = {
    "user_hangup": "You hung up",
    "max_duration": "The call reached its maximum duration",
    "question_timeout": "You did not answer in time, the callee was told we will call back",
    "voicemail": "Reached voicemail",
    "browser_never_connected": "The browser page never connected its audio",
    "user_canceled": "Canceled by you",
    "approval_ttl": "The approval expired",
}


# ---- helpers ----------------------------------------------------------------------------------
def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return ""
    s = max(0, int(round(seconds)))
    m, sec = divmod(s, 60)
    if m == 0:
        return f"{sec}s"
    return f"{m}m {sec:02d}s" if sec else f"{m}m"


def effective_outcome(call: dict) -> str:
    """One word for what happened: success/partial/failed/needs_followup/voicemail/no_answer/
    busy, or the state itself (aborted, canceled, expired, or a live state)."""
    state = call.get("state", "")
    if state in (states.NO_ANSWER, states.BUSY):
        return state
    result = call.get("result") or {}
    outcome = str(result.get("outcome") or call.get("outcome") or "")
    if outcome in ("success", "partial", "failed", "needs_followup", "voicemail",
                   "no_answer", "busy"):
        return outcome
    if state == states.FAILED:
        return "failed"
    if state == states.COMPLETED:
        return "needs_followup"
    return state


def call_duration_s(call: dict) -> int | None:
    """Talk time: Twilio's CallDuration or the bridge stamp (``duration_s``), else the result's
    own figure, else ended minus dialed."""
    for value in (call.get("duration_s"), (call.get("result") or {}).get("duration_s")):
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    if call.get("ended_at") is not None and call.get("dialed_at") is not None:
        return max(0, int(call["ended_at"] - call["dialed_at"]))
    return None


def _is_web(call: dict) -> bool:
    return str(call.get("provider_sid") or "").startswith("web-")


def estimate_cost(call: dict, duration_s: int | None, config: Any = None) -> float:
    """Estimated USD: Twilio per started minute (0 in web mode) + OpenAI Realtime audio per
    second. ``config`` is a callable ``(key, default)``; an estimate, never a bill."""
    if not duration_s:
        return 0.0
    get = config or (lambda _k, d: d)
    twilio = _num(get("twilio_cost_per_min_usd", DEFAULT_TWILIO_PER_MIN), DEFAULT_TWILIO_PER_MIN)
    rt = _num(get("realtime_cost_per_min_usd", DEFAULT_REALTIME_PER_MIN),
              DEFAULT_REALTIME_PER_MIN)
    telephony = 0.0 if _is_web(call) else math.ceil(duration_s / 60) * twilio
    return round(telephony + duration_s / 60 * rt, 2)


def _num(value: Any, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def _sensitive_values(call: dict, config: Any) -> list[str]:
    """Values the user put in the brief for sensitive fields (+ callback number): never echoed."""
    out: list[str] = []
    for key, value in ((call.get("brief") or {}).get("facts_raw") or {}).items():
        if consent.field_for_fact(key, value) and isinstance(value, (str, int, float)):
            text = str(value).strip()
            if len(text) >= 4:
                out.append(text)
    if config is not None:
        cb = str(config("callback_number", "") or "").strip()
        if cb:
            out.append(cb)
    return out


def scrub(text: Any, call: dict | None = None, config: Any = None, limit: int = 240) -> str:
    """Single-line, length-capped text with sensitive values removed."""
    out = " ".join(str(text or "").split())
    for value in _sensitive_values(call or {}, config):
        out = out.replace(value, "[redacted]")
    out = consent.redact_text(out, set())
    return out[: limit - 1] + "…" if len(out) > limit else out


def _sentence(text: str) -> str:
    text = text.strip()
    return text if not text or text[-1] in ".!?" else text + "."


def agreed_text(result: dict, call: dict, config: Any = None) -> str:
    """"Tomorrow 19:00, party of 2, inside, under Winch. Confirmation B47." from ``agreed``."""
    agreed = result.get("agreed")
    if not isinstance(agreed, dict):
        return ""
    clean: dict[str, str] = {}
    for key, value in agreed.items():
        if value in (None, "", [], {}) or consent.classify_key(key):
            continue
        if consent.field_for_fact(key, value):
            continue
        clean[str(key).strip().lower().replace(" ", "_")] = scrub(value, call, config, 120)
    if not clean:
        return ""
    parts: list[str] = []
    when = " ".join(x for x in (clean.get("date"), clean.get("time")) if x)
    if when:
        parts.append(when)
    if clean.get("party_size"):
        parts.append(f"party of {clean['party_size']}")
    if clean.get("notes"):
        parts.append(clean["notes"])
    if clean.get("name"):
        parts.append(f"under {clean['name']}")
    if clean.get("price"):
        parts.append(f"price {clean['price']}")
    for key, value in clean.items():
        if key not in AGREED_ORDER and key not in _SKIP_KEYS:
            parts.append(f"{key.replace('_', ' ')} {value}")
    text = ", ".join(parts)
    if text:
        text = text[0].upper() + text[1:]
    code = clean.get("confirmation_number")
    out = _sentence(text) if text else ""
    if code:
        out = (out + " " if out else "") + f"Confirmation {code}."
    return out


def _reason_text(call: dict) -> str:
    reason = str(call.get("reason") or (call.get("result") or {}).get("reason") or "")
    if reason in REASON_TEXT:
        return REASON_TEXT[reason]
    return scrub(reason, call, limit=160) if reason and reason not in (
        "completed", "no_answer", "busy", "failed", "canceled", "aborted") else ""


# ---- the result message -----------------------------------------------------------------------
def result_message(call: dict, config: Any = None, *, store_transcripts: bool = True,
                   retried: bool = False, has_transcript: bool = True) -> str:
    """The one final message for a finished call. ``config`` is ``svc.config``-like."""
    cid = call["id"]
    outcome = effective_outcome(call)
    result = call.get("result") or {}
    dur = call_duration_s(call)
    next_steps = scrub(result.get("next_steps"), call, config) or "nothing"
    summary = scrub(result.get("summary"), call, config)
    facts = agreed_text(result, call, config)
    state = call.get("state", "")
    icon = ICONS.get(state if state in ("aborted", "canceled", "expired") else outcome, "ℹ️")

    if outcome in ("success", "partial", "failed", "needs_followup") and state in (
            states.COMPLETED, states.FAILED, states.ABORTED):
        why = _reason_text(call)
        nxt = f" Next: {next_steps.rstrip('.')}."
        if state == states.ABORTED:
            head = f"{ICONS['aborted']} Call ended early: {why or 'stopped'}."
            if facts or summary and not why:
                head += " " + _sentence(facts or summary)
        else:
            label = {"success": "Success", "partial": "Partly done", "failed": "Failed",
                     "needs_followup": "Needs follow-up"}[outcome]
            head = f"{icon} {label}. " + _sentence(facts or summary or why
                                                     or "No details were recorded")
        lines = [head + nxt + _tail(call, dur, config)]
    elif outcome == "no_answer":
        lines = [f"{icon} No answer after {_ring_seconds(call)}. Nobody picked up."]
    elif outcome == "busy":
        lines = [f"{icon} Line busy. The number was engaged."]
    elif outcome == "voicemail":
        left = bool((call.get("brief") or {}).get("limits", {}).get("voicemail_message"))
        lines = [f"{icon} Reached voicemail, "
                 + ("left a message." if left else "did not leave a message.")
                 + (f" Next: {next_steps}." if left and next_steps != "nothing" else "")
                 + _tail(call, dur, config)]
    elif state == states.CANCELED:
        why = _reason_text(call) or "Canceled before it was answered"
        lines = [f"{icon} Call canceled: {why}."]
    elif state == states.EXPIRED:
        lines = [f"{icon} Call expired: {_reason_text(call) or 'it was not approved in time'}."]
    else:
        lines = [f"{icon} Call ended ({state or outcome}): "
                 f"{_reason_text(call) or summary or 'no result recorded'}."]
    if outcome in RETRYABLE and state in states.TERMINAL:
        if retried:
            lines.append("Already retried once.")
        else:
            lines.append(f"Retry? Reply /phone retry {cid}")
    if not store_transcripts:
        lines.append("Transcript: not stored (store_transcripts is off).")
    elif has_transcript:
        lines.append(f"Transcript: /phone transcript {cid}")
    return "\n".join(lines)


def _tail(call: dict, dur: int | None, config: Any) -> str:
    if not dur:
        return ""
    cost = estimate_cost(call, dur, config)
    return f" {fmt_duration(dur)}, about ${cost:.2f}."


def _ring_seconds(call: dict) -> str:
    if call.get("ended_at") is not None and call.get("dialed_at") is not None:
        elapsed = max(0.0, call["ended_at"] - call["dialed_at"])
        return f"{max(5, int(round(elapsed / 5.0)) * 5)} s"
    return "the ring timeout"


def context_line(call: dict, config: Any = None, store_transcripts: bool = True,
                 has_transcript: bool = True) -> str:
    """The result text compacted into one data line for the agent's context."""
    text = result_message(call, config, store_transcripts=store_transcripts,
                          has_transcript=has_transcript)
    return " | ".join(text.splitlines())[:700]


# ---- transcripts ------------------------------------------------------------------------------
ROLE_LABEL = {"agent": "Agent", "callee": "Them", "assistant": "Agent", "user": "Them"}


def transcript_lines(rows: list[dict]) -> list[str]:
    out = []
    for row in rows:
        text = " ".join(str(row.get("text") or "").split())
        if text:
            out.append(f"{ROLE_LABEL.get(row.get('role'), str(row.get('role') or '?').title())}: "
                       f"{text}")
    return out


def chunk_lines(lines: list[str], limit: int = CHUNK_LIMIT) -> list[str]:
    """Pack lines into messages shorter than ``limit``; an over-long line is split on spaces."""
    pieces: list[str] = []
    for line in lines:
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(line[:cut])
            line = "  " + line[cut:].lstrip()
        pieces.append(line)
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + 1 + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def transcript_messages(call: dict, rows: list[dict], limit: int = CHUNK_LIMIT) -> list[str]:
    """Telegram-safe messages for a transcript, each prefixed with a part marker when split."""
    lines = transcript_lines(rows)
    if not lines:
        return []
    head = f"Transcript of call {call['id']} to {call['callee_name']}"
    dur = call_duration_s(call)
    if dur:
        head += f" ({fmt_duration(dur)})"
    room = limit - 80  # space for the header and the "(2/3)" marker
    chunks = chunk_lines(lines, room)
    total = len(chunks)
    out = []
    for i, chunk in enumerate(chunks, 1):
        mark = f" ({i}/{total})" if total > 1 else ""
        out.append(f"{head}{mark}:\n{chunk}" if i == 1 or total > 1 else chunk)
    return out


# ---- call log ---------------------------------------------------------------------------------
OUTCOME_WORD = {"no_answer": "no answer", "needs_followup": "needs follow-up"}


def log_line(call: dict) -> str:
    when = datetime.fromtimestamp(call["created_at"], UTC).strftime("%b %d %H:%M")
    outcome = effective_outcome(call)
    if call.get("state") not in states.TERMINAL:
        outcome = str(call.get("state"))
    word = OUTCOME_WORD.get(outcome, outcome).replace("_", " ")
    dur = call_duration_s(call) if call.get("state") in states.TERMINAL else None
    dur_text = fmt_duration(dur) if dur and outcome not in ("no_answer", "busy") else "-"
    return f"{when} UTC  {call['callee_name']}  {word}  {dur_text}  {call['id']}"


_SLUG = re.compile(r"\s+")


def late_answer_facts(store: Any, call_id: str) -> dict[str, str]:
    """Answers the user gave after the question timed out, keyed by the question text."""
    facts: dict[str, str] = {}
    for q in store.questions_for_call(call_id):
        if q["status"] == "late" and (q.get("answer") or "").strip():
            key = _SLUG.sub(" ", str(q["question"])).strip()[:80] or q["id"]
            facts[key] = str(q["answer"]).strip()
    return facts
