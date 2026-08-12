"""Device key tests — workstream D."""

from __future__ import annotations

import base64
import os
import stat

import pytest

from byoai.recorder.keys import (
    PRIVATE_KEY_FILENAME,
    PUBLIC_KEY_FILENAME,
    DeviceKey,
    InsecureKeyPermissions,
    derive_device_id,
    load_or_create_device_key,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")


def test_creates_key_file_with_0600(tmp_path):
    key = load_or_create_device_key(tmp_path)
    key_file = tmp_path / PRIVATE_KEY_FILENAME

    assert key_file.exists()
    assert len(key_file.read_bytes()) == 32
    if os.name == "posix":
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert (tmp_path / PUBLIC_KEY_FILENAME).read_text().strip() == key.public_key_b64


def test_creates_missing_directory(tmp_path):
    target = tmp_path / "nested" / "keys"
    key = load_or_create_device_key(target)
    assert (target / PRIVATE_KEY_FILENAME).exists()
    assert key.device_id.startswith("dev_")


def test_device_id_is_stable_across_restarts(tmp_path):
    first = load_or_create_device_key(tmp_path)
    second = load_or_create_device_key(tmp_path)

    assert second.device_id == first.device_id
    assert second.public_key_b64 == first.public_key_b64
    # Reload must not regenerate the key.
    assert first.verify(first.public_key_b64, b"probe", second.sign(b"probe"))


def test_device_id_is_deterministic_and_distinct(tmp_path):
    a = load_or_create_device_key(tmp_path / "a")
    b = load_or_create_device_key(tmp_path / "b")

    assert derive_device_id(a.public_key_b64) == a.device_id
    assert a.device_id != b.device_id
    assert len(a.device_id) == len("dev_") + 26
    assert a.device_id[4:].isupper() or a.device_id[4:].isdigit()


def test_sign_verify_roundtrip(tmp_path):
    key = load_or_create_device_key(tmp_path)
    sig = key.sign(b"payload")

    assert sig.startswith("ed25519:")
    assert DeviceKey.verify(key.public_key_b64, b"payload", sig) is True
    assert DeviceKey.verify(key.public_key_b64, b"payloae", sig) is False


def test_verify_rejects_wrong_key_and_malformed_input(tmp_path):
    key = load_or_create_device_key(tmp_path / "a")
    other = load_or_create_device_key(tmp_path / "b")
    sig = key.sign(b"payload")

    assert DeviceKey.verify(other.public_key_b64, b"payload", sig) is False
    assert DeviceKey.verify(key.public_key_b64, b"payload", "not-a-sig") is False
    assert DeviceKey.verify(key.public_key_b64, b"payload", "ed25519:!!!!") is False
    assert DeviceKey.verify("!!!not-base64", b"payload", sig) is False
    assert DeviceKey.verify(key.public_key_b64, b"payload", None) is False  # type: ignore[arg-type]


def test_flipping_one_signature_byte_fails_verification(tmp_path):
    key = load_or_create_device_key(tmp_path)
    sig = key.sign(b"payload")
    raw = bytearray(base64.b64decode(sig.removeprefix("ed25519:")))
    raw[0] ^= 0x01
    tampered = "ed25519:" + base64.b64encode(bytes(raw)).decode()

    assert DeviceKey.verify(key.public_key_b64, b"payload", tampered) is False


def test_private_key_has_no_export_path(tmp_path):
    key = load_or_create_device_key(tmp_path)
    raw_private = (tmp_path / PRIVATE_KEY_FILENAME).read_bytes()

    assert not hasattr(key, "__dict__")  # __slots__, no stray attributes
    exported = {
        name
        for name in dir(key)
        if not name.startswith("_") and isinstance(getattr(key, name, None), (str, bytes))
    }
    assert exported == {"device_id", "public_key_b64"}
    for value in (key.device_id, key.public_key_b64, repr(key)):
        assert base64.b64encode(raw_private).decode() not in value
        assert raw_private.hex() not in value


@posix_only
def test_refuses_to_load_group_readable_key(tmp_path):
    load_or_create_device_key(tmp_path)
    key_file = tmp_path / PRIVATE_KEY_FILENAME
    key_file.chmod(0o640)

    with pytest.raises(InsecureKeyPermissions):
        load_or_create_device_key(tmp_path)


@posix_only
@pytest.mark.parametrize("mode", [0o604, 0o660, 0o666, 0o700, 0o777])
def test_refuses_all_wider_than_0600(tmp_path, mode):
    load_or_create_device_key(tmp_path)
    (tmp_path / PRIVATE_KEY_FILENAME).chmod(mode)

    with pytest.raises(InsecureKeyPermissions):
        load_or_create_device_key(tmp_path)


@posix_only
def test_accepts_narrower_than_0600(tmp_path):
    original = load_or_create_device_key(tmp_path)
    (tmp_path / PRIVATE_KEY_FILENAME).chmod(0o400)

    assert load_or_create_device_key(tmp_path).device_id == original.device_id


def test_rejects_corrupt_key_file(tmp_path):
    load_or_create_device_key(tmp_path)
    key_file = tmp_path / PRIVATE_KEY_FILENAME
    key_file.write_bytes(b"nope")
    if os.name == "posix":
        key_file.chmod(0o600)

    with pytest.raises(ValueError, match="corrupt"):
        load_or_create_device_key(tmp_path)


def test_no_temp_files_left_behind(tmp_path):
    load_or_create_device_key(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [PRIVATE_KEY_FILENAME, PUBLIC_KEY_FILENAME]
    )
