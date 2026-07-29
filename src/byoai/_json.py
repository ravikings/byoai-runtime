"""JSON codec used on the runtime's hot paths.

Uses orjson (Rust) when installed — ``pip install byoai-runtime[perf]`` — and
falls back to the stdlib transparently. Set ``BYOAI_JSON=std`` to force the
stdlib backend (useful for A/B benchmarking).

Contract (identical on both backends):

* Output is canonical-compact UTF-8 (no spaces, no ASCII escaping), so
  fingerprints hashed from ``dumps(..., sort_keys=True)`` are stable across
  backends and across installs that add/remove orjson.
* ``sort_keys`` ordering is guaranteed for string keys only (orjson sorts
  non-string keys by their stringified form; stdlib by raw value).
* Non-finite floats (NaN/Infinity) are NOT round-trip-safe: orjson serializes
  them as ``null``. Don't put them in cached values or transport payloads.

This embodies the project's performance philosophy: Python developer
experience, Rust acceleration where profiling shows it matters.
"""

from __future__ import annotations

import json as _stdlib_json
import os
from typing import Any

_orjson = None
if os.environ.get("BYOAI_JSON", "").lower() != "std":  # pragma: no branch
    try:
        import orjson as _orjson  # type: ignore[no-redef]
    except ImportError:
        _orjson = None


def backend_name() -> str:
    return "orjson" if _orjson is not None else "json (stdlib)"


if _orjson is not None:
    _oj = _orjson

    def dumps(obj: Any, *, sort_keys: bool = False, default: Any = None) -> str:
        # OPT_NON_STR_KEYS matches the stdlib's coercion of int/bool dict keys.
        option = _oj.OPT_NON_STR_KEYS | (_oj.OPT_SORT_KEYS if sort_keys else 0)
        return _oj.dumps(obj, option=option, default=default).decode()

    def loads(data: str | bytes) -> Any:
        return _oj.loads(data)

else:

    def dumps(obj: Any, *, sort_keys: bool = False, default: Any = None) -> str:
        # Compact separators + no ASCII escaping = byte-identical output to
        # orjson for the common payload shapes (str keys, str/int/float/bool).
        return _stdlib_json.dumps(
            obj,
            sort_keys=sort_keys,
            default=default,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def loads(data: str | bytes) -> Any:
        return _stdlib_json.loads(data)
