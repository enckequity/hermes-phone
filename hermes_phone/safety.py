"""Hard safety rules: E.164 normalisation, blocklist, allowlist, quiet hours, rate limits.

Everything here is fail-closed. ``phonenumbers`` is imported lazily so that ``register()``
stays cheap.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from . import states
from .store import Store

ALLOWLIST_ENV = "HERMES_PHONE_TEST_ALLOWLIST"
ALLOW_ALL_ENV = "HERMES_PHONE_ALLOW_ALL"  # developer-only; the model cannot set env vars

EMERGENCY_NUMBERS = frozenset({
    "911", "112", "999", "000", "110", "119", "100", "101", "102", "103", "104", "105",
    "106", "107", "108", "109", "933", "988",
})
MIN_NATIONAL_DIGITS = 6
QUIET_START_HOUR = 8
QUIET_END_HOUR = 21
MIN_GAP_SAME_NUMBER_S = 60
DEFAULT_DAILY_LIMIT = 10

_TEL_PREFIX = re.compile(r"^\s*tel:\s*", re.IGNORECASE)
_EXT = re.compile(r"(;ext=|;|\bext\.?|\bx)\s*\d+\s*$", re.IGNORECASE)


@lru_cache(maxsize=1)
def _pn():
    import phonenumbers  # lazy

    return phonenumbers


@dataclass(frozen=True)
class Check:
    ok: bool
    reason: str = ""


OK = Check(True)


def _clean(raw: object) -> str:
    text = "" if raw is None else str(raw)
    text = _TEL_PREFIX.sub("", text)
    text = _EXT.sub("", text.strip())
    return text


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _candidates(raw: object) -> set[str]:
    """Every national-digit string the input could plausibly mean, for blocklist matching."""
    text = _clean(raw)
    digits = _digits(text)
    cands = {digits, digits.lstrip("0")}
    if text.startswith("+") or (digits.startswith("00") and len(digits) >= 8):
        intl = digits.startswith("00") and not text.startswith("+")
        probe = "+" + (digits[2:] if intl else digits)
        try:
            parsed = _pn().parse(probe, None)
            cands.add(str(parsed.national_number))
        except Exception:  # noqa: BLE001
            pass
    if len(digits) == 11 and digits.startswith("1"):
        cands.add(digits[1:])
    cands.discard("")
    return cands


def is_blocked(raw: object) -> Check:
    """Hard blocklist. Returns ``Check(False, reason)`` when the number must never be dialed."""
    text = _clean(raw)
    if not _digits(text):
        return Check(False, "No digits in the phone number.")
    cands = _candidates(raw)
    for cand in cands | {_digits(text)}:
        if cand in EMERGENCY_NUMBERS:
            return Check(False, "This is an emergency number. hermes-phone never dials it.")
        if len(cand) < MIN_NATIONAL_DIGITS:
            return Check(False, "Short codes and emergency-length numbers are blocked.")
    for cand in cands:
        if len(cand) == 10 and cand[:3] in {"900", "976"}:
            return Check(False, "Premium-rate numbers (1-900 / 1-976) are blocked.")
    try:
        pn = _pn()
        parsed = _parse(raw)
        if parsed is not None and pn.number_type(parsed) == pn.PhoneNumberType.PREMIUM_RATE:
            return Check(False, "Premium-rate numbers are blocked.")
    except Exception:  # noqa: BLE001
        pass
    return OK


def _parse(raw: object):
    text = _clean(raw)
    digits = _digits(text)
    if text.startswith("+"):
        probe = "+" + digits
    elif digits.startswith("00") and len(digits) >= 8:
        probe = "+" + digits[2:]
    elif len(digits) == 11 and digits.startswith("1"):
        probe = "+" + digits
    else:
        return None
    parsed = _pn().parse(probe, None)
    return parsed


def normalize_e164(raw: object) -> tuple[str | None, str]:
    """Return ``(e164, "")`` or ``(None, reason)``. Does NOT apply the blocklist."""
    try:
        parsed = _parse(raw)
        if parsed is None:
            return None, "Number must be in international E.164 format, e.g. +14155550123."
        pn = _pn()
        if not pn.is_possible_number(parsed):
            return None, "That does not look like a possible phone number."
        return pn.format_number(parsed, pn.PhoneNumberFormat.E164), ""
    except Exception:  # noqa: BLE001
        return None, "Could not parse the phone number."


def validate_number(raw: object) -> tuple[str | None, str]:
    """Blocklist first, then normalisation. The single entry point used by tools."""
    blocked = is_blocked(raw)
    if not blocked.ok:
        return None, blocked.reason
    e164, reason = normalize_e164(raw)
    if e164 is None:
        return None, reason
    # Re-check the normalised form (defence in depth against parser tricks).
    blocked = is_blocked(e164)
    if not blocked.ok:
        return None, blocked.reason
    return e164, ""


# -- allowlist ---------------------------------------------------------------------------
def parse_allowlist(value: str | None) -> set[str]:
    out: set[str] = set()
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        e164, _ = normalize_e164(item)
        if e164:
            out.add(e164)
    return out


def check_allowlist(e164: str, environ: Mapping[str, str] | None = None) -> Check:
    """Fail closed: an unset or empty allowlist refuses every number.

    The only way to disable it is the developer exporting ``HERMES_PHONE_ALLOW_ALL=1``.
    """
    env = os.environ if environ is None else environ
    if env.get(ALLOW_ALL_ENV, "").strip() == "1":
        return OK
    allowed = parse_allowlist(env.get(ALLOWLIST_ENV))
    if not allowed:
        return Check(False, f"Dialing is disabled: {ALLOWLIST_ENV} is unset or empty. "
                            "The developer must allowlist a test number first.")
    if e164 not in allowed:
        return Check(False, "That number is not on the developer allowlist.")
    return OK


# -- quiet hours -------------------------------------------------------------------------
def check_quiet_hours(e164: str, now: datetime | None = None, override: bool = False,
                      fallback_tz: str = "") -> Check:
    """Refuse outside 08:00 to 21:00 callee local time.

    Fails closed: unknown zones refuse (unless ``fallback_tz`` is given, which is then used), and
    all candidate zones must agree.
    """
    if override:
        return OK
    from zoneinfo import ZoneInfo

    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    try:
        from phonenumbers import timezone as pn_timezone  # lazy

        zones = [z for z in pn_timezone.time_zones_for_number(_pn().parse(e164, None))
                 if z and not z.startswith("Etc/Unknown")]
    except Exception:  # noqa: BLE001
        zones = []
    if not zones and fallback_tz:
        zones = [fallback_tz]
    if not zones:  # fail closed: cannot prove it is business hours
        return Check(False, "Quiet hours: the callee's time zone is unknown. "
                            "Ask the user to confirm the time and override.")
    for zone in zones:
        try:
            hour = now.astimezone(ZoneInfo(zone)).hour
        except Exception:  # noqa: BLE001
            return Check(False, f"Quiet hours: unknown time zone {zone}. Ask for an override.")
        if not (QUIET_START_HOUR <= hour < QUIET_END_HOUR):
            return Check(False, f"Quiet hours: it is outside {QUIET_START_HOUR:02d}:00 to "
                                f"{QUIET_END_HOUR:02d}:00 for the callee ({zone}). "
                                "Ask the user to override or schedule it later.")
    return OK


# -- rate limits -------------------------------------------------------------------------
def check_rate_limits(store: Store, e164: str, *, daily_limit: int = DEFAULT_DAILY_LIMIT,
                      exclude_call_id: str = "") -> Check:
    now = store.clock()
    for call in store.calls_in_states(states.LIVE):
        if call["id"] != exclude_call_id:
            return Check(False, "Another call is already active (max 1 active call).")
    if store.count_dialed_since(now - 86400) >= daily_limit:
        return Check(False, f"Daily call limit reached ({daily_limit} per 24 h).")
    last = store.last_dialed_to(e164)
    if last is not None and now - last < MIN_GAP_SAME_NUMBER_S:
        wait = int(MIN_GAP_SAME_NUMBER_S - (now - last)) + 1
        return Check(False, f"Called this number moments ago. Wait {wait} s before retrying.")
    return OK
