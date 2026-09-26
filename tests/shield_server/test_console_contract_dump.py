"""Dumps real responses from every admin endpoint the console reads into
``web/src/mocks/__contract__/shield_server/*.json``.

This is the backend half of pinning the console/server contract: a vitest
test (``web/src/mocks/__contract__/shieldServerContract.test.ts``) parses
these same files with the console's own zod schemas. A change on either side
that breaks the shape fails a test — this one if the backend's own shape
changes unexpectedly, the vitest one if the console's schema no longer
accepts what the backend actually sends.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from byoai.recorder.canonical import canonicalize
from byoai.shield_server.app import create_app
from byoai.shield_server.config import ShieldServerConfig

CONTRACT_DIR = (
    Path(__file__).resolve().parents[2] / "web" / "src" / "mocks" / "__contract__" / "shield_server"
)


def _shape(value):
    """Keys and JSON types only. Ids, keys and timestamps are fresh on every
    run, so comparing values would fail (or, when writing, dirty the tree)
    on every test run."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


def _dump(name: str, data) -> None:
    """Check ``data`` has the same shape as the committed fixture. With
    ``BYOAI_UPDATE_CONTRACT=1`` it rewrites the fixture instead — run that
    after an intentional response change, then commit the JSON."""
    path = CONTRACT_DIR / f"{name}.json"
    if os.environ.get("BYOAI_UPDATE_CONTRACT") == "1" or not path.exists():
        CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return
    committed = json.loads(path.read_text())
    assert _shape(data) == _shape(committed), (
        f"{name}: the admin API response shape changed. If that is intended, "
        "rerun with BYOAI_UPDATE_CONTRACT=1 and commit the fixture, then check "
        "the console's zod schemas still accept it.")


@pytest.fixture
def dump_cfg(tmp_path: Path) -> ShieldServerConfig:
    return ShieldServerConfig(
        data_dir=tmp_path / "data", host="127.0.0.1", port=17840,
        public_url="http://127.0.0.1:17840", org="default", admin_token="test-admin-token",
    )


def test_dump_console_contract_fixtures(dump_cfg):
    import asyncio

    import httpx

    app = create_app(dump_cfg)
    admin = {"Authorization": f"Bearer {dump_cfg.admin_token}"}

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # -- info, and policy before anything is ever set (null shapes) ---
            info = (await client.get("/api/v1/shield/info", headers=admin)).json()
            _dump("info", info)
            policy_null = (await client.get("/api/v1/shield/policy", headers=admin)).json()
            _dump("policy_null", policy_null)

            # -- enrol a real device and give it a policy + a checkpoint -----
            priv = Ed25519PrivateKey.generate()
            pub_b64 = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
            token = (await client.post("/api/v1/shield/tokens",
                                       json={"label": "Contract Mac"}, headers=admin)).json()
            _dump("tokens_mint", token)
            enroll = await client.post("/v1/enroll", json={"public_key": pub_b64, "token": token["token"]})
            device_id = enroll.json()["device_id"]

            put_body = {"mode": "redact", "apps": {"claude": True}, "keep_text": False,
                       "retention_days": 30, "notice": True, "managed_by": "Contract Org IT",
                       "locked": ["mode", "notice"]}
            policy = (await client.put("/api/v1/shield/policy", json=put_body, headers=admin)).json()
            _dump("policy", policy)

            device_policy = (await client.get(
                f"/api/v1/shield/devices/{device_id}/policy", headers=admin)).json()
            _dump("device_policy_null", device_policy)

            override_body = {**put_body, "mode": "block", "managed_by": "Contract Org IT (override)"}
            device_policy_set = (await client.put(
                f"/api/v1/shield/devices/{device_id}/policy", json=override_body,
                headers=admin)).json()
            _dump("device_policy", device_policy_set)

            cp_body = {
                "device_id": device_id,
                "checkpoints": [{
                    "checkpoint_id": "shield:1:aaaa", "session_id": "coriqo-shield",
                    "kind": "byoai.shield.checkpoint.v1", "seq_start": 1, "seq_end": 1,
                    "chain_hash": "a" * 64, "entries": 1,
                    "ts_device": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "checkpoint": {}, "record_id": "rec1", "prev_chain_hash": None,
                    "sig": "ed25519:deadbeef",
                }],
                "sent_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "shield": {"protecting": True, "reasons": [], "mode": "redact", "policy_version": None},
            }
            canon = canonicalize(cp_body)
            sig = "ed25519:" + base64.b64encode(priv.sign(canon)).decode()
            await client.post("/v1/checkpoints/batch", content=gzip.compress(canon), headers={
                "content-type": "application/json", "content-encoding": "gzip",
                "x-coriqo-device": device_id, "x-coriqo-signature": sig,
            })

            devices = (await client.get("/api/v1/shield/devices", headers=admin)).json()
            _dump("devices", devices)

            drift = (await client.get("/api/v1/shield/policy/drift", headers=admin)).json()
            _dump("drift", drift)

            tokens = (await client.get("/api/v1/shield/tokens", headers=admin)).json()
            _dump("tokens", tokens)

    asyncio.run(go())

    for name in ("info", "policy_null", "tokens_mint", "policy", "device_policy_null",
                "device_policy", "devices", "drift", "tokens"):
        assert (CONTRACT_DIR / f"{name}.json").exists()
