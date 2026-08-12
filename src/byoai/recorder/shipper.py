"""Batching shipper for the agent recorder — workstream G.

Reads confirmed-unsynced entries off the local :class:`~byoai.recorder.ledger.Ledger`,
batches them (spec §9.2: 100 events, 1 MB, or 5 seconds — whichever trips
first), gzips and signs the batch, and POSTs it to Coriqo's
``/v1/ingest/batch``. Delivery is at-least-once: the server dedupes on
``entry_hash``, so a batch that gets partially or fully resent after a crash
or a timeout is harmless.

The ledger is the source of truth (spec §9.1) — this module never blocks the
agent and never raises out of :meth:`Shipper.run_forever`; failures are
logged and retried with backoff.
"""

from __future__ import annotations

import gzip
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from byoai.recorder.canonical import canonicalize
from byoai.recorder.keys import DeviceKey
from byoai.recorder.ledger import Ledger

__all__ = [
    "CheckpointShipResult",
    "ShipError",
    "ShipResult",
    "Shipper",
]

logger = logging.getLogger(__name__)

_INGEST_PATH = "/v1/ingest/batch"
_CHECKPOINT_INGEST_PATH = "/v1/checkpoints/batch"
_MAX_BACKOFF_SECONDS = 60.0
_INITIAL_BACKOFF_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ShipResult:
    accepted: int
    duplicates: int
    gaps: list[list[int]]
    synced_up_to: int


@dataclass(frozen=True, slots=True)
class CheckpointShipResult:
    accepted: int
    duplicates: int
    synced_checkpoint_up_to: int


