"""Adversarial tests against IngestStore's tenancy and integrity boundary.

Threat model: a compromised or malicious *device* (and, for the enrolment
path, a caller holding a valid enrolment token for SOME tenant). Each test
below is an exploit attempt, and each docstring records the attack it was
written for.

Most of these started life as ``test_hole_*`` — tests that asserted the BAD
outcome because the hole was real. They are now ``test_defends_*``: same
attack, and the assertion is that it fails. The few that remain
``test_hole_*`` are holes the hardening did not close; they document
behaviour that is still exploitable, not behaviour that is endorsed.
``test_holds_*`` are defences that were there all along.
"""

from __future__ import annotations

import pytest
from tests.ingest.conftest import device_identity

from byoai.ingest.store import (
    CheckpointConflict,
    DeviceRevoked,
    Enrolment,
    EnrolmentRefused,
    EntryHashCollision,
    MalformedEntry,
    SeqConflict,
    UnknownDeviceError,
)


def _enrol(store, tenant, seed, *, supersedes=None, interval=None):
    pub, did = device_identity(seed)
    store.record_enrolment(
        Enrolment(
            device_id=did,
            tenant_slug=tenant,
            public_key_b64=pub,
            enrolled_at="2026-01-01T00:00:00Z",
            supersedes_device_id=supersedes,
            expected_interval_s=interval,
        )
    )
    return did


def _entry(device_id, seq, *, entry_hash=None, kind="tool_call", event=None):
    return {
        "seq": seq,
        "entry_hash": entry_hash or f"h:{device_id}:{seq}",
        "event": event if event is not None else {"kind": kind, "device_id": device_id},
    }


def _tenant_of(store, device_id):
    row = store._conn.execute(
        "SELECT t.slug FROM enrolments e JOIN tenants t USING (tenant_id) WHERE e.device_id = ?",
        (device_id,),
    ).fetchone()
    return None if row is None else row["slug"]


def _count(store, device_id):
    return store._conn.execute(
        "SELECT COUNT(*) c FROM entries WHERE device_id = ?", (device_id,)
    ).fetchone()["c"]


def _state(store, device_id):
    return store._conn.execute(
        "SELECT * FROM device_state WHERE device_id = ?", (device_id,)
    ).fetchone()


# =================================================== 1. cross-tenant write ==


def test_defends_reenrolment_cannot_move_device_to_another_tenant(store):
    """Re-enrolling a device under a second tenant used to be absorbed
    silently: ON CONFLICT left tenant_id alone, so the device stayed put but
    the caller got no signal. It is now a refusal, and the device is still
    where it was bound."""
    did = _enrol(store, "tenant-a", "dev1")
    with pytest.raises(EnrolmentRefused):
        _enrol(store, "tenant-b", "dev1")
    assert _tenant_of(store, did) == "tenant-a"
    assert [d["device_id"] for d in store.devices("tenant-b")] == []


def test_defends_reenrolment_cannot_overwrite_the_key_of_another_tenants_device(store):
    """CROSS-TENANT KEY SUBSTITUTION.

    ON CONFLICT DID update public_key_b64, and the store never checked that
    device_id == derive_device_id(public_key_b64). So an attacker holding a
    valid tenant-b enrolment token, who knows a tenant-a device_id (it is
    public: it appears in tenant-a's reports and in every batch header), could
    re-enrol that device_id with the ATTACKER's own key. The row stayed in
    tenant-a; the key that authenticated it became the attacker's, and every
    subsequent signed batch from the attacker was admitted as tenant-a
    evidence.

    Two independent refusals now stop this: the id/key derivation check, and
    the cross-tenant re-enrolment check.
    """
    victim_pub, victim = device_identity("victim")
    _enrol(store, "tenant-a", "victim")
    attacker_pub, _ = device_identity("attacker")

    with pytest.raises(EnrolmentRefused):
        store.record_enrolment(
            Enrolment(
                device_id=victim,  # not derived from attacker_pub
                tenant_slug="tenant-b",
                public_key_b64=attacker_pub,
                enrolled_at="2026-06-01T00:00:00Z",
            )
        )
    row = store._conn.execute(
        "SELECT public_key_b64 FROM enrolments WHERE device_id = ?", (victim,)
    ).fetchone()
    assert row["public_key_b64"] == victim_pub
    assert _tenant_of(store, victim) == "tenant-a"

    # Even attempted from inside tenant-a — where the tenant check would not
    # fire — the derivation check refuses the substitution.
    with pytest.raises(EnrolmentRefused):
        store.record_enrolment(
            Enrolment(
                device_id=victim,
                tenant_slug="tenant-a",
                public_key_b64=attacker_pub,
                enrolled_at="2026-06-01T00:00:00Z",
            )
        )
    row = store._conn.execute(
        "SELECT public_key_b64 FROM enrolments WHERE device_id = ?", (victim,)
    ).fetchone()
    assert row["public_key_b64"] == victim_pub


