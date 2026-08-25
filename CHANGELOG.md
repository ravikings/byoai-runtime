# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(pre-1.0: minor versions may include breaking changes).

## [Unreleased]

### Changed
- **`@governed_tool(capture_arguments=…)` now defaults to `False`.** A governed
  tool's arguments routinely carry account numbers, customer identifiers and
  credentials, and nothing redacts them yet, so capturing them by default meant
  a package built to produce a defensible record was quietly collecting secrets.
  Opt in per tool once the arguments are known to be safe.
- **`Verdict.__repr__` and `ProposedAction.__repr__` no longer render
  `detail` / `arguments`.** The dataclass-generated reprs put the denied tool,
  its mandate and the call's arguments into any log line, traceback or f-string
  that formatted them — including one an integrator writes back into a model's
  context. `MandateDeniedError` already kept `detail` out of `str()`; this
  closes the same hole on the verdict object itself. Read `.detail` and
  `.arguments` explicitly when logging for an operator.

### Added
- **Denial latch (`byoai.recorder.denial_latch`).** A denial used to stop one
  call and nothing else, so an agent that ignored the refusal could attempt the
  same out-of-mandate tool for as long as its loop ran, and nothing counted it.
  The latch is keyed on `(run, principal, tool)`: the first denial is remembered,
  every later attempt at that tool is refused straight from the latch **without
  re-running the scope check** — the same tool against the same snapshot cannot
  answer differently — and at `BYOAI_MANDATE_HALT_THRESHOLD` attempts (default 3,
  counting the first denial) the run is halted. A halted run refuses every
  subsequent call, in-scope tools included, with `MandateRunHaltedError` (new, in
  `byoai.errors`, subclassing `MandateDeniedError`), so an existing handler still
  stops the call while `isinstance` / `exc.halted` tells a supervising loop "this
  tool is refused" from "this run is over"; `exc.attempts` and `exc.run_id` carry
  the finding. The model-facing string is the same fixed `MODEL_MESSAGE` across
  first denial, repeat and halt — escalating detail as an agent tries harder
  would hand it a hint sheet exactly when it is probing the control — while the
  count and the halt go to the operator `WARNING` and to `LatchedDenial.attempts`
  for the packet that will seal verdicts. The run is `ProposedAction.trajectory_id`,
  else the run bound by `run_scope()`, else a fallback id belonging to the
  `MandateGate`, so unrelated agents in one process never share a bucket and the
  latch is never silently off. Latch state is per-process and in memory: a run
  spanning processes, or a restarted host, starts counting from zero. Only scope
  denials are latched: a suspension, a stale snapshot and a missing one are all
  transient, and remembering them would turn one refresh blip into a permanently
  halted run. A new mandate version clears that agent's buckets, and its halt if
  it was that agent's doing, because that is the one thing that can change the
  answer; it is tracked per agent rather than per run so a delegator and its
  delegated child, which hold different versions, do not wipe each other's count.
- **Delegated scope attenuation (`byoai.recorder.delegation`).** When one agent
  hands work to another, the second has no mandate of its own for that run. Its
  effective scope is the intersection of its own mandate with the delegator's
  effective scope at the moment of delegation, pinned to the delegator's
  `mandate_version_id`, so delegation can only narrow and spawning a sub-agent is
  never a route to a tool the parent was refused. The child's standing mandate is
  untouched. `delegated_gate(parent, child)` returns a `DelegatedGate`, a
  `MandateGate` subclass that wraps the child's gate rather than replacing it —
  suspension, staleness and the fail-open/fail-closed fork still decide, and the
  delegated scope only narrows the result — so `@governed_tool` needs no separate
  wiring. `delegation_policy` must be `attenuated`; `none`, absent or
  unrecognised raises `DelegationRefusedError`, as does passing
  `max_delegation_depth` (where `0` forbids delegation and `null` leaves the
  policy as the only gate). An undeclared delegation gets the empty scope, which
  denies by rule rather than by accident. `intersect_tools` branches on `is None`
  and nothing else: unrestricted ∩ X is X, anything ∩ `[]` is `[]`. The delegated
  gate also keeps the delegator's gate and asks it first, so suspending or
  narrowing the delegator reaches the sub-agent it lent authority to within one
  refresh interval rather than never. `mandate_enforcement` and
  `max_delegation_depth` attenuate along with the tools — the delegator's dial
  decides whether a delegated breach blocks, any `enforce` up the chain enforces,
  and the depth limit is the tightest one anywhere up the chain. This applies
  to *delegation* between different agents, not to *nesting* — a sub-run of the
  same agent is already covered by that agent's one mandate.
