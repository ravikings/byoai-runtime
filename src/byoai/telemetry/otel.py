"""OpenTelemetry tracing for the runtime (zero-SaaS: OTLP to YOUR collector).

One span per execution (``byoai.execute``), one child span per pipeline stage,
and span events for provider lifecycle (started/completed/failed — retries and
fallbacks show up as multiple event pairs). Usage/cost land as attributes
following the OTel GenAI semantic conventions where they apply.

    from byoai.telemetry.otel import instrument

    instrument(runtime)                      # uses the globally configured SDK
    instrument(runtime, tracer_provider=tp)  # or an explicit provider

Or declaratively (sets up an OTLP exporter to your existing collector)::

    Runtime(..., telemetry={"provider": "opentelemetry",
                            "endpoint": "http://otel-collector.internal:4317",
                            "service_name": "my-app"})

Requires the ``otel`` extra: ``pip install byoai-runtime[otel]``. Nothing in
this module is imported unless telemetry is actually enabled, so the runtime
has zero overhead without it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "byoai.telemetry.otel requires OpenTelemetry: pip install 'byoai-runtime[otel]'"
    ) from exc

from .. import events as ev
from ..context import RequestContext
from ..middleware import CallNext, Middleware

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

_STATE_KEY = "byoai.otel_stage_spans"


class OpenTelemetryMiddleware(Middleware):
    """Wraps every execution in a ``byoai.execute`` span."""

    def __init__(self, tracer_provider: Any | None = None) -> None:
        self._tracer = trace.get_tracer("byoai", tracer_provider=tracer_provider)

    @property
    def tracer(self) -> Any:
        return self._tracer

    async def __call__(self, ctx: RequestContext, call_next: CallNext) -> None:
        with self._tracer.start_as_current_span("byoai.execute") as span:
            span.set_attribute("byoai.request_id", ctx.request_id)
            if ctx.pipeline_name:
                span.set_attribute("byoai.pipeline", ctx.pipeline_name)
            if ctx.user_id:
                span.set_attribute("byoai.user_id", ctx.user_id)
            try:
                await call_next(ctx)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                self._finalize(span, ctx)

    def _finalize(self, span: Any, ctx: RequestContext) -> None:
        span.set_attribute("byoai.cached", ctx.cached)
        span.set_attribute("byoai.elapsed_ms", round(ctx.elapsed_ms, 3))
        if ctx.model:
            span.set_attribute("gen_ai.response.model", ctx.model)
        if ctx.provider:
            span.set_attribute("gen_ai.system", ctx.provider)
        if ctx.usage.total_tokens:
            span.set_attribute("gen_ai.usage.input_tokens", ctx.usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", ctx.usage.output_tokens)
        # Always set: consumers must be able to tell "$0" from "not recorded".
        span.set_attribute("byoai.cost_usd", ctx.usage.cost_usd)
        # A stage that raised never got its STAGE_COMPLETED event — close its
        # span here so nothing leaks.
        for leftover_span, token in ctx.state.pop(_STATE_KEY, {}).values():
            otel_context.detach(token)
            leftover_span.set_status(Status(StatusCode.ERROR, "stage did not complete"))
            leftover_span.end()


def instrument(runtime: Runtime, *, tracer_provider: Any | None = None) -> Runtime:
    """Attach tracing to a runtime: request spans, per-stage child spans, and
    provider lifecycle span events."""
    middleware = OpenTelemetryMiddleware(tracer_provider)
    runtime.use(middleware)
    tracer = middleware.tracer

    def on_stage_started(event: str, payload: dict[str, Any]) -> None:
        ctx: RequestContext = payload["ctx"]
        span = tracer.start_span(f"byoai.stage.{payload['stage']}")
        # Make the stage span the active context for the stage's duration so
        # spans created inside the stage (e.g. instrumented HTTP clients)
        # parent to the stage, not the request. Stages run sequentially in the
        # same task between the started/completed events, so attach/detach
        # pairs nest correctly.
        token = otel_context.attach(trace.set_span_in_context(span))
        ctx.state.setdefault(_STATE_KEY, {})[payload["stage"]] = (span, token)

    def on_stage_completed(event: str, payload: dict[str, Any]) -> None:
        ctx: RequestContext = payload["ctx"]
        entry = ctx.state.get(_STATE_KEY, {}).pop(payload["stage"], None)
        if entry is not None:
            span, token = entry
            otel_context.detach(token)
            span.end()

    def on_provider_event(event: str, payload: dict[str, Any]) -> None:
        attributes: dict[str, Any] = {"provider": payload.get("provider", "")}
        if payload.get("model"):
            attributes["model"] = payload["model"]
        if payload.get("error"):
            attributes["error"] = payload["error"]
        usage = payload.get("usage")
        if usage is not None:
            attributes["input_tokens"] = usage.input_tokens
            attributes["output_tokens"] = usage.output_tokens
        trace.get_current_span().add_event(event, attributes)

    def on_cache_event(event: str, payload: dict[str, Any]) -> None:
        trace.get_current_span().add_event(event)

    runtime.on(ev.STAGE_STARTED, on_stage_started)
    runtime.on(ev.STAGE_COMPLETED, on_stage_completed)
    runtime.on("provider.*", on_provider_event)
    runtime.on("cache.*", on_cache_event)
    return runtime


def configure_otlp(
    *,
    endpoint: str,
    service_name: str = "byoai-runtime",
    headers: dict[str, str] | None = None,
) -> Any:
    """Create an SDK TracerProvider exporting OTLP to an existing collector
    (Grafana Tempo, Datadog agent, Honeycomb, Jaeger, ...). Returns the
    provider — pass it to :func:`instrument`."""
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "OTLP export requires the SDK + exporter: pip install 'byoai-runtime[otel]'"
        ) from exc
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, headers=headers))
    )
    return provider
