"""Coriqo Shield → Coriqo: publish this Mac's signed seal, rarely and reliably.

What goes out is one small thing: the chain's current checkpoint (Merkle
root, entry count, Ed25519 signature by this Mac's key), wrapped in the shape
Coriqo's existing device checkpoint route already stores
(``POST /v1/checkpoints/batch``, the same route the agent recorder uses). No
messages, no rule matches, no ledger rows.

Design goals, in order:

* **No support after setup.** Setup is one enrolment token, pasted once.
  After that the Mac signs every request with its own key; there is no API
  key or password on disk to rotate or leak.
* **Cheap.** A request only when the chain grew, at most every
  ``every_hours`` (6 by default), plus "Send now". Often zero a day.
* **Never breaks capture.** Publishing runs on its own thread, catches
  everything, and never blocks, slows or stops checking.
* **Self-healing.** A failure backs off (1 min, doubling, capped at 6 h,
  honouring ``Retry-After``) and retries on its own. State is written
  atomically and survives restarts. Coriqo dedupes by checkpoint id, so a
  resend after a crash is harmless.
* **One human-actionable state.** If Coriqo refuses this Mac (401/403: the
  device was revoked or the tenant moved), the status says so and what to do.
  Everything else fixes itself.
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import httpx

from byoai.recorder.enroll import EnrollmentError, enroll, load_enrollment_state
from byoai.recorder.keys import atomic_write_bytes
from byoai.recorder.shipper import ShipError, post_signed_batch

if TYPE_CHECKING:  # pragma: no cover
    from byoai.integrations.shield import SealChain

CHECKPOINT_PATH = "/v1/checkpoints/batch"
STATE_FILE = "coriqo_publish.json"
DEFAULT_EVERY_HOURS = 6
BACKOFF_FIRST_S = 60
BACKOFF_CAP_S = 6 * 3600
TICK_S = 60
TIMEOUT_S = 15


@dataclass
class PublishState:
    last_height: int = 0          # chain height covered by the last accepted send
    last_sent_at: float = 0.0     # epoch seconds
    failures: int = 0             # consecutive failed attempts
    next_attempt_at: float = 0.0  # epoch seconds; 0 = no backoff in force
    last_error: str | None = None
    needs_attention: bool = False  # Coriqo refused this Mac (401/403)


class Publisher:
    """Owns the enrolment, the send decision and the retry state for one Mac.

    ``now`` and ``client_factory`` are injectable for tests; production uses
    wall-clock time and a fresh short-lived httpx client per attempt (so a
    wedged connection can never outlive one request).
    """

    def __init__(self, seals: "SealChain", key_dir: Path, state_dir: Path, *,
                 every_hours: float = DEFAULT_EVERY_HOURS,
                 now: Callable[[], float] = time.time,
                 client_factory: Callable[[], httpx.Client] | None = None) -> None:
        self.seals = seals
        self.key_dir = Path(key_dir)
        self.state_path = Path(state_dir) / STATE_FILE
        self.every_s = max(0.0, every_hours) * 3600
        self._now = now
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=TIMEOUT_S))
        self._lock = threading.Lock()
        self.state = self._load()

    # -- state ------------------------------------------------------------
    def _load(self) -> PublishState:
        try:
            data = json.loads(self.state_path.read_text())
            known = {k: v for k, v in data.items() if k in PublishState.__annotations__}
            return PublishState(**known)
        except (OSError, ValueError, TypeError):
            return PublishState()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.state_path, json.dumps(asdict(self.state)).encode(),
                           mode=0o600, prefix=".coriqo-publish-")

    # -- enrolment ----------------------------------------------------------
    def enrolment(self):
        try:
            return load_enrollment_state(self.key_dir)
        except EnrollmentError:
            return None

    def enrol(self, base_url: str, token: str) -> dict:
        """Exchange a one-time Coriqo enrolment token for this Mac's device
        identity. Uses the Mac's existing Shield key; nothing secret is kept
        afterwards. Re-enrolling replaces the old identity."""
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("Enter the Coriqo address, starting with https://")
        if not token.strip():
            raise ValueError("Paste the enrolment token from Coriqo")
        with self._lock:
            enroll(coriqo_base_url=base_url, token=token.strip(),
                   key_dir=self.key_dir, force=True)
            # A new identity starts clean: send the current seal straight away.
            self.state = PublishState()
            self._save()
        return self.status()

    # -- decision ---------------------------------------------------------
    def due(self) -> bool:
        s = self.state
        if self.enrolment() is None or self.seals.height == 0:
            return False
        if self.seals.height <= s.last_height:
            return False  # nothing new: never send the same seal twice
        now = self._now()
        if now < s.next_attempt_at:
            return False  # backing off
        return s.last_sent_at == 0 or now - s.last_sent_at >= self.every_s

    def tick(self) -> bool:
        """Send if due. Never raises. Returns whether a send was attempted."""
        try:
            if not self.due():
                return False
            self.send()
            return True
        except Exception:  # noqa: BLE001 - the loop must never die
            return True

    def send(self) -> dict:
        """Send the current seal now (used by tick and by "Send now").
        Returns the status; failures are recorded, not raised."""
        with self._lock:
            state_now = self._now()
            enrolment = self.enrolment()
            if enrolment is None:
                raise ValueError("This Mac isn't connected to Coriqo yet")
            if self.seals.height == 0:
                return self.status()
            # Signed under the chain's own lock, so the root, the height and
            # the signature describe the same entries even if the watcher
            # seals a new one this instant. Everything below reads cp only.
            cp = self.seals.sign_checkpoint()
            height = int(cp["height"])
            entry = {
                # Coriqo stores device checkpoints by id; this one is stable
                # for a given head, so a resend is a duplicate, not a row.
                "checkpoint_id": f"shield:{cp['root_hex'][:32]}:{height}",
                "session_id": "coriqo-shield",
                "kind": cp.get("kind", "byoai.shield.checkpoint.v1"),
                "seq_start": 1,
                "seq_end": height,
                "chain_hash": cp["root_hex"],
                "signature": cp["sig"],
                "ts_device": cp["ts"],
                # The document the Mac signed, untouched, for later checks.
                "checkpoint": cp,
            }
            body = {"device_id": self.seals._key.device_id, "checkpoints": [entry]}
            try:
                with self._client_factory() as client:
                    resp = post_signed_batch(client, enrolment.coriqo_base_url,
                                             self.seals._key, CHECKPOINT_PATH, body)
            except ShipError as exc:
                self._failed(state_now, exc)
                return self.status()
            except Exception as exc:  # noqa: BLE001 - anything else is transient too
                self._failed(state_now, ShipError(str(exc)))
                return self.status()
            if resp.get("rejected"):
                self._failed(state_now, ShipError("Coriqo asked for this seal again later"))
                return self.status()
            self.state = PublishState(last_height=height, last_sent_at=state_now)
            self._save()
            return self.status()

    def _failed(self, now: float, exc: ShipError) -> None:
        s = self.state
        s.failures += 1
        delay = min(BACKOFF_CAP_S, BACKOFF_FIRST_S * 2 ** (s.failures - 1))
        delay *= random.uniform(0.8, 1.2)  # spread retries from many Macs
        if exc.retry_after:
            delay = max(delay, float(exc.retry_after))
        s.next_attempt_at = now + delay
        s.needs_attention = exc.status_code in (401, 403)
        s.last_error = (
            "Coriqo no longer accepts this Mac. Ask your Coriqo admin for a new "
            "enrolment token and connect again."
            if s.needs_attention else
            "Couldn't reach Coriqo; Shield will try again on its own."
        )
        self._save()

    # -- reporting ----------------------------------------------------------
    def status(self) -> dict:
        e = self.enrolment()
        s = self.state
        next_at = None
        if e is not None:
            if s.next_attempt_at > self._now():
                next_at = s.next_attempt_at
            elif s.last_sent_at:
                next_at = s.last_sent_at + self.every_s
        return {
            "connected": e is not None,
            "base_url": e.coriqo_base_url if e else None,
            "tenant": e.tenant_slug if e else None,
            "device_id": e.device_id if e else None,
            "enrolled_at": e.enrolled_at if e else None,
            "last_sent_at": s.last_sent_at or None,
            "last_height": s.last_height,
            "pending_entries": max(0, self.seals.height - s.last_height),
            "next_attempt_at": next_at,
            "every_hours": self.every_s / 3600,
            "last_error": s.last_error,
            "needs_attention": s.needs_attention,
        }

    # -- loop -----------------------------------------------------------------
    def start(self) -> threading.Thread:
        def run() -> None:
            while True:
                self.tick()
                time.sleep(TICK_S)
        t = threading.Thread(target=run, name="shield-publisher", daemon=True)
        t.start()
        return t