def test_defends_enrolment_rejects_device_id_not_derived_from_public_key(store):
    """There was no binding check at all: any (device_id, key) pair was
    accepted, which is the primitive every other enrolment attack was built
    on. An arbitrary identifier is now refused and nothing is written."""
    pub, _ = device_identity("keyseed")
    with pytest.raises(EnrolmentRefused) as exc:
        store.record_enrolment(
            Enrolment(
                device_id="dev_TOTALLY_MADE_UP",
                tenant_slug="tenant-a",
                public_key_b64=pub,
                enrolled_at="2026-01-01T00:00:00Z",
            )
        )
    assert "dev_TOTALLY_MADE_UP" in str(exc.value)
    assert store.devices("tenant-a") == []
    assert _tenant_of(store, "dev_TOTALLY_MADE_UP") is None


def test_holds_sql_metacharacters_are_inert(store):
    """Wildcards/quotes: everything is parameterised and matched with `=`.

    device_id is now derived and so cannot carry metacharacters, but the
    tenant slug is still free-form caller input and reaches the same queries.
    """
    weird_tenant = "tenant-%' OR '1'='1"
    odd = _enrol(store, weird_tenant, "weird")
    victim = _enrol(store, "tenant-b", "victim2")
    store.accept_batch(odd, [_entry(odd, 1)])

    assert [d["device_id"] for d in store.devices("tenant-b")] == [victim]
    assert store.coverage("tenant-b")["enrolled"] == 1
    # '%' is not a wildcard here, and the injected quote did not break out.
    assert [d["device_id"] for d in store.devices(weird_tenant)] == [odd]


def test_holds_unenrolled_device_cannot_write(store):
    with pytest.raises(UnknownDeviceError):
        store.accept_batch("dev_GHOST", [_entry("dev_GHOST", 1)])
    with pytest.raises(UnknownDeviceError):
        store.accept_checkpoints("dev_GHOST", [])


# ==================================================== 2. cross-tenant read ==


def test_holds_devices_and_coverage_are_tenant_scoped(store):
    a = _enrol(store, "tenant-a", "a1")
    b = _enrol(store, "tenant-b", "b1")
    store.accept_batch(b, [_entry(b, 1), _entry(b, 3)])
    cov_a = store.coverage("tenant-a")
    assert [d["device_id"] for d in store.devices("tenant-a")] == [a]
    assert cov_a["enrolled"] == 1
    assert cov_a["seq_gaps"] == {}
    assert b not in [d["device_id"] for d in cov_a["never_seen"]]


def test_defends_supersedes_device_id_cannot_name_a_foreign_tenants_device(store):
    """supersedes_device_id was FK-checked against enrolments but NOT against
    the same tenant, so tenant-b could point at a tenant-a device and
    tenant-b's devices() would render that foreign device_id — a leak, and a
    fabricated rotation lineage across a tenant boundary."""
    a = _enrol(store, "tenant-a", "a2")

    with pytest.raises(EnrolmentRefused):
        _enrol(store, "tenant-b", "b2", supersedes=a)

    assert store.devices("tenant-b") == []
    assert a not in str(store.devices("tenant-b"))

    # A predecessor that does not exist at all is refused the same way.
    _, ghost = device_identity("ghost-predecessor")
    with pytest.raises(EnrolmentRefused):
        _enrol(store, "tenant-b", "b3", supersedes=ghost)


