from __future__ import annotations

import httpx
import pytest

from byoai.errors import ProviderError
from byoai.providers.embeddings import OpenAICompatEmbedder
from byoai.providers.gemini import GeminiProvider
from byoai.types import Message

MESSAGES = [
    Message(role="system", content="be brief"),
    Message(role="user", content="hi"),
]


def make_gemini(handler) -> GeminiProvider:
    client = httpx.AsyncClient(
        base_url="https://gemini.test/v1beta", transport=httpx.MockTransport(handler)
    )
    return GeminiProvider(model="gemini-2.0-flash", api_key="k", client=client)


async def test_gemini_complete_maps_roles_and_usage():
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "hello"}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2},
        })

    provider = make_gemini(handler)
    response = await provider.complete(MESSAGES, temperature=0.3, max_tokens=64)
    assert captured["path"].endswith("gemini-2.0-flash:generateContent")
    assert "systemInstruction" in captured["body"]
    assert "maxOutputTokens" in captured["body"]
    assert response.content == "hello"
    assert response.usage.total_tokens == 6
    assert response.finish_reason == "stop"


async def test_gemini_forwards_tool_messages_as_user_role():
    captured = {}

    def handler(request):
        captured["body"] = request.read().decode()
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
        })

    provider = make_gemini(handler)
    await provider.complete([
        Message(role="user", content="what's the weather?"),
        Message(role="assistant", content="let me check"),
        Message(role="tool", content="72F and sunny"),
    ])
    assert "72F and sunny" in captured["body"]  # tool output reaches the model


async def test_gemini_empty_candidates_is_provider_error():
    provider = make_gemini(lambda request: httpx.Response(200, json={"candidates": []}))
    with pytest.raises(ProviderError):
        await provider.complete(MESSAGES)


async def test_gemini_stream():
    def handler(request):
        body = (
            'data: {"candidates": [{"content": {"parts": [{"text": "hel"}]}}]}\n\n'
            'data: {"candidates": [{"content": {"parts": [{"text": "lo"}]}}], '
            '"usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2}}\n\n'
        )
        return httpx.Response(200, content=body.encode(),
                              headers={"content-type": "text/event-stream"})

    provider = make_gemini(handler)
    chunks = [c async for c in provider.stream(MESSAGES)]
    assert "".join(c.delta for c in chunks if not c.done) == "hello"
    assert chunks[-1].done and chunks[-1].usage.total_tokens == 6


def make_embedder(handler) -> OpenAICompatEmbedder:
    client = httpx.AsyncClient(
        base_url="https://emb.test/v1", transport=httpx.MockTransport(handler)
    )
    return OpenAICompatEmbedder(model="text-embedding-3-small", api_key="k", client=client)


async def test_embedder_single_and_batch():
    def handler(request):
        import json

        n = len(json.loads(request.read())["input"])
        return httpx.Response(200, json={
            "data": [{"embedding": [0.1 * (i + 1), 0.2]} for i in range(n)]
        })

    embedder = make_embedder(handler)
    single = await embedder("hello")
    assert single == [0.1, 0.2]
    batch = await embedder.embed_batch(["a", "b"])
    assert len(batch) == 2 and batch[1][0] == pytest.approx(0.2)


async def test_embedder_count_mismatch_is_provider_error():
    embedder = make_embedder(
        lambda request: httpx.Response(200, json={"data": []})
    )
    with pytest.raises(ProviderError):
        await embedder("hello")
