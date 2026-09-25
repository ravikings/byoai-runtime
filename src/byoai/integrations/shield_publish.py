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

from byoai.recorder.canonical import canonicalize
from byoai.recorder.enroll import EnrollmentError, enroll, load_enrollment_state
from byoai.recorder.keys import atomic_write_bytes
from byoai.recorder.shipper import ShipError, post_signed_batch

if TYPE_CHECKING:  # pragma: no cover
    from byoai.integrations.shield import SealChain

CHECKPOINT_PATH = "/v1/checkpoints/batch"
SESSION_ID = "coriqo-shield"  # Coriqo tells a Shield Mac's seals apart by this
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
STATE_FILE = "coriqo_publish.json"
DEFAULT_EVERY_HOURS = 6
BACKOFF_FIRST_S = 60
BACKOFF_CAP_S = 6 * 3600
TICK_S = 60
TIMEOUT_S = 15


@dataclass
class PublishState:
    last_root: str | None = None  # chain root covered by the last accepted send
    last_height: int = 0          # its entry count, for the status line
    last_seq: int = 0             # number of the last accepted send
    last_sent_at: float = 0.0     # epoch seconds
    #: The signed entry waiting for Coriqo's acknowledgement. Resent unchanged
    #: until acknowledged, so a crash between Coriqo storing it and us writing
    #: this file can only ever produce a duplicate, never two different heads
    #: under one number (which Coriqo would rightly treat as a conflict).
    outbox: dict | None = None
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
        # trust_env=False: never route through a proxy. On a Mac running
        # Shield, the system proxy IS Shield's capture proxy; going through it
        # would fail its certificate check on every send.
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=TIMEOUT_S, trust_env=False))
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
        from urllib.parse import urlsplit
        base_url = base_url.strip().rstrip("/")
        parts = urlsplit(base_url)
        local = (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            # The token is a one-time credential: it only crosses a network
            # encrypted. Plain http is allowed for a Coriqo on this machine.
            raise ValueError("Enter the Coriqo address, starting with https://")
        if not token.strip():
            raise ValueError("Paste the enrolment token from Coriqo")
        with self._lock:
            with self._client_factory() as client:
                enroll(coriqo_base_url=base_url, token=token.strip(),
                       key_dir=self.key_dir, force=True, http_client=client)
            # A new identity starts clean (its own numbering, empty outbox):
            # the current seal goes out straight away.
            self.state = PublishState()
            self._save()
        return self.status()

    # -- decision ---------------------------------------------------------
    def has_new(self) -> bool:
        """Anything sealed since the last accepted send. Compared by root, not
        entry count: the seal chain keeps a bounded window, so its count stops
        growing while its root keeps changing."""
        return self.seals.height > 0 and self.seals.root_hex() != self.state.last_root

    def due(self) -> bool:
        s = self.state
        if self.enrolment() is None:
            return False
        if s.outbox is None and not self.has_new():
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

    def _build_entry(self) -> dict:
        """A new outbox entry: the current seal, numbered and signed so
        Coriqo's checkpoint verification accepts it as this device's."""
        # Signed under the chain's own lock: root, count and signature describe
        # the same entries even if the watcher seals a new one this instant.
        cp = self.seals.sign_checkpoint()
        seq = self.state.last_seq + 1
        entry = {
            # One number per send, never reused: two different heads can never
            # share a range, which Coriqo would read as a conflicting record.
            "checkpoint_id": f"shield:{seq}:{cp['root_hex'][:32]}",
            "session_id": SESSION_ID,
            "kind": cp.get("kind", "byoai.shield.checkpoint.v1"),
            "seq_start": seq,
            "seq_end": seq,
            "chain_hash": cp["root_hex"],
            "entries": int(cp["height"]),
            "ts_device": cp["ts"],
            # The seal as the Mac signed it, kept whole for offline checks.
            "checkpoint": cp,
        }
        # Coriqo verifies Ed25519 over canonical(entry minus "sig"), the same
        # rule the recorder's checkpoints follow.
        entry["sig"] = self.seals._key.sign(canonicalize(entry))
        return entry

    def send(self) -> dict:
        """Send now (used by tick and by "Send now"): the waiting outbox entry
        if there is one, else the current seal. Returns the status; failures
        are recorded and retried, never raised."""
        with self._lock:
            state_now = self._now()
            enrolment = self.enrolment()
            if enrolment is None:
                raise ValueError("This Mac isn't connected to Coriqo yet")
            if self.state.outbox is None:
                if not self.has_new():
                    return self.status()
                self.state.outbox = self._build_entry()
                self._save()  # before sending: see PublishState.outbox
            entry = self.state.outbox
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
                # Coriqo's "send this again later": keep the outbox, back off.
                self._failed(state_now, ShipError("Coriqo asked for this seal again later"))
                return self.status()
            self.state = PublishState(
                last_root=entry["chain_hash"], last_height=int(entry.get("entries") or 0),
                last_seq=int(entry["seq_end"]), last_sent_at=state_now,
                last_error=("Coriqo stored this seal but couldn't verify it."
                            if resp.get("malformed") else None),
            )
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
            "has_new": s.outbox is not None or self.has_new(),
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
