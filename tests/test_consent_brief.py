from __future__ import annotations

import pytest

from hermes_phone import brief, consent


@pytest.mark.parametrize("key,field", [
    ("card_number", "card"), ("credit_card", "card"), ("home_address", "address"),
    ("passport_number", "id_number"), ("ssn", "id_number"), ("iban", "bank"),
    ("bank_account_number", "bank"), ("email", "email"), ("mobile_phone", "phone"),
    ("cvv", "cvv"), ("CVC", "cvv"), ("security_code", "cvv"), ("password", "password"),
    ("pin", "password"), ("otp", "otp"), ("verification_code", "otp"), ("name", None),
    ("party_size", None), ("time", None), ("call_id", None),
])
def test_classify_key(key, field):
    assert consent.classify_key(key) == field


def test_value_patterns_catch_disguised_data():
    assert consent.field_for_fact("note", "4111 1111 1111 1111") == "card"
    assert consent.field_for_fact("note", "me@example.com") == "email"
    assert consent.field_for_fact("note", "123-45-6789") == "id_number"
    assert consent.field_for_fact("note", "GB82WEST12345698765432") == "bank"
    assert consent.field_for_fact("note", "table for 4 at 20:00") is None
    assert consent.field_for_fact("order", "1234 5678") is None  # fails Luhn / too short


def test_forbidden_wins_over_consent():
    safe, withheld = consent.build_safe_facts(
        {"cvv": "123", "otp": "1", "password": "p", "name": "W"},
        {"card", "address", "id_number", "bank", "email", "phone", "cvv", "otp", "password"})
    assert safe == {"name": "W"}
    assert len(withheld) == 3 and all("never shared" in w for w in withheld)


def test_nested_forbidden_detected():
    assert consent.field_for_fact("extra", {"inner": {"cvv": "1"}}) == "cvv"
    assert consent.field_for_fact("extra", [{"card_number": "x"}]) == "card"


def test_safe_facts_with_grant():
    safe, withheld = consent.build_safe_facts({"home_address": "1 Main St", "name": "W"})
    assert safe == {"name": "W"} and "no consent" in withheld[0]
    assert "1 Main St" not in " ".join(withheld)
    safe, _ = consent.build_safe_facts({"home_address": "1 Main St"}, {"address"})
    assert safe == {"home_address": "1 Main St"}


def test_redact_text():
    text = "card 4111 1111 1111 1111 mail a@b.co ssn 123-45-6789"
    out = consent.redact_text(text, set())
    assert "4111" not in out and "a@b.co" not in out and "123-45-6789" not in out
    assert "4111" in consent.redact_text(text, {"card"})


def test_opening_line():
    assert brief.opening_line("Winch") == "Hi, this is an AI assistant calling on behalf of Winch."
    assert brief.opening_line("Winch", True).endswith(" This call may be recorded.")
    assert brief.opening_line("").startswith("Hi, this is an AI assistant")
    assert brief.opening_line(None).startswith("Hi, this is an AI assistant")


def test_clamp_and_cost():
    assert brief.clamp_duration("abc") == 300
    assert brief.clamp_duration(10_000) == 900
    assert brief.estimate_cost_usd(300) == 1.25


def test_card_contains_spec_fields():
    b = brief.build_brief(callee_name="Bella", number="+12125550123", number_source="user",
                          goal="Book", facts={"name": "W", "card": "4111 1111 1111 1111"},
                          sensitive_requested=["card"], granted=set(), user_name="Winch",
                          max_duration_s=300, record=False, leave_voicemail=False,
                          language="en", quiet_hours_override=False)
    card = brief.format_card(b, "c1")
    for part in ("Who: Bella", "Number: +12125550123", "Goal: Book", "Opening line:",
                 "Will share: name: W", "Won't share:", "Limits:", "Reply \"call\" to dial"):
        assert part in card
    assert "4111" not in card
