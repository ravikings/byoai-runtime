"""Tests for the recorder's device-side payload-mode redaction (spec §7)."""

from __future__ import annotations

import hashlib
import json

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.redact import PayloadMode, apply_payload_mode, redact_free_text

SALT = "salt-abc123"


def test_full_mode_returns_payload_unchanged():
    payload = {"command": "ls -la", "count": 3}
    out = apply_payload_mode(payload, PayloadMode.FULL, session_salt=SALT)
    assert out == payload
    assert out is payload


def test_hash_only_mode_returns_empty_dict():
    payload = {"api_key": "sk-abcdefghijklmnop", "email": "user@example.com"}
    out = apply_payload_mode(payload, PayloadMode.HASH_ONLY, session_salt=SALT)
    assert out == {}


def test_redacted_mode_masks_api_key():
    payload = {"auth_token": "sk-abcdefghijklmnopqrstuvwxyz012345"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["auth_token"] == "[REDACTED:api_key]"


def test_redacted_mode_masks_bearer_token():
    payload = {"header": "Bearer abc123def456ghi789"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["header"] == "[REDACTED:api_key]"


def test_redacted_mode_masks_generic_high_entropy_secret_keyed_field():
    payload = {"api_key": "aB3xY9qZ7wL2mN8pR4sT6vU1yC5eG0hJ"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["api_key"] == "[REDACTED:api_key]"


def test_redacted_mode_masks_email():
    payload = {"contact": "someone@example.com"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["contact"] == "[REDACTED:email]"


def test_redacted_mode_masks_ssn():
    payload = {"note": "123-45-6789"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["note"] == "[REDACTED:ssn]"


def test_redacted_mode_masks_credit_card():
    # 4111111111111111 is a well-known Luhn-valid test Visa number.
    payload = {"card": "4111111111111111"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["card"] == "[REDACTED:credit_card]"


def test_redacted_mode_does_not_flag_luhn_invalid_digit_run_as_credit_card():
    payload = {"card": "1234567890123456"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["card"] != "[REDACTED:credit_card]"


def test_redacted_mode_masks_aws_key():
    payload = {"access_key_id": "AKIAABCDEFGHIJKLMNOP"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["access_key_id"] == "[REDACTED:aws_key]"


def test_redacted_mode_salted_hashes_low_entropy_bool():
    payload = {"active": True}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    expected = f"[REDACTED:hash:{hashlib.sha256(f'{SALT}:True'.encode()).hexdigest()[:16]}]"
    assert out["active"] == expected


# ---------------------------------------------------------- redact_free_text


def test_redact_free_text_leaves_plain_prose_unchanged():
    text = "Application app_5510 for Priya Kestrel has been approved."
    assert redact_free_text(text) == text


def test_redact_free_text_masks_embedded_email():
    text = "Contact the applicant at priya.kestrel@example.com for follow-up."
    out = redact_free_text(text)
    assert "priya.kestrel@example.com" not in out
    assert "[REDACTED:email]" in out
    # Only the matched span is replaced, not the whole string.
    assert out.startswith("Contact the applicant at ")
    assert out.endswith(" for follow-up.")


def test_redact_free_text_masks_embedded_ssn():
    text = "SSN on file: 123-45-6789, confirmed against the document."
    out = redact_free_text(text)
    assert "123-45-6789" not in out
    assert "[REDACTED:ssn]" in out


def test_redact_free_text_masks_embedded_luhn_valid_credit_card():
    text = "Card ending in 4242 was charged: 4111111111111111 declined."
    out = redact_free_text(text)
    assert "4111111111111111" not in out
    assert "[REDACTED:credit_card]" in out


def test_redact_free_text_does_not_flag_luhn_invalid_digit_run():
    text = "Reference number 1234567890123456 was logged."
    out = redact_free_text(text)
    assert "1234567890123456" in out
    assert "[REDACTED:credit_card]" not in out


def test_redact_free_text_masks_embedded_api_key():
    text = "Rotate the leaked key sk-abcdefghijklmnopqrstuvwxyz012345 immediately."
    out = redact_free_text(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "[REDACTED:api_key]" in out


def test_redact_free_text_masks_multiple_spans_in_one_string():
    text = "Reach user@example.com or check SSN 123-45-6789 before releasing funds."
    out = redact_free_text(text)
    assert out.count("[REDACTED:") == 2
    assert "user@example.com" not in out
    assert "123-45-6789" not in out
    assert "before releasing funds." in out


def test_redact_free_text_empty_string_is_unchanged():
    assert redact_free_text("") == ""


def test_redacted_mode_salted_hashes_low_entropy_int():
    payload = {"count": 42}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    expected = f"[REDACTED:hash:{hashlib.sha256(f'{SALT}:42'.encode()).hexdigest()[:16]}]"
    assert out["count"] == expected


def test_redacted_mode_salted_hashes_short_string():
    payload = {"code": "ab12"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    expected = f"[REDACTED:hash:{hashlib.sha256(f'{SALT}:ab12'.encode()).hexdigest()[:16]}]"
    assert out["code"] == expected


def test_equal_low_entropy_values_hash_identically_within_same_salt():
    payload = {"a": 7, "b": 7}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["a"] == out["b"]


def test_equal_low_entropy_values_hash_differently_with_different_salt():
    payload = {"a": 7}
    out1 = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt="salt-one")
    out2 = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt="salt-two")
    assert out1["a"] != out2["a"]


def test_redacted_mode_walks_nested_dicts_and_lists():
    payload = {
        "outer": {
            "inner_email": "nested@example.com",
            "items": [{"ssn": "123-45-6789"}, {"count": 1}],
        }
    }
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert out["outer"]["inner_email"] == "[REDACTED:email]"
    assert out["outer"]["items"][0]["ssn"] == "[REDACTED:ssn]"
    assert (
        out["outer"]["items"][1]["count"]
        == apply_payload_mode({"count": 1}, PayloadMode.REDACTED, session_salt=SALT)["count"]
    )


def test_redacted_mode_never_mutates_dict_keys():
    payload = {"user@example.com": "value"}
    out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert "user@example.com" in out
    assert out["user@example.com"] != "user@example.com"


def test_redacted_mode_does_not_mutate_original_payload():
    payload = {"email": "someone@example.com", "nested": {"count": 1}}
    original = {"email": "someone@example.com", "nested": {"count": 1}}
    apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT)
    assert payload == original


def test_payload_hash_identical_regardless_of_mode():
    raw_payload = {
        "command": "curl https://example.com",
        "api_key": "sk-abcdefghijklmnopqrstuvwxyz012345",
        "email": "user@example.com",
        "count": 3,
    }
    raw_hash = sha256_hex(canonicalize(raw_payload))

    for mode in PayloadMode:
        # Simulate the real wiring: hash the raw payload first, then decide
        # what ships. The hash must never depend on the shipped payload.
        computed_hash = sha256_hex(canonicalize(raw_payload))
        _ = apply_payload_mode(raw_payload, mode, session_salt=SALT)
        assert computed_hash == raw_hash


def test_an_unknown_kind_gets_no_passthrough():
    """The vocabulary allowlist is opt-in per kind, and the default is the
    stricter behaviour: a caller that doesn't name a kind, or names one with no
    entry, must not accidentally inherit another kind's exemptions."""
    payload = {"action": "INTERVENED", "categories": [{"name": "LegalAdvice"}]}
    for kind in (None, "tool_use", "message"):
        out = apply_payload_mode(payload, PayloadMode.REDACTED, session_salt=SALT, kind=kind)
        assert out["action"] != "INTERVENED"
        assert out["categories"][0]["name"] != "LegalAdvice"


def test_only_the_allowlisted_keys_of_a_guardrail_survive():
    """The raw assessments are NOT allowlisted, because they can carry the
    matched span of text — for a PII entity, the address itself."""
    payload = {
        "action": "INTERVENED",
        "stage": "output",
        "categories": [{"policy": "pii", "name": "EMAIL", "action": "ANONYMIZED"}],
        "output_assessments": [
            {"sensitiveInformationPolicy": {"piiEntities": [{"match": "a@b.test"}]}}
        ],
        "bedrock_trace_id": "demo-4",
    }
    out = apply_payload_mode(
        payload, PayloadMode.REDACTED, session_salt=SALT, kind="guardrail_intervention"
    )
    assert out["action"] == "INTERVENED"
    assert out["stage"] == "output"
    assert out["categories"] == payload["categories"]
    assert "a@b.test" not in json.dumps(out["output_assessments"])
    # Not allowlisted either — an id is not vocabulary a reviewer reads.
    assert out["bedrock_trace_id"] != "demo-4"


def test_hash_only_still_ships_nothing_for_a_guardrail():
    """The allowlist is a REDACTED-mode concession. An operator who chose
    hash-only asked for no payload bytes at all, and this must not carve an
    exception into that."""
    payload = {"action": "INTERVENED", "categories": [{"name": "LegalAdvice"}]}
    out = apply_payload_mode(
        payload, PayloadMode.HASH_ONLY, session_salt=SALT, kind="guardrail_intervention"
    )
    assert out == {}