- **`@governed_tool` (`byoai.recorder.governed_tool`).** The decorator integrators put on their own
  tool functions, and the seam where a mandate denial actually stops something: the gate is
  consulted first and on a `Deny` the wrapped function is never entered. One decorator handles sync
  and async tools, preserves `__name__`, `__doc__` and the signature via `functools.wraps` plus an
  explicit `__signature__`, defaults the tool name to `fn.__name__` and takes `name=` when the
  Python name and the approved name differ. A denial raises `MandateDeniedError` (new, in
  `byoai.errors`, deriving from `ByoAIError`), which is terminal and non-retryable by construction:
  `str(exc)` is the fixed `MODEL_MESSAGE` and nothing else — that string is what agent frameworks
  feed back into the model — while the tool, mandate version, staleness and reason live on
  `exc.verdict` / `exc.operator_detail` and in a `WARNING` logged on the raising path. `retryable`
  is `False` and there is no `retry_after`, so it cannot be mistaken for a transient provider error.
  A `Flag` still runs the function, which is what `mandate_enforcement: observe` is for. The gate
  is resolved per call from `@governed_tool(gate=…)` (a gate or a zero-argument factory) then from
  `set_default_gate()` / `use_gate()`, held in a `ContextVar` so two agents in one process do not
  share a mandate and tests do not leak bindings. With no gate bound, or a no-op gate from an
  unenrolled host, the decorator runs the function and logs one line.
- **Local mandate enforcement (`byoai.recorder.mandate`).** `MandateGate.decide()` answers whether
  a proposed tool call is inside the agent's approved scope from a cached snapshot, with no network
  I/O on the decide path — an agent whose availability depended on a governance service's is not
  one a bank ships. The snapshot refreshes on a background interval of half the per-agent
  `max_staleness_s`, handles Coriqo's `304` as "unchanged, still fresh", and survives a failed
  refresh with the cached snapshot intact, so a blip cannot become an outage. Verdicts are `Allow`,
  `Flag` (allowed and recorded, which is what `mandate_enforcement: observe` is for) and `Deny`,
  each carrying `reason`, `mandate_version_id` and `snapshot_age_s`; a `Deny` is terminal and its
  `model_message` is one fixed sentence, because a denial that explains itself is a hint sheet for
  routing around the control. `enforcement_posture` (`fail_open`/`fail_closed`) governs only the
  cases the gate cannot evaluate — a stale snapshot or none at all — while a suspended agent denies
  under both. `allowed_tools: null` (unrestricted) and `[]` (nothing permitted) stay distinct.
  New env vars `BYOAI_MANDATE_POSTURE` and `BYOAI_MANDATE_MAX_STALENESS_S` cover the window before
  the first snapshot lands. With no Coriqo identity configured the gate is a logged no-op; a static
  API-key identity is refused with `EnforcementIdentityUnavailableError`.
  `AsyncCoriqoAgentsClient.fetch_mandate_conditional()` is the conditional (`If-None-Match`) fetch
  behind it; `fetch_mandate()` is unchanged.
- **Enrollment records the Coriqo tenant.** `byoai-recorder-enroll` takes `--tenant-slug`, and a
  `tenant_slug` in the enrollment response takes precedence over it; either way it is persisted in
  `enrollment.json` and exposed as `CoriqoIdentity.tenant_slug`. An enrolled device can now set
  `X-Tenant-Slug` on signed enforcement requests from its own state, instead of also needing the
  legacy publish-only `BYOAI_CORIQO_TENANT_SLUG` in its environment.
  `AsyncCoriqoAgentsClient` resolves the tenant from an explicit `tenant_slug=`, then the identity,
  then `BYOAI_CORIQO_TENANT_SLUG`, and still refuses a signed call with none of them before it
  reaches the network. An `enrollment.json` written before this change loads unchanged, falls back
  to the env var, and logs one warning per process naming the re-enrollment command.
