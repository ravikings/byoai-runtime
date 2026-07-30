from __future__ import annotations

import pytest
from tests.conftest import FakeProvider

from byoai import Runtime
from byoai.cache.memory import MemoryCache

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from byoai.telemetry.otel import instrument  # noqa: E402


def make_traced_runtime(**runtime_kwargs):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = Runtime(providers=[FakeProvider()], **runtime_kwargs)
    instrument(runtime, tracer_provider=provider)
    return runtime, exporter


async def test_execute_span_with_attributes_and_stage_children():
    runtime, exporter = make_traced_runtime()
    await runtime.execute("hi", user_id="u1")

    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["byoai.execute"]
    assert root.context is not None  # always set for a span that finished normally
    assert root.attributes is not None  # always set: OpenTelemetryMiddleware sets byoai.cached
    assert root.attributes["byoai.cached"] is False
    assert root.attributes["gen_ai.system"] == "fake"
    assert root.attributes["gen_ai.response.model"] == "fake-1"
    assert root.attributes["gen_ai.usage.input_tokens"] == 10
    assert root.attributes["gen_ai.usage.output_tokens"] == 5
    assert root.attributes["byoai.user_id"] == "u1"

    stage_spans = [name for name in spans if name.startswith("byoai.stage.")]
    assert "byoai.stage.context_resolver" in stage_spans
    assert "byoai.stage.provider_call" in stage_spans
    for name in stage_spans:
        parent = spans[name].parent
        assert parent is not None  # every stage span is a child of the root span
        assert parent.span_id == root.context.span_id


async def test_provider_lifecycle_span_events():
    runtime, exporter = make_traced_runtime()
    await runtime.execute("hi")
    # Provider events attach to the active span — the provider_call stage span.
    stage = next(
        s for s in exporter.get_finished_spans() if s.name == "byoai.stage.provider_call"
    )
    event_names = [event.name for event in stage.events]
    assert "provider.started" in event_names
    assert "provider.completed" in event_names
    completed = next(e for e in stage.events if e.name == "provider.completed")
    assert completed.attributes is not None  # on_provider_event always passes attributes
    assert completed.attributes["gen_ai.usage.input_tokens"] == 10
    assert completed.attributes["gen_ai.system"] == "fake"


async def test_cache_hit_recorded():
    runtime, exporter = make_traced_runtime(cache=MemoryCache())
    await runtime.execute("same q")
    await runtime.execute("same q")
    roots = [s for s in exporter.get_finished_spans() if s.name == "byoai.execute"]
    assert roots[0].attributes is not None and roots[1].attributes is not None
    assert roots[0].attributes["byoai.cached"] is False
    assert roots[1].attributes["byoai.cached"] is True
    assert roots[0].attributes["byoai.cost_usd"] == 0.0  # $0 is recorded, not omitted
    all_events = [e.name for s in exporter.get_finished_spans() for e in s.events]
    assert "cache.hit" in all_events and "cache.miss" in all_events


async def test_error_marks_span_and_ends_stage_spans():
    from opentelemetry.trace import StatusCode

    from byoai import AllProvidersFailed

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = Runtime(providers=[FakeProvider(fail_times=99, fail_retryable=False)])
    instrument(runtime, tracer_provider=provider)

    with pytest.raises(AllProvidersFailed):
        await runtime.execute("hi")

    spans = {s.name: s for s in exporter.get_finished_spans()}
    root = spans["byoai.execute"]
    assert root.status.status_code == StatusCode.ERROR
    assert any(e.name == "exception" for e in root.events)
    # the failing stage's span was force-closed, not leaked
    stage = spans["byoai.stage.provider_call"]
    assert stage.status.status_code == StatusCode.ERROR


async def test_declarative_config_wiring():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = Runtime(
        providers=[FakeProvider()],
        telemetry={"provider": "opentelemetry", "tracer_provider": provider},
    )
    await runtime.execute("hi")
    assert any(s.name == "byoai.execute" for s in exporter.get_finished_spans())


async def test_prebuilt_tracer_provider_accepted_directly():
    # telemetry= accepts a pre-built provider, mirroring cache=/vector_store=.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    runtime = Runtime(providers=[FakeProvider()], telemetry=provider)
    await runtime.execute("hi")
    assert any(s.name == "byoai.execute" for s in exporter.get_finished_spans())
    # caller-owned provider: close() must NOT shut it down
    assert runtime._owned_tracer_provider is None


async def test_empty_endpoint_rejected():
    from byoai import ConfigurationError

    with pytest.raises(ConfigurationError):
        Runtime(
            providers=[FakeProvider()],
            telemetry={"provider": "opentelemetry", "endpoint": ""},
        )


async def test_owned_provider_shut_down_on_close():
    class FakeTracerProvider:
        def __init__(self):
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    runtime = Runtime(providers=[FakeProvider()])
    fake = FakeTracerProvider()
    runtime._owned_tracer_provider = fake
    await runtime.close()
    assert fake.shutdown_called


async def test_untraced_runtime_has_no_overhead_hooks():
    runtime = Runtime(providers=[FakeProvider()])
    assert len(runtime.middleware) == 0
    result = await runtime.execute("hi")
    assert result.content == "hello from fake"
