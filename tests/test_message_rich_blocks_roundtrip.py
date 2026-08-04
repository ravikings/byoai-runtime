"""Step 2 regression tests: prove the runtime's Message model + ContextResolver
+ Anthropic adapter carry rich Anthropic content blocks — ``tool_use``,
``tool_result``, and ``cache_control`` annotations — verbatim from an
Anthropic-shaped request all the way to the outgoing provider wire body.

This is the load-bearing guarantee for the strangler's step 3 (routing the
proxy's ``/v1/messages`` through ``Runtime``): if a block or a ``cache_control``
key were silently dropped in normalization, the proxy's whole value prop
(prompt caching + faithful pass-through) would break.

CONFIRMED at baseline: ``Message.content`` is ``str | list[dict] | None`` and
carries the blocks unmodified; no model change was needed for these to pass.
"""

from __future__ import annotations

from byoai.context import RequestContext
from byoai.providers.anthropic import AnthropicProvider
from byoai.stages import ContextResolver

BIG_LOG = "traceback line\n" * 200
SYSTEM_BLOCK = {
    "type": "text",
    "text": "You are a coding agent.",
    "cache_control": {"type": "ephemeral"},
}
TOOL_RESULT_BLOCK = {
    "type": "tool_result",
    "tool_use_id": "toolu_1",
    "content": BIG_LOG,
}
TOOL_USE_BLOCK = {
    "type": "tool_use",
    "id": "toolu_1",
    "name": "bash",
    "input": {"command": "pytest"},
}
CACHED_TEXT_BLOCK = {
    "type": "text",
    "text": "large pinned context " * 100,
    "cache_control": {"type": "ephemeral"},
}


def _anthropic_body() -> dict:
    """An Anthropic /v1/messages-shaped body with every rich block type."""
    return {
        "messages": [
            {"role": "system", "content": [SYSTEM_BLOCK]},
            {"role": "assistant", "content": [TOOL_USE_BLOCK]},
            {"role": "user", "content": [TOOL_RESULT_BLOCK, CACHED_TEXT_BLOCK]},
        ]
    }


async def test_context_resolver_preserves_rich_blocks_verbatim():
    ctx = RequestContext(input=_anthropic_body())
    await ContextResolver().execute(ctx)

    by_role = {m.role: m for m in ctx.messages}
    system, assistant, user = (
        by_role["system"].content,
        by_role["assistant"].content,
        by_role["user"].content,
    )
    assert isinstance(system, list) and isinstance(assistant, list) and isinstance(user, list)
    # system block + its cache_control survive
    assert system == [SYSTEM_BLOCK]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # tool_use survives verbatim
    assert assistant == [TOOL_USE_BLOCK]
    # tool_result (large) + a second cache_control block survive verbatim
    assert user == [TOOL_RESULT_BLOCK, CACHED_TEXT_BLOCK]
    assert user[1]["cache_control"] == {"type": "ephemeral"}


async def test_anthropic_payload_carries_blocks_and_cache_control_to_the_wire():
    ctx = RequestContext(input=_anthropic_body())
    await ContextResolver().execute(ctx)

    provider = AnthropicProvider(model="claude-x", api_key="test-key")
    body = provider._payload(ctx.messages, {})

    # system field is the pre-built block list, cache_control intact
    assert body["system"] == [SYSTEM_BLOCK]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}

    # the non-system messages reach the wire with their blocks unmodified
    wire = {m["role"]: m["content"] for m in body["messages"]}
    assert "system" not in wire  # system is lifted to the top-level field
    assert wire["assistant"] == [TOOL_USE_BLOCK]
    assert wire["user"] == [TOOL_RESULT_BLOCK, CACHED_TEXT_BLOCK]
    assert wire["user"][1]["cache_control"] == {"type": "ephemeral"}
