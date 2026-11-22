"""Brief builder, opening line, cost estimate, voice payload and approval card text."""
from __future__ import annotations

import math
import re
from typing import Any

from . import consent

DISCLOSURE = "Hi, this is an AI assistant calling on behalf of {user_name}."
RECORDED = "This call may be recorded."
DEFAULT_MAX_DURATION_S = 300
DEFAULT_QUESTION_TIMEOUT_S = 75
MIN_QUESTION_TIMEOUT_S = 30
MAX_QUESTION_TIMEOUT_S = 120
HARD_MAX_DURATION_S = 900
MIN_DURATION_S = 30
COST_PER_MINUTE_USD = 0.25  # rough Twilio + Realtime estimate, shown as an estimate only
NAME_MAX = 80
# Who the call is for, as a business hears it: never a placeholder ("calling on behalf of my user" sounds robotic, #2872).
_GENERIC_NAME_RE = re.compile(
    r"(?i)^\s*(?:(?:my|the|a|an|our|your|their|his|her)\s+)?"
    r"(?:(?:user|customer|client|owner|boss|employer)(?:'s|s')?\s+)?(?:local\s+|small\s+)?"
    r"(?:business\s+|company\s+)?"
    r"(?:user|customer|client|owner|person|someone|somebody|business|company|caller|principal|boss|employer|human|"
    r"me|them|him|her|unknown|n/?a|none|null|wife|husband|partner|spouse|mom|mum|dad|mother|father|parent|family|"
    r"friend|son|daughter|kid|child|manager|team|office|store|shop|household|account holder|patient|member)s?\s*$")
NAME_FACTS = ("business_name", "name", "booking_name", "full_name", "owner_name")
WONT_SHARE_ALWAYS = "card data, address, ID numbers, anything not listed above"


def is_generic_name(name: Any) -> bool:
    return not isinstance(name, str) or not " ".join(name.split()) or bool(_GENERIC_NAME_RE.match(name))


def resolve_name(*candidates: Any, facts: dict | None = None) -> str:
    """The first real person or business name among the candidates, then the facts' name fields; "" when none."""
    for value in (*candidates, *((facts or {}).get(k) for k in NAME_FACTS)):
        if not is_generic_name(value):
            return " ".join(str(value).split())[:NAME_MAX]
    return ""


def opening_line(user_name: str, record: bool = False) -> str:
    """First thing the callee hears. AI disclosure is always the first sentence, naming who the call is for."""
    if is_generic_name(user_name):
        raise ValueError("on_behalf_of is required: the person or business this call is for, by name")
    line = DISCLOSURE.format(user_name=" ".join(user_name.split())[:NAME_MAX])
    if record:
        line += " " + RECORDED
    return line


_DISCLOSURE_RE = re.compile(
    r"(?-i:\bAI\b|\bA\.I\.)|artificial intelligence|virtual assistant|automated assistant"
    r"|digital assistant", re.I)


def disclosure_in(text: str) -> bool:
    """Does this agent line contain the AI disclosure (SPEC criterion 4)?"""
    return bool(_DISCLOSURE_RE.search(text or ""))


def clamp_duration(value: Any, default: int = DEFAULT_MAX_DURATION_S) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(MIN_DURATION_S, min(HARD_MAX_DURATION_S, n))


