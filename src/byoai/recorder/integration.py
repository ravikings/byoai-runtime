"""Wires the recorder's capture pipeline into a running proxy.

Everything else in this package is a pure library. This module is the one
place that owns process-lifetime state: the on-disk ledger, the device key,
and the checkpoint scheduler, all gated behind ``BYOAI_RECORDER_ENABLED``.

Failure posture (spec §9.3): recording must never add latency to or block the
token stream, and it must never crash the request path. Every public method
here swallows its own exceptions except for the one case the spec calls out
explicitly — ``strict_mode`` plus a ledger write failure — which the caller
is expected to turn into a 503.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from .canonical import canonicalize, sha256_hex
from .extract import PartialEvent, StreamExtractor, extract_request_events, extract_response_events
from .ledger import Ledger, LedgerWriteError
from .schema import AgentEvent, new_event_id

if TYPE_CHECKING:
    from .checkpoint import Checkpointer
    from .keys import DeviceKey
    from .shipper import Shipper

log = logging.getLogger("byoai.recorder")

__all__ = ["Recorder", "get_recorder", "reset_recorder_for_tests"]


def _default_dir() -> Path:
    return Path(os.getenv("BYOAI_RECORDER_DIR", str(Path.home() / ".byoai" / "recorder")))


class Recorder:
    """Promotes captured events into the sealed ledger for one device."""

    def __init__(self, *, dir: Path | str | None = None, strict_mode: bool | None = None) -> None:  # noqa: A002
        # Imported lazily: both need `cryptography`, which lives behind the
        # `byoai-runtime[recorder]` extra. A base install must be able to
        # import this module (and the rest of the proxy) without it —
        # cryptography is only required once recording is actually enabled.
        from .checkpoint import Checkpointer
        from .keys import load_or_create_device_key

        base = Path(dir) if dir is not None else _default_dir()
        base.mkdir(parents=True, exist_ok=True)
        self.strict_mode = (
            strict_mode
            if strict_mode is not None
            else os.getenv("BYOAI_RECORDER_STRICT", "0") == "1"
        )
        self.key: DeviceKey = load_or_create_device_key(base)
        self.ledger = Ledger(base / "ledger.db", self.key.device_id, strict_mode=self.strict_mode)
        self.checkpointer: Checkpointer = Checkpointer(self.ledger, self.key)

        self._shipper: Shipper | None = None
        self._shipper_stop: threading.Event | None = None
        self._shipper_thread: threading.Thread | None = None
        self._start_shipper(base)

    def _start_shipper(self, base: Path) -> None:
        # Shipping requires both an enrolled device (we need a device_id
        # Coriqo recognizes) and a configured Coriqo URL. Either being
        # absent just means "not shipping yet" — not an error.
        from .enroll import load_enrollment_state
        from .shipper import Shipper

        state = load_enrollment_state(base)
        if state is None:
            log.info("recorder: not enrolled, shipper disabled (run byoai-recorder-enroll)")
            return
        try:
            self._shipper = Shipper(self.ledger, self.key, coriqo_base_url=state.coriqo_base_url)
            self._shipper_stop = threading.Event()
            self._shipper_thread = threading.Thread(
                target=self._shipper.run_forever,
                kwargs={"stop": self._shipper_stop},
                name="byoai-recorder-shipper",
                daemon=True,
            )
            self._shipper_thread.start()
        except Exception:  # noqa: BLE001
            log.exception("recorder: failed to start shipper, will not sync to Coriqo")
            self._shipper = None
            self._shipper_stop = None
            self._shipper_thread = None

    def close(self) -> None:
        try:
            if self._shipper_stop is not None:
                self._shipper_stop.set()
            if self._shipper_thread is not None:
                self._shipper_thread.join(timeout=5.0)
            if self._shipper is not None:
                self._shipper.close()
        finally:
            try:
                self.checkpointer.flush()
            finally:
                self.ledger.close()

    # ------------------------------------------------------------- capture

    def _promote(self, partial: PartialEvent) -> AgentEvent:
        payload_hash = sha256_hex(canonicalize(partial.payload))
        return AgentEvent(
            schema_version=partial.schema_version,
            event_id=new_event_id(),
            device_id=self.key.device_id,
            session_id=partial.session_id,
            seq=0,  # stamped by Ledger.append
            kind=partial.kind,
            ts_device=partial.ts_device,
            ts_monotonic_ns=partial.ts_monotonic_ns,
            tool_use_id=partial.tool_use_id,
            tool_name=partial.tool_name,
            payload=partial.payload,
            payload_hash=payload_hash,
            model=partial.model,
            provider=partial.provider,
        )

    def record(self, partial: PartialEvent) -> None:
        """Append one captured event. May raise ``LedgerWriteError`` in strict mode."""
        event = self._promote(partial)
        try:
            entry = self.ledger.append(event)
        except LedgerWriteError:
            raise
        except Exception:  # noqa: BLE001 - never let a capture bug break the proxy
            log.exception("recorder: failed to append event kind=%s", partial.kind)
            return
        if entry is not None:
            try:
                self.checkpointer.note(entry.seq)
            except Exception:  # noqa: BLE001
                log.exception("recorder: checkpoint scheduling failed")

    def record_many(self, partials: list[PartialEvent]) -> None:
        for partial in partials:
            self.record(partial)

    def record_request_body(self, body: dict, *, session_id: str) -> None:
        try:
            partials = extract_request_events(body, session_id=session_id)
        except Exception:  # noqa: BLE001
            log.exception("recorder: request extraction failed")
            return
        self.record_many(partials)

    def record_response_body(self, body: dict, *, session_id: str) -> None:
        try:
            partials = extract_response_events(body, session_id=session_id)
        except Exception:  # noqa: BLE001
            log.exception("recorder: response extraction failed")
            return
        self.record_many(partials)

    def new_stream_extractor(self, *, session_id: str, model: str | None) -> StreamExtractor:
        return StreamExtractor(session_id=session_id, model=model)

    def feed_stream_chunk(self, extractor: StreamExtractor, chunk: bytes) -> None:
        try:
            partials = extractor.feed(chunk)
        except Exception:  # noqa: BLE001
            log.exception("recorder: stream extraction failed")
            return
        self.record_many(partials)

    def close_stream_extractor(self, extractor: StreamExtractor) -> None:
        try:
            partials = extractor.close()
        except Exception:  # noqa: BLE001
            log.exception("recorder: stream close extraction failed")
            return
        self.record_many(partials)


_recorder: Recorder | None = None
_recorder_checked = False


def get_recorder() -> Recorder | None:
    """Return the process-wide Recorder, or None if disabled/unavailable.

    Enabled via ``BYOAI_RECORDER_ENABLED=1``. Construction failures (e.g. an
    unwritable ledger directory) are logged and treated as disabled rather
    than crashing the proxy on import/startup.
    """
    global _recorder, _recorder_checked
    if _recorder_checked:
        return _recorder
    _recorder_checked = True
    if os.getenv("BYOAI_RECORDER_ENABLED", "0") != "1":
        return None
    try:
        _recorder = Recorder()
    except Exception:  # noqa: BLE001
        log.exception("recorder: failed to initialize, recording disabled for this process")
        _recorder = None
    return _recorder


def reset_recorder_for_tests() -> None:
    """Test-only hook: drop the cached singleton so the next get_recorder() re-reads env."""
    global _recorder, _recorder_checked
    if _recorder is not None:
        _recorder.close()
    _recorder = None
    _recorder_checked = False
