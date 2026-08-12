"""In-memory mock Coriqo server for exercising the agent recorder's client
code (`byoai.recorder.enroll`, `byoai.recorder.shipper`) over a real HTTP
round trip.

This is a **dev/test tool only** — it is not a real Coriqo implementation and
must never be imported by anything under ``src/byoai/recorder/``. Its only
job is to independently re-derive and verify what the client actually put on
the wire (signature over the exact canonical bytes received, gzip framing,
header names, status codes) so a client/server wire-format mismatch shows up
as a real HTTP failure instead of silently passing a hand-crafted
``httpx.MockTransport`` fake.

No persistence across restarts — state lives in memory for the lifetime of a
``MockCoriqo`` instance.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field

import fastapi
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from byoai.recorder.checkpoint import verify_checkpoint
from byoai.recorder.keys import DeviceKey, derive_device_id
from byoai.recorder.merkle import InclusionProof, build_epoch_tree

__all__ = ["EnrolledDevice", "StoredEntry", "StoredCheckpoint", "Epoch", "MockCoriqo"]


@dataclass
class EnrolledDevice:
    device_id: str
    public_key_b64: str
    coriqo_base_url: str


@dataclass
class StoredEntry:
    device_id: str
    seq: int
    entry_hash: str
    event: dict


@dataclass
class StoredCheckpoint:
    device_id: str
    seq_end: int
    body: dict
    epoch_index: int | None = None  # which Epoch (if any) this is a leaf of


@dataclass
class Epoch:
    """A tenant-level epoch tree built over checkpoints received so far
    (spec section 6.2, level 3). Real Coriqo builds one every 10 minutes;
    the mock builds one whenever ``MockCoriqo.build_epoch`` is called, since
    tests control time explicitly rather than waiting on a wall clock."""

    index: int
    root: bytes
    checkpoint_keys: list[tuple[str, int]]  # (device_id, seq_end), proof-index order
    proofs: list[InclusionProof]

    def proof_for(self, device_id: str, seq_end: int) -> InclusionProof | None:
        try:
            i = self.checkpoint_keys.index((device_id, seq_end))
        except ValueError:
            return None
        return self.proofs[i]


@dataclass
class _DeviceState:
    device: EnrolledDevice
    entries_by_hash: dict[str, StoredEntry] = field(default_factory=dict)
    seqs: set[int] = field(default_factory=set)
    gapped_seqs: set[int] = field(default_factory=set)
    checkpoints_by_seq_end: dict[int, StoredCheckpoint] = field(default_factory=dict)


class MockCoriqo:
    """In-memory Coriqo double. One instance = one fake tenant."""

    def __init__(self, *, valid_tokens: set[str] | None = None) -> None:
        """``valid_tokens``: single-use enrollment tokens this server will
        accept. Defaults to ``{"cik_live_test"}``. Consumed tokens are
        removed after use (single-use, matches spec §8.2) — a repeat enroll
        with the same token gets 409."""
        self._valid_tokens: set[str] = (
            set(valid_tokens) if valid_tokens is not None else {"cik_live_test"}
        )
        self._consumed_tokens: set[str] = set()
        self._base_url = "http://mock-coriqo.test"
        self._devices: dict[str, _DeviceState] = {}
        self._epochs: list[Epoch] = []

        app = FastAPI()
        app.post("/v1/enroll")(self._enroll)
        app.post("/v1/ingest/batch")(self._ingest_batch)
        app.post("/v1/checkpoints/batch")(self._checkpoints_batch)
        self._app = app

    @property
    def app(self) -> fastapi.FastAPI:
        return self._app

    # -- test-introspection surface, not part of the real Coriqo API ------

    def devices(self) -> dict[str, EnrolledDevice]:
        return {device_id: state.device for device_id, state in self._devices.items()}

    def entries_for(self, device_id: str) -> list[StoredEntry]:
        state = self._devices.get(device_id)
        if state is None:
            return []
        return sorted(state.entries_by_hash.values(), key=lambda e: e.seq)

    def checkpoints_for(self, device_id: str) -> list[StoredCheckpoint]:
        state = self._devices.get(device_id)
        if state is None:
            return []
        return sorted(state.checkpoints_by_seq_end.values(), key=lambda c: c.seq_end)

    def epochs(self) -> list[Epoch]:
        return list(self._epochs)

    def build_epoch(self) -> Epoch | None:
        """Build a tenant epoch tree over every checkpoint received so far
        that isn't already a leaf of an earlier epoch (spec section 6.2,
        level 3). Returns ``None`` if there is nothing pending.

        Real Coriqo does this on a 10-minute timer across all devices in a
        tenant; the mock exposes it as an explicit call so tests control
        exactly when an epoch closes instead of racing a wall clock.
        """
        pending: list[StoredCheckpoint] = []
        for state in self._devices.values():
            pending.extend(
                cp for cp in state.checkpoints_by_seq_end.values() if cp.epoch_index is None
            )
        if not pending:
            return None

        pending.sort(key=lambda cp: (cp.device_id, cp.seq_end))
        bodies = [cp.body for cp in pending]
        root, proofs = build_epoch_tree(bodies)

        index = len(self._epochs)
        for cp in pending:
            cp.epoch_index = index

        epoch = Epoch(
            index=index,
            root=root,
            checkpoint_keys=[(cp.device_id, cp.seq_end) for cp in pending],
            proofs=proofs,
        )
        self._epochs.append(epoch)
        return epoch

    def inject_gap(self, device_id: str, seq: int) -> None:
        """Mark ``seq`` as permanently gapped: future ingest calls report it
        in ``gaps`` forever, for testing the shipper's gap-handling path
        against a real HTTP round trip rather than a canned MockTransport
        response."""
        state = self._devices.get(device_id)
        if state is None:
            raise KeyError(f"unknown device_id: {device_id}")
        state.gapped_seqs.add(seq)

    # -- routes -------------------------------------------------------------

    async def _enroll(self, request: Request) -> JSONResponse:
        body = await request.json()
        token = body.get("token")
        public_key_b64 = body.get("public_key")

        if not isinstance(token, str) or not isinstance(public_key_b64, str):
            return JSONResponse(
                {"error": "malformed enrollment request"}, status_code=401
            )

        if token in self._consumed_tokens:
            return JSONResponse({"error": "token already used"}, status_code=409)

        if token not in self._valid_tokens:
            return JSONResponse({"error": "unknown enrollment token"}, status_code=401)

        self._valid_tokens.discard(token)
        self._consumed_tokens.add(token)

        device_id = derive_device_id(public_key_b64)
        device = EnrolledDevice(
            device_id=device_id,
            public_key_b64=public_key_b64,
            coriqo_base_url=self._base_url,
        )
        self._devices.setdefault(
            device_id, _DeviceState(device=device)
        )
        # Re-enrolling with a fresh token under the same key just refreshes
        # the recorded device entry.
        self._devices[device_id].device = device

        return JSONResponse(
            {"device_id": device_id, "coriqo_base_url": self._base_url},
            status_code=201,
        )

    async def _authenticate_and_decode(
        self, request: Request
    ) -> tuple[str, dict] | JSONResponse:
        """Shared wire-verification for both batch endpoints: missing-header
        check, device lookup, gzip-vs-raw decoding, request-signature
        verification (over the exact bytes received off the wire — never a
        re-serialized/re-parsed version, which is the whole point of this
        server existing), JSON parsing, and device_id header/body agreement.

        Returns ``(device_id, payload)`` on success, or a ready-to-return
        :class:`JSONResponse` error on the first failure.
        """
        device_id = request.headers.get("x-coriqo-device")
        signature = request.headers.get("x-coriqo-signature")

        if not device_id or not signature:
            return JSONResponse(
                {"error": "missing device/signature headers"}, status_code=401
            )

        state = self._devices.get(device_id)
        if state is None:
            return JSONResponse({"error": "unknown device_id"}, status_code=401)

        raw_body = await request.body()
        if request.headers.get("content-encoding") == "gzip":
            try:
                canonical_body = gzip.decompress(raw_body)
            except OSError:
                return JSONResponse({"error": "invalid gzip body"}, status_code=401)
        else:
            canonical_body = raw_body

        if not DeviceKey.verify(state.device.public_key_b64, canonical_body, signature):
            return JSONResponse({"error": "bad signature"}, status_code=401)

        try:
            payload = json.loads(canonical_body)
        except ValueError:
            return JSONResponse({"error": "body is not valid JSON"}, status_code=401)

        if payload.get("device_id") != device_id:
            return JSONResponse(
                {"error": "device_id mismatch between header and body"},
                status_code=401,
            )

        return device_id, payload

    async def _ingest_batch(self, request: Request) -> JSONResponse:
        result = await self._authenticate_and_decode(request)
        if isinstance(result, JSONResponse):
            return result
        device_id, payload = result
        state = self._devices[device_id]

        entries = payload.get("entries", [])

        accepted = 0
        duplicates = 0
        for wire_entry in entries:
            seq = wire_entry["seq"]
            entry_hash = wire_entry["entry_hash"]
            event = wire_entry["event"]

            if entry_hash in state.entries_by_hash:
                duplicates += 1
                continue

            state.entries_by_hash[entry_hash] = StoredEntry(
                device_id=device_id, seq=seq, entry_hash=entry_hash, event=event
            )
            state.seqs.add(seq)
            accepted += 1

        gaps = [[seq, seq] for seq in sorted(state.gapped_seqs)]

        return JSONResponse(
            {"accepted": accepted, "duplicates": duplicates, "gaps": gaps},
            status_code=202,
        )

    async def _checkpoints_batch(self, request: Request) -> JSONResponse:
        result = await self._authenticate_and_decode(request)
        if isinstance(result, JSONResponse):
            return result
        device_id, payload = result

        checkpoints = payload.get("checkpoints", [])

        accepted = 0
        duplicates = 0
        # Per-checkpoint rejections (distinct from a request-level 401):
        # the request signature only proves the device sent this batch, not
        # that any individual checkpoint inside it is genuine — a checkpoint
        # that fails its own signature check (§6.2) is reported back here so
        # the shipper can retry it specifically, instead of us either
        # silently dropping it or aborting the whole batch (which would also
        # discard whatever in the same batch was fine).
        state = self._devices[device_id]
        rejected: list[dict] = []
        for body in checkpoints:
            seq_end = body["seq_end"]

            if seq_end in state.checkpoints_by_seq_end:
                duplicates += 1
                continue

            if not verify_checkpoint(body, state.device.public_key_b64):
                rejected.append(
                    {"seq_end": seq_end, "reason": "checkpoint signature does not verify"}
                )
                continue

            state.checkpoints_by_seq_end[seq_end] = StoredCheckpoint(
                device_id=device_id, seq_end=seq_end, body=body
            )
            accepted += 1

        return JSONResponse(
            {"accepted": accepted, "duplicates": duplicates, "rejected": rejected},
            status_code=202,
        )

