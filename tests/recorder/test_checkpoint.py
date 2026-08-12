"""Checkpoint tests — workstream D.

The ledger is always a local fake — these tests are about checkpoint content
and triggers, not SQLite.
"""

from __future__ import annotations

import itertools

import pytest

from byoai.recorder.canonical import canonicalize
from byoai.recorder.checkpoint import Checkpointer, checkpoint_signing_bytes, verify_checkpoint
from byoai.recorder.keys import DeviceKey, load_or_create_device_key


class FakeLedger:
    """Just enough Ledger surface for the Checkpointer: a head and a sink."""

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self.checkpoints: list[dict] = []
        self.head = "sha256:" + "00" * 32

    def advance(self) -> str:
        self.head = "sha256:" + f"{next(self._counter):064x}"
        return self.head

    def append_checkpoint(self, cp: dict) -> None:
        self.checkpoints.append(cp)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def key(tmp_path):
    return load_or_create_device_key(tmp_path)


@pytest.fixture
def ledger():
    return FakeLedger()


def make_checkpointer(ledger, key, clock=None, **kwargs) -> Checkpointer:
    clock = clock or FakeClock()
    return Checkpointer(ledger, key, monotonic=clock, wall_clock=lambda: 1_700_000_000.5, **kwargs)


def feed(cp: Checkpointer, ledger: FakeLedger, count: int, start: int = 1) -> list[dict]:
    emitted = []
    for seq in range(start, start + count):
        ledger.advance()
        out = cp.note(seq)
        if out is not None:
            emitted.append(out)
    return emitted


# -- shape ----------------------------------------------------------------


def test_checkpoint_shape_matches_spec(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=4)
    emitted = feed(cp, ledger, 4)

    assert len(emitted) == 1
    checkpoint = emitted[0]
    assert set(checkpoint) == {
        "device_id",
        "seq_start",
        "seq_end",
        "chain_head",
        "ts_device",
        "sig",
    }
    assert checkpoint["device_id"] == key.device_id
    assert checkpoint["seq_start"] == 1
    assert checkpoint["seq_end"] == 4
    assert checkpoint["chain_head"] == ledger.head
    assert checkpoint["ts_device"].endswith("Z")
    assert checkpoint["sig"].startswith("ed25519:")
    assert ledger.checkpoints == [checkpoint]


def test_signature_covers_canonicalized_checkpoint_without_sig(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=2)
    checkpoint = feed(cp, ledger, 2)[0]

    unsigned = {k: v for k, v in checkpoint.items() if k != "sig"}
    assert checkpoint_signing_bytes(checkpoint) == canonicalize(unsigned)
    assert DeviceKey.verify(key.public_key_b64, canonicalize(unsigned), checkpoint["sig"])
    assert verify_checkpoint(checkpoint, key.public_key_b64) is True


@pytest.mark.parametrize("field", ["device_id", "seq_start", "seq_end", "ts_device"])
def test_mutating_any_signed_field_invalidates(ledger, key, field):
    cp = make_checkpointer(ledger, key, every_events=2)
    checkpoint = dict(feed(cp, ledger, 2)[0])

    original = checkpoint[field]
    checkpoint[field] = original + 1 if isinstance(original, int) else original + "x"
    assert verify_checkpoint(checkpoint, key.public_key_b64) is False


def test_flipping_one_byte_of_chain_head_invalidates(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=2)
    checkpoint = dict(feed(cp, ledger, 2)[0])

    head = checkpoint["chain_head"]
    prefix, digest = head.split(":", 1)
    flipped = f"{int(digest[:2], 16) ^ 0x01:02x}" + digest[2:]
    checkpoint["chain_head"] = f"{prefix}:{flipped}"

    assert checkpoint["chain_head"] != head
    assert verify_checkpoint(checkpoint, key.public_key_b64) is False


def test_wrong_public_key_and_missing_sig_rejected(ledger, key, tmp_path):
    other = load_or_create_device_key(tmp_path / "other")
    cp = make_checkpointer(ledger, key, every_events=1)
    checkpoint = feed(cp, ledger, 1)[0]

    assert verify_checkpoint(checkpoint, other.public_key_b64) is False
    unsigned = {k: v for k, v in checkpoint.items() if k != "sig"}
    assert verify_checkpoint(unsigned, key.public_key_b64) is False


# -- triggers -------------------------------------------------------------


def test_event_trigger_fires_on_nth_event(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=256, every_seconds=60.0)

    for seq in range(1, 256):
        ledger.advance()
        assert cp.note(seq) is None, f"emitted early at {seq}"
    ledger.advance()
    checkpoint = cp.note(256)

    assert checkpoint is not None
    assert (checkpoint["seq_start"], checkpoint["seq_end"]) == (1, 256)


def test_default_thresholds_are_256_and_60s(ledger, key):
    cp = Checkpointer(ledger, key)
    assert cp._every_events == 256
    assert cp._every_seconds == 60.0