- **One Coriqo identity resolver (`byoai.recorder.identity`).** `resolve_identity()` returns the
  device-enrolled Ed25519 identity when this host has one, falls back to the static
  `BYOAI_CORIQO_API_KEY` credentials (publish-only, warned about once per process), and returns
  `None` when neither is configured. `CoriqoIdentity.require_enforcement()` is what mandate
  enforcement calls: it hands back a signer for a device identity and raises
  `EnforcementIdentityUnavailableError` for a static key, which cannot sign and, carrying
  `governance:approve`, would let an agent edit its own mandate. Key material stays inside
  `byoai.recorder.keys` — the identity holds a `Signer`, never raw bytes. New errors
  `CoriqoIdentityError` and `EnforcementIdentityUnavailableError` derive from `ByoAIError`.
  `CoriqoCredentials` and `CoriqoAgentsClient` are unchanged.
- **Async, retrying Coriqo client (`byoai.recorder.coriqo_async`).** `AsyncCoriqoAgentsClient`
  mirrors the synchronous client on `httpx.AsyncClient`, for the mandate-enforcement path: a
  runtime that refreshes a cached policy snapshot on a background interval can't block its event
  loop for a round trip, and shouldn't read one transient blip as a failed refresh. `RetryPolicy`
  retries 429/502/503/504 with exponential backoff and jitter and honours a server-sent
  `Retry-After` (and the `retry_after` on a `RateLimitError` from a transport hook) — but only for
  idempotent reads. Writes are sent once: a resent trace or verdict is a second decision in the
  record. The one opt-in exception, `RetryPolicy(retry_writes=True)`, covers `register_agent` with
  an `external_id`, which is Coriqo's own idempotency key.

  Enforcement calls (`fetch_mandate`, `record_verdict`, `record_signed_trace`) authenticate with
  the device key from `resolve_identity()` and never send `X-API-Key`, signing each attempt over
  canonical JSON of `{body_sha256, method, path, public_key, timestamp}` with the query string in
  the path and the signer's own key inside the signed bytes. A static-key identity raises
  `EnforcementIdentityUnavailableError` at the client. Errors keep the sync contract:
  `CoriqoAgentsError` with `status_code`/`detail`, `AgentSuspendedError` on 423.

  The synchronous `CoriqoAgentsClient` is unchanged.

- **`byoai.recorder.keys.load_device_key()`** — the load-only half of
  `load_or_create_device_key()`, returning `None` instead of minting a keypair when the directory
  has no key. It still reconciles an interrupted rotation first, so a confirmed staged key is
  promoted rather than reported absent.

### Changed
- `CoriqoAgentsError` now derives from `ByoAIError` as well as `RuntimeError`, so one
  `except ByoAIError` covers every runtime failure. Existing `except RuntimeError` code keeps
  working.

## [0.1.0a5] - 2026-08-14

### Added
- **Agent recorder (`byoai.recorder`, opt-in).** With `BYOAI_RECORDER_ENABLED=1`, `byoai-cache`
  seals every `tool_use`/`tool_result` pair the agent exchanges with the model into a local
  hash-chained SQLite ledger, signed in checkpoints by a device-held Ed25519 key. It tees bytes
  that have already been forwarded, so it never blocks or delays the token stream, and a recording
  failure is logged rather than failing the request unless `BYOAI_RECORDER_STRICT=1`. Ships behind
  its own extra: `pip install --pre "byoai-runtime[recorder]"`.

  New console scripts: `coriqo-verify` (offline verification — re-derives every hash from the
  stored ledger rather than trusting it, catching a tampered row, a deleted one, or a forged
  checkpoint signature), `byoai-recorder-enroll`, and `byoai-recorder-rotate-key` (rotates a
  device key with cross-signed continuity, so the handoff is itself sealed into the chain).

  `BYOAI_RECORDER_PAYLOAD_MODE` chooses what payload bytes reach the ledger — `hash-only`,
  `redacted` (the default; masks detected secrets and PII), or `full`. `payload_hash` always
  commits to the raw, unredacted payload regardless of mode, so redaction never weakens the
  evidence. Events carry `trace_id`/`span_id`/`parent_span_id` attribution, so a ledger holds
  enough lineage to reconstruct sub-agent trees and resumed sessions without changing the chain's
  flat, append-only shape.

