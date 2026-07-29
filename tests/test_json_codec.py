from __future__ import annotations

import json as stdlib_json
import subprocess
import sys

from byoai._json import backend_name, dumps, loads

PAYLOAD = {
    "messages": [{"role": "user", "content": "héllo wörld — ünïcode"}],
    "model": "gpt-4o",
    "pipeline": None,
    "temperature": 0.2,
    "n": 3,
    "flag": True,
}


def test_round_trip():
    assert loads(dumps(PAYLOAD)) == PAYLOAD
    assert loads(dumps("just a string")) == "just a string"


def test_sort_keys_is_deterministic():
    a = dumps(PAYLOAD, sort_keys=True)
    b = dumps(dict(reversed(list(PAYLOAD.items()))), sort_keys=True)
    assert a == b


def test_output_is_canonical_compact_across_backends():
    """Fingerprints must not depend on whether orjson is installed."""
    code = (
        "import sys; sys.path.insert(0, 'tests'); "
        "from byoai._json import dumps, backend_name; "
        f"print(backend_name()); print(dumps({PAYLOAD!r}, sort_keys=True))"
    )
    forced_std = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, check=True,
        env={"BYOAI_JSON": "std", "PATH": "", "PYTHONPATH": ":".join(sys.path)},
    ).stdout.strip().splitlines()
    assert forced_std[0] == "json (stdlib)"
    assert forced_std[1] == dumps(PAYLOAD, sort_keys=True)


def test_stdlib_parse_compatibility():
    # whatever we emit must be parseable by any strict JSON consumer
    assert stdlib_json.loads(dumps(PAYLOAD)) == PAYLOAD


def test_non_str_keys_coerced_like_stdlib():
    payload = {1: "a", 2.5: "b", False: "c"}
    assert loads(dumps(payload)) == loads(stdlib_json.dumps(payload))


def test_backend_name_reports():
    assert backend_name() in ("orjson", "json (stdlib)")
