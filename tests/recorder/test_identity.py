"""Coriqo identity resolution — device key preferred, static key legacy."""

from __future__ import annotations

import json
import logging

import pytest

from byoai.errors import (
    ByoAIError,
    CoriqoIdentityError,
    EnforcementIdentityUnavailableError,
)
from byoai.recorder.enroll import ENROLLMENT_FILENAME
from byoai.recorder.identity import (
    CoriqoIdentity,
    DeviceKeySigner,
    IdentitySource,
    default_key_dir,
    reset_identity_warning_for_tests,
    resolve_identity,
)
from byoai.recorder.keys import DeviceKey, load_or_create_device_key


@pytest.fixture(autouse=True)
def _rearm_warning():
    reset_identity_warning_for_tests()
    yield
    reset_identity_warning_for_tests()


@pytest.fixture
def no_static_key(monkeypatch):
    for name in ("BYOAI_CORIQO_URL", "BYOAI_CORIQO_API_KEY", "BYOAI_CORIQO_TENANT_SLUG"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def static_key(monkeypatch):
    monkeypatch.setenv("BYOAI_CORIQO_URL", "https://coriqo.test")
    monkeypatch.setenv("BYOAI_CORIQO_API_KEY", "cq_sa_legacy")
    monkeypatch.setenv("BYOAI_CORIQO_TENANT_SLUG", "acme_bank")


def _enroll_on_disk(
    key_dir, *, base_url="https://device.coriqo.test", tenant_slug=None
) -> DeviceKey:
    """Write enrollment state by hand. ``tenant_slug=None`` reproduces a file
    written before the tenant was recorded — the state every device already in
    the field is in."""
    key_dir.mkdir(parents=True, exist_ok=True)
    key = load_or_create_device_key(key_dir)
    state = {
        "device_id": "dev_enrolled_1",
        "coriqo_base_url": base_url,
        "enrolled_at": "2026-01-01T00:00:00Z",
    }
    if tenant_slug is not None:
        state["tenant_slug"] = tenant_slug
    (key_dir / ENROLLMENT_FILENAME).write_text(json.dumps(state))
    return key


def test_device_identity_preferred_over_static_key(tmp_path, static_key):
    _enroll_on_disk(tmp_path)

    identity = resolve_identity(key_dir=tmp_path)

    assert identity is not None
    assert identity.source == IdentitySource.DEVICE
    assert identity.enforcement_capable is True
    assert identity.enrolled_device_id == "dev_enrolled_1"
    # The device's own enrollment URL wins; the static key's is not consulted.
    assert identity.base_url == "https://device.coriqo.test"
    assert identity.credentials is None


def test_device_id_follows_the_signing_key_after_rotation(tmp_path, no_static_key):
    """Rotation replaces the live key without rewriting enrollment.json, so the
    reported device_id has to come from the key that actually signs — otherwise
    the claim and the proof describe two different keys."""
    from byoai.recorder.ledger import Ledger
    from byoai.recorder.rotation import rotate_key

    key = _enroll_on_disk(tmp_path)
    ledger = Ledger(tmp_path / "ledger.db", key.device_id)
    new_key = rotate_key(key_dir=tmp_path, ledger=ledger, reason="rotation")

    identity = resolve_identity(key_dir=tmp_path)
    assert identity is not None
    assert identity.enrolled_device_id == "dev_enrolled_1"
    assert identity.device_id == new_key.device_id
    assert DeviceKey.verify(new_key.public_key_b64, b"scope", identity.sign(b"scope"))


def test_confirmed_rotation_is_reconciled_rather_than_reported_missing(tmp_path, no_static_key):
    """A crash mid-rotation can leave no live private key with a confirmed
    staged one waiting. That must be promoted, not answered with 're-enroll'."""
    from byoai.recorder.keys import (
        PENDING_ROTATION_DIRNAME,
        PRIVATE_KEY_FILENAME,
        PUBLIC_KEY_FILENAME,
        _mark_promotion_confirmed,
        load_or_create_device_key,
    )

    old = _enroll_on_disk(tmp_path)
    staged_dir = tmp_path / PENDING_ROTATION_DIRNAME
    staged_dir.mkdir()
    staged = load_or_create_device_key(staged_dir)
    _mark_promotion_confirmed(tmp_path, old_device_id=old.device_id)
    (tmp_path / PRIVATE_KEY_FILENAME).unlink()
    (tmp_path / PUBLIC_KEY_FILENAME).unlink()

    identity = resolve_identity(key_dir=tmp_path)
    assert identity is not None
    assert identity.device_id == staged.device_id
    assert DeviceKey.verify(staged.public_key_b64, b"x", identity.sign(b"x"))


def test_legacy_only_identity_is_publish_only(tmp_path, static_key):
    identity = resolve_identity(key_dir=tmp_path)

    assert identity is not None
    assert identity.source == IdentitySource.API_KEY
    assert identity.enforcement_capable is False
    assert identity.credentials is not None
    assert identity.credentials.api_key == "cq_sa_legacy"


def test_legacy_warning_fires_once_across_resolutions(tmp_path, static_key, caplog):
    with caplog.at_level(logging.WARNING, logger="byoai.recorder.identity"):
        for _ in range(4):
            resolve_identity(key_dir=tmp_path)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "byoai-recorder-enroll" in message
    assert "enforcement" in message


def test_no_identity_configured_returns_none(tmp_path, no_static_key):
    assert resolve_identity(key_dir=tmp_path) is None


def test_enforcement_request_on_legacy_identity_raises(tmp_path, static_key):
    identity = resolve_identity(key_dir=tmp_path)
    assert identity is not None

    with pytest.raises(EnforcementIdentityUnavailableError) as excinfo:
        identity.require_enforcement()

    assert "byoai-recorder-enroll" in str(excinfo.value)
    # Applications catch runtime failures without importing provider SDK types.
    assert isinstance(excinfo.value, CoriqoIdentityError)
    assert isinstance(excinfo.value, ByoAIError)

    with pytest.raises(EnforcementIdentityUnavailableError):
        identity.sign(b"payload")


def test_device_signature_verifies_with_device_key(tmp_path, no_static_key):
    key = _enroll_on_disk(tmp_path)
    identity = resolve_identity(key_dir=tmp_path)
    assert identity is not None

    signature = identity.sign(b"GET /mandate/scope")

    assert signature.startswith("ed25519:")
    assert DeviceKey.verify(key.public_key_b64, b"GET /mandate/scope", signature)
    assert not DeviceKey.verify(key.public_key_b64, b"tampered", signature)
    assert identity.require_enforcement().public_key_b64 == key.public_key_b64


def test_injected_fake_signer_needs_no_filesystem():
    class FakeSigner:
        device_id = "dev_fake"
        public_key_b64 = "AAAA"

        def sign(self, data: bytes) -> str:
            return "ed25519:fake-" + data.decode()

    identity = CoriqoIdentity.from_device(
        base_url="https://coriqo.test/", device_id="dev_fake", signer=FakeSigner()
    )

    assert identity.enforcement_capable is True
    assert identity.base_url == "https://coriqo.test"  # trailing slash trimmed
    assert identity.sign(b"abc") == "ed25519:fake-abc"


def test_corrupt_enrollment_state_does_not_fall_back_to_static_key(tmp_path, static_key):
    (tmp_path / ENROLLMENT_FILENAME).write_text("{not json")

    with pytest.raises(CoriqoIdentityError):
        resolve_identity(key_dir=tmp_path)


def test_enrolled_without_key_refuses_to_mint_a_new_identity(tmp_path, no_static_key):
    key = _enroll_on_disk(tmp_path)
    from byoai.recorder.keys import PRIVATE_KEY_FILENAME

    (tmp_path / PRIVATE_KEY_FILENAME).unlink()

    identity = resolve_identity(key_dir=tmp_path)
    assert identity is not None
    with pytest.raises(CoriqoIdentityError) as excinfo:
        identity.sign(b"anything")
    assert "no device key" in str(excinfo.value)
    assert key.device_id not in str(excinfo.value)


def test_default_key_dir_follows_recorder_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("BYOAI_RECORDER_DIR", str(tmp_path / "custom"))
    assert default_key_dir() == tmp_path / "custom"


def test_signer_defers_key_load_until_used(tmp_path):
    signer = DeviceKeySigner(tmp_path / "missing")
    assert signer.key_dir == tmp_path / "missing"  # constructing touched nothing
    with pytest.raises(CoriqoIdentityError):
        signer.sign(b"x")


def test_device_identity_exposes_the_enrolled_tenant(tmp_path, no_static_key):
    _enroll_on_disk(tmp_path, tenant_slug="enrolled_bank")

    identity = resolve_identity(key_dir=tmp_path)

    assert identity is not None
    assert identity.tenant_slug == "enrolled_bank"


def test_static_key_identity_exposes_its_tenant(tmp_path, static_key):
    identity = resolve_identity(key_dir=tmp_path / "empty")

    assert identity is not None
    assert identity.source == IdentitySource.API_KEY
    assert identity.tenant_slug == "acme_bank"


def test_a_tenantless_enrollment_loads_and_warns_once(tmp_path, no_static_key, caplog):
    """An enrollment.json written before the tenant field existed must keep
    working: no crash, no silent re-enrollment, and one note naming what to
    run — not one per resolve, which a refresh loop would turn into a flood."""
    _enroll_on_disk(tmp_path)

    with caplog.at_level(logging.WARNING, logger="byoai.recorder.identity"):
        first = resolve_identity(key_dir=tmp_path)
        second = resolve_identity(key_dir=tmp_path)

    assert first is not None and second is not None
    assert first.source == IdentitySource.DEVICE
    assert first.enforcement_capable is True
    assert first.tenant_slug is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert ENROLLMENT_FILENAME in message
    assert "BYOAI_CORIQO_TENANT_SLUG" in message
    assert "byoai-recorder-enroll" in message
