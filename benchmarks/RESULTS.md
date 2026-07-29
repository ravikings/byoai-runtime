# Day-one performance baseline

Measured 2026-07-29 on a 12-core Apple Silicon dev machine, Python 3.11.
Provider is a local mock (instant responses), so every number below measures
**ByoAI + transport overhead**, not LLM latency. Reproduce with the scripts in
this directory.

## Runtime core (in-process, `bench_runtime.py`, n=20k c=200)

| Configuration                              | Throughput   | mean    | p99     |
| ------------------------------------------ | ------------ | ------- | ------- |
| Bare runtime (pipeline + events)           | 86,856 req/s | 0.007ms | 0.014ms |
| With cache — all misses (fingerprint+store)| 55,599 req/s | 0.013ms | 0.038ms |
| With cache — all hits (short-circuit)      | 69,169 req/s | 0.009ms | 0.015ms |
| 5 middleware + 4 event subscriptions       | 39,111 req/s | 0.020ms | 0.038ms |
| Streaming (9 chunks/req)                   | 58,686 req/s | 528k chunks/s |

Per-request runtime overhead is **7–20 µs** — three to four orders of
magnitude below a real LLM call. The runtime is not the bottleneck.

## HTTP transports (ApacheBench, 10k requests, c=200, keep-alive, 1 process)

| Transport            | Throughput    | p50  | p95  | p99   | Failed |
| -------------------- | ------------- | ---- | ---- | ----- | ------ |
| FastAPI (uvicorn)    |  4,312 req/s  | 31ms | 68ms | 241ms | 0      |
| **Robyn**            | **15,731 req/s** | 12ms | 14ms | 21ms  | 0      |

Robyn is ~3.6× faster with far tighter tails at the same concurrency —
matching the architecture doc's recommendation of Robyn as the primary HTTP
runtime. Both endpoints served the full pipeline (context resolution, cache,
provider routing) per request.

## Queue workers (in-process, MemoryJobQueue, 20k jobs)

| Concurrency | Throughput     |
| ----------- | -------------- |
| 16          | 15,127 jobs/s  |
| 64          | 15,448 jobs/s  |

All jobs acked, zero failures, zero drops on graceful shutdown.

## JSON codec: stdlib vs orjson (`bench_json.py`, added 2026-07-29)

The largest remaining Python slice on the request path is JSON encode/decode
(transport framing, cache fingerprinting, provider stream parsing). Swapping
in orjson (Rust) behind `byoai._json` — optional `perf` extra, transparent
stdlib fallback:

| Hot path                         | stdlib json  | orjson       | speedup |
| -------------------------------- | ------------ | ------------ | ------- |
| Cache fingerprint                | 443k ops/s   | 1,130k ops/s | 2.6×    |
| dumps small stream frame         | 1.22M ops/s  | 7.24M ops/s  | 6.0×    |
| dumps result dict                | 391k ops/s   | 2.02M ops/s  | 5.2×    |
| loads result dict                | 662k ops/s   | 2.22M ops/s  | 3.4×    |
| Provider SSE line parse          | 1.29M ops/s  | 4.54M ops/s  | 3.5×    |
| **sse_stream end-to-end frames** | 496k/s       | 750k/s       | +51%    |
| **ws_reply end-to-end frames**   | 478k/s       | 744k/s       | +56%    |

Cache-miss execute (fingerprint on the hot path) rose 55.6k → 60.8k req/s.
A/B: `BYOAI_JSON=std` forces the stdlib backend.

## Semantic (intent) cache (`bench_semantic.py`, added 2026-07-29)

Exact-match caching only serves identical requests; the semantic cache embeds
the query and serves any *similar-intent* query from cache — no LLM call.

| Lookup vs cache size (768-dim)   | Latency          |
| -------------------------------- | ---------------- |
| 1,000 entries                    | 37 µs/lookup     |
| 10,000 entries                   | 435 µs/lookup    |
| 100,000 entries                  | 5.5 ms/lookup    |

End-to-end with a simulated 800ms LLM and 15ms embedding API:

| Path                              | Latency  |
| --------------------------------- | -------- |
| Cold (embed + LLM call)           | 824 ms   |
| **Intent hit (embed + lookup)**   | **16 ms** (~50× faster) |

The dominant cost of an intent hit is the embedding API round-trip, not the
lookup. Brute-force exact cosine (numpy) is deliberate — no ANN index to
build or tune — and stays sub-ms through ~30k entries; swap in an ANN-backed
store via the `byoai.semantic_caches` entry-point group beyond that.

## Method notes / honesty

- The pure-Python `loadtest.py` generator saturates around ~750 req/s per
  process — fine for correctness-under-load smoke tests, but use `ab`/`wrk`
  for throughput numbers (that ceiling is the *generator*, not the server).
- HTTP numbers above are dominated by cache hits (same query repeated), which
  is the honest way to isolate transport+runtime capacity; with unique
  queries, throughput is bounded by whatever your provider can sustain.
- Single process each. Robyn and uvicorn both scale further with workers.