# ============================================= 3. denominator manipulation ==


def test_hole_any_enrolment_token_can_inflate_another_tenants_denominator(store):
    """STILL OPEN. ensure_tenant() resolves an existing slug, so a caller who
    can enrol at all can enrol devices into someone else's slug: `enrolled`
    and `never_seen` both move, drowning the real never_seen list.

    The device_id/key binding raised the cost — each junk device now needs a
    real keypair — but generating fifty Ed25519 keys is free. Closing this
    needs authorisation on the enrolment call, which is above this store's
    layer; the store has no notion of who is asking.
    """
    real = _enrol(store, "acme", "real")
    for i in range(50):
        _enrol(store, "acme", f"junk{i}")
    cov = store.coverage("acme")
    assert cov["enrolled"] == 51
    assert len(cov["never_seen"]) == 51
    assert real in [d["device_id"] for d in cov["never_seen"]]


def test_defends_empty_batch_does_not_remove_a_device_from_never_seen(store):
    """LIVENESS/DENOMINATOR FORGERY. accept_batch() called _touch_state()
    unconditionally, so a batch with zero entries set last_batch_at and the
    device left never_seen for `reporting` without ever shipping a single
    piece of evidence.

    Contact is now recorded separately from evidence: the POST is counted, the
    liveness signal is not moved.
    """
    did = _enrol(store, "acme", "quiet")
    assert store.coverage("acme")["never_seen"][0]["device_id"] == did

    res = store.accept_batch(did, [])
    assert (res.accepted, res.duplicates, res.gaps) == (0, 0, [])

    cov = store.coverage("acme")
    assert [d["device_id"] for d in cov["never_seen"]] == [did]
    assert cov["reporting"] == []

    st = _state(store, did)
    assert st["last_batch_at"] is None and st["first_batch_at"] is None
    # The contact itself is not hidden — it is just not evidence.
    assert st["batches_received"] == 1
    assert st["last_contact_at"] is not None


def test_holds_device_cannot_deflate_another_tenants_counts(store):
    a = _enrol(store, "tenant-a", "a3")
    _enrol(store, "tenant-a", "a4")
    hostile = _enrol(store, "tenant-b", "hostile")
    store.accept_batch(hostile, [_entry(hostile, i) for i in range(1, 20)])
    assert store.coverage("tenant-a")["enrolled"] == 2
    assert len(store.coverage("tenant-a")["never_seen"]) == 2
    assert a in [d["device_id"] for d in store.coverage("tenant-a")["never_seen"]]


# ==================================================== 4. entry_hash trust ==


def test_defends_entry_hash_squat_cannot_suppress_another_devices_entry(store):
    """EVIDENCE SUPPRESSION ACROSS TENANTS.

    idx_entries_hash was a GLOBAL unique index and accept_batch used
    INSERT OR IGNORE. entry_hash is attacker-chosen wire data. A device in
    tenant-b that guessed or knew a hash a tenant-a device was about to ship
    could insert it first; the victim's genuine entry was then silently
    dropped and reported back as a DUPLICATE, so the shipper advanced its
    watermark and never retried. The evidence was destroyed and the store
    reported success.

    Dedupe is now per-device, and a hash held by another device raises rather
    than being mistaken for redelivery — the victim's shipper learns the
    batch did not land and will resend it.
    """
    victim = _enrol(store, "tenant-a", "victim3")
    attacker = _enrol(store, "tenant-b", "attacker3")

    doomed = "hash-the-victim-will-use"
    store.accept_batch(attacker, [_entry(attacker, 1, entry_hash=doomed)])

    with pytest.raises(EntryHashCollision) as exc:
        store.accept_batch(
            victim,
            [_entry(victim, 7, entry_hash=doomed, event={"kind": "policy_denial"})],
        )
    assert exc.value.device_id == victim
    assert exc.value.hashes == [doomed]
    # Nothing was swallowed and nothing was mislabelled as redelivery: the
    # victim's entry is simply not stored, and the failure is loud.
    assert _count(store, victim) == 0
    assert store.coverage("tenant-a")["reporting"] == []
    assert [d["device_id"] for d in store.coverage("tenant-a")["never_seen"]] == [victim]


