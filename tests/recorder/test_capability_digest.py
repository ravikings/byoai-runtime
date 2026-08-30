"""Golden-vector self-consistency tests for the cap-digest-v1 port.

These do NOT prove byte-for-byte parity with Coriqo's own implementation —
that repo's fixtures were not visible when this was written (see the warning
in ``byoai.recorder.capability_digest``). What they prove is that this port
has the three properties the spec calls out as load-bearing: tool-order
independence, key-order independence, null/absent-field equivalence, and
int/float equivalence for whole numbers. A real cross-repo parity check has
to run once both sides exist, against a shared fixture.
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


def test_system_prompt_none_and_empty_string_hash_identically() -> None:
    assert system_prompt_sha256(None) == system_prompt_sha256("")


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
