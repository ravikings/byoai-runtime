# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor versions may include breaking changes).

## [Unreleased]

### Changed
- CI now gates on `pyright` (previously `continue-on-error`, tracked as a known gap). Fixed the
  ~60 pre-existing type errors this surfaced — mostly Optional-narrowing gaps around
  reassigned-parameter patterns in the Redis-backed adapters (`cache/redis.py`,
  `cache/semantic.py`, `workers.py`), `hasattr`-based cross-version duck typing in the
  FastAPI/MCP integrations' shutdown-hook wiring, and Optional-attribute access in test
  assertions against OpenTelemetry's SDK types. Along the way, `benchmarks/bench_json.py`'s
  cache-fingerprint benchmark was calling an instance method unbound and crashed if actually
  run — now constructs a real `CacheLookup` and measures the intended path.
- `AnthropicProvider`/`GeminiProvider`'s construction-time API-key fail-fast now only backs off
  for a caller-supplied auth header (this adapter's own, any capitalization, or a generic
  `Authorization`) in `default_headers=` — an unrelated header (e.g. a tracing header) no
  longer silently disables the check.

### Added
- `byoai.integrations.flask` (`attach`/`get_runtime`/`execute`/`stream_response`) — Flask's
  WSGI/sync model meets `Runtime`'s asyncio-native providers via a persistent background
  event-loop thread, so route handlers stay plain `def` with no `flask[async]`/asgiref
  dependency. Requires the new `flask` extra. See `docs/guides/flask.md` for the gunicorn
  `--preload` fork-safety constraint this bridge is subject to.
- `Runtime.execute()`/`.stream()` gained `system_prompt=` (per-call override of the
  constructor default — `""` clears it for that call, `None` falls back) and
  `provider_metadata=` (forwarded into the provider's own request payload, e.g. Anthropic's
  `metadata.user_id` — distinct from the existing app-level `metadata=`, which never reaches
  the provider).
- `Message.content` now accepts a list of provider content blocks in addition to plain text,
  and `ExecutionResult` gained `finish_reason`/`raw` fields, so Anthropic `tool_use` responses,
  the provider's own response id, and prompt-cache token counts are reachable instead of
  silently discarded. Only the Anthropic/Bedrock/Vertex adapters handle list-valued content
  correctly today — see `docs/guides/providers.md`'s new "Anthropic tool use and content
  blocks" section.