def test_defends_entry_hash_squat_cannot_punch_a_hole_mid_stream(store):
    """Same primitive, aimed at a mid-stream seq: the victim used to end up
    with a real hole in its chain that it would never resend. The batch is now
    refused whole, so the victim retries rather than skipping past seq 5."""
    victim = _enrol(store, "tenant-a", "v4")
    attacker = _enrol(store, "tenant-b", "a4x")
    store.accept_batch(attacker, [_entry(attacker, 99, entry_hash=f"h:{victim}:5")])

    with pytest.raises(EntryHashCollision):
        store.accept_batch(victim, [_entry(victim, i) for i in range(1, 10)])

    # Atomic refusal: not one of the nine entries landed, so there is no
    # half-stored batch for the victim to be confused about later.
    assert _count(store, victim) == 0
    assert store.seq_gaps(victim) == []


def test_hole_same_device_can_bind_one_hash_to_a_different_seq(store):
    """STILL OPEN (by design of the dedupe key). A malicious device can
    pre-claim its own future hashes, blocking real entries at whatever seq it
    likes. Per-device scoping closed the cross-device version of this; within
    one device, an entry_hash reused at another seq is indistinguishable on
    the wire from redelivery, so it is still reported as a duplicate.

    The remaining exposure is a device censoring its own evidence — which it
    could equally do by never shipping it — but a customer relying on that
    device's stream still sees success for an entry that was never stored.
    """
    did = _enrol(store, "tenant-a", "self")
    store.accept_batch(did, [_entry(did, 1, entry_hash="H")])
    res = store.accept_batch(did, [_entry(did, 2, entry_hash="H")])
    assert res.duplicates == 1
    assert [
        r["seq"]
        for r in store._conn.execute(
            "SELECT seq FROM entries WHERE device_id = ?", (did,)
        ).fetchall()
    ] == [1]


def test_defends_existing_seq_cannot_be_overwritten_and_the_attempt_is_raised(store):
    """No in-place tampering with a stored entry, even by the device that
    wrote it — and the attempt is now surfaced rather than absorbed.

    The PK (device_id, seq) plus OR IGNORE always kept the original body, but
    a rewrite used to be reported back as an ordinary duplicate. Same seq with
    different content is a contradiction in the evidence, so it raises: the
    store already refuses cross-device hash collisions, and swallowing this
    one left no record that a conflicting body was ever offered.
    """
    did = _enrol(store, "tenant-a", "immut")
    store.accept_batch(did, [_entry(did, 1, event={"kind": "tool_call"})])

    with pytest.raises(SeqConflict) as excinfo:
        store.accept_batch(
            did, [_entry(did, 1, entry_hash="different", event={"kind": "REWRITTEN"})]
        )
    assert excinfo.value.seq == 1
    assert excinfo.value.offered_entry_hash == "different"

    row = store._conn.execute(
        "SELECT kind, entry_hash FROM entries WHERE device_id = ? AND seq = 1", (did,)
    ).fetchone()
    assert row["kind"] == "tool_call" and row["entry_hash"] == f"h:{did}:1"


# ================================================= 5. data shape attacks ==


def test_defends_malformed_entry_leaves_no_partially_committed_batch(store):
    """isolation_level=None meant every INSERT autocommitted, so a failure
    partway through a batch left the earlier rows persisted, skipped
    _touch_state and lost the AcceptResult — the shipper never learned what
    landed and resent the whole batch over rows that were already there.

    The batch is one transaction now: a malformed entry rolls all of it back.
    """
    did = _enrol(store, "tenant-a", "shape")
    with pytest.raises(MalformedEntry):
        store.accept_batch(did, [_entry(did, 1), {"seq": 2}])
    assert _count(store, did) == 0
    # Consistent with storing nothing, the device is still never-seen.
    assert store.coverage("tenant-a")["never_seen"][0]["device_id"] == did


