"""Ingest read model — at-least-once delivery, coverage, and gap reporting.

These tests pin the constraints stated in ``byoai/ingest/store.py``'s module
docstring: the shipper only advances its watermark after a 2xx, so any batch
can arrive twice; and every coverage figure is computed against enrolments,
never against the set of devices that happened to ship.
"""

from __future__ import annotations

from tests.ingest.conftest import device_identity

from byoai.ingest.store import Enrolment, IngestStore

TENANT = "acme"
# Real derived identities: record_enrolment refuses any device_id that is not
# derive_device_id(public_key_b64), so these can no longer be literals.
DEVICE_PUB, DEVICE = device_identity("delivery-primary")
OTHER_PUB, OTHER = device_identity("delivery-other")
_PUBKEYS = {DEVICE: DEVICE_PUB, OTHER: OTHER_PUB}


def enrol(store: IngestStore, device_id: str = DEVICE, tenant: str = TENANT) -> None:
    store.record_enrolment(
        Enrolment(
            device_id=device_id,
            tenant_slug=tenant,
            public_key_b64=_PUBKEYS[device_id],
            enrolled_at="2026-08-01T00:00:00.000000Z",
        )
    )


def entry(seq: int, *, device_id: str = DEVICE, kind: str = "tool_use") -> dict:
    """One wire entry in the shape ``shipper.ship_once`` posts."""
    return {
        "seq": seq,
        "entry_hash": f"h_{device_id}_{seq}",
        "event": {
            "kind": kind,
            "device_id": device_id,
            "seq": seq,
            "session_id": "sess_1",
            "ts_device": "2026-08-01T00:00:00.000000Z",
            "trace_id": "trace_1",
        },
    }


def state(store: IngestStore, device_id: str = DEVICE, tenant: str = TENANT) -> dict:
    (row,) = [d for d in store.devices(tenant) if d["device_id"] == device_id]
    return row


# ------------------------------------------------------- at-least-once delivery


def test_identical_redelivery_is_all_duplicates_and_changes_no_stored_state(store):
    enrol(store)
    batch = [entry(1), entry(2), entry(3)]

    first = store.accept_batch(DEVICE, batch)
    assert (first.accepted, first.duplicates) == (3, 0)
    before = state(store)

    second = store.accept_batch(DEVICE, batch)
    assert (second.accepted, second.duplicates) == (0, 3)

    after = state(store)
    assert after["entries_received"] == before["entries_received"] == 3
    assert after["last_seq_received"] == before["last_seq_received"] == 3
    assert after["first_batch_at"] == before["first_batch_at"]


def test_partially_overlapping_redelivery_splits_into_duplicates_and_accepted(store):
    enrol(store)
    store.accept_batch(DEVICE, [entry(1), entry(2), entry(3)])

    # A crash after commit but before the watermark write resends from 2.
    result = store.accept_batch(DEVICE, [entry(2), entry(3), entry(4), entry(5)])

    assert result.duplicates == 2
    assert result.accepted == 2
    assert state(store)["entries_received"] == 5


def test_counts_do_not_inflate_under_repeated_redelivery(store):
    enrol(store)
    batch = [entry(1), entry(2), entry(3)]
    for _ in range(4):
        store.accept_batch(DEVICE, batch)

    row = state(store)
    assert row["entries_received"] == 3
    assert row["last_seq_received"] == 3
    # The batch counter is the one number that legitimately rises: four POSTs
    # really did arrive, even though they carried nothing new.
    assert row["batches_received"] == 4


def test_redelivery_after_a_gap_was_filled_reports_the_gap_as_closed(store):
    enrol(store)
    store.accept_batch(DEVICE, [entry(1), entry(4)])
    store.accept_batch(DEVICE, [entry(2), entry(3)])

    replay = store.accept_batch(DEVICE, [entry(1), entry(4)])

    assert (replay.accepted, replay.duplicates) == (0, 2)
    assert replay.gaps == []
    assert store.seq_gaps(DEVICE) == []


# ------------------------------------------------------------------- coverage


def test_enrolled_device_that_never_ships_is_in_never_seen_and_counted_as_enrolled(store):
    enrol(store)

    cov = store.coverage(TENANT)

    assert cov["enrolled"] == 1
    assert [d["device_id"] for d in cov["never_seen"]] == [DEVICE]
    assert cov["reporting"] == []


def test_device_that_shipped_once_and_stopped_is_reporting_not_never_seen(store):
    enrol(store)
    enrol(store, OTHER)
    store.accept_batch(DEVICE, [entry(1)])

    cov = store.coverage(TENANT)

    assert [d["device_id"] for d in cov["reporting"]] == [DEVICE]
    assert [d["device_id"] for d in cov["never_seen"]] == [OTHER]


def test_enrolling_a_device_raises_enrolled_with_no_batch_arriving(store):
    enrol(store)
    assert store.coverage(TENANT)["enrolled"] == 1

    enrol(store, OTHER)

    assert store.coverage(TENANT)["enrolled"] == 2


def test_devices_without_checkpoint_lists_devices_with_entries_but_no_checkpoints(store):
    enrol(store)
    enrol(store, OTHER)
    store.accept_batch(DEVICE, [entry(1), entry(2)])
    store.accept_batch(OTHER, [entry(1, device_id=OTHER)])
    store.accept_checkpoints(
        OTHER,
        [
            {
                "seq_start": 1,
                "seq_end": 1,
                "chain_head": "head_1",
                "ts_device": "2026-08-01T00:00:00.000000Z",
                "sig": "sig_1",
            }
        ],
    )

    cov = store.coverage(TENANT)

    # Entry and checkpoint streams ship on independent watermarks, so entries
    # ahead of checkpoints is a normal state, not corruption.
    assert [d["device_id"] for d in cov["devices_without_checkpoint"]] == [DEVICE]
    assert state(store, OTHER)["last_checkpoint_seq"] == 1