class ShipError(RuntimeError):
    """Network failure or non-2xx after this attempt's retries (there are none
    inside :meth:`Shipper.ship_once` — the caller/run loop owns retry policy)."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Shipper:
    """Batches unsynced ledger entries and ships them to Coriqo.

    ``ship_once`` is the testable unit: read a batch, send it, advance the
    watermark. ``run_forever`` is the thin loop the proxy process owns as a
    background thread, wired to a :class:`threading.Event` for prompt
    shutdown.
    """

    def __init__(
        self,
        ledger: Ledger,
        key: DeviceKey,
        *,
        coriqo_base_url: str,
        max_batch_events: int = 100,
        max_batch_bytes: int = 1_000_000,
        max_batch_seconds: float = 5.0,
        http_client: httpx.Client | None = None,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._ledger = ledger
        self._key = key
        self._base_url = coriqo_base_url.rstrip("/")
        self._max_batch_events = max_batch_events
        self._max_batch_bytes = max_batch_bytes
        self._max_batch_seconds = max_batch_seconds
        self._owns_client = http_client is None
        self._client = http_client if http_client is not None else httpx.Client()
        # Interruptible sleep: given a timeout, returns True if `stop` fired
        # during the wait (run_forever should exit then), False on timeout.
        # Defaults to a real threading.Event().wait bound at call time; tests
        # can inject a fake to assert exact backoff durations without a real
        # multi-second sleep.
        self._wait = wait

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- single batch attempt ------------------------------------------------

    def ship_once(self) -> ShipResult | None:
        """One batch attempt. Returns ``None`` if there was nothing unsynced.

        Reads up to ``max_batch_events`` unsynced entries, then trims further
        to stay under ``max_batch_bytes`` (pre-gzip, of the canonical body).
        Signs ``canonicalize(body)`` with the device key and gzips those same
        canonical bytes as the request body, so the signature covers exactly
        what's on the wire. On ``202`` the watermark is advanced to the highest contiguous
        shipped seq that is not inside any gap the server reported — a
        gapped seq (and everything after it in this batch) is left unsynced
        so the next attempt retries it.

        Raises :class:`ShipError` on any network failure or non-2xx response;
        does not retry internally.
        """
        entries = self._ledger.read_unsynced(limit=self._max_batch_events)
        if not entries:
            return None

        batch = []
        total_bytes = 0
        for entry in entries:
            wire_entry = {
                "seq": entry.seq,
                "entry_hash": entry.entry_hash,
                "event": entry.event.to_dict(),
            }
            entry_bytes = len(canonicalize(wire_entry))
            if batch and total_bytes + entry_bytes > self._max_batch_bytes:
                break
            batch.append(wire_entry)
            total_bytes += entry_bytes

        body = {"device_id": self._key.device_id, "entries": batch}
        resp_body = self._post_signed_batch(_INGEST_PATH, body)

        accepted = int(resp_body.get("accepted", 0))
        duplicates = int(resp_body.get("duplicates", 0))
        gaps = [list(g) for g in resp_body.get("gaps", [])]

        shipped_seqs = [wire_entry["seq"] for wire_entry in batch]
        synced_up_to = self._advance_watermark(
            self._ledger.get_synced_up_to,
            self._ledger.set_synced_up_to,
            shipped_seqs,
            gaps,
        )

        return ShipResult(
            accepted=accepted,
            duplicates=duplicates,
            gaps=gaps,
            synced_up_to=synced_up_to,
        )

    def ship_checkpoints_once(self) -> CheckpointShipResult | None:
        """One checkpoint batch attempt. Returns ``None`` if nothing was pending.

        Checkpoints ship on their own watermark and their own endpoint,
        independent of entry shipping — a checkpoint only becomes useful to
        Coriqo (as a leaf in a tenant epoch tree, §6.2 level 3) once it
        exists, regardless of whether the entries it summarizes have shipped
        yet. Delivery is at-least-once, same as entries: the server dedupes
        by ``(device_id, seq_end)``.

        Raises :class:`ShipError` on any network failure or non-2xx
        response; does not retry internally.
        """
        checkpoints = self._ledger.read_unsynced_checkpoints(limit=self._max_batch_events)
        if not checkpoints:
            return None

        body = {"device_id": self._key.device_id, "checkpoints": checkpoints}
        resp_body = self._post_signed_batch(_CHECKPOINT_INGEST_PATH, body)

        accepted = int(resp_body.get("accepted", 0))
        duplicates = int(resp_body.get("duplicates", 0))
        # Per-checkpoint outcomes the server explicitly rejected (e.g. a
        # malformed or badly-signed checkpoint) — distinct from ``accepted``
        # and ``duplicates``. Any seq_end reported here (and anything after
        # it in this batch) must never be marked synced, mirroring how
        # ``ship_once`` treats gapped seqs, or a rejected checkpoint would be
        # silently marked as synced forever and never retried, leaving a
        # permanent hole in the epoch tree.
        try:
            rejected_seq_ends = [int(r["seq_end"]) for r in resp_body.get("rejected", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise ShipError(
                f"response from {_CHECKPOINT_INGEST_PATH} had a malformed 'rejected' "
                f"entry: {resp_body.get('rejected')!r}"
            ) from exc
        rejected_ranges = [[seq_end, seq_end] for seq_end in rejected_seq_ends]

        shipped_seq_ends = [cp["seq_end"] for cp in checkpoints]
        synced_checkpoint_up_to = self._advance_watermark(
            self._ledger.get_synced_checkpoint_up_to,
            self._ledger.set_synced_checkpoint_up_to,
            shipped_seq_ends,
            rejected_ranges,
        )

        return CheckpointShipResult(
            accepted=accepted,
            duplicates=duplicates,
            synced_checkpoint_up_to=synced_checkpoint_up_to,
        )

    def _post_signed_batch(self, path: str, body: dict) -> dict:
        """Canonicalize, sign, gzip, and POST ``body`` to ``path``; return the
        parsed JSON response.

        Shared by :meth:`ship_once` and :meth:`ship_checkpoints_once`: both
        batch kinds sign/gzip/POST the same way, use the same headers, and
        treat network failure / non-2xx / non-JSON responses identically.
        Raises :class:`ShipError` in all of those cases; does not retry
        internally.
        """
        canonical_body = canonicalize(body)
        signature = self._key.sign(canonical_body)
        payload = gzip.compress(canonical_body)

        try:
            response = self._client.post(
                f"{self._base_url}{path}",
                content=payload,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                    "x-coriqo-device": self._key.device_id,
                    "x-coriqo-signature": signature,
                },
            )
        except httpx.HTTPError as exc:
            raise ShipError(f"request to {path} failed: {exc}") from exc

        if response.status_code // 100 != 2:
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            raise ShipError(
                f"request to {path} rejected: HTTP {response.status_code} {response.text}",
                retry_after=retry_after,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ShipError(
                f"response from {path} was not valid JSON: {response.text}"
            ) from exc

    def _advance_watermark(
        self,
        get_current: Callable[[], int],
        set_current: Callable[[int], None],
        shipped_seqs: list[int],
        gaps: list[list[int]],
    ) -> int:
        """Advance a synced-up-to watermark up to (but never past) the first
        gapped/rejected seq in this batch, if any.

        Generic over which watermark: ``ship_once`` passes the entry
        watermark accessors with the server's reported ``gaps`` ranges;
        ``ship_checkpoints_once`` passes the checkpoint watermark accessors
        with single-point ranges built from the server's ``rejected``
        seq_ends. Same discipline either way: a gapped/rejected seq (and
        everything after it in this batch) is left unsynced so the next
        attempt retries it.
        """
        current = get_current()
        if not shipped_seqs:
            return current

        first_gap_seq: int | None = None
        for start, _end in gaps:
            if start <= current:
                # Already synced past this gap in an earlier batch (or the
                # server is echoing a stale/unrelated historical gap) — it
                # has nothing to do with this batch and must not block it
                # from ever advancing again.
                continue
            if first_gap_seq is None or start < first_gap_seq:
                first_gap_seq = start

        target = max(shipped_seqs)
        if first_gap_seq is not None:
            # Never mark the gapped seq (or anything after it) as synced.
            safe_seqs = [s for s in shipped_seqs if s < first_gap_seq]
            if not safe_seqs:
                return current
            target = max(safe_seqs)

        if target <= current:
            return current

        set_current(target)
        return target

    # -- background loop ------------------------------------------------------

    def run_forever(self, *, stop: threading.Event) -> None:
        """Ship batches until ``stop`` is set.

        Sleeps up to ``max_batch_seconds`` between attempts (interruptible by
        ``stop``), calls :meth:`ship_once`, and on :class:`ShipError` applies
        exponential backoff with jitter (capped at 60s), honoring a
        ``Retry-After`` value if the failure carried one. Never raises out of
        the loop; exits promptly even mid-backoff-sleep.
        """
        wait_fn = self._wait if self._wait is not None else stop.wait
        backoff = _INITIAL_BACKOFF_SECONDS
        while not stop.is_set():
            try:
                self.ship_once()
                self.ship_checkpoints_once()
                backoff = _INITIAL_BACKOFF_SECONDS
            except ShipError as exc:
                logger.warning("recorder shipper batch failed: %s", exc)
                if exc.retry_after is not None:
                    wait_seconds = exc.retry_after
                else:
                    wait_seconds = min(backoff, _MAX_BACKOFF_SECONDS)
                    wait_seconds = wait_seconds * (0.5 + random.random())
                    backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
                if wait_fn(wait_seconds):
                    return
                continue
            except Exception:  # pragma: no cover - defensive, must never propagate
                logger.exception("recorder shipper hit an unexpected error")

            if wait_fn(self._max_batch_seconds):
                return


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
