"""Per-call consent model for sensitive fields.

Nothing sensitive may reach the voice model (instructions or tool output) unless the user
granted consent for that specific field on that specific call. ``cvv``, ``password`` and
``otp`` are never shared, consent or not (v1 rule).
"""
from __future__ import annotations

import re
from typing import Any

SENSITIVE_FIELDS = ("card", "address", "id_number", "bank", "email", "phone")
FORBIDDEN_FIELDS = ("cvv", "password", "otp")

# Key-name patterns -> field. Forbidden fields are checked first.
_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cvv", re.compile(r"cvv|cvc|csc|security[_\s-]?code|card[_\s-]?verif")),
    ("otp", re.compile(r"otp|one[_\s-]?time|2fa|mfa|verification[_\s-]?code"
                       r"|auth(entication)?[_\s-]?code|sms[_\s-]?code")),
    ("password", re.compile(r"passw|passcode|\bpin\b|secret|credential|api[_\s-]?key|token")),
    ("card", re.compile(r"card|credit|debit|\bcc\b|\bpan\b|expir")),
    ("id_number", re.compile(r"passport|ssn|social[_\s-]?security|national[_\s-]?id|tax[_\s-]?id"
                             r"|\bid[_\s-]?(number|num|no)\b|\bid\b|licen[cs]e|\bnin\b|\bitin\b")),
    ("bank", re.compile(r"bank|iban|routing|swift|\bbic\b|sort[_\s-]?code"
                        r"|account[_\s-]?(number|no|num)")),
    ("address", re.compile(r"address|street|\bzip\b|postcode|postal|\bcity\b|apartment|\bapt\b")),
    ("email", re.compile(r"e[_\s-]?mail")),
    ("phone", re.compile(r"phone|mobile|\btel\b|\bcell\b|callback|fax")),
)

# Value patterns: catch sensitive data hiding under an innocent key or inside free text.
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card_matches(text: str) -> list[str]:
    out = []
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            out.append(m.group(0))
    return out


def classify_key(key: object) -> str | None:
    """Map a fact key such as ``card_number`` or ``home_address`` to a field, or None."""
    k = str(key).lower()
    for field, pattern in _KEY_PATTERNS:
        if pattern.search(k):
            return field
    return None


def classify_text(text: str) -> set[str]:
    """Fields detected by value patterns in free text."""
    found: set[str] = set()
    if _card_matches(text):
        found.add("card")
    if _EMAIL_RE.search(text):
        found.add("email")
    if _SSN_RE.search(text):
        found.add("id_number")
    if _IBAN_RE.search(text):
        found.add("bank")
    return found


def redact_text(text: str, granted: set[str]) -> str:
    """Remove value-pattern matches for fields without consent (used on the free-text goal)."""
    out = text
    if "card" not in granted:
        for m in _card_matches(out):
            out = out.replace(m, "[redacted]")
    if "email" not in granted:
        out = _EMAIL_RE.sub("[redacted]", out)
    if "id_number" not in granted:
        out = _SSN_RE.sub("[redacted]", out)
    if "bank" not in granted:
        out = _IBAN_RE.sub("[redacted]", out)
    return out


def _flatten(value: Any) -> list[str]:
    if isinstance(value, dict):
        out: list[str] = []
        for k, v in value.items():
            out.append(str(k))
            out.extend(_flatten(v))
        return out
    if isinstance(value, (list, tuple, set)):
        return [s for v in value for s in _flatten(v)]
    return [str(value)]


def field_for_fact(key: object, value: Any) -> str | None:
    """Most restrictive field a fact belongs to (forbidden beats sensitive), or None."""
    by_key = classify_key(key)
    if by_key in FORBIDDEN_FIELDS:
        return by_key
    detected = by_key
    if isinstance(value, dict):  # nested facts: classify children, any forbidden wins
        for k, v in value.items():
            sub = field_for_fact(k, v)
            if sub in FORBIDDEN_FIELDS:
                return sub
            detected = detected or sub
        return detected
    if isinstance(value, (list, tuple, set)):
        for v in value:
            sub = field_for_fact(key, v)
            if sub in FORBIDDEN_FIELDS:
                return sub
            detected = detected or sub
        return detected
    if detected is None:
        found = classify_text(str(value))
        detected = sorted(found)[0] if found else None
    return detected


def build_safe_facts(facts: dict[str, Any] | None, granted: set[str] | frozenset[str] = frozenset()
                     ) -> tuple[dict[str, Any], list[str]]:
    """Return ``(safe_facts, withheld)``.

    ``withheld`` lists ``"<key> (<field>)"`` entries, never the values. Forbidden fields are
    dropped even when consent is present.
    """
    safe: dict[str, Any] = {}
    withheld: list[str] = []
    for key, value in (facts or {}).items():
        field = field_for_fact(key, value)
        if field in FORBIDDEN_FIELDS:
            withheld.append(f"{key} ({field}, never shared)")
        elif field is not None and field not in granted:
            withheld.append(f"{key} ({field}, no consent)")
        else:
            safe[str(key)] = value
    return safe, withheld


def is_forbidden_field(name: str) -> bool:
    return name.strip().lower() in FORBIDDEN_FIELDS


def grantable(name: str) -> bool:
    return name.strip().lower() in SENSITIVE_FIELDS


# ---- mid-call questions -------------------------------------------------------------------
# Question text that clearly asks for sensitive data. Forbidden kinds are checked first.
_QUESTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cvv", re.compile(r"\b(cvv2?|cvc2?|csc)\b|security code|card verification", re.I)),
    ("otp", re.compile(r"\botp\b|one[- ]time (pass)?(code|password|pin)|verification code"
                       r"|(sms|text|2fa|authentication|auth) code|\b2fa\b", re.I)),
    ("password", re.compile(r"passw(or)?d|passcode|\bpin( code| number)?\b|security answer",
                            re.I)),
    ("card", re.compile(r"card number|credit card|debit card|payment card|expir(y|ation) date"
                        r"|\bcard\b.{0,20}\b(details|info|number)", re.I)),
    ("id_number", re.compile(r"passport|social security|\bssn\b|\bnin\b|id (number|card)"
                             r"|national id|identity (number|card)|driver'?s licen[cs]e"
                             r"|tax (id|number)", re.I)),
    ("bank", re.compile(r"\bbank\b|\biban\b|routing number|account number|sort code|\bswift\b",
                        re.I)),
    ("address", re.compile(r"(home|street|billing|delivery|mailing|postal|your|full) address"
                           r"|\bzip ?code\b|\bpostcode\b|where do you live", re.I)),
)


def classify_question(field: str, question: str) -> str | None:
    """Most restrictive sensitive/forbidden field a mid-call question asks for, or None.

    Looks at the short ``field`` key the model chose and at the question text, so a model that
    labels a CVV request as ``notes`` is still caught.
    """
    candidates = [classify_key(field)] if field else []
    for name, pattern in _QUESTION_PATTERNS:
        if pattern.search(question or ""):
            candidates.append(name)
    for forbidden in FORBIDDEN_FIELDS:
        if forbidden in candidates:
            return forbidden
    for name in SENSITIVE_FIELDS:
        if name in candidates:
            return name
    return None


def text_fields(text: str, granted: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """Sensitive fields present in ``text`` that are not covered by ``granted``."""
    return {f for f in classify_text(text) if f not in granted}
