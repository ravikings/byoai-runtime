"""Invariants of the ingest read model that the product's claims rest on.

Three families, each one a thing that would quietly break a customer's
evidence if it regressed:

* **tenancy** — a device's tenant comes from its enrolment row and nothing
  else, so no batch volume, no second tenant's activity, and no unenrolled
  caller can move a number in someone else's report;
* **identity** — ``device_id`` is derived from the public key, so rotation
  mints a *new* device and history does not follow it; ``supersedes_device_id``
  is the only thread back to the predecessor;
* **seq namespacing** — ``seq`` is per-device, so the same number on two
  devices is two entries and neither device's gaps depend on the other's.

Uses the real :class:`IngestStore` against a ``tmp_path`` sqlite file and the
real :func:`derive_device_id`, so a change to either the schema or the id
derivation shows up here rather than in production.
"""

from __future__ import annotations

import hashlib

import pytest
from tests.ingest.conftest import device_identity

from byoai.ingest.store import (
    Enrolment,
    EnrolmentRefused,
    IngestStore,
    UnknownDeviceError,
)


def _enrol(store: IngestStore, tenant: str, seed: str, *, supersedes: str | None = None) -> str:
    pub, device_id = device_identity(seed)
    store.record_enrolment(
        Enrolment(
            device_id=device_id,
            tenant_slug=tenant,
            public_key_b64=pub,
            enrolled_at="2026-01-01T00:00:00Z",
            supersedes_device_id=supersedes,
        )
    )
    return device_id


def _entry(device_id: str, seq: int, *, kind: str = "tool_call") -> dict:
    """One batch entry. ``entry_hash`` is globally unique per (device, seq),
    matching the real hash's commitment to the device inside the event."""
    return {
        "seq": seq,
        "entry_hash": hashlib.sha256(f"{device_id}:{seq}".encode()).hexdigest(),
        "event": {"kind": kind, "session_id": "ses_1", "ts_device": "2026-01-01T00:00:01Z"},
    }


def _ship(store: IngestStore, device_id: str, seqs) -> None:
    store.accept_batch(device_id, [_entry(device_id, s) for s in seqs])


# --------------------------------------------------------------------- tenancy


def test_device_of_one_tenant_never_appears_in_another_tenants_views(store):
    a = _enrol(store, "acme", "acme-1")
    b = _enrol(store, "globex", "globex-1")
    for _ in range(5):
        _ship(store, a, range(1, 20))

    assert [d["device_id"] for d in store.devices("acme")] == [a]
    assert [d["device_id"] for d in store.devices("globex")] == [b]

    cov_b = store.coverage("globex")
    reported_in_b = {
        d["device_id"] for d in cov_b["never_seen"] + cov_b["reporting"]
    } | set(cov_b["seq_gaps"])
    assert a not in reported_in_b
    assert reported_in_b == {b}


def test_tenant_report_is_unmoved_by_activity_in_another_tenant(store):
    a1 = _enrol(store, "acme", "acme-1")
    a2 = _enrol(store, "acme", "acme-2")
    _ship(store, a1, [1, 2, 4])

    before = store.coverage("acme")

    # A whole second tenant arrives, enrols devices, ships, leaves gaps.
    for i in range(3):
        b = _enrol(store, "globex", f"globex-{i}")
        _ship(store, b, [1, 5, 9])

    after = store.coverage("acme")

    assert after["enrolled"] == before["enrolled"] == 2
    assert [d["device_id"] for d in after["never_seen"]] == [a2]
    assert [d["device_id"] for d in after["reporting"]] == [a1]
    assert after["seq_gaps"] == before["seq_gaps"] == {a1: [[3, 3]]}
    assert set(after["seq_gaps"]) == {a1}


