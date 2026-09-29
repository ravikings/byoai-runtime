"""Admin auth (missing/wrong token -> 401), the device-policy 404 for an
unknown device, and the public-keys shape."""

from __future__ import annotations

import httpx
import pytest


@pytest.fixture
async def async_client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_missing_admin_token_is_401(async_client):
    r = await async_client.get("/api/v1/shield/devices")
    assert r.status_code == 401


async def test_wrong_admin_token_is_401(async_client):
    r = await async_client.get("/api/v1/shield/devices",
                               headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_malformed_authorization_header_is_401(async_client):
    r = await async_client.get("/api/v1/shield/devices",
                               headers={"Authorization": "Basic xyz"})
    assert r.status_code == 401


async def test_correct_admin_token_is_accepted(async_client, cfg):
    r = await async_client.get("/api/v1/shield/devices",
                               headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert r.status_code == 200
    assert r.json() == {"devices": []}


async def test_unknown_device_policy_put_is_404(async_client, cfg):
    body = {"mode": "redact", "apps": {"claude": True}, "keep_text": False,
            "retention_days": 30, "notice": True, "managed_by": "Test Org"}
    r = await async_client.put("/api/v1/shield/devices/dev_ghost/policy", json=body,
                               headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert r.status_code == 404


async def test_unknown_device_policy_delete_is_404(async_client, cfg):
    r = await async_client.delete("/api/v1/shield/devices/dev_ghost/policy",
                                  headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert r.status_code == 404


async def test_unknown_device_revoke_is_404(async_client, cfg):
    r = await async_client.delete("/api/v1/shield/devices/dev_ghost",
                                  headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert r.status_code == 404


async def test_bad_policy_body_is_400(async_client, cfg):
    body = {"mode": "not_a_mode", "apps": {"claude": True}, "keep_text": False,
            "retention_days": 30, "notice": True, "managed_by": "Test Org"}
    r = await async_client.put("/api/v1/shield/policy", json=body,
                               headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert r.status_code == 400


async def test_public_keys_shape_needs_no_auth(async_client, signer):
    r = await async_client.get("/api/v1/checkpoints/public-keys")
    assert r.status_code == 200
    body = r.json()
    assert "keys" in body and isinstance(body["keys"], list) and len(body["keys"]) == 1
    entry = body["keys"][0]
    assert entry["key_id"] == signer.key_id
    assert "BEGIN PUBLIC KEY" in entry["public_key_pem"]
    assert entry["revoked"] is False
    assert entry["active"] is True


async def test_console_router_requires_admin_token(async_client):
    r = await async_client.get("/v1/console/fleet", params={"tenant": "default"})
    assert r.status_code == 401


async def test_console_router_works_with_admin_token(async_client, cfg):
    r = await async_client.get("/v1/console/fleet", params={"tenant": "default"},
                               headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert r.status_code == 200
    assert r.json()["tenant"] == "default"


@pytest.mark.parametrize(
    "path", ["/v1/console/fleet", "/v1/console/fleet/devices",
             "/v1/console/fleet/coverage", "/v1/console/fleet/findings"]
)
async def test_console_router_refuses_other_org(async_client, cfg, path):
    """The public console is single-org (§4.2): this server serves only
    `cfg.org`, and any other tenant slug in the query string is a 404, not an
    (empty or someone else's) fleet — a many-org view is Coriqo's paid MSP
    console."""
    r = await async_client.get(path, params={"tenant": "some-other-org"},
                               headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert r.status_code == 404


async def test_console_root_lands_on_this_servers_org(async_client, cfg):
    """/console/ goes to the served org, not the SPA's build-time default,
    so the free server's console works out of the box."""
    r = await async_client.get("/console/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == f"/console/{cfg.org}/fleet"


async def test_mint_and_list_and_revoke_tokens(async_client, cfg):
    headers = {"Authorization": f"Bearer {cfg.admin_token}"}
    r = await async_client.post("/api/v1/shield/tokens", json={"label": "mac1"}, headers=headers)
    assert r.status_code == 200
    minted = r.json()
    token = minted["token"]
    assert token.startswith("shieldtok_")
    assert minted["revoked"] is False
    # Only a short, non-secret id is exposed — never the full hash and never
    # the token itself again after this response.
    assert len(minted["token_id"]) == 12
    assert minted["token_id"] not in token

    r = await async_client.get("/api/v1/shield/tokens", headers=headers)
    rows = r.json()["tokens"]
    assert len(rows) == 1 and rows[0]["label"] == "mac1" and rows[0]["revoked"] is False
    assert rows[0]["token_id"] == minted["token_id"]
    assert "token_hash" not in rows[0] and "token" not in rows[0]

    r = await async_client.delete(f"/api/v1/shield/tokens/{rows[0]['token_id']}", headers=headers)
    assert r.status_code == 200

    r = await async_client.get("/api/v1/shield/tokens", headers=headers)
    assert r.json()["tokens"][0]["revoked"] is True


async def test_info_needs_admin_token(async_client, cfg, signer):
    r = await async_client.get("/api/v1/shield/info")
    assert r.status_code == 401
    r = await async_client.get("/api/v1/shield/info",
                               headers={"Authorization": f"Bearer {cfg.admin_token}"})
    assert r.status_code == 200
    assert r.json() == {"public_url": cfg.public_url, "org": cfg.org, "key_id": signer.key_id}


async def test_drift_wraps_devices_and_only_lists_drifted(async_client, cfg):
    headers = {"Authorization": f"Bearer {cfg.admin_token}"}
    r = await async_client.get("/api/v1/shield/policy/drift", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"devices": []}