def test_event_windows_are_contiguous_and_non_overlapping(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=10)
    emitted = feed(cp, ledger, 35)

    assert [(c["seq_start"], c["seq_end"]) for c in emitted] == [(1, 10), (11, 20), (21, 30)]
    assert cp.flush()["seq_start"] == 31


def test_time_trigger_fires_without_hitting_event_count(ledger, key):
    clock = FakeClock()
    cp = make_checkpointer(ledger, key, clock=clock, every_events=256, every_seconds=60.0)

    ledger.advance()
    assert cp.note(1) is None
    clock.advance(59.9)
    ledger.advance()
    assert cp.note(2) is None

    clock.advance(0.1)
    ledger.advance()
    checkpoint = cp.note(3)

    assert checkpoint is not None
    assert (checkpoint["seq_start"], checkpoint["seq_end"]) == (1, 3)


def test_tick_fires_time_trigger_with_no_new_event(ledger, key):
    clock = FakeClock()
    cp = make_checkpointer(ledger, key, clock=clock, every_seconds=60.0)

    ledger.advance()
    cp.note(1)
    assert cp.tick() is None

    clock.advance(60.0)
    checkpoint = cp.tick()

    assert checkpoint is not None and checkpoint["seq_end"] == 1
    assert cp.tick() is None


def test_idle_recorder_emits_nothing_however_long_it_waits(ledger, key):
    clock = FakeClock()
    cp = make_checkpointer(ledger, key, clock=clock, every_seconds=60.0)

    clock.advance(3600.0)
    assert cp.tick() is None
    assert cp.flush() is None
    assert ledger.checkpoints == []

    # The clock window starts at the first event, not at construction.
    ledger.advance()
    assert cp.note(1) is None


def test_time_window_resets_after_each_emit(ledger, key):
    clock = FakeClock()
    cp = make_checkpointer(ledger, key, clock=clock, every_seconds=60.0)

    ledger.advance()
    cp.note(1)
    clock.advance(60.0)
    assert cp.tick() is not None

    ledger.advance()
    assert cp.note(2) is None
    clock.advance(59.0)
    assert cp.tick() is None
    clock.advance(1.0)
    assert cp.tick() is not None


def test_event_trigger_wins_when_it_comes_first(ledger, key):
    clock = FakeClock()
    cp = make_checkpointer(ledger, key, clock=clock, every_events=3, every_seconds=60.0)

    emitted = feed(cp, ledger, 3)
    assert len(emitted) == 1
    assert clock.now == 1000.0  # no time passed; the count trigger fired


# -- flush ----------------------------------------------------------------


def test_flush_emits_the_tail(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=10)
    feed(cp, ledger, 3)

    checkpoint = cp.flush()

    assert checkpoint is not None
    assert (checkpoint["seq_start"], checkpoint["seq_end"]) == (1, 3)
    assert verify_checkpoint(checkpoint, key.public_key_b64)


def test_flush_on_empty_recorder_emits_nothing(ledger, key):
    cp = make_checkpointer(ledger, key)

    assert cp.flush() is None
    assert ledger.checkpoints == []


def test_flush_does_not_duplicate_an_already_emitted_checkpoint(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=4)
    emitted = feed(cp, ledger, 4)

    assert cp.flush() is None
    assert cp.flush() is None
    assert ledger.checkpoints == emitted
    assert len(ledger.checkpoints) == 1


def test_repeated_flush_after_tail_is_idempotent(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=10)
    feed(cp, ledger, 2)

    first = cp.flush()
    assert first is not None
    assert cp.flush() is None
    assert len(ledger.checkpoints) == 1
    assert cp.last_checkpointed_seq == 2
    assert cp.pending_events == 0


def test_events_after_flush_start_a_new_window(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=10)
    feed(cp, ledger, 2)
    cp.flush()

    ledger.advance()
    cp.note(3)
    second = cp.flush()

    assert (second["seq_start"], second["seq_end"]) == (3, 3)
    assert len(ledger.checkpoints) == 2


# -- misc -----------------------------------------------------------------


def test_rejects_nonsense_thresholds(ledger, key):
    with pytest.raises(ValueError):
        Checkpointer(ledger, key, every_events=0)
    with pytest.raises(ValueError):
        Checkpointer(ledger, key, every_seconds=0)


def test_chain_head_is_read_at_emit_time(ledger, key):
    cp = make_checkpointer(ledger, key, every_events=2)
    ledger.advance()
    cp.note(1)
    stale = ledger.head
    ledger.advance()
    checkpoint = cp.note(2)

    assert checkpoint["chain_head"] == ledger.head != stale


def test_checkpoint_is_appended_before_being_returned(ledger, key):
    """The verifier reads checkpoints from the ledger, so persistence is not optional."""
    cp = make_checkpointer(ledger, key, every_events=1)
    ledger.advance()
    checkpoint = cp.note(7)

    assert ledger.checkpoints[-1] is checkpoint
    assert verify_checkpoint(ledger.checkpoints[-1], key.public_key_b64)