def test_batch_from_unenrolled_device_is_refused_not_auto_enrolled(store):
    _enrol(store, "acme", "acme-1")
    _, stranger = device_identity("never-enrolled")

    with pytest.raises(UnknownDeviceError) as exc:
        _ship(store, stranger, [1, 2, 3])
    assert exc.value.device_id == stranger

    # The denominator must not have moved, and no entries may have landed.
    cov = store.coverage("acme")
    assert cov["enrolled"] == 1
    assert stranger not in {d["device_id"] for d in store.devices("acme")}
    assert store.seq_gaps(stranger) == []


def test_enrolment_row_not_a_batch_is_what_sets_a_devices_tenant(store):
    """Tenancy is bound at enrolment and is not re-derivable from traffic:
    shipping into the store never introduces or moves a tenant binding."""
    a = _enrol(store, "acme", "shared-machine")
    _ship(store, a, [1, 2, 3])
    assert store.coverage("globex-does-not-exist")["enrolled"] == 0
    assert [d["device_id"] for d in store.devices("acme")] == [a]


# -------------------------------------------------------------------- identity


def test_rotation_mints_a_new_device_id_and_a_second_enrolment_row(store):
    old_pub, old_id = device_identity("machine-01-key-1")
    new_pub, new_id = device_identity("machine-01-key-2")
    assert old_pub != new_pub
    assert old_id != new_id, "device_id is derived from the public key"

    _enrol(store, "acme", "machine-01-key-1")
    _enrol(store, "acme", "machine-01-key-2", supersedes=old_id)

    rows = store.devices("acme")
    assert len(rows) == 2, "one physical machine, two key identities, two rows"

    by_id = {r["device_id"]: r for r in rows}
    assert by_id[new_id]["supersedes_device_id"] == old_id
    assert by_id[old_id]["supersedes_device_id"] is None

    # The chain is walkable from the devices() rows alone.
    chain = [new_id]
    while by_id[chain[-1]]["supersedes_device_id"]:
        chain.append(by_id[chain[-1]]["supersedes_device_id"])
    assert chain == [new_id, old_id]


def test_rotated_to_device_is_never_seen_despite_its_predecessors_history(store):
    old_id = _enrol(store, "acme", "machine-01-key-1")
    _ship(store, old_id, range(1, 101))
    new_id = _enrol(store, "acme", "machine-01-key-2", supersedes=old_id)

    cov = store.coverage("acme")
    assert cov["enrolled"] == 2
    assert [d["device_id"] for d in cov["never_seen"]] == [new_id]
    assert [d["device_id"] for d in cov["reporting"]] == [old_id]

    new_row = next(d for d in store.devices("acme") if d["device_id"] == new_id)
    assert new_row["entries_received"] == 0
    assert new_row["last_seq_received"] is None
    assert new_row["last_batch_at"] is None


# ------------------------------------------------------------- seq namespacing


def test_same_seq_on_two_devices_is_two_entries_neither_overwriting(store):
    a = _enrol(store, "acme", "dev-a")
    b = _enrol(store, "acme", "dev-b")

    ra = store.accept_batch(a, [_entry(a, 7)])
    rb = store.accept_batch(b, [_entry(b, 7)])
    assert (ra.accepted, ra.duplicates) == (1, 0)
    assert (rb.accepted, rb.duplicates) == (1, 0)

    rows = {d["device_id"]: d for d in store.devices("acme")}
    assert rows[a]["entries_received"] == 1
    assert rows[b]["entries_received"] == 1
    assert rows[a]["last_seq_received"] == rows[b]["last_seq_received"] == 7


def test_one_devices_gaps_are_unaffected_by_another_devices_seqs(store):
    a = _enrol(store, "acme", "dev-a")
    b = _enrol(store, "acme", "dev-b")

    _ship(store, a, [1, 4])          # a is missing 2..3
    _ship(store, b, [2, 3])          # exactly the numbers a is missing

    assert store.seq_gaps(a) == [[2, 3]], "b's seqs do not fill a's hole"
    assert store.seq_gaps(b) == []
    assert store.coverage("acme")["seq_gaps"] == {a: [[2, 3]]}