- **Publishing runs to Coriqo's agent API (`byoai.recorder.coriqo_agents`).** Registers an agent
  with a Coriqo instance and publishes each recorded run as a trajectory plus one decision trace
  per sealed step, so Coriqo holds the agent registry, the mandate each agent may act under, and
  its own hash-chained trail of what the agent did.

  Two properties make what lands there evidence rather than telemetry: steps are read back out of
  the sealed ledger (nothing is published that wasn't recorded first), and each step's
  `args_hash`/`result_hash` are the ledger's own `payload_hash` values, with the row's
  `entry_hash` cited as an external grounding anchor. Both stores therefore commit to the same
  bytes — a hash off a Coriqo trace resolves to the sealed row behind it, and `coriqo-verify`
  still checks the ledger offline. Only digests cross the wire, never raw payloads.

  Nothing here runs automatically: session boundaries and agent identity are application concepts
  the recorder can't infer, so the caller drives it. Configured with `BYOAI_CORIQO_URL`,
  `BYOAI_CORIQO_API_KEY`, and `BYOAI_CORIQO_TENANT_SLUG`; unset means nothing is published.

- `Ledger.read_session()` — reconstructs one run from the ledger by `session_id`, replacing the
  hand-rolled SQLite query callers were otherwise writing.

- **`examples/agent_showcase`** — a runnable demo of nine banking and healthcare agents with a
  UI, live Anthropic/OpenAI calls (falling back to recorded transcripts when a key is absent),
  sealed replay, a tamper demo, and end-to-end Coriqo publishing. Serves on port 8001, since a
  local Coriqo API binds 8000.

### Fixed
- Recorder key revocation could be bypassed: stale-key detection gated on the device-controlled
  `ts_device` field, which the schema itself documents as untrusted, so a device could keep
  signing with a revoked key and backdate `ts_device` to slip past detection. Detection now keys
  off the `KEY_ROTATED` event's own trusted `seq`.
- `rotate_key()` promoted a staged key with two non-atomic `os.replace` calls, leaving a crash
  window with no live private key — after which key loading would silently generate a brand-new
  random orphan identity. Promotion now resolves deterministically to either the old or the new
  key after a crash at any point, never a third one.
- The `record_failure` marker inherited the `session_id` of whichever dropped event happened to
  be last in a batch, mislabelling earlier sessions' drops; it now uses a device-level sentinel.
- `byoai-recorder-enroll` crashed with a raw traceback when the on-disk device key was truncated
  or corrupt, instead of the clean `enrollment failed: …` exit — which is precisely the situation
  someone runs the command to recover from.
- Recorder robustness: `extract.py` no longer silently drops `tool_use` input when a delta arrives
  before its content-block start (it emits a parse-failure marker instead); the shipper no longer
  advances its watermark past server-rejected checkpoints; and Rekor audit-path reconstruction is
  iterative rather than recursive, so an adversarial `hashes` list can't exhaust the stack.

### Changed
- **Docs correction.** README and CONFIGURATION described the recorder's device ledger sync
  (`/v1/enroll`, `/v1/ingest/batch`) as an available feature. No Coriqo serves those endpoints —
  the client is built against the spec and exercised only against the mock server under
  `tests/recorder/`. Both documents now say so, and point at the agent API above as the
  integration that works today.

## [0.1.0a4] - 2026-08-10

### Fixed
- `SessionDedup` could replace an agent's task prompt with a dedup placeholder. It hashed every
  user-turn `text` block over 2,000 chars, but such a block is the *instruction*, not a file
  snapshot — a long prompt sent through the proxy came out the other side as
  `[byoai-runtime: Duplicate file snapshot detected (SHA: ...)]` and the model never saw the task.
  Dedup now considers `tool_result` blocks only and never rewrites user text.
- Dedup is no longer keyed on cross-request session state, which made it non-idempotent: the hash
  was recorded on first sight, so resending an identical body had the *second* copy collapsed. A
  client retrying a failed call therefore got a gutted request, and the failure looked like the
  environment was eating prompts. Occurrences are now compared within a single request body, so
  an identical resend produces an identical upstream request. This also makes the placeholder's
  claim true by construction — the surviving full copy is always present in the same request,
  where the old wording could point at content the model could no longer see.

  Consequence: `REDIS_URL` and `BYOAI_SESSION_TTL_SECONDS` no longer influence dedup. The hash
  stores are unchanged and still exported; dedup simply doesn't consult them.

## [0.1.0a3] - 2026-08-07

### Fixed
- Capped the core dependency at `httpx>=0.25,<1.0`. Installing an alpha requires `pip install
  --pre`, and `--pre` allows pre-releases for *every* package in the resolve, not just this one —
  so the uncapped requirement pulled `httpx 1.0.dev3`, whose API no longer has `httpx.Timeout`.
  A clean `pip install --pre "byoai-runtime[agent-context-cache]"` on 0.1.0a2 therefore installed
  a `byoai-cache` that raised `AttributeError` on import. Same hazard applies to any dependency
  that publishes a pre-release; this is the one that bit.

## [0.1.0a2] - 2026-08-07

### Fixed
- Docs told you to install the `byoai-cache` proxy with a bare `pip install byoai-runtime`, which
  produces a console script that fails on import: the proxy needs FastAPI, uvicorn, and redis, and
  the base package depends only on `httpx`. The correct install is
  `pip install --pre "byoai-runtime[agent-context-cache]"`, now used in the README, CONFIGURATION,
  and the getting-started extras list (which was also missing the `flask` and `agent-context-cache`
  extras entirely).

### Breaking
- Removed `Runtime(cache_ttl=...)`. TTL is now configured in exactly one place —
  `cache={"default_ttl": ...}`, or the matching arg on a pre-built `CacheStore`.
  `MemoryCache`/`RedisCache` now default `default_ttl` to `3600` (was `None`), so a cache
  built with no `default_ttl` no longer caches forever by accident.

### Added
- `BYOAI_RETENTION_DAYS` (default `90`) caps how long rows live in the proxy's durable SQLite
  log, applied once per proxy start. Previously the log was append-only with no ceiling, so the
  file and the unfiltered `SUM()` queries behind `/v1/stats/permanent` both grew with uptime.
  `0` restores the keep-forever behavior.
- `byoai-cache prune [--days N] [--no-vacuum]` applies the retention window on demand, for a
  proxy that has been running for months without a restart. The delete is safe against a live
  instance; reclaiming the freed space is not, so pass `--no-vacuum` (or stop the proxy) if one
  is running — `VACUUM` takes an exclusive lock that WAL mode does not let readers through.

- `docs/guides/testing.md` — testing a `Runtime`-based app without a real API key, Redis, or
  Postgres, using the existing in-process adapters (`providers=`/`vector_store=`/`embedder=`
  bare functions, `cache={"provider": "memory"}`, `MemoryJobQueue`).
- `Message` gained `tool_call_id`/`tool_calls`, completing the OpenAI-compatible tool-calling
  round trip (sending a tool result back). See `docs/guides/providers.md`.
- `MemorySemanticCache`/`RedisSemanticCache` and `PgVectorStore` gained `metric=`
  (`cosine`/`dot`/`euclidean` or `cosine`/`l2`/`inner_product` respectively, plus a custom
  callable for the semantic caches) instead of a hardcoded metric.
- `ProviderRouter`/`Runtime` gained `selection=` (`"ordered"` default, `"round_robin"`, or a
  custom callable) controlling which provider is tried first each call.
- `byoai.integrations.flask` (`attach`/`get_runtime`/`execute`/`stream_response`) — a Flask
  (WSGI) bridge to `Runtime`'s asyncio-native providers via a background event-loop thread.
  Requires the new `flask` extra; see `docs/guides/flask.md`.
- `Runtime.execute()`/`.stream()` gained `system_prompt=` (per-call override) and
  `provider_metadata=` (forwarded into the provider's own request payload, e.g. Anthropic's
  `metadata.user_id`).
- `Message.content` now accepts a list of provider content blocks, not just plain text;
  `ExecutionResult` gained `finish_reason`/`raw`. Anthropic/Bedrock/Vertex handle list-valued
  content today — see `docs/guides/providers.md`.
- `StreamChunk` gained `tool_call`, so a forced-`tool_choice` streaming call now actually
  yields content instead of nothing. Fixed across Anthropic, Bedrock/Vertex,
  OpenAI-compatible, and every transport (`transport.stream_frames()`, FastAPI/Flask SSE).
- `AnthropicProvider`/`AnthropicBedrockProvider`/`AnthropicVertexProvider` gained
  `cache_system=` for Anthropic's server-side prompt caching, and now parse
  `cache_read_input_tokens`/`cache_creation_input_tokens` into `Usage`.
- `providers=`/`vector_store=` now accept a bare async function directly — no class required —
  matching the existing `embedder=`/`Pipeline.add()` pattern.
- `AnthropicBedrockProvider`/`AnthropicVertexProvider` (`llm={"provider": "bedrock"/"vertex"}`)
  — Anthropic models via AWS Bedrock or Google Vertex AI. New `bedrock`/`vertex` extras.
- `PipelineNotFoundError`/`AllProvidersFailedError` — PEP 8-compliant names for the two
  exceptions that lacked the `Error` suffix (old names remain as aliases).
- `Runtime.aclose()` as an alias for `close()`.
- All library-owned HTTP clients send a `User-Agent: byoai-runtime/<version>` header.
- `default_params=` on `OpenAICompatProvider`/`build_openai_client` for query parameters
  applied to every request (how the Azure preset now passes `api-version`).
- Library logging on the `byoai.*` loggers (silent by default via `NullHandler`).
- CI now measures test coverage; the release workflow runs the full test matrix before
  publishing and pins the PyPI publish action to a commit SHA; builds are checked with
  `twine check --strict`.

### Changed
- `InMemoryHashStore` now caps hashes per session (`max_hashes_per_session`, default 5,000,
  oldest evicted first) in addition to the existing session cap, so peak memory is bounded by
  the two caps rather than growing with the length of any single conversation.
- `RedisHashStore` keeps mirroring every add into its in-memory fallback. Skipping the mirror
  while Redis is healthy would save memory, but it makes correctness depend on detecting
  recovery and replaying state between two stores; that was implemented, reviewed, and reverted
  after each edge case (partial failures, per-session vs. store-wide health, TTL expiry,
  concurrent writes mid-replay) turned out to be a way to silently report already-sent content
  as new. The mirror's cost is bounded by the `InMemoryHashStore` caps above.
- README's Quickstart now leads with a minimal "Hello world" example before the fuller
  "Production Setup" one.
- `build_cache`, `PgVectorStore`, `make_redis_client`, and the `azure_openai`/`pinecone`
  provider presets now raise `ConfigurationError` on invalid or missing config (a stale `url`
  left over from switching `provider` to `"memory"`, `min_size`/`max_size` pool kwargs colliding
  with the typed constructor args, `sentinels`/`service_name` given without `mode="sentinel"`,
  missing API keys) instead of silently ignoring it or failing later with an opaque error.
- `transport.parse_payload` raises `ConfigurationError` when a top-level field (e.g. `model`)
  collides with the same key inside `options`, instead of letting `options` silently win.
- `OpenAICompatEmbedder.embed_batch()` reorders responses by each item's own `"index"` field
  instead of trusting response order, fixing a silent vector/text mismatch against backends
  that return embeddings out of request order.
- README's vector-database list now only lists adapters that actually ship (pgvector,
  Pinecone, Qdrant), with a pointer to the `byoai.vector_stores` plugin mechanism for the rest.