- `StreamChunk` gained `tool_call` (a new `ToolCallDelta`: `index`/`id`/`name` on a tool-use
  block's start, `partial_json` fragments on each following chunk). Previously `stream()` only
  handled `text_delta` events, so a forced-`tool_choice` streaming call — whose only content
  *is* tool-use JSON — silently produced nothing. Fixed in `AnthropicProvider` (SSE parsing)
  and `AnthropicBedrockProvider`/`AnthropicVertexProvider` (switched from the SDK's
  `stream.text_stream`, which has the same text-only blind spot, to iterating its raw events
  directly). `Runtime.stream()`'s final chunk and `ctx.raw_response` now also carry the
  provider's full raw response (id, complete content blocks) — previously only `execute()`
  exposed this, leaving a streaming caller nothing to hang audit logging or a tool call's final
  parsed arguments on. Every transport carries `tool_call` in its frames too — `transport.
  stream_frames()` (used by `sse_stream()`/`ws_reply()`, so Robyn/MCP/WebSocket inherit the fix)
  and the FastAPI and Flask integrations' own SSE loops (`stream_response()` in each — they
  don't route through `stream_frames()`) all filtered on `chunk.done or chunk.delta`, which is
  false for a chunk whose only content is a `tool_call` (`delta` defaults to `""`); all three
  now also check `chunk.tool_call is not None`.
- `OpenAICompatProvider.stream()` had the same `tool_call` gap — `delta.tool_calls` was dropped
  entirely, so a forced-`tool_choice` call yielded nothing there either — now fixed the same way,
  reusing `ToolCallDelta`. Streaming also never captured `finish_reason` at all (unlike
  `complete()`, which always had it); the final chunk's `raw` now assembles a full
  `choices[0].message` (accumulated content plus any tool calls) the same way the Anthropic
  fix above does. `GeminiProvider.stream()` had the narrower version of the same
  `finish_reason` gap and is fixed too.
- A handful of adjacent correctness fixes surfaced while building the above: `CacheLookup`'s
  exact-match fingerprint no longer includes `provider_metadata=` (it's audit-only correlation
  data, e.g. `user_id`, that never changes what answer comes back — including it meant any app
  tagging calls with a per-request id silently never got a cache hit); `OpenAICompatProvider`
  now strips `metadata=` before sending the request body, matching `GeminiProvider`, since
  several OpenAI-compatible backends this adapter fronts (Azure, vLLM, ...) reject unrecognized
  fields; a malformed/truncated `tool_use` JSON fragment now raises a `ProviderError` instead of
  silently becoming `input=None` in `raw`; `build_anthropic_system_field` no longer sends
  `system: []` (which Anthropic's API rejects) when every system message's content was an empty
  block list; a tool-only streamed turn (all `tool_call` chunks, no text) no longer writes an
  empty-string response into the semantic cache — `Runtime.stream()`'s accumulated `ctx.response`
  ends up `""` rather than `None` for a turn like that, and the write-back guard now excludes
  both, so a later semantically-similar query reaches the provider again instead of being served
  a cached blank answer; `AnthropicProvider.stream()`'s reconstructed `raw["content"]` now merges
  `thinking_delta`/`signature_delta` events too (extended-thinking blocks), not just
  `text_delta`/`input_json_delta` — a thinking block used to come back empty since only its
  `content_block_start` copy was kept and later deltas were dropped, corrupting the round-trip
  Anthropic requires when thinking and tool_use are interleaved (the reconstructed thinking
  content is internal to `raw` only, never surfaced via `StreamChunk.delta`, since it isn't the
  visible answer); and `require_text_content()` (Gemini/OpenAI-compat rejecting Anthropic-shaped
  list content) now raises `ProviderError` instead of `ConfigurationError` — the latter escaped
  `ProviderRouter`'s `except ProviderError` fallback handling entirely, so a message carrying
  prior-turn `tool_use`/`tool_result` blocks (this same PR's own feature) hard-failed a fallback
  chain instead of trying the next provider.
- `AnthropicProvider`/`AnthropicBedrockProvider`/`AnthropicVertexProvider` gained
  `cache_system=` (wraps a plain-string system prompt in a `cache_control: {"type":
  "ephemeral"}` block for Anthropic's server-side prompt caching) and now parse
  `cache_read_input_tokens`/`cache_creation_input_tokens` into the new
  `Usage.cache_read_tokens`/`.cache_creation_tokens` fields. Along the way, fixed a
  pre-existing bug shared by both adapters' system-prompt joining: a system message whose
  content was a list raised an uncaught `TypeError` instead of a clean error.
- `providers=` and `vector_store=` now accept a bare async function directly — no class required
  — auto-wrapped in `FunctionProvider`/`FunctionVectorStore`, matching the existing bare-callable
  pattern for `embedder=` and `Pipeline.add()`. See CONTRIBUTING.md's "bring your own function"
  design principle for which extension points get this treatment and which don't.
- `AnthropicBedrockProvider`/`AnthropicVertexProvider` (`llm={"provider": "bedrock"/"vertex", ...}`)
  — Anthropic models via AWS Bedrock or Google Vertex AI. Requires the new `bedrock`/`vertex`
  extras (depends on the `anthropic` SDK for AWS SigV4/GCP OAuth signing — the one deliberate
  exception to every other adapter's httpx-only, no-SDK design). Error classification reuses
  `raise_for_status()` against the SDK's own `httpx.Response`, so retry/fallback behaves
  identically to every other provider.
- `PipelineNotFoundError` and `AllProvidersFailedError` — PEP 8 names for the two exceptions
  that lacked the `Error` suffix. The old `PipelineNotFound`/`AllProvidersFailed` names remain
  as aliases, so existing `except` clauses keep working. `PipelineNotFoundError` also
  subclasses `LookupError`.
- `Runtime.aclose()` as an alias for `close()`, matching the async-native naming convention.
- All library-owned HTTP clients now send a `User-Agent: byoai-runtime/<version>` header
  (overridable via `default_headers`).
- `default_params=` on `OpenAICompatProvider`/`build_openai_client` — query parameters applied
  to every request (how the Azure preset now passes `api-version`).
- Library logging: cache write failures and event-handler exceptions are logged on the
  `byoai.*` loggers (a `NullHandler` is installed on the package logger; nothing is printed
  unless the application configures logging). `EventBus` without an `error_handler` logs
  swallowed subscriber exceptions at WARNING instead of discarding them.
- CI measures test coverage; the release workflow runs the full test matrix before publishing
  and pins the PyPI publish action to a commit SHA. Builds are checked with
  `twine check --strict`. A `.pre-commit-config.yaml` mirrors the ruff CI check locally.

### Changed
- The version is single-sourced from `src/byoai/_version.py`; `pyproject.toml` declares it
  `dynamic` and reads it at build time, so `byoai.__version__` can no longer drift from the
  published package version.
- The Robyn integration maps runtime errors to meaningful HTTP statuses instead of a blanket
  422: `400` malformed payload, `404` unknown pipeline, `429` provider rate limit (echoing
  `Retry-After`), `502` provider failure; other runtime errors keep `422`.
- `AnthropicProvider` and `GeminiProvider` raise `ConfigurationError` at construction when
  neither an API key (argument or environment) nor any `default_headers` are supplied, instead
  of sending a credential-less request and failing later with a provider-side 401. Passing
  `default_headers=` disables the check, for gateways that authenticate out-of-band or under a
  different header. When no key resolves, the credential header is omitted rather than sent
  blank.
- `Retry-After` headers in RFC 9110 HTTP-date form are now honored as backoff hints
  (previously only the delay-seconds form was).
- The pgvector filter compiler binds metadata field names as query parameters instead of
  splicing them into the SQL text. Generated clauses now read `meta->>$1 = $2` rather than
  `meta->>'field' = $1`; semantics are unchanged.
- OTel provider span events use GenAI semantic-convention attribute keys (`gen_ai.system`,
  `gen_ai.request.model`/`gen_ai.response.model`, `gen_ai.usage.*`, `error.message`), and
  `byoai.execute` spans carry `gen_ai.operation.name` and `gen_ai.request.model`. Dashboards
  filtering on the old ad-hoc keys (`provider`, `model`, `input_tokens`) need updating.
- `MemorySemanticCache` runs its similarity math in a worker thread once the cache exceeds
  4096 entries, so large lookups no longer stall the event loop.
- `ExecutionResult.context` and `RequestContext.documents` are now precisely typed
  (`RequestContext` / `list[Document]` instead of `Any`), and `Runtime`'s `embedder=`/
  `semantic_cache=` parameters are typed against the `Embedder`/`SemanticCacheStore`
  protocols.

### Fixed
- Streaming adapters (OpenAI-compatible, Anthropic, Gemini) now raise `ProviderError` on an
  in-band `error` event mid-stream. Previously the event was silently skipped and the
  truncated generation was delivered as a clean, successful completion.
- FastAPI's `stream_response()` turns a mid-stream runtime error into a final
  `data: {"error": ..., "done": true}` SSE event instead of tearing the connection, matching
  the Robyn integration and `transport.sse_stream`.
- The Azure OpenAI preset passes `api-version` as a real query parameter. It was previously
  baked into `base_url`, where httpx's path concatenation placed the request path after the
  query string.
- `byoai.integrations.flask`'s bridge no longer hangs a request thread forever on shutdown, and
  finalizes every in-flight/abandoned stream instead of leaking the provider connection behind
  it. `_FlaskBridge.run()`/`run_stream()` block on a future with no timeout; `close()` used to
  just stop the background event loop without cancelling any in-flight one, so a request thread
  mid-stream at the moment of a graceful shutdown (e.g. gunicorn's SIGTERM) waited on a future
  that would never resolve. `close()` now cancels every pending future first — `run_coroutine_
  threadsafe`'s returned future stays in `concurrent.futures`' `PENDING` state throughout, so
  `cancel()` succeeds synchronously and unblocks the waiting thread immediately regardless of
  what the underlying coroutine was doing — and `run()`'s "check closed, then register" and
  `close()`'s "mark closed, then snapshot pending" now share one lock, closing a TOCTOU window
  where a future scheduled right after `close()`'s snapshot could still hang forever once the
  loop's thread has exited. Every `run_stream()` session is now tracked for its whole lifetime,
  not just while it happens to have a live future, so `close()` also finds and finalizes a
  stream that's idle *between* chunks — the common case, since most of a real SSE stream's
  lifetime has no future in flight for `close()` to cancel at all — finalized exactly once
  regardless of whether `run_stream()`'s own cleanup or `close()`'s shutdown sweep gets there
  first; that cleanup is only bounded by a timeout when actually racing shutdown, so an ordinary
  exit (natural exhaustion, an early client disconnect) still lets a legitimately slow provider
  teardown run to completion instead of being cut off by a fixed cap. A cancelled in-flight
  future's own task cancellation already finalizes its generator as it unwinds, so `run_stream()`
  no longer *also* tries to close it a second time in that case — racing a brand-new close
  attempt against a task that may still be mid-unwind on the same generator raised `RuntimeError:
  aclose(): asynchronous generator is already running`, silently swallowed into a warning log.
  `close()`'s idle-stream sweep now finalizes every remaining stream concurrently (scheduling
  every `aclose()` up front, then waiting on them together) instead of one at a time — N streams
  each needing their own real time to close serialized into roughly N times that total instead
  of the single slowest one, and split whatever budget was left unevenly, starving out whichever
  stream went last. `close()`'s budget is now split rather than each phase getting its own full
  separate `timeout` (stacked, that previously multiplied `close(timeout=5.0)`'s worst case to
  roughly 3-4x the requested timeout — a process supervisor's SIGKILL grace period is typically
  sized for the requested timeout, so `close()` could get killed mid-shutdown before `runtime.
  aclose()` ever ran, leaking the provider's connections instead of the graceful close this
  exists to guarantee) — but also not a single deadline every phase shares (streams get whatever
  was left, however little): a starved `runtime.aclose()` doesn't just skip cleanly, the timeout
  firing cancels it *mid-teardown*, which can leave the provider's httpx client in a worse,
  half-torn-down state than either "closed" or "never touched." Streams get at most half the
  budget, `runtime.aclose()` a guaranteed floor of the other half — but not a *fixed* half
  regardless: if stream cleanup finishes early (the common case — most shutdowns have no active
  streams at all), `runtime.aclose()` gets that unused time back too, rather than a runtime
  teardown that legitimately needs more than a fixed half (but less than the full requested
  timeout) being prematurely cancelled for no reason. `close()` also no longer depends on
  `run_stream()`'s own (separate-thread) cleanup having already removed a cancelled stream from
  consideration by the time `close()` reads it for the idle-stream sweep — cancelling a
  `run_coroutine_threadsafe` future only *requests* the underlying task's cancellation
  asynchronously (it doesn't run inline), so that removal could lag behind `close()`'s read,
  letting the sweep schedule a second, racing `aclose()` on a generator whose cancellation was
  still unwinding through it — the exact `RuntimeError: aclose(): asynchronous generator is
  already running` class of bug two entries up, reintroduced by a different path. `close()` now
  excludes a cancelled stream from its own sweep synchronously, in the same step that cancels its
  future, rather than relying on that other thread to get there first. The final `thread.join()`
  now also draws from what's left of the overall requested timeout instead of getting its own
  fresh full budget on top — stacked with the streams/`runtime.aclose()` split, that pushed
  `close()`'s total worst case to roughly 1.5x the requested timeout, silently undermining the
  bound the split above exists to guarantee. A `run_stream()` call reaching cleanup after
  `close()` has already fully finished (loop stopped, thread joined) now fails immediately
  instead of blocking the calling thread for a full cleanup timeout waiting on a callback nothing
  will ever run; an *ordinary* (non-shutdown) cleanup's wait is no longer literally unbounded
  either, closing a narrow TOCTOU window (a concurrent `close()` stopping the loop between this
  method's liveness check and its scheduling call) that could otherwise hang a request thread
  forever instead of just very rarely, very slowly — bounded generously enough (minutes) that no
  realistic teardown is cut off early. A future cancelled for a reason unrelated to the bridge
  closing (not via `close()`) no longer gets misreported as "bridge is closed" — and
  `stream_response()`'s SSE loop now also catches that same raw (unrelated) cancellation (which
  its `ByoAIError`-only guard previously let escape unhandled instead of ending the stream with
  the same clean terminal error frame a runtime error gets), falling back to a "request
  cancelled" description since `str(CancelledError())` is always empty and an `{"error": "",
  "done": true}` frame told the client nothing. `run()` explicitly closes a caller's coroutine
  instead of dropping it unawaited when the bridge is already closed, avoiding a "coroutine was
  never awaited" warning at GC time.
- `ContextResolver`'s session-history coercion no longer turns `content: null` on an assistant
  turn (the standard shape for a tool-call-only turn in stored OpenAI/Anthropic history) into
  the literal text `"None"`. That turn — and the `"tool"` message(s) immediately following it,
  now-unanswerable tool_result replies to a turn that no longer exists — are dropped from history
  entirely; a still-considered `""` instead wasn't safe either, since Anthropic's API rejects any
  non-final message with empty content outright, and the tool call itself isn't representable in
  this coercion anyway, so a turn with no visible text has nothing worth keeping. (The detection
  itself is scoped to a dict that actually has a `content` key set to `null` — a dict with no
  `content` key at all, a malformed/legacy history entry, is a different, unrelated shape and must
  not also delete a well-formed `"tool"` reply that happens to follow it.)

  Dropping that turn — or `max_history_messages` truncation landing between an assistant turn and
  its tool replies, or between any two turns of an otherwise-unremarkable alternating conversation
  — can leave two consecutive same-role messages, or a truncated history that starts on
  `"assistant"`/`"tool"` instead of `"user"`. Every provider enforcing strict alternation (e.g.
  Anthropic, which also requires the first message to be role `"user"`) rejects either outright,
  the same class of whole-request failure this fix exists to prevent, just reached through
  different paths. Fixed on two fronts: truncated history now has *any* leading non-`"user"`
  message(s) dropped, not only a leading `"tool"` reply; and two same-role messages are merged
  into one (content concatenated; a differing `name` is dropped, `metadata` dicts merged) — but
  only at the exact seam left behind by an actual dropped turn, never just because two adjacent
  messages happen to share a role. That scoping matters on its own: merging *any* adjacent
  same-role pair in the final message list, regardless of why they were adjacent, would silently
  collapse a caller's own intentionally-distinct same-role turns (e.g. `ctx.input` supplied
  directly as a list of `Message`s, a documented, supported shape) into one, discarding per-turn
  content and attribution the caller never asked to have merged. Merging metadata dicts no longer
  crashes with `TypeError` when a `Message`'s `metadata` is explicitly `None` instead of the
  default empty dict (the field's declared type doesn't actually stop a caller from constructing
  one that way). Whether history's last message is eligible for the one cross-list merge seam
  (see above) is now computed as a direct byproduct of the same single pass that decides every
  other merge in the list, instead of a second, separately maintained scan over the raw history —
  the two had different ideas of which trailing items were "skippable" (only a `"tool"` reply vs.
  anything that doesn't coerce to a message at all), so a malformed trailing entry after a dropped
  turn could make the second scan miss a merge the first pass correctly still owed, reopening the
  same-role-messages failure this all exists to prevent. `_coerce_message`'s content=null drop
  also now applies to a tool-call-only turn supplied as an already-constructed `Message` object,
  not just the role/content dict shape — the `Message` branch previously returned any instance
  completely unchanged, so a caller reconstructing history as `Message` objects directly (rather
  than dicts) could still send an invalid `content: null` assistant turn straight to the provider.
- `close()`'s cancellation loop now checks `future.cancel()`'s own return value before marking
  its owning stream's handle done and excluding it from the idle-stream sweep, instead of doing
  so unconditionally. `cancel()` is a no-op (returns `False`) if the future already completed
  normally — e.g. the provider yielded its next chunk right as shutdown began — and marking the
  handle done regardless made `_finalize_stream` silently skip a stream that in fact still needed
  a real `aclose()`, leaking it and the provider connection behind it. Only a future actually
  interrupted by this cancel call is now excluded synchronously here; every other handle still
  reaches the idle-stream sweep, where it's properly finalized. `run_stream()`'s `finally` block
  also now reads `self._closed` under the same lock guarding every other access to it, rather than
  unlocked, before picking the finalization timeout. Its shutdown-racing cleanup call now also
  uses the timeout `close()` was actually invoked with instead of a hardcoded `5.0` — a tight
  `close(timeout=0.3)` (e.g. a short preStop grace period) no longer leaves a request thread's own
  cleanup free to block up to 5 seconds regardless of what the caller asked for, which risked a
  process supervisor's SIGKILL grace period (typically sized for the requested timeout) expiring
  mid-cleanup. `close()` also no longer relies on a single `_active_streams` snapshot taken some
  time after its `pending` snapshot: a stream that was idle (and thus invisible to the `pending`
  snapshot) at the instant `close()` began could still race into actively closing itself — e.g. an
  early client disconnect, or the WSGI layer finally pulling the next chunk — in the window between
  the two snapshots, landing in `run_stream()`'s own finally block and scheduling a *new*,
  not-yet-tracked cleanup future close() had no way to know about. `close()` now re-checks for any
  such newly-registered future once more right after its idle-stream sweep and waits for it too,
  instead of proceeding straight to stopping the loop and potentially abandoning that cleanup
  mid-flight.
- The synchronous `execute()` helper now catches `concurrent.futures.CancelledError` the same way
  `stream_response()` already does, wrapping it in `ByoAIError` instead of letting the raw
  `concurrent.futures` exception escape a Flask view's usual `except ByoAIError:` handling as an
  unhandled 500 — `close()` actively cancelling in-flight futures made this a live, not just
  theoretical, race.
- `stream_response()`'s SSE loop no longer mislabels a genuine (non-cancellation) `ByoAIError`
  that happens to carry no message (e.g. a bring-your-own-function provider raising `ByoAIError()`
  bare) as `"request cancelled"`. That fallback text — needed because `str(CancelledError())` is
  always empty — now only applies to an actual `concurrent.futures.CancelledError`; any other
  empty-message error falls back to its exception class name instead, so a client-side handler
  branching on the cancellation message doesn't get misled into thinking a request was cancelled
  when it wasn't.

## [0.1.0a1] - 2026-07-29

Initial alpha release.

### Added
- Core runtime: `Runtime`, `Pipeline`/`PipelineStage`, `Middleware`, request context, and the
  structured error hierarchy (`ByoAIError`, `ProviderError`, `RateLimitError`, `AllProvidersFailed`, etc.).
- Provider router with fallback/failover, `httpx`-based OpenAI-compatible, Anthropic, and Gemini
  providers, plus an `embedder=` adapter for any OpenAI-compatible `/embeddings` endpoint.
- Cache adapters: in-memory and Redis (standalone/cluster/Sentinel), with namespaced isolation
  from existing application keys.
- Semantic (intent) cache — serve similar, not just identical, queries from cache via embedding
  similarity, in-process (`MemorySemanticCache`) or shared across workers on Redis
  (`RedisSemanticCache`).
- Vector store adapters for pgvector, Qdrant, and Pinecone, including a cross-provider AST filter
  parser and a `VectorRetrieve` pipeline stage for retrieval-augmented generation.
- Plugin system: unrecognized `provider` values for `llm=`, `cache=`, `vector_store=`,
  `embedder=`, and `semantic_cache=` resolve through Python entry points.
- OpenTelemetry tracing (`byoai.telemetry.otel`): one span per execution, per-stage child spans,
  provider lifecycle span events, and OTLP export (gRPC or HTTP) to an existing collector.
- `orjson`-backed hot-path JSON codec (`perf` extra) for lower-overhead request/response encoding.
- FastAPI integration (`byoai.integrations.fastapi`): `attach`, `get_runtime`, SSE `stream_response`.
- Robyn integration, WebSocket transport, and background queue workers.
- MCP integration (`byoai.integrations.mcp`): expose a runtime as an MCP tool server (`execute` /
  `execute_stream` tools) over stdio or streamable HTTP, standalone or mounted into an existing
  FastAPI/Starlette app.
- `CONFIGURATION.md`: authoritative parameter-by-parameter reference across every component.
- Provider adapters gained `retryable_status=` overrides and gateway-friendly path overrides
  (`chat_path=`/`messages_path=`/`embeddings_path=`); the embeddings adapter transparently chunks
  large `embed_batch()` calls via `max_batch_size=`.
- `RuntimeWorker` gained a `shutdown_timeout=` for bounded graceful shutdown;
  `MemoryJobQueue`/`RedisStreamQueue` gained backpressure/trim controls (`maxsize=`, `maxlen=`).
- Benchmark suite for JSON encoding, runtime throughput, and multi-process Robyn scaling
  (72.5k req/s aggregate across 8 processes on the cache-hit path).

### Fixed
- `byoai.integrations.robyn`'s SSE stream route (`POST /byoai/stream`) crashed with
  `AttributeError: 'dict' object has no attribute 'set'` on every call — it passed a plain dict
  as `StreamingResponse(headers=...)`, but Robyn's default-SSE-header code calls `.set()` on it,
  which only Robyn's own `Headers` type supports. Wrapped in `Headers(...)`.
- A malformed (non-JSON) 200 response — e.g. a misconfigured gateway returning an HTML error
  page — leaked a raw `json.JSONDecodeError` past every provider adapter (`OpenAICompatProvider`,
  `AnthropicProvider`, `GeminiProvider`, `OpenAICompatEmbedder`) instead of a `ProviderError`,
  breaking the router's retry/fallback and error-typing contract. Added a shared
  `parse_json_response()` helper used by all four.
- `SemanticCacheLookup` only caught `ByoAIError` around the embedder/store call, so a
  user-supplied `embedder=` callable (explicitly a supported use case — any
  `async (str) -> list[float]`) raising a plain exception (`ConnectionError`, `TimeoutError`,
  etc.) failed the whole request instead of degrading to a cache miss, contradicting the
  documented "a semantic-cache or embedder hiccup must never fail a request" guarantee.
  Broadened to catch any exception.

[Unreleased]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/ravikings/byoai-runtime/releases/tag/v0.1.0a1
