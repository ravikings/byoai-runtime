"""
openai_compat.py — Anthropic Messages API <-> OpenAI Chat Completions API
translation layer for byoai-runtime.

Why this exists: Claude Code (and anything else speaking the Anthropic
Messages API) always sends requests in Anthropic's shape — `system`,
`messages` with content blocks, `tool_use`/`tool_result` blocks,
`input_schema` on tools, etc. Many other LLM services (self-hosted vLLM,
Ollama, Groq, OpenRouter, Together, and most local model servers) only
speak the OpenAI Chat Completions shape instead. This module lets the
proxy accept an Anthropic-shaped request, translate it to OpenAI's shape,
forward it to any OpenAI-spec-compatible base_url, and translate the
response (streaming or not) back to Anthropic's shape — so the client
never has to know or care which backend actually served the request.

This module has no FastAPI/httpx-app dependency of its own; main.py wires
it in via BACKENDS routing and passes in the shared http_client.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

# ---------------------------------------------------------------------------
# Request translation: Anthropic Messages -> OpenAI Chat Completions
# ---------------------------------------------------------------------------


def _anthropic_content_to_openai_parts(content) -> tuple[list | str, list, list]:
    """
    Split a single Anthropic message's content into:
      - content_parts: OpenAI-style content parts (text / image_url) for a
        normal user/assistant message,
      - extra_messages: messages that must be emitted as SEPARATE OpenAI
        messages (tool results become role="tool" messages; OpenAI has no
        equivalent of inlining a tool result inside a user turn),
      - tool_calls: OpenAI-shaped tool_calls to attach to an assistant
        message (from Anthropic tool_use blocks).
    Returns (content_parts, extra_messages, tool_calls).
    """
    if isinstance(content, str):
        return content, [], []

    content_parts = []
    extra_messages = []
    tool_calls = []

    for block in content:
        btype = block.get("type")
        if btype == "text":
            content_parts.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image":
            source = block.get("source", {})
            media_type = source.get("media_type", "image/png")
            data = source.get("data", "")
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{data}"},
                }
            )
        elif btype == "tool_use":
            # Assistant-side tool call -> OpenAI tool_calls on the assistant message
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )
        elif btype == "tool_result":
            # User-side tool result -> its own OpenAI role="tool" message
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                # Flatten any nested text blocks; images inside tool_result
                # aren't representable in a plain "tool" message under the
                # OpenAI spec, so we keep text only (documented limitation).
                result_content = "\n".join(
                    b.get("text", "")
                    for b in result_content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            extra_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": result_content
                    if isinstance(result_content, str)
                    else json.dumps(result_content),
                }
            )
        elif btype == "document":
            # No direct OpenAI equivalent for a raw document block; degrade
            # to a text marker rather than silently dropping it unnoticed.
            content_parts.append(
                {
                    "type": "text",
                    "text": (
                        "[byoai-runtime: document block present but not "
                        "translatable to OpenAI format]"
                    ),
                }
            )

    return content_parts, extra_messages, tool_calls


def anthropic_to_openai_request(body: dict) -> dict:
    """
    Translate a full Anthropic Messages API request body into an OpenAI
    Chat Completions request body.
    """
    openai_messages = []

    system = body.get("system")
    if system:
        if isinstance(system, str):
            openai_messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text = "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
            if text:
                openai_messages.append({"role": "system", "content": text})

    for msg in body.get("messages", []):
        role = msg.get("role")
        content = msg.get("content", "")
        parts, extra_messages, tool_calls = (
            _anthropic_content_to_openai_parts(content)
            if isinstance(content, list)
            else (content, [], [])
        )

        if role == "assistant":
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if isinstance(parts, str):
                assistant_msg["content"] = parts if parts else None
            else:
                text_only = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
                assistant_msg["content"] = text_only if text_only else None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            openai_messages.append(assistant_msg)
        else:
            # user role: tool_result blocks became "extra_messages" (role=tool)
            # and must be emitted BEFORE the remaining user content, since
            # OpenAI expects tool results immediately after the assistant
            # turn that requested them.
            openai_messages.extend(extra_messages)
            has_real_content = (isinstance(parts, str) and parts) or (
                isinstance(parts, list) and len(parts) > 0
            )
            if has_real_content:
                openai_messages.append({"role": "user", "content": parts})

    openai_body = {
        "model": body.get("model"),
        "messages": openai_messages,
        "stream": body.get("stream", False),
    }
    if "max_tokens" in body:
        openai_body["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        openai_body["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        openai_body["stop"] = body["stop_sequences"]

    tools = body.get("tools")
    if tools:
        openai_body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    tool_choice = body.get("tool_choice")
    if tool_choice:
        tc_type = tool_choice.get("type")
        if tc_type == "auto":
            openai_body["tool_choice"] = "auto"
        elif tc_type == "any":
            openai_body["tool_choice"] = "required"
        elif tc_type == "tool":
            openai_body["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice.get("name", "")},
            }

    return openai_body


# ---------------------------------------------------------------------------
# Response translation: OpenAI Chat Completions -> Anthropic Messages
# ---------------------------------------------------------------------------

_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
    None: "end_turn",
}


def _detect_stop_sequence(
    text: str | None, stop_sequences: list | None, choice: dict | None = None
) -> str | None:
    """
    OpenAI's `finish_reason: "stop"` covers BOTH a natural end-of-turn and
    generation halting because it hit one of the caller's `stop` strings —
    unlike Anthropic, which has a distinct `stop_reason: "stop_sequence"`.
    Most backends (including Ollama) give no further signal: the matched
    stop string is truncated out of the output entirely, so it can't be
    recovered from the text afterwards, and `finish_reason` alone can't
    tell "hit a stop word" apart from "model naturally finished." That
    ambiguity is unresolvable from here for those backends — reporting
    "stop_sequence" on every ordinary completion would be a worse default
    than the current "always end_turn."

    Two narrower signals we CAN use when present:
    - vLLM's OpenAI-compatible server adds a non-standard `stop_reason` on
      the choice set to the actual matched string (null for natural EOS).
    - If a backend does include the trailing stop text verbatim (some do),
      an exact suffix match is a reliable positive signal.
    """
    if choice:
        vllm_stop_reason = choice.get("stop_reason")
        if isinstance(vllm_stop_reason, str) and vllm_stop_reason:
            return vllm_stop_reason
    if not text or not stop_sequences:
        return None
    for seq in stop_sequences:
        if seq and text.endswith(seq):
            return seq
    return None


def openai_to_anthropic_response(
    openai_resp: dict, requested_model: str, stop_sequences: list | None = None
) -> dict:
    """Translate one complete (non-streaming) OpenAI chat.completion response
    into an Anthropic Messages API response body."""
    choice = (openai_resp.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content_blocks = []

    text = message.get("content")
    if text:
        content_blocks.append({"type": "text", "text": text})

    for tc in message.get("tool_calls", []) or []:
        func = tc.get("function", {})
        try:
            tool_input = json.loads(func.get("arguments") or "{}")
        except json.JSONDecodeError:
            tool_input = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "input": tool_input,
            }
        )

    usage = openai_resp.get("usage", {})
    resp_id = openai_resp.get("id") or (
        "msg_" + hashlib.sha256(json.dumps(openai_resp).encode()).hexdigest()[:24]
    )

    finish_reason = choice.get("finish_reason")
    matched_stop = (
        _detect_stop_sequence(text, stop_sequences, choice) if finish_reason == "stop" else None
    )
    stop_reason = (
        "stop_sequence" if matched_stop else _FINISH_REASON_MAP.get(finish_reason, "end_turn")
    )

    return {
        "id": resp_id,
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": matched_stop,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# Streaming translation: OpenAI SSE chunks -> Anthropic SSE events
# ---------------------------------------------------------------------------


async def translate_openai_stream_to_anthropic_sse(
    openai_lines: AsyncIterator[str], requested_model: str, stop_sequences: list | None = None
) -> AsyncIterator[bytes]:
    """
    Consume an OpenAI-style SSE line stream ("data: {...}\\n\\n" chunks,
    terminated by "data: [DONE]") and yield Anthropic-style SSE event bytes.

    Anthropic's stream shape is a fixed sequence:
      message_start -> content_block_start -> (repeated) content_block_delta
      -> content_block_stop -> message_delta -> message_stop

    OpenAI's is a flat sequence of incremental `delta` objects. We track
    enough state to emit one Anthropic content_block per distinct OpenAI
    content type (text vs. each tool call index) as they first appear.
    """
    msg_id = "msg_" + hashlib.sha256(str(id(openai_lines)).encode()).hexdigest()[:24]

    def sse(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    text_block_open = False
    text_block_index = None
    tool_blocks_open = {}  # openai tool_call index -> anthropic content block index
    next_block_index = 0
    finish_reason = None
    finish_choice = None
    usage = {}
    accumulated_text = ""

    async for line in openai_lines:
        if not line.startswith("data:"):
            continue
        payload_str = line[len("data:") :].strip()
        if payload_str == "[DONE]":
            break
        try:
            chunk = json.loads(payload_str)
        except json.JSONDecodeError:
            continue

        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta", {})
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]
            finish_choice = choice
        if chunk.get("usage"):
            usage = chunk["usage"]

        if "content" in delta and delta["content"]:
            if not text_block_open:
                yield sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": next_block_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                text_block_open = True
                text_block_index = next_block_index
                next_block_index += 1
            accumulated_text += delta["content"]
            yield sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": text_block_index,
                    "delta": {"type": "text_delta", "text": delta["content"]},
                },
            )

        for tc_delta in delta.get("tool_calls", []) or []:
            idx = tc_delta.get("index", 0)
            if idx not in tool_blocks_open:
                anthropic_idx = next_block_index
                next_block_index += 1
                tool_blocks_open[idx] = anthropic_idx
                func = tc_delta.get("function", {})
                yield sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": anthropic_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc_delta.get("id", ""),
                            "name": func.get("name", ""),
                            "input": {},
                        },
                    },
                )
            anthropic_idx = tool_blocks_open[idx]
            func = tc_delta.get("function", {})
            if func.get("arguments"):
                yield sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": anthropic_idx,
                        "delta": {"type": "input_json_delta", "partial_json": func["arguments"]},
                    },
                )

    if text_block_open:
        yield sse("content_block_stop", {"type": "content_block_stop", "index": text_block_index})
    for anthropic_idx in tool_blocks_open.values():
        yield sse("content_block_stop", {"type": "content_block_stop", "index": anthropic_idx})

    matched_stop = (
        _detect_stop_sequence(accumulated_text, stop_sequences, finish_choice)
        if finish_reason == "stop"
        else None
    )
    stream_stop_reason = (
        "stop_sequence" if matched_stop else _FINISH_REASON_MAP.get(finish_reason, "end_turn")
    )

    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stream_stop_reason, "stop_sequence": matched_stop},
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        },
    )
    yield sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# Backend forwarding helper (uses the caller's shared httpx.AsyncClient)
# ---------------------------------------------------------------------------


async def forward_to_openai_compatible(
    http_client,
    base_url: str,
    api_key: str,
    anthropic_body: dict,
    extra_headers: dict | None = None,
):
    """
    Translate `anthropic_body`, POST it to `{base_url}/chat/completions`,
    and return the raw httpx.Response (caller decides how to translate it
    back — streaming vs non-streaming need different handling upstream).
    """
    openai_body = anthropic_to_openai_request(anthropic_body)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)

    url = base_url.rstrip("/") + "/chat/completions"
    if openai_body.get("stream"):
        req = http_client.build_request("POST", url, json=openai_body, headers=headers)
        return await http_client.send(req, stream=True)
    return await http_client.post(url, json=openai_body, headers=headers, timeout=120.0)
