"""AIR-7e: end-to-end round trip against a REAL running Coriqo dev stack.

This is not a mock-transport test (see test_end_to_end_sync.py /
test_shipper_wiring.py for those) — it drives every production seam this
program built (AIR-7a..d) against a live `docker compose` Coriqo instance:

  1. Admin-login to the seeded ``acme_bank`` tenant and register two real
     agents over HTTP (``service.register_agent`` seals each one's first
     ModelVersion at registration, so each already has a live
     ``mandate_version_id`` with no separate approval step needed).
  2. Mint a real single-use enrolment token and enrol a real device
     (real Ed25519 keypair, real HTTP) with
     :func:`byoai.recorder.enroll.enroll`.
  3. Fetch each agent's real mandate snapshot over the device-signed
     enforcement API (:meth:`AsyncCoriqoAgentsClient.fetch_mandate`) — this
     both gives this test the real snapshot shape Coriqo actually serves
     and creates the live ``AgentDeviceBinding`` attestation ingest checks.
  4. Build a parent :class:`MandateGate` and a delegated child gate from
     those real snapshots, wire a real local :class:`Ledger` +
     :class:`VerdictRecorder`, and drive two ``@governed_tool`` calls
     through them: one under the parent's own mandate (resource-bearing,
     ``on_behalf_of`` empty) and one delegated (``on_behalf_of``
     non-empty) — this is the "one delegated tool call, one resource-
     bearing call" the packet asks for.
  5. Checkpoint the ledger and ship the resulting CEI v2 envelope through
     :meth:`Shipper.ship_attestations_once` — the actual production
     shipping path (AIR-7d/§3e), not a hand-built request.
  6. Assert Coriqo answers ``sealed`` (not ``duplicate``, not refused);
     re-run the same shipping pass and assert it becomes a no-op
     (idempotency — the watermark already advanced past it).
  7. On Coriqo's side, seal a checkpoint over the child agent's mandate
     chain, export its governance events, and run
     ``tools/verify_proof.py --attestation`` against the exported fixture —
     the same technique ``test_verify_proof_attestation.py`` uses — and
     assert it reproduces the chain head and that ``resource``/
     ``on_behalf_of`` are present and correct on the far side.

Skipped (not failed) unless a real Coriqo instance answers at
``CORIQO_LIVE_BASE_URL`` (default ``http://localhost:8000``) — this test
must never fake the round trip.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

CORIQO_BASE_URL = os.environ.get("CORIQO_LIVE_BASE_URL", "http://localhost:8000")
CORIQO_REPO = Path(os.environ.get("CORIQO_REPO_PATH", "/Users/abdulrabiu/Documents/GitHub/coriqo"))
TENANT_SLUG = os.environ.get("CORIQO_LIVE_TENANT_SLUG", "acme_bank")
ADMIN_USERNAME = os.environ.get("CORIQO_LIVE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("CORIQO_LIVE_ADMIN_PASSWORD", "admin123")


def _live_reachable() -> bool:
    try:
        r = httpx.get(f"{CORIQO_BASE_URL}/health", timeout=2.0)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(
    not _live_reachable(),
    reason=(
        f"no real Coriqo instance reachable at {CORIQO_BASE_URL} — AIR-7e must "
        "prove a real round trip, not simulate one; bring the stack up with "
        "`make dev` in the coriqo repo to run this test"
    ),
)


# -- HTTP helpers against the live admin API ---------------------------------

def _admin_token() -> str:
    r = httpx.post(
        f"{CORIQO_BASE_URL}/api/v1/auth/token",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "tenant_slug": TENANT_SLUG},
        timeout=10.0,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _register_agent(token: str, *, name: str, external_id: str, allowed_tools: list[str],
                     delegation_policy: str = "attenuated") -> dict:
    r = httpx.post(
        f"{CORIQO_BASE_URL}/api/v1/agents",
        json={
            "name": name, "external_id": external_id, "mandate": f"AIR-7e fixture agent {name}",
            "system": "air-7e-roundtrip", "risk_tier": "high", "allowed_tools": allowed_tools,
            "delegation_policy": delegation_policy,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def _tenant_schema(token: str) -> str:
    """Decode the tenant_schema claim off the admin JWT rather than
    hand-typing it — schema names carry a random hex suffix
    (``t_<slug>_<8 hex>``), never just ``t_<slug>``."""
    import base64 as _b64

    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    claims = json.loads(_b64.urlsafe_b64decode(payload_b64))
    return claims["tenant_schema"]


def _mint_enrollment_token(token: str, *, label: str) -> str:
    r = httpx.post(
        f"{CORIQO_BASE_URL}/api/v1/agent-runtime/enrollment-tokens",
        json={"label": label},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


# -- the round trip -----------------------------------------------------------

@pytest.fixture(scope="module")
def live_setup(tmp_path_factory):
    from byoai.recorder.attestation import CEI_ATTESTABLE_KINDS  # noqa: F401 - sanity import
    from byoai.recorder.checkpoint import Checkpointer
    from byoai.recorder.coriqo_async import AsyncCoriqoAgentsClient
    from byoai.recorder.delegation import EffectiveScope, delegated_gate
    from byoai.recorder.enroll import enroll
    from byoai.recorder.governed_tool import governed_tool, use_gate
    from byoai.recorder.identity import CoriqoIdentity, DeviceKeySigner
    from byoai.recorder.keys import load_or_create_device_key
    from byoai.recorder.ledger import Ledger
    from byoai.recorder.mandate import MandateGate
    from byoai.recorder.shipper import Shipper
    from byoai.recorder.verdicts import VerdictRecorder, use_verdict_recorder

    run_id = uuid.uuid4().hex[:8]
    admin = _admin_token()

    parent = _register_agent(
        admin, name=f"air7e-parent-{run_id}", external_id=f"air7e-parent-{run_id}",
        allowed_tools=["payments.refund", "payments.list"],
    )
    child = _register_agent(
        admin, name=f"air7e-child-{run_id}", external_id=f"air7e-child-{run_id}",
        allowed_tools=["payments.list"], delegation_policy="none",
    )
    parent_agent_id = parent["agent_id"]
    child_agent_id = child["agent_id"]
    assert parent.get("mandate_version_id"), "registration must seal a mandate version"
    assert child.get("mandate_version_id"), "registration must seal a mandate version"

    # -- enrol a real device --------------------------------------------------
    key_dir = tmp_path_factory.mktemp("air7e_device")
    enroll_token = _mint_enrollment_token(admin, label=f"air7e-device-{run_id}")
    state = enroll(coriqo_base_url=CORIQO_BASE_URL, token=enroll_token, key_dir=key_dir)
    device_key = load_or_create_device_key(key_dir)
    identity = CoriqoIdentity.from_device(
        base_url=CORIQO_BASE_URL, device_id=state.device_id,
        signer=DeviceKeySigner(key_dir), tenant_slug=state.tenant_slug or TENANT_SLUG,
    )

    async def _fetch_mandates() -> tuple[dict, dict]:
        async with AsyncCoriqoAgentsClient(identity) as client:
            parent_snapshot = await client.fetch_mandate(parent_agent_id)
            child_snapshot = await client.fetch_mandate(child_agent_id)
            return parent_snapshot, child_snapshot

    # Real fetch_mandate call: binds this device to both agents (first
    # contact, resolve_bound_agent) AND hands back Coriqo's real snapshot
    # shape — not a hand-built payload.
    parent_snapshot_payload, child_snapshot_payload = asyncio.run(_fetch_mandates())

    parent_gate = MandateGate(None, agent_id=parent_agent_id)
    parent_gate.apply_snapshot(parent_snapshot_payload)
    child_gate = MandateGate(None, agent_id=child_agent_id)
    child_gate.apply_snapshot(child_snapshot_payload)
    delegated = delegated_gate(parent_gate, child_gate, child_agent_id=child_agent_id)

    ledger_path = tmp_path_factory.mktemp("air7e_ledger") / "ledger.db"
    ledger = Ledger(ledger_path, device_key.device_id)
    recorder = VerdictRecorder(ledger=ledger)

    @governed_tool(name="payments.list")
    def list_payments() -> str:
        return "ok"

    @governed_tool(name="payments.list")
    def list_payments_delegated() -> str:
        return "ok"

    with use_verdict_recorder(recorder):
        # 1. resource-bearing call under the parent's own mandate — on_behalf_of empty.
        with use_gate(parent_gate):
            list_payments()
        # 2. delegated call — resource-bearing AND on_behalf_of non-empty.
        with use_gate(delegated):
            list_payments_delegated()

    # -- checkpoint the whole window and ship it ------------------------------
    checkpointer = Checkpointer(ledger, device_key, every_events=1)
    last_seq = ledger.next_seq - 1
    assert last_seq >= 2, "both governed_tool calls must have appended events"
    for seq in range(1, last_seq + 1):
        checkpointer.note(seq)

    # No `attestation_client=` here: leave it to Shipper's production path,
    # which builds and closes a fresh AsyncCoriqoAgentsClient (and the
    # httpx.AsyncClient it owns) inside each `asyncio.run` call. A real
    # client injected across multiple `ship_attestations_once`-internal
    # `_attest` calls would bind its connection pool to the first call's
    # event loop and then hand it to a second, already-closed loop — see
    # Shipper._do_attest's own docstring on why only a mock-transport
    # client is safe to reuse that way. This run's two governed_tool calls
    # (parent + delegated child) land under two different agent ids, so
    # ship_attestations_once makes exactly two `_attest` calls.
    shipper = Shipper(
        ledger, device_key, coriqo_base_url=CORIQO_BASE_URL,
        tenant_slug=state.tenant_slug or TENANT_SLUG,
    )
    try:
        first_result = shipper.ship_attestations_once()
        second_result = shipper.ship_attestations_once()
    finally:
        shipper.close()

    return {
        "admin": admin, "parent_agent_id": parent_agent_id, "child_agent_id": child_agent_id,
        "child_mandate_version_id": child["mandate_version_id"],
        "first_result": first_result, "second_result": second_result,
        "ledger": ledger, "device_key": device_key,
    }


def test_first_attestation_pass_ships_one_envelope(live_setup):
    result = live_setup["first_result"]
    assert result is not None, "the checkpointed window must have produced at least one envelope"
    # One envelope per distinct agent present in the checkpointed window —
    # the parent's own call and the delegated child's call are two agents.
    assert result.envelopes_shipped == 2, result
    assert result.duplicates == 0, result


def test_second_attestation_pass_is_a_no_op(live_setup):
    """The watermark already advanced past the sealed window — re-running
    the shipping pass must ship nothing, proving this is safe to re-run
    rather than resealing a second, divergent attestation."""
    assert live_setup["second_result"] is None, live_setup["second_result"]


def test_coriqo_side_offline_verification_reproduces_the_chain(live_setup, tmp_path):
    """Seal a checkpoint over the child agent's chain, export its governance
    events, and run tools/verify_proof.py --attestation on the far side —
    the same technique api/tests/test_verify_proof_attestation.py uses
    against a real seal, run here against THIS test's real attestation."""
    admin = live_setup["admin"]
    mandate_version_id = live_setup["child_mandate_version_id"]
    TENANT_SCHEMA = _tenant_schema(admin)

    r = httpx.post(
        f"{CORIQO_BASE_URL}/api/v1/checkpoints",
        json={"subject": "org_default", "trigger": "manual", "version_id": mandate_version_id},
        headers={"Authorization": f"Bearer {admin}"}, timeout=30.0,
    )
    assert r.status_code == 201, r.text
    checkpoint = r.json()

    r = httpx.get(
        f"{CORIQO_BASE_URL}/api/v1/checkpoints/public-key",
        headers={"Authorization": f"Bearer {admin}"}, timeout=10.0,
    )
    assert r.status_code == 200, r.text
    pubkey_pem = r.text
    pubkey_path = tmp_path / "checkpoint_pubkey.pem"
    pubkey_path.write_text(pubkey_pem)

    export_script = f"""
import asyncio, json, sys
sys.path.insert(0, "/app")
from api.database import get_tenant_db
from api.domains.checkpoints.export import _events_by_version
from api.db.models import EvidenceRecord
from sqlalchemy import select

async def _main():
    async for s in get_tenant_db({TENANT_SCHEMA!r}):
        by_version, _, _ = await _events_by_version(s)
        events = by_version.get({mandate_version_id!r}, [])
        rows = await s.execute(
            select(EvidenceRecord)
            .where(EvidenceRecord.record_type == "execution_attestation")
            .where(EvidenceRecord.subject_ref == {mandate_version_id!r})
            .order_by(EvidenceRecord.created_at.desc())
        )
        record = rows.scalars().first()
        print(json.dumps({{
            "events": events,
            "envelope": record.payload if record else None,
            "content_hash": record.content_hash if record else None,
        }}))
        break

asyncio.run(_main())
"""
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "-e", "PYTHONPATH=/app", "api", "python3", "-c", export_script],
        cwd=str(CORIQO_REPO), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"export failed: {proc.stderr}"
    exported = json.loads(proc.stdout.strip().splitlines()[-1])
    assert exported["envelope"] is not None, "no sealed execution_attestation found for this mandate version"
    assert exported["events"], "the sealed attestation's governance event did not export"

    envelope = exported["envelope"]
    env_events = envelope["events"]
    assert any(e.get("on_behalf_of") for e in env_events), \
        "the shipped envelope must carry at least one non-empty on_behalf_of (the delegated call)"
    assert all(e.get("resource") for e in env_events), \
        "every shipped event must carry a resource (both calls were resource-bearing)"

    fixture = {
        "envelope": envelope, "record": {"content_hash": exported["content_hash"]},
        "checkpoint": checkpoint, "events": exported["events"],
    }
    fixture_path = tmp_path / "air7e_fixture.json"
    fixture_path.write_text(json.dumps(fixture))

    sys.path.insert(0, str(CORIQO_REPO / "tools"))
    import verify_proof as vp  # type: ignore

    ok = vp.run_attestation(str(fixture_path), str(pubkey_path))
    assert ok, "tools/verify_proof.py --attestation did not accept the real round-tripped envelope"
