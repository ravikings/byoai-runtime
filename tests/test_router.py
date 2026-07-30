from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai.errors import AllProvidersFailed, ConfigurationError
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


async def test_list_content_incompatibility_falls_through_not_hard_fails():
    # Regression: a message with list-valued content (Anthropic tool_use/
    # tool_result blocks from a prior turn) reaching a non-Anthropic provider
    # in a fallback chain used to raise ConfigurationError from
    # require_text_content(), which ProviderRouter's `except ProviderError`
    # never caught — the whole request hard-failed instead of falling
    # through to a provider that can actually handle it.
    from byoai.providers.openai_compat import OpenAICompatProvider

    incompatible = OpenAICompatProvider(model="m", api_key="k")
    fallback = FakeProvider(name="fallback", reply="from fallback")
    router = ProviderRouter([incompatible, fallback], retry_policy=FAST)
    blocky = [
        Message(
            role="user",
            content=[{"type": "tool_use", "id": "x", "name": "y", "input": {}}],
        )
    ]
    response = await router.complete(blocky)
    assert response.content == "from fallback"
    assert response.provider == "fallback"
    await incompatible.close()


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


async def test_selection_default_is_ordered():
    a = FakeProvider(name="a", reply="from a")
    b = FakeProvider(name="b", reply="from b")
    router = ProviderRouter([a, b], retry_policy=FAST)
    first = await router.complete(MESSAGES)
    second = await router.complete(MESSAGES)
    assert first.provider == "a"
    assert second.provider == "a"  # "ordered" never rotates


async def test_selection_round_robin_rotates_starting_provider():
    a = FakeProvider(name="a", reply="from a")
    b = FakeProvider(name="b", reply="from b")
    c = FakeProvider(name="c", reply="from c")
    router = ProviderRouter([a, b, c], retry_policy=FAST, selection="round_robin")
    served = [(await router.complete(MESSAGES)).provider for _ in range(4)]
    assert served == ["a", "b", "c", "a"]


async def test_selection_round_robin_still_falls_through_on_failure():
    # Rotation changes who goes first, but a failure must still fall through
    # every remaining provider in ring order — nothing gets skipped outright.
    a = FakeProvider(name="a", fail_times=99)
    b = FakeProvider(name="b", fail_times=99)
    c = FakeProvider(name="c", reply="from c")
    router = ProviderRouter([a, b, c], retry_policy=FAST, selection="round_robin")
    first = await router.complete(MESSAGES)  # starts at a: a fails, b fails, c serves
    assert first.provider == "c"
    second = await router.complete(MESSAGES)  # starts at b: b fails, c serves
    assert second.provider == "c"


async def test_selection_custom_callable():
    a = FakeProvider(name="a", reply="from a")
    b = FakeProvider(name="b", reply="from b")

    def reversed_order(providers):
        return list(reversed(providers))

    router = ProviderRouter([a, b], retry_policy=FAST, selection=reversed_order)
    response = await router.complete(MESSAGES)
    assert response.provider == "b"


async def test_selection_callable_mutating_its_argument_does_not_corrupt_the_router():
    # Regression: self._select(self.providers) used to hand a custom
    # selection= callable the router's live provider list. A callable that
    # mutates in place instead of returning a new sequence (e.g. .pop() to
    # "exclude" a provider it considers unhealthy for just this call) used
    # to permanently corrupt self.providers for every future call too.
    a = FakeProvider(name="a", reply="from a")
    b = FakeProvider(name="b", reply="from b")

    def pop_first(providers):
        providers.pop(0)  # in-place mutation, not a copy
        return providers

    router = ProviderRouter([a, b], retry_policy=FAST, selection=pop_first)
    first = await router.complete(MESSAGES)
    assert first.provider == "b"
    assert len(router.providers) == 2  # "a" must still be there for the next call
    second = await router.complete(MESSAGES)
    assert second.provider == "b"  # pop_first drops index 0 again, "a" survives


async def test_selection_unknown_name_rejected():
    with pytest.raises(ConfigurationError):
        ProviderRouter([FakeProvider()], selection="least_loaded")  # type: ignore[arg-type]