def clamp_question_timeout(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = DEFAULT_QUESTION_TIMEOUT_S
    return max(MIN_QUESTION_TIMEOUT_S, min(MAX_QUESTION_TIMEOUT_S, n))


def estimate_cost_usd(duration_s: int) -> float:
    return round(math.ceil(duration_s / 60) * COST_PER_MINUTE_USD, 2)


def build_brief(*, callee_name: str, number: str, number_source: str, goal: str,
                facts: dict[str, Any], sensitive_requested: list[str], granted: set[str],
                user_name: str, max_duration_s: int, record: bool, leave_voicemail: bool,
                language: str, quiet_hours_override: bool,
                question_timeout_s: int = DEFAULT_QUESTION_TIMEOUT_S,
                inquiry_only: bool = False) -> dict:
    safe, withheld = consent.build_safe_facts(facts, granted)
    needs_consent = [f for f in sensitive_requested if f not in granted]
    return {
        "who": callee_name,
        "number": number,
        "number_source": number_source or "unspecified",
        "goal": consent.redact_text(goal, granted),
        "opening_line": opening_line(user_name, record),
        "will_share": safe,
        "wont_share": [WONT_SHARE_ALWAYS, *withheld],
        "needs_consent": needs_consent,
        "limits": {
            "max_duration_s": max_duration_s,
            "max_cost_estimate_usd": estimate_cost_usd(max_duration_s),
            "recording": bool(record),
            "voicemail_message": bool(leave_voicemail),
            "question_timeout_s": clamp_question_timeout(question_timeout_s),
        },
        "language": language,
        "quiet_hours_override": bool(quiet_hours_override),
        "inquiry_only": bool(inquiry_only),
        "user_name": " ".join(user_name.split())[:NAME_MAX],
    }


def refresh_brief(call: dict, granted: set[str]) -> dict:
    """Recompute the public parts of a stored brief after consent changed. Returns the new brief
    (without raw facts); the caller persists it merged with the raw data."""
    old = call["brief"]
    new = _rebuild(call, old, granted)
    if old["limits"].get("max_cost_estimate_usd", 0) is None:  # remote mode: no plugin estimate
        new["limits"]["max_cost_estimate_usd"] = None
    return new


def _rebuild(call: dict, old: dict, granted: set[str]) -> dict:
    # A card prepared before names were required may hold the old "my user": use a name fact, or ask for a new card.
    name = resolve_name(old.get("user_name"), facts=old.get("facts_raw") or {})
    if not name:
        raise ValueError("This card does not say who the call is for. Ask me to prepare the call again with your name.")
    return build_brief(
        callee_name=call["callee_name"], number=call["number"],
        number_source=call.get("number_source") or "", goal=call["goal"],
        facts=old.get("facts_raw", {}), sensitive_requested=old.get("sensitive_requested", []),
        granted=granted, user_name=name,
        max_duration_s=old["limits"]["max_duration_s"], record=old["limits"]["recording"],
        leave_voicemail=old["limits"]["voicemail_message"], language=old.get("language", "en"),
        quiet_hours_override=old.get("quiet_hours_override", False),
        question_timeout_s=old["limits"].get("question_timeout_s", DEFAULT_QUESTION_TIMEOUT_S),
        inquiry_only=bool(old.get("inquiry_only")))


def voice_payload(call: dict, granted: set[str]) -> dict:
    """Everything the voice model may ever see for a call. Built only from consent-filtered data.

    P2 must build the Realtime session instructions and tool outputs from this dict only.
    """
    brief = call["brief"]
    raw_facts = brief.get("facts_raw", {})
    safe, _ = consent.build_safe_facts(raw_facts, granted)
    return {
        "call_id": call["id"],
        "callee_name": call["callee_name"],
        "goal": consent.redact_text(call["goal"], granted),
        "opening_line": brief["opening_line"],
        "facts": safe,
        "language": brief.get("language", "en"),
        "max_duration_s": brief["limits"]["max_duration_s"],
        "record": brief["limits"]["recording"],
        "leave_voicemail": brief["limits"]["voicemail_message"],
        "user_name": brief["user_name"],
        "inquiry_only": bool(brief.get("inquiry_only")),
    }


def format_card(brief: dict, call_id: str) -> str:
    lim = brief["limits"]
    lines = [
        f"Call brief {call_id}",
        f"Who: {brief['who']}",
        f"Number: {brief['number']} (source: {brief['number_source']})",
        f"Goal: {brief['goal']}",
        f'Opening line: "{brief["opening_line"]}"',
    ]
    if brief["will_share"]:
        shared = "; ".join(f"{k}: {_fmt(v)}" for k, v in brief["will_share"].items())
    else:
        shared = "nothing beyond the goal"
    lines.append(f"Will share: {shared}")
    lines.append("Won't share: " + "; ".join(brief["wont_share"]))
    if brief["needs_consent"]:
        lines.append("Needs your consent to share: " + ", ".join(brief["needs_consent"])
                     + " (left out unless you allow it when asked during the call)")
    cost = lim.get("max_cost_estimate_usd")
    lines.append(
        f"Limits: max {lim['max_duration_s']} s, "
        + (f"est. cost up to ${cost:.2f}, " if cost is not None else "")
        + f"recording {'on' if lim['recording'] else 'off'}"
    )
    if brief["quiet_hours_override"]:
        lines.append("Quiet hours override: yes")
    if brief.get("inquiry_only"):
        lines.append("Inquiry only: the agent asks and listens; it books or agrees to nothing")
    lines.append('Reply "call" to dial or "cancel".')
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return value if isinstance(value, str) else str(value)
