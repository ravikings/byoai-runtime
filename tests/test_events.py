from __future__ import annotations

from byoai.events import EventBus


async def test_exact_and_wildcard_subscription():
    bus = EventBus()
    seen: list[tuple[str, dict]] = []
    bus.on("provider.completed", lambda event, p: seen.append((event, p)))
    bus.on("provider.*", lambda event, p: seen.append((event, p)))
    await bus.emit("provider.completed", model="m")
    assert len(seen) == 2
    assert all(event == "provider.completed" for event, _ in seen)
    assert seen[0][1] == {"model": "m"}


async def test_async_handlers_awaited():
    bus = EventBus()
    seen = []

    async def handler(event, payload):
        seen.append(event)

    bus.on("x", handler)
    await bus.emit("x")
    assert seen == ["x"]


async def test_handler_errors_are_isolated():
    errors = []
    bus = EventBus(error_handler=lambda event, exc: errors.append((event, str(exc))))
    bus.on("x", lambda event, p: 1 / 0)
    ok = []
    bus.on("x", lambda event, p: ok.append(True))
    await bus.emit("x")
    assert ok == [True]
    assert len(errors) == 1


async def test_unsubscribe():
    bus = EventBus()
    seen = []
    unsubscribe = bus.on("x", lambda event, p: seen.append(1))
    await bus.emit("x")
    unsubscribe()
    await bus.emit("x")
    assert seen == [1]
