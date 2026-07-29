from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai.errors import AllProvidersFailed
from byoai.providers.router import ProviderRouter, RetryPolicy
from byoai.types import Message

FAST = RetryPolicy(max_retries=2, base_delay=0.001, max_delay=0.01)
MESSAGES = [Message(role="user", content="hi")]


async def test_retry_then_success():
    provider = FakeProvider(fail_times=2)
    router = ProviderRouter([provider], retry_policy=FAST)
    response = await router.complete(MESSAGES)
    assert response.content == "hello from fake"
    assert provider.calls == 3


async def test_fallback_to_second_provider():
    primary = FakeProvider(name="primary", fail_times=99)
    fallback = FakeProvider(name="fallback", reply="from fallback")
    router = ProviderRouter([primary, fallback], retry_policy=FAST)
    response = await router.complete(MESSAGES)
    assert response.content == "from fallback"
    assert response.provider == "fallback"
    assert primary.calls == 3  # 1 initial + 2 retries


async def test_non_retryable_skips_retries():
    primary = FakeProvider(name="primary", fail_times=99, fail_retryable=False)
    fallback = FakeProvider(name="fallback", reply="from fallback")
    router = ProviderRouter([primary, fallback], retry_policy=FAST)
    response = await router.complete(MESSAGES)
    assert response.provider == "fallback"
    assert primary.calls == 1


async def test_all_providers_failed():
    router = ProviderRouter(
        [FakeProvider(name="a", fail_times=99), FakeProvider(name="b", fail_times=99)],
        retry_policy=FAST,
    )
    with pytest.raises(AllProvidersFailed) as excinfo:
        await router.complete(MESSAGES)
    assert len(excinfo.value.errors) == 6  # 2 providers x 3 attempts


async def test_stream_fallback_before_first_token():
    primary = FakeProvider(name="primary", fail_times=99, fail_retryable=False)
    fallback = FakeProvider(name="fallback", reply="streamed ok")
    router = ProviderRouter([primary, fallback], retry_policy=FAST)
    chunks = [chunk async for chunk in router.stream(MESSAGES)]
    assert "".join(c.delta for c in chunks).strip() == "streamed ok"


async def test_empty_provider_list_rejected():
    with pytest.raises(ValueError):
        ProviderRouter([])
