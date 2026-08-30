"""Golden-vector tests for the cap-digest-v1 port.

Most of these prove self-consistency (tool-order independence, key-order
independence, null/absent-field equivalence, int/float equivalence). The
`Cross-repo parity` section below additionally byte-compares against literal
digest values computed from Coriqo's own
``api/utils/capability_digest.py::compute_capability_digest`` (read from a
sibling checkout during a 2026-08-30 audit) — real parity, not just internal
consistency. Update these vectors if Coriqo's own test file
(``api/tests/test_capability_digest.py``) ever gains new golden values.
"""

from __future__ import annotations

from byoai.recorder.capability_digest import (
    DIGEST_SPEC,
    canonicalize_tool,
    compute_capability_digest,
    system_prompt_sha256,
)


def _tool(name: str, description: str | None = "does a thing", **schema: object) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": schema} if schema else {"type": "object"},
    }


def test_digest_spec_literal() -> None:
    assert DIGEST_SPEC == "cap-digest-v1"


def test_tool_order_independence() -> None:
    a = [_tool("search"), _tool("write"), _tool("read")]
    b = [_tool("write"), _tool("read"), _tool("search")]
    digest_a, _ = compute_capability_digest(tools=a, system_prompt="hi", model_id="m1")
    digest_b, _ = compute_capability_digest(tools=b, system_prompt="hi", model_id="m1")
    assert digest_a == digest_b


def test_tool_key_order_independence() -> None:
    tool_a = {"name": "search", "description": "d", "input_schema": {"a": 1, "b": 2}}
    tool_b = {"input_schema": {"b": 2, "a": 1}, "description": "d", "name": "search"}
    assert canonicalize_tool(tool_a) == canonicalize_tool(tool_b)
    digest_a, _ = compute_capability_digest(tools=[tool_a], system_prompt=None, model_id=None)
    digest_b, _ = compute_capability_digest(tools=[tool_b], system_prompt=None, model_id=None)
    assert digest_a == digest_b


def test_integral_float_equals_int() -> None:
    tool_float = {"name": "t", "description": None, "input_schema": {"min": 1.0}}
    tool_int = {"name": "t", "description": None, "input_schema": {"min": 1}}
    digest_float, _ = compute_capability_digest(
        tools=[tool_float], system_prompt=None, model_id=None
    )
    digest_int, _ = compute_capability_digest(
        tools=[tool_int], system_prompt=None, model_id=None
    )
    assert digest_float == digest_int


def test_explicit_null_equals_absent_field() -> None:
    tool_explicit_null = {"name": "t", "description": None, "input_schema": None}
    tool_absent = {"name": "t"}
    assert canonicalize_tool(tool_explicit_null) == canonicalize_tool(tool_absent)
    digest_explicit, _ = compute_capability_digest(
        tools=[tool_explicit_null], system_prompt=None, model_id=None
    )
    digest_absent, _ = compute_capability_digest(
        tools=[tool_absent], system_prompt=None, model_id=None
    )
    assert digest_explicit == digest_absent


def test_extra_fields_are_dropped() -> None:
    tool_plain = {"name": "t", "description": "d", "input_schema": {"a": 1}}
    tool_with_extra = {**tool_plain, "cache_control": {"type": "ephemeral"}, "extra": 42}
    digest_plain, _ = compute_capability_digest(
        tools=[tool_plain], system_prompt=None, model_id=None
    )
    digest_extra, _ = compute_capability_digest(
        tools=[tool_with_extra], system_prompt=None, model_id=None
    )
    assert digest_plain == digest_extra


def test_system_prompt_none_is_none_not_empty_string_hash() -> None:
    # Matches Coriqo's compute_capability_digest exactly: None in, None out
    # (embedded as JSON null in the envelope) — a real, distinct state from
    # an explicitly empty prompt, which still hashes to a real digest.
    assert system_prompt_sha256(None) is None
    assert system_prompt_sha256("") is not None
    assert system_prompt_sha256("") != system_prompt_sha256(None)


def test_system_prompt_sha256_is_stable_hex() -> None:
    digest = system_prompt_sha256("you are a helpful assistant")
    assert len(digest) == 64
    assert digest == system_prompt_sha256("you are a helpful assistant")