- `Pipeline.remove()`/`.replace()` accept `name=` to target one stage among several of the same
  `stage_type`. Calling either with no matching criteria now raises `ConfigurationError`
  instead of silently matching nothing.
- Documented two known `RedisStreamQueue`/`RuntimeWorker` gaps in `docs/guides/workers.md`: no
  reclaim (`XCLAIM`/`XAUTOCLAIM`) for a crashed consumer's pending entries, and `RuntimeWorker`
  doesn't own or close the queue's connection.
- CI now gates on `pyright` (previously advisory); fixed ~60 pre-existing type errors this
  surfaced, mostly Optional-narrowing gaps in the Redis-backed adapters.
- `AnthropicProvider`/`GeminiProvider`'s construction-time API-key check now only backs off for
  an actual auth header in `default_headers=`, not any unrelated header.
- `PgVectorStore.search()` offloads embedding-literal construction to a thread above 1024
  dimensions, avoiding event-loop stalls on large vectors, and hoists invariant SQL parts out
  of the per-call query string.
- Version is now single-sourced from `src/byoai/_version.py` (`pyproject.toml` reads it
  dynamically), so `byoai.__version__` can no longer drift from the published package version.
- Robyn integration maps runtime errors to real HTTP statuses (`400`/`404`/`429`/`502`) instead
  of a blanket `422`.
