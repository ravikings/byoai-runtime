# Telemetry (OpenTelemetry)

Requires the `otel` extra: `pip install "byoai-runtime[otel]"`. Nothing in
`byoai.telemetry.otel` is imported unless telemetry is actually enabled, so the runtime has zero
overhead without it.

Zero-SaaS: traces export via OTLP to a collector you already run (Grafana Tempo, Datadog agent,
Honeycomb, Jaeger, ...) — nothing is sent to a third-party ByoAI service.

One span covers each execution (`byoai.execute`), with one child span per pipeline stage and
span events for provider lifecycle (`started`/`completed`/`failed` — retries and fallbacks show
up as multiple event pairs). Usage and cost land as span attributes, following the OTel GenAI
semantic conventions where they apply.

## Declarative setup

```python
runtime = Runtime(
    llm={"provider": "openai", "model": "gpt-4o"},
    telemetry={
        "endpoint": "http://otel-collector.internal:4317",
        "service_name": "my-app",
    },
)
```

Omit `endpoint` to attach to the process's globally configured OpenTelemetry SDK instead of
creating a new OTLP exporter:

```python
runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"}, telemetry={})
```

`protocol` is `"grpc"` (default, port 4317) or `"http"`/`"http/protobuf"` (port 4318) — useful
when a corporate ingress only allows the latter. `compression` is `"gzip"` or `None`.
`resource_attributes` adds to (not replaces) `service.name`, e.g.
`{"service.version": "1.2.0", "deployment.environment": "prod"}`. `Runtime.close()` shuts down a
tracer provider it created for you; a `tracer_provider` you pass in is yours to manage.

## Manual setup

```python
from byoai.telemetry.otel import instrument

instrument(runtime)                          # uses the globally configured SDK
instrument(runtime, tracer_provider=my_tp)   # or an explicit provider
```

`telemetry=` also accepts an already-built `TracerProvider` directly (instead of a config dict),
mirroring how `cache=` and `vector_store=` accept pre-built adapter instances.