def test_defends_negative_and_bool_seq_are_refused(store):
    """seq was unvalidated: int() took negatives, bools and 2**63-scale ints.
    One entry at seq -1 next to one at 2**62 made seq_gaps emit a single range
    spanning the whole integer line — a one-batch denial of service against
    the coverage report."""
    did = _enrol(store, "tenant-a", "seqs")
    with pytest.raises(MalformedEntry):
        store.accept_batch(
            did,
            [
                _entry(did, -1, entry_hash="n1"),
                _entry(did, True, entry_hash="n2"),
                _entry(did, 2**62, entry_hash="n3"),
            ],
        )
    assert _count(store, did) == 0
    assert store.seq_gaps(did) == []

    # Each shape is refused on its own, not merely as part of that batch.
    for bad in (-1, True, 1.0, "0007", None):
        with pytest.raises(MalformedEntry):
            store.accept_batch(did, [_entry(did, bad, entry_hash="solo")])
    assert _count(store, did) == 0


def test_defends_string_seq_is_not_coerced_silently(store):
    """A numeric string used to be coerced to an int, and a float that no int
    can hold raised a raw sqlite/overflow error rather than a typed one.
    Silent coercion is how a malformed batch becomes a plausible-looking row
    nobody questions."""
    did = _enrol(store, "tenant-a", "coerce")
    with pytest.raises(MalformedEntry):
        store.accept_batch(did, [{"seq": "0007", "entry_hash": "s1", "event": {"kind": "k"}}])
    with pytest.raises(MalformedEntry):
        store.accept_batch(did, [{"seq": 1.5e400, "entry_hash": "s2", "event": {}}])
    assert store._conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0


def test_defends_empty_or_non_string_entry_hash_is_refused(store):
    """entry_hash is the dedupe key. An empty or non-string one would make
    every such entry collide with the next, quietly turning fresh evidence
    into "duplicates"."""
    did = _enrol(store, "tenant-a", "hashshape")
    for bad in ("", None, 17, ["h"]):
        # Built inline rather than via _entry(): a falsy entry_hash must reach
        # the store as written, not be replaced by the helper's default.
        with pytest.raises(MalformedEntry):
            store.accept_batch(
                did, [{"seq": 1, "entry_hash": bad, "event": {"kind": "tool_call"}}]
            )
    assert _count(store, did) == 0


def test_defends_event_device_id_may_not_disagree_with_authenticated_device(store):
    """The stored event_json could name a different device than the row's
    device_id: the store never cross-checked, and event_json is what an
    exported evidence bundle shows a reader. A tenant-a device could therefore
    file evidence that reads as a tenant-b device's."""
    a = _enrol(store, "tenant-a", "d1")
    b = _enrol(store, "tenant-b", "d2")

    with pytest.raises(MalformedEntry) as exc:
        store.accept_batch(a, [_entry(a, 1, event={"kind": "tool_call", "device_id": b})])
    assert b in str(exc.value) and a in str(exc.value)
    assert _count(store, a) == 0

    # The agreeing case is still accepted, and so is an event that simply
    # omits the field — the shipper does not always include it.
    store.accept_batch(a, [_entry(a, 1, event={"kind": "tool_call", "device_id": a})])
    store.accept_batch(a, [_entry(a, 2, event={"kind": "tool_call"})])
    assert _count(store, a) == 2


def test_defends_duplicate_seq_within_one_batch_is_a_contradiction_not_redelivery(store):
    """Two entries at the same seq with different hashes in ONE batch.

    Previously the second lost to the (device_id, seq) primary key and was
    counted as a duplicate, so a device claiming two different entries at one
    position in its own chain looked like an ordinary resend. It now raises,
    and because the batch is atomic neither entry is stored — the device is
    told its batch was contradictory rather than half-accepted.
    """
    did = _enrol(store, "tenant-a", "dupseq")
    with pytest.raises(SeqConflict):
        store.accept_batch(
            did, [_entry(did, 1, entry_hash="x1"), _entry(did, 1, entry_hash="x2")]
        )
    n = store._conn.execute(
        "SELECT COUNT(*) n FROM entries WHERE device_id = ?", (did,)
    ).fetchone()["n"]
    assert n == 0