def test_different_system_prompt_changes_digest() -> None:
    tools = [_tool("search")]
    digest_a, hash_a = compute_capability_digest(
        tools=tools, system_prompt="prompt A", model_id="m"
    )
    digest_b, hash_b = compute_capability_digest(
        tools=tools, system_prompt="prompt B", model_id="m"
    )
    assert digest_a != digest_b
    assert hash_a != hash_b


def test_different_model_id_changes_digest() -> None:
    tools = [_tool("search")]
    digest_a, _ = compute_capability_digest(tools=tools, system_prompt=None, model_id="model-a")
    digest_b, _ = compute_capability_digest(tools=tools, system_prompt=None, model_id="model-b")
    assert digest_a != digest_b


def test_different_tool_set_changes_digest() -> None:
    digest_a, _ = compute_capability_digest(
        tools=[_tool("search")], system_prompt=None, model_id=None
    )
    digest_b, _ = compute_capability_digest(
        tools=[_tool("search"), _tool("write")], system_prompt=None, model_id=None
    )
    assert digest_a != digest_b


def test_digest_is_deterministic() -> None:
    tools = [_tool("write"), _tool("search")]
    digest_a, _ = compute_capability_digest(tools=tools, system_prompt="p", model_id="m")
    digest_b, _ = compute_capability_digest(tools=tools, system_prompt="p", model_id="m")
    assert digest_a == digest_b


# ---------------------------------------------------------------------------
# Cross-repo parity — literal digests computed from Coriqo's
# api/utils/capability_digest.py, not just this port's own output.
# ---------------------------------------------------------------------------


def test_small_magnitude_float_matches_coriqo_python_exponent_form() -> None:
    # Coriqo's canonical JSON uses Python's own repr()/json.dumps shortest-
    # round-trip float formatting, NOT ECMAScript Number::toString. The two
    # disagree at small magnitudes — Python: "1e-07" (2-digit zero-padded
    # exponent), ECMAScript: "1e-7" — which is exactly the bug this module
    # had until 2026-08-30 (it delegated to this repo's RFC-8785/JCS
    # canonical.py instead of reimplementing Coriqo's json.dumps-based
    # scheme). A tool schema with a small epsilon/threshold float is a
    # plausible real value, not a contrived edge case.
    digest_a, _ = compute_capability_digest(
        tools=[{"name": "x", "input_schema": {"threshold": 1e-7}}],
        system_prompt=None, model_id=None,
    )
    assert digest_a == "a258137decd201c512d519cfcaf639f1dfccd88c99d448d1c9c1d8bf8da4c2b6"

    # 1e-5: Python's repr keeps this in exponential form ("1e-05"); JCS's
    # fixed/exponential threshold instead renders it as a decimal
    # ("0.00001") — a second, independent divergence point at a different
    # magnitude boundary.
    digest_b, _ = compute_capability_digest(
        tools=[{"name": "x", "input_schema": {"threshold": 1e-5}}],
        system_prompt=None, model_id=None,
    )
    assert digest_b == "9fb25ceb4918b14dc72bf6c5aa330909753e040348fd26bd49b5bef68e40b41b"


def test_astral_plane_sibling_keys_match_coriqo_code_point_order() -> None:
    # Coriqo sorts dict keys via Python's json.dumps(sort_keys=True), i.e.
    # Unicode code-point order. JCS (RFC 8785) instead sorts by UTF-16 code
    # unit — the two agree for BMP-only keys but disagree when a sibling key
    # sits in U+E000-U+FFFF next to a key above U+FFFF (an astral
    # character's leading surrogate, 0xD800, sorts BELOW 0xE000 in
    # UTF-16-code-unit order but ABOVE it in code-point order).
    digest, _ = compute_capability_digest(
        tools=[{"name": "x", "input_schema": {"": 1, "\U00010000": 2}}],
        system_prompt=None, model_id=None,
    )
    assert digest == "e4475f962ea1c7f0d299895bbdee6f0714faa81d886b03181a4ad683572bc147"


def test_golden_envelope_matches_coriqo_empty_tools_no_prompt() -> None:
    # Same input/output pair as Coriqo's
    # test_golden_envelope_digest_for_empty_tools_and_no_prompt.
    digest, prompt_hash = compute_capability_digest(
        tools=[], system_prompt=None, model_id="claude-x",
    )
    assert prompt_hash is None
    import hashlib
    expected_payload = b'{"model_id":"claude-x","spec":"cap-digest-v1","system_prompt_sha256":null,"tools":[]}'
    assert digest == hashlib.sha256(expected_payload).hexdigest()
