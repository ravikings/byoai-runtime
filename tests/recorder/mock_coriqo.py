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

from byoai.recorder.keys import DeviceKey, derive_device_id

__all__ = ["EnrolledDevice", "StoredEntry", "MockCoriqo"]


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
class _DeviceState:
    device: EnrolledDevice
    entries_by_hash: dict[str, StoredEntry] = field(default_factory=dict)
    seqs: set[int] = field(default_factory=set)
    gapped_seqs: set[int] = field(default_factory=set)


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

        app = FastAPI()
        app.post("/v1/enroll")(self._enroll)
        app.post("/v1/ingest/batch")(self._ingest_batch)
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

    async def _ingest_batch(self, request: Request) -> JSONResponse:
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

        # Verify against the *exact* bytes received off the wire (after
        # gzip decompression), never a re-serialized/re-parsed version —
        # this is the whole point of this server existing.
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