def test_hole_no_size_limit_on_event_payload(store):
    """STILL OPEN. A single entry carries an unbounded event dict straight
    into the DB, so batch size is the only thing standing between a device and
    the disk behind this store."""
    did = _enrol(store, "tenant-a", "big")
    big = {"kind": "tool_call", "blob": "A" * (4 * 1024 * 1024)}
    store.accept_batch(did, [_entry(did, 1, event=big)])
    n = store._conn.execute("SELECT LENGTH(event_json) n FROM entries").fetchone()["n"]
    assert n > 4_000_000


def test_defends_non_dict_event_raises_a_typed_error(store):
    """A non-dict event used to escape as an AttributeError from deep inside
    the insert — untyped, so a caller could not tell malformed wire data from
    a store bug, and the partially-written batch stayed behind it."""
    did = _enrol(store, "tenant-a", "nd")
    with pytest.raises(MalformedEntry):
        store.accept_batch(did, [{"seq": 1, "entry_hash": "z", "event": "not-a-dict"}])
    assert _count(store, did) == 0


def test_defends_inverted_checkpoint_range_is_refused(store):
    """A checkpoint claiming seq_end < seq_start is not a range at all, and it
    used to be stored — with last_checkpoint_seq then reporting the *end* of a
    backwards interval as the device's progress."""
    did = _enrol(store, "tenant-a", "cp")
    with pytest.raises(MalformedEntry):
        store.accept_checkpoints(
            did,
            [{"seq_start": 500, "seq_end": 1, "chain_head": "x", "ts_device": "t", "sig": "s"}],
        )
    assert store.devices("tenant-a")[0]["last_checkpoint_seq"] is None
    for bad in ({"seq_start": -1, "seq_end": 5}, {"seq_start": 1, "seq_end": "9"}):
        with pytest.raises(MalformedEntry):
            store.accept_checkpoints(
                did, [{**bad, "chain_head": "x", "ts_device": "t", "sig": "s"}]
            )


def test_hole_checkpoint_may_claim_a_seq_range_with_no_entries_behind_it(store):
    """STILL OPEN. Nothing cross-checks a checkpoint against the entries
    actually received, so a device can claim a chain head at seq 10**9 having
    shipped one entry. The store records what arrived; proving a chain head is
    a device-side, offline act (see the module docstring), but the number is
    still rendered as this device's checkpoint progress."""
    did = _enrol(store, "tenant-a", "cpclaim")
    store.accept_batch(did, [_entry(did, 1)])
    store.accept_checkpoints(
        did,
        [{"seq_start": 1, "seq_end": 10**9, "chain_head": "x", "ts_device": "t", "sig": "s"}],
    )
    assert store.devices("tenant-a")[0]["last_checkpoint_seq"] == 10**9
    assert store.devices("tenant-a")[0]["last_seq_received"] == 1


# ====================================================== 6. liveness lies ==


def test_holds_ts_device_cannot_move_last_batch_at(store):
    """last_batch_at is the server clock, not the untrusted host clock."""
    did = _enrol(store, "tenant-a", "clock")
    store.accept_batch(
        did, [_entry(did, 1, event={"kind": "k", "ts_device": "2099-01-01T00:00:00Z"})]
    )
    assert store.devices("tenant-a")[0]["last_batch_at"].startswith("20")
    assert not store.devices("tenant-a")[0]["last_batch_at"].startswith("2099")


def test_defends_replaying_old_entries_does_not_keep_a_dead_device_looking_alive(store):
    """Every batch used to bump last_batch_at even when 100% of it was
    duplicate, so a device whose agent had stopped emitting — or an attacker
    replaying one captured batch — stayed out of every silence report forever,
    at zero cost. last_batch_at now only moves when new evidence arrives."""
    did = _enrol(store, "tenant-a", "zombie")
    store.accept_batch(did, [_entry(did, 1)])
    before = store.devices("tenant-a")[0]["last_batch_at"]

    for _ in range(5):
        res = store.accept_batch(did, [_entry(did, 1)])
        assert res.accepted == 0 and res.duplicates == 1

    d = store.devices("tenant-a")[0]
    assert d["last_batch_at"] == before, "replay is contact, not evidence"
    assert d["batches_received"] == 6
    assert d["entries_received"] == 1  # six "batches", one entry ever
    # Contact is still recorded, for diagnosing a device that is alive but
    # producing nothing — it is just not the liveness signal.
    assert _state(store, did)["last_contact_at"] > before