- `AnthropicProvider`/`GeminiProvider` raise `ConfigurationError` at construction when neither
  an API key nor `default_headers` are supplied, instead of failing later with a 401.
- `Retry-After` headers in RFC 9110 HTTP-date form are now honored, not just delay-seconds form.
- pgvector filter compiler binds metadata field names as query parameters instead of splicing
  them into SQL text.
- OTel spans use GenAI semantic-convention attribute keys (`gen_ai.*`) — dashboards filtering on
  the old ad-hoc keys (`provider`, `model`, `input_tokens`) need updating.
- `MemorySemanticCache` runs its similarity math in a worker thread past 4096 entries.
- `ExecutionResult.context`/`RequestContext.documents` are precisely typed; `Runtime`'s
  `embedder=`/`semantic_cache=` are typed against their protocols.

### Fixed
- `ProviderRouter.complete()`/`.stream()` pass a shallow copy of `self.providers` to a custom
  `selection=` callable instead of the live list, so an in-place-mutating callable can no
  longer corrupt the router's provider order for every future call.
- `Runtime`'s non-cosine-metric threshold guard now reads the semantic cache's actually-resolved
  metric instead of re-deriving a `"cosine"` default, fixing a silent always-miss for
  plugin caches whose default metric isn't cosine.
- `RuntimeWorker._process()` no longer silently swallows an exception from
  `queue.push_result()`/`.ack()` raised after the runtime already answered; now logged and
  counted via the new `RuntimeWorker.errors`.