def test_coverage_on_a_tenant_with_no_enrolments_returns_zero_rather_than_raising(store):
    store.ensure_tenant("empty-co")

    cov = store.coverage("empty-co")

    assert cov["enrolled"] == 0
    assert cov["never_seen"] == []
    assert cov["reporting"] == []
    assert cov["devices_without_checkpoint"] == []
    assert cov["seq_gaps"] == {}


def test_coverage_states_its_blind_spot_and_names_enrolments_as_the_basis(store):
    enrol(store)

    blind_spot = store.coverage(TENANT)["blind_spot"]

    assert blind_spot["basis"] == "enrolments"
    assert "enrol" in blind_spot["statement"].lower()


# ----------------------------------------------------------------------- gaps


def test_interior_hole_is_a_gap_but_stopping_early_is_not(store):
    enrol(store)
    enrol(store, OTHER)
    store.accept_batch(DEVICE, [entry(1), entry(2), entry(5), entry(6)])
    # OTHER simply stops at 3: indistinguishable from having exactly 3 events,
    # so the store must not invent a gap it cannot know about.
    store.accept_batch(OTHER, [entry(n, device_id=OTHER) for n in (1, 2, 3)])

    assert store.seq_gaps(DEVICE) == [[3, 4]]
    assert store.seq_gaps(OTHER) == []
    assert store.coverage(TENANT)["seq_gaps"] == {DEVICE: [[3, 4]]}


def test_filling_a_hole_with_a_later_batch_removes_it_from_seq_gaps(store):
    enrol(store)
    first = store.accept_batch(DEVICE, [entry(1), entry(4)])
    assert first.gaps == [[2, 3]]

    filled = store.accept_batch(DEVICE, [entry(2), entry(3)])

    assert filled.gaps == []
    assert store.coverage(TENANT)["seq_gaps"] == {}


def test_duplicate_only_redelivery_is_contact_not_evidence(store):
    """Redelivery proves the device reached us; it does not prove the device
    is still producing. The store keeps those apart: a wholly-duplicate batch
    bumps last_contact_at and batches_received, but last_batch_at — the
    liveness signal coverage() reads — only moves when something new lands.
    Otherwise a device replaying one captured batch stays out of the silence
    report forever at zero cost."""
    enrol(store)
    store.accept_batch(DEVICE, [entry(1)])
    before = state(store)["last_batch_at"]

    replay = store.accept_batch(DEVICE, [entry(1)])
    assert (replay.accepted, replay.duplicates) == (0, 1)

    after = state(store)
    assert after["last_batch_at"] == before
    assert after["batches_received"] == 2
    contact = store._conn.execute(
        "SELECT last_contact_at FROM device_state WHERE device_id = ?", (DEVICE,)
    ).fetchone()["last_contact_at"]
    assert contact > before


def test_an_empty_batch_never_moves_a_device_out_of_never_seen(store):
    """The degenerate case of the same rule: zero entries is contact with no
    evidence at all, so the coverage denominator does not move."""
    enrol(store)

    result = store.accept_batch(DEVICE, [])

    assert (result.accepted, result.duplicates, result.gaps) == (0, 0, [])
    cov = store.coverage(TENANT)
    assert [d["device_id"] for d in cov["never_seen"]] == [DEVICE]
    assert cov["reporting"] == []
    assert state(store)["batches_received"] == 1


def test_checkpoint_only_device_is_flagged_as_talking_without_shipping_evidence(store):
    """A device can communicate without producing evidence — a third state.

    Checkpoints record contact but deliberately do not count as evidence:
    nothing validates a checkpoint's seq_end against entries actually
    received, so crediting it would let a device that has stopped recording
    forge its own liveness with a signed claim about history it never shipped.

    Folding that into `never_seen` alone would call an actively-communicating
    device unheard-of; folding it into `reporting` would credit it with
    evidence it never sent. It is neither, and gets its own list.
    """
    pk, device_id = device_identity("checkpoint-only")
    store.record_enrolment(Enrolment(device_id, "acme", pk, "2026-08-01T00:00:00Z"))

    fresh = store.coverage("acme")
    assert [d["device_id"] for d in fresh["never_seen"]] == [device_id]
    assert fresh["contact_without_evidence"] == []

    store.accept_checkpoints(
        device_id,
        [{"seq_start": 1, "seq_end": 9, "chain_head": "h", "ts_device": "t", "sig": "s"}],
    )
    talking = store.coverage("acme")
    assert [d["device_id"] for d in talking["contact_without_evidence"]] == [device_id]
    # Still without evidence, so still short of `reporting`.
    assert talking["reporting"] == []
    assert [d["device_id"] for d in talking["never_seen"]] == [device_id]

    store.accept_batch(
        device_id, [{"seq": 1, "entry_hash": "cp-only-h1", "event": {"kind": "message"}}]
    )
    reporting = store.coverage("acme")
    assert [d["device_id"] for d in reporting["reporting"]] == [device_id]
    assert reporting["never_seen"] == []
    assert reporting["contact_without_evidence"] == []
