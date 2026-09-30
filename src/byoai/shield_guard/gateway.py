"""Glue between :func:`byoai.shield_guard.check` and the ``byoai-cache`` proxy.

``BYOAI_SHIELD_GUARD`` = ``off`` (default) | ``observe`` (record, forward as
sent) | ``enforce`` (block or redact). ``BYOAI_SHIELD_POLICY`` names a Shield
policy file. Evidence rows hold rule names, lengths and a keyed fingerprint,
never text.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from . import Decision, blocked_payload, check_raw, load_policy_from

log = logging.getLogger("byoai.shield_guard")

EVIDENCE_KIND = "shield_guard"
OPENAI_PATHS = ("chat/completions", "responses")


def mode() -> str:
    m = os.getenv("BYOAI_SHIELD_GUARD", "off").strip().lower()
    return m if m in ("observe", "enforce") else "off"


def openai_upstream() -> str:
    return os.getenv("BYOAI_SHIELD_OPENAI_UPSTREAM", "https://api.openai.com").rstrip("/")


def _key_path() -> Path | None:
    p = os.getenv("BYOAI_SHIELD_KEY_PATH")
    return Path(p).expanduser() if p else None


_policy_cache: dict = {}


def _policy() -> dict:
    """The policy file's contents, re-read only when the file changes."""
    path = os.getenv("BYOAI_SHIELD_POLICY")
    if not path:
        return load_policy_from(None)
    p = Path(path).expanduser()
    try:
        st = p.stat()
        sig = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (str(p), None, None)
    hit = _policy_cache.get("v")
    if hit and hit[0] == sig:
        return hit[1]
    policy = load_policy_from(p)
    _policy_cache["v"] = (sig, policy)
    return policy


@dataclass
class Outcome:
    blocked: dict | None   # error payload when the request must not go on
    decision: Decision
    forward: bytes         # bytes to send upstream for a passthrough
    drop_encoding: bool    # True only when ``forward`` is not the original body
    parse: bytes           # the readable body, for routes that read it themselves


def guard_raw(raw: bytes, content_encoding: str | None, shape: str) -> Outcome:
    """Run the guard on one request body as received, under the current mode.
    Only ``enforce`` ever changes what is sent; ``observe`` sends the original
    bytes and headers."""
    res = check_raw(raw, content_encoding, policy=_policy(),
                    on_warn=os.getenv("BYOAI_SHIELD_ON_WARN", "block"),
                    key_path=_key_path(), app=os.getenv("BYOAI_SHIELD_APP") or None)
    d = res.decision
    if mode() != "enforce":
        return Outcome(None, d, raw, False, res.decoded or raw)
    if d.action == "block":
        return Outcome(blocked_payload(d, shape), d, raw, False, res.decoded)
    if res.rewritten is not None:
        return Outcome(None, d, res.rewritten, True, res.rewritten)
    return Outcome(None, d, raw, False, res.decoded)


def record_evidence(recorder, outcome: Outcome, *, provider: str, path: str,
                    session_id: str, trace_id: str = "", span_id: str = "") -> None:
    """Seal one row through the recorder when it is enabled. Raises only
    ``LedgerWriteError`` (strict mode); other failures are logged."""
    if recorder is None or (outcome.decision.action == "allow" and not outcome.decision.rules):
        return
    from ..recorder.extract import PartialEvent
    from ..recorder.ledger import LedgerWriteError
    from ..recorder.schema import now_monotonic_ns, now_ts_device
    d = outcome.decision
    enforced = mode() == "enforce"
    payload = {
        "guard_mode": mode(), "provider": provider, "path": path,
        "action": d.action,
        "forwarded": not (enforced and d.action == "block"),
        "redacted": enforced and d.action == "redact",
        "rules": d.rules, "chars": d.chars, "text_hmac": d.text_hmac,
        "rules_version": d.rules_version,
    }
    try:
        recorder.record(PartialEvent(
            session_id=session_id, kind=EVIDENCE_KIND, ts_device=now_ts_device(),
            ts_monotonic_ns=now_monotonic_ns(), tool_use_id=None, tool_name=None,
            payload=payload, model=None, provider="shield_guard",
            trace_id=trace_id, span_id=span_id))
    except LedgerWriteError:
        raise
    except Exception:  # noqa: BLE001 - evidence must not break the proxy
        log.exception("shield_guard: could not record evidence")


def error_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode()