- Streaming adapters (OpenAI-compatible, Anthropic, Gemini) now raise `ProviderError` on an
  in-band `error` event mid-stream, instead of silently delivering a truncated generation as a
  clean success.
- FastAPI's `stream_response()` turns a mid-stream runtime error into a clean terminal SSE
  event instead of tearing the connection.
- Azure OpenAI preset passes `api-version` as a real query parameter (previously baked into
  `base_url`, where it broke httpx's path concatenation).
- `byoai.integrations.flask`'s bridge no longer hangs a request thread on shutdown or leaks
  provider connections behind in-flight/abandoned streams. `close()` now cancels pending
  futures, finalizes idle streams concurrently, and bounds total shutdown time to the
  requested `timeout` instead of stacking per-phase timeouts.
- `ContextResolver`'s session-history coercion no longer turns a tool-call-only assistant
  turn's `content: null` into the literal text `"None"`. That turn and its now-unanswerable
  `"tool"` replies are dropped, and adjacent same-role messages are merged at the resulting
  seam so truncation/dropping can't leave a provider-rejected same-role/wrong-starting-role
  history.
- Synchronous `execute()` now catches `concurrent.futures.CancelledError` and wraps it in
  `ByoAIError`, matching `stream_response()`'s existing behavior.
- `stream_response()`'s SSE loop no longer mislabels a genuine empty-message `ByoAIError` as
  `"request cancelled"`.

### Security
- `PgVectorStore` redacts a DSN's embedded password (`postgresql://user:pass@host` →
  `postgresql://***@host`) before a connection failure can leak it into an error message.

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

[Unreleased]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a5...HEAD
[0.1.0a5]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a4...v0.1.0a5
[0.1.0a4]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a3...v0.1.0a4
[0.1.0a3]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a2...v0.1.0a3
[0.1.0a2]: https://github.com/ravikings/byoai-runtime/compare/v0.1.0a1...v0.1.0a2
[0.1.0a1]: https://github.com/ravikings/byoai-runtime/releases/tag/v0.1.0a1