def test_defends_a_device_that_only_ever_replays_stays_never_seen(store):
    """The extreme form of the same lie: a device whose very first batch is
    wholly duplicate hash-squatting on itself can never buy its way out of
    never_seen without producing evidence."""
    did = _enrol(store, "tenant-a", "replay-only")
    store.accept_batch(did, [])
    store.accept_batch(did, [])
    cov = store.coverage("tenant-a")
    assert [d["device_id"] for d in cov["never_seen"]] == [did]
    assert cov["reporting"] == []
    assert _state(store, did)["batches_received"] == 2


def test_hole_checkpoint_only_traffic_updates_liveness_without_evidence(store):
    """STILL OPEN. accept_checkpoints() touches last_checkpoint_at with an
    unvalidated, signature-unchecked checkpoint, so devices_without_checkpoint
    clears on the device's say-so. Note the blast radius is smaller than it
    was: checkpoints do not touch last_batch_at, so this cannot move a device
    out of never_seen — only out of devices_without_checkpoint."""
    did = _enrol(store, "tenant-a", "cponly")
    store.accept_batch(did, [_entry(did, 1)])
    assert store.coverage("tenant-a")["devices_without_checkpoint"] != []
    store.accept_checkpoints(
        did, [{"seq_start": 1, "seq_end": 1, "chain_head": "c", "ts_device": "t", "sig": "s"}]
    )
    assert store.coverage("tenant-a")["devices_without_checkpoint"] == []


def test_defends_revoked_device_cannot_write_on_either_path(store):
    """revoked_at was surfaced but nothing in accept_batch consulted it, so a
    device that had been cut off could keep writing into the record it was cut
    off from. Both ingest paths now refuse it — if only one did, the
    denominator could be moved through the quieter one."""
    did = _enrol(store, "tenant-a", "rev")
    store.accept_batch(did, [_entry(did, 1)])
    store._conn.execute(
        "UPDATE enrolments SET revoked_at = ? WHERE device_id = ?", ("2026-02-01T00:00:00Z", did)
    )

    with pytest.raises(DeviceRevoked) as exc:
        store.accept_batch(did, [_entry(did, 2)])
    assert exc.value.device_id == did and exc.value.revoked_at == "2026-02-01T00:00:00Z"

    with pytest.raises(DeviceRevoked):
        store.accept_checkpoints(
            did, [{"seq_start": 1, "seq_end": 1, "chain_head": "c", "ts_device": "t", "sig": "s"}]
        )

    row = store.devices("tenant-a")[0]
    assert row["entries_received"] == 1, "nothing written after revocation"
    assert row["last_checkpoint_seq"] is None


def test_defends_conflicting_checkpoint_at_an_existing_seq_end_is_raised(store):
    """A checkpoint resent at the same seq_end with a different chain head.

    Identical rule to SeqConflict on the entries path: redelivery is
    byte-identical, so the same position asserting a different chain head is a
    contradiction about what history existed — which is precisely the claim a
    checkpoint exists to make. It used to be absorbed as a duplicate, leaving
    the first assertion in place with no signal that a second, incompatible
    one had ever been offered.

    This was the fourth divergence found between the two ingest paths; the
    shared `_atomic` guard now holds the parts that must not differ.
    """
    did = _enrol(store, "tenant-a", "cpconflict")
    cp = {"seq_start": 1, "seq_end": 9, "chain_head": "headA", "ts_device": "t", "sig": "s"}
    store.accept_checkpoints(did, [cp])

    # Byte-identical resend is ordinary redelivery.
    assert store.accept_checkpoints(did, [cp]).duplicates == 1

    with pytest.raises(CheckpointConflict) as excinfo:
        store.accept_checkpoints(did, [{**cp, "chain_head": "headB"}])
    assert excinfo.value.seq_end == 9
    assert excinfo.value.stored_chain_head == "headA"

    row = store._conn.execute(
        "SELECT chain_head FROM checkpoints WHERE device_id = ? AND seq_end = 9", (did,)
    ).fetchone()
    assert row["chain_head"] == "headA"
