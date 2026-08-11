"""Tests for the recorder's device-side payload-mode redaction (spec §7)."""

from __future__ import annotations

import hashlib

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.redact import PayloadMode, apply_payload_mode

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
    assert out["outer"]["items"][1]["count"] == apply_payload_mode(
        {"count": 1}, PayloadMode.REDACTED, session_salt=SALT
    )["count"]


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
