"""Enrollment flow tests — workstream F."""

from __future__ import annotations

import base64
import contextlib
import json

import httpx
import pytest

from byoai.recorder import enroll as enroll_module
from byoai.recorder.enroll import (
    EnrollmentError,
    EnrollmentState,
    enroll,
    enroll_cli,
    load_enrollment_state,
)
from byoai.recorder.keys import PRIVATE_KEY_FILENAME, load_or_create_device_key


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@contextlib.contextmanager
def _mocked_httpx_client(handler):
    """enroll_cli builds its own httpx.Client internally; to keep tests
    network-free, monkeypatch httpx.Client to return one bound to a mock
    transport for the duration of the block."""
    real_client_cls = httpx.Client

    def fake_client(*args, **kwargs):
        return real_client_cls(transport=_mock_transport(handler))

    enroll_module.httpx.Client = fake_client
    try:
        yield
    finally:
        enroll_module.httpx.Client = real_client_cls


def test_enroll_success_persists_state(tmp_path):
    key_dir = tmp_path / "device"
    requests_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        body = json.loads(request.content)
        assert set(body.keys()) == {"public_key", "token"}
        assert body["token"] == "cik_live_abc123"
        return httpx.Response(
            201,
            json={"device_id": "dev_XYZ", "coriqo_base_url": "https://coriqo.example.com"},
        )

    client = httpx.Client(transport=_mock_transport(handler))

    state = enroll(
        coriqo_base_url="https://coriqo.example.com",
        token="cik_live_abc123",
        key_dir=key_dir,
        http_client=client,
    )

    assert isinstance(state, EnrollmentState)
    assert state.device_id == "dev_XYZ"
    assert state.coriqo_base_url == "https://coriqo.example.com"
    assert len(requests_seen) == 1

    persisted = load_enrollment_state(key_dir)
    assert persisted == state

    state_path = key_dir / "enrollment.json"
    assert state_path.exists()
    mode = state_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_enroll_is_idempotent_no_second_network_call(tmp_path):
    key_dir = tmp_path / "device"
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(201, json={"device_id": "dev_ONE"})

    client = httpx.Client(transport=_mock_transport(handler))

    state1 = enroll(
        coriqo_base_url="https://coriqo.example.com",
        token="cik_live_abc123",
        key_dir=key_dir,
        http_client=client,
    )
    assert call_count == 1

    # Second call: same key_dir, no force -> must not hit the network again.
    state2 = enroll(
        coriqo_base_url="https://coriqo.example.com",
        token="cik_live_abc123",
        key_dir=key_dir,
        http_client=client,
    )
    assert call_count == 1
    assert state1 == state2


def test_enroll_rejected_token_raises_and_persists_nothing(tmp_path):
    key_dir = tmp_path / "device"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "token expired"})

    client = httpx.Client(transport=_mock_transport(handler))

    with pytest.raises(EnrollmentError):
        enroll(
            coriqo_base_url="https://coriqo.example.com",
            token="cik_live_expired",
            key_dir=key_dir,
            http_client=client,
        )

    assert load_enrollment_state(key_dir) is None
    assert not (key_dir / "enrollment.json").exists()


def test_private_key_never_appears_in_request_body(tmp_path):
    key_dir = tmp_path / "device"
    # Pre-create the device key so we know its raw private bytes.
    key = load_or_create_device_key(key_dir)
    private_key_path = key_dir / PRIVATE_KEY_FILENAME
    raw_private_bytes = private_key_path.read_bytes()
    private_b64 = base64.b64encode(raw_private_bytes).decode("ascii")

    captured_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_bodies.append(request.content)
        return httpx.Response(201, json={"device_id": key.device_id})

    client = httpx.Client(transport=_mock_transport(handler))

    enroll(
        coriqo_base_url="https://coriqo.example.com",
        token="cik_live_abc123",
        key_dir=key_dir,
        http_client=client,
    )

    assert len(captured_bodies) == 1
    body_text = captured_bodies[0].decode("utf-8")
    # Raw private key bytes, and its base64 encoding, must never appear on
    # the wire in any form.
    assert raw_private_bytes.hex() not in body_text
    assert private_b64 not in body_text
    for chunk in captured_bodies:
        assert raw_private_bytes not in chunk


def test_load_enrollment_state_missing_returns_none(tmp_path):
    assert load_enrollment_state(tmp_path / "nowhere") is None


def test_load_enrollment_state_corrupt_json_raises_enrollment_error(tmp_path):
    key_dir = tmp_path / "device"
    key_dir.mkdir(parents=True)
    (key_dir / "enrollment.json").write_text("{not valid json")

    with pytest.raises(EnrollmentError):
        load_enrollment_state(key_dir)


def test_load_enrollment_state_missing_field_raises_enrollment_error(tmp_path):
    key_dir = tmp_path / "device"
    key_dir.mkdir(parents=True)
    (key_dir / "enrollment.json").write_text(json.dumps({"device_id": "dev_X"}))

    with pytest.raises(EnrollmentError):
        load_enrollment_state(key_dir)


def test_enroll_cli_success(tmp_path, capsys):
    key_dir = tmp_path / "device"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"device_id": "dev_CLI"})

    with _mocked_httpx_client(handler):
        rc = enroll_cli(
            [
                "--coriqo-url",
                "https://coriqo.example.com",
                "--token",
                "cik_live_abc123",
                "--key-dir",
                str(key_dir),
            ]
        )

    assert rc == 0
    out = capsys.readouterr().out
    assert "dev_CLI" in out
    assert load_enrollment_state(key_dir).device_id == "dev_CLI"


def test_enroll_cli_insecure_key_permissions_returns_nonzero(tmp_path, capsys):
    # load_or_create_device_key raises InsecureKeyPermissions (a
    # PermissionError, not an EnrollmentError) when an existing key file on
    # disk has overly-permissive mode bits. The CLI must turn that into the
    # same clean "enrollment failed: ..." exit, not an uncaught traceback.
    from byoai.recorder.keys import PRIVATE_KEY_FILENAME, load_or_create_device_key

    key_dir = tmp_path / "device"
    load_or_create_device_key(key_dir)
    (key_dir / PRIVATE_KEY_FILENAME).chmod(0o644)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the network with an insecure key")

    with _mocked_httpx_client(handler):
        rc = enroll_cli(
            [
                "--coriqo-url",
                "https://coriqo.example.com",
                "--token",
                "cik_live_abc123",
                "--key-dir",
                str(key_dir),
            ]
        )

    assert rc == 1
    err = capsys.readouterr().err
    assert "enrollment failed" in err


def test_enroll_cli_failure_returns_nonzero(tmp_path, capsys):
    key_dir = tmp_path / "device"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad token"})

    with _mocked_httpx_client(handler):
        rc = enroll_cli(
            [
                "--coriqo-url",
                "https://coriqo.example.com",
                "--token",
                "cik_live_bad",
                "--key-dir",
                str(key_dir),
            ]
        )

    assert rc == 1
    err = capsys.readouterr().err
    assert "enrollment failed" in err