def test_device_seq_primary_key_holds_under_interleaved_redelivery(store):
    a = _enrol(store, "acme", "dev-a")
    b = _enrol(store, "acme", "dev-b")

    # Interleaved, overlapping batches — at-least-once delivery from two
    # devices whose seq ranges fully collide.
    for _round in range(3):
        for seqs in ([1, 2, 3], [3, 4, 5]):
            ra = store.accept_batch(a, [_entry(a, s) for s in seqs])
            rb = store.accept_batch(b, [_entry(b, s) for s in seqs])
            assert ra.accepted + ra.duplicates == len(seqs)
            assert rb.accepted + rb.duplicates == len(seqs)

    rows = {d["device_id"]: d for d in store.devices("acme")}
    assert rows[a]["entries_received"] == 5
    assert rows[b]["entries_received"] == 5
    assert store.seq_gaps(a) == store.seq_gaps(b) == []
    assert rows[a]["batches_received"] == rows[b]["batches_received"] == 6


# ------------------------------------------------- re-enrolment / other writes


def test_re_enrolling_a_device_under_another_tenant_is_refused(store):
    """Tenancy is bound once, at first enrolment. A later ``record_enrolment``
    naming a different tenant is now refused outright rather than absorbed.

    Absorbing it was not safe: the ON CONFLICT clause left ``tenant_id`` alone
    but did update ``public_key_b64``, so an operator mistake — or a caller
    holding an enrolment token for any tenant at all — could re-point a
    victim's device at a key of their choosing. The refusal keeps the device,
    its key and its evidence where they were bound.
    """
    d = _enrol(store, "acme", "dev-a")
    _ship(store, d, [1, 2, 3])
    pub, _ = device_identity("dev-a")

    with pytest.raises(EnrolmentRefused):
        _enrol(store, "globex", "dev-a")  # same public key => same device_id

    assert [r["device_id"] for r in store.devices("acme")] == [d]
    assert store.devices("globex") == []
    assert store.coverage("globex")["enrolled"] == 0
    assert store.coverage("acme")["enrolled"] == 1
    # And the key that speaks for the device is untouched.
    row = store._conn.execute(
        "SELECT public_key_b64 FROM enrolments WHERE device_id = ?", (d,)
    ).fetchone()
    assert row["public_key_b64"] == pub


def test_enrolment_requires_the_device_id_derived_from_the_public_key(store):
    """The id/key binding is what makes every other tenancy check meaningful:
    without it any id can be enrolled against an unrelated key, which is the
    primitive behind cross-tenant key substitution."""
    pub, device_id = device_identity("bound")
    _, other_id = device_identity("someone-else")

    with pytest.raises(EnrolmentRefused):
        store.record_enrolment(
            Enrolment(
                device_id=other_id,
                tenant_slug="acme",
                public_key_b64=pub,
                enrolled_at="2026-01-01T00:00:00Z",
            )
        )
    assert store.devices("acme") == []
    assert device_id != other_id


def test_supersedes_must_name_a_device_in_the_same_tenant(store):
    """``supersedes_device_id`` is the only thread back to a predecessor, so a
    cross-tenant link would fabricate a rotation lineage across a boundary —
    and render a foreign tenant's device_id inside this tenant's report."""
    foreign = _enrol(store, "globex", "globex-machine")

    with pytest.raises(EnrolmentRefused):
        _enrol(store, "acme", "acme-machine", supersedes=foreign)

    assert store.devices("acme") == []
    assert [d["device_id"] for d in store.devices("globex")] == [foreign]


def test_checkpoints_from_unenrolled_device_are_refused_like_batches(store):
    """Both ingest paths gate on enrolment and fail the same way, so a caller
    can handle unknown devices uniformly instead of catching a raw sqlite
    foreign-key error from one of them."""
    _enrol(store, "acme", "dev-a")
    _, stranger = device_identity("never-enrolled")
    cp = {"seq_start": 1, "seq_end": 9, "chain_head": "h", "ts_device": "t", "sig": "s"}

    with pytest.raises(UnknownDeviceError):
        store.accept_checkpoints(stranger, [cp])
