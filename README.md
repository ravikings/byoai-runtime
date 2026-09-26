# ByoAI Runtime (`byoai-runtime`)

> **Bring Your Own Infrastructure (BYOI). ByoAI Brings the Runtime.**

[![CI](https://github.com/ravikings/byoai-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/ravikings/byoai-runtime/actions/workflows/ci.yml)
[![PyPI version](https://badge.fury.io/py/byoai-runtime.svg)](https://badge.fury.io/py/byoai-runtime)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/downloads/)
[![OpenTelemetry](https://img.shields.io/badge/Observability-OpenTelemetry-purple.svg)](https://opentelemetry.io/)

ByoAI Runtime is an infrastructure-agnostic AI agent engine and workflow execution layer for Python. It connects directly to your existing Redis clusters, vector databases (pgvector, Pinecone, Qdrant), LLMs, and telemetry pipelines **without requiring data migrations, vector re-indexing, database schema alterations, or vendor lock-in.**

---

## 🖥️ Console (web UI) — in development

`web/` holds the ByoAI Console: a read-only operator UI over the agent
recorder's shipped evidence. It answers the question a single ledger file
structurally cannot — *which of my devices have reported, and what is
missing* — across a whole fleet.

**Status: reads real evidence; nothing ships into it yet.** The console calls
`/v1/console/*`, a read-only surface over `byoai.ingest` mounted into the
proxy, so what you see is what devices actually shipped. Point it at a store
with `BYOAI_INGEST_DB` (default `~/.byoai/ingest.db`).

What is still missing is the other direction: there is no `/v1/ingest/batch`
endpoint, so a recorder cannot yet ship into this store — it has to be
populated by `byoai.ingest.IngestStore` directly. And no verify walk runs on
this side, so every device reads as `unverified` rather than `intact`:
absence of a failed check is not a pass.

Some fields are reported as unknown rather than zero, on purpose. Backlog,
pending checkpoints and oldest-unshipped describe what a device is still
holding; the ingest side sees what arrived and cannot see what is queued, and
a device that stopped shipping looks identical to one with nothing left to
send. Zero there would claim "nothing outstanding" from data nobody has.

The front end runs against an MSW mock only in `npm run dev`; the built
console served by the proxy always talks to the real API.

### Reaching it after a pip install

The built console ships inside the wheel, so there is no Node toolchain to
install at your end:

```bash
pip install --pre "byoai-runtime[agent-context-cache]"
byoai-cache console      # starts the proxy in the background, prints the URL
#   → http://localhost:8787/console/
```

`console` is `start` plus the console URL — same single process, and
`byoai-cache stop` stops it. Any of `byoai-cache`, `byoai-cache start` or
`byoai-cache serve` serves `/console/` just as well. The console sits behind
the same `BYOAI_PROXY_TOKEN` gate as the rest of the API: when the API needs
the token, so does the UI. Set `BYOAI_CONSOLE=0` to leave it off entirely.

### From a source checkout

A git checkout does **not** contain the built console — `dist` output is never
committed. Build it once before `/console/` will serve anything:

```bash
npm --prefix web install && npm --prefix web run build
```

That writes into `src/byoai/console_static/`, which the proxy serves directly
and which the wheel picks up. Until you run it, `/console/` answers `503` with
that command in the body rather than a bare 404 — a missing build is a setup
state, not an error.

### Front-end development

```bash
cd web
npm install
npm run dev      # http://localhost:5174/console/ (Shield: http://localhost:5174/shield)
```

`npm run dev` proxies `/v1` to a running context-cache proxy
(`BYOAI_PROXY_URL`, default `http://127.0.0.1:8787`), so no CORS
configuration is needed on the Python side. See
[CONFIGURATION.md](./CONFIGURATION.md#console-web-ui) for the environment
variables.

---

## 📥 Ingest read model (`byoai.ingest`) — in development

The recorder ships signed batches outward. `byoai.ingest.IngestStore` is the
side that reads them back: a tenant-scoped store of accepted evidence, plus the
enrolment records needed to notice evidence that **never arrived**.

```python
from byoai.ingest import IngestStore, Enrolment

store = IngestStore("ingest.db")
store.record_enrolment(Enrolment(device_id, "acme", public_key_b64, enrolled_at))
store.accept_batch(device_id, entries)      # dedupes; refuses unknown devices
report = store.coverage("acme")             # what this tenant cannot account for
```

**Status: no HTTP layer yet.** The store assumes its caller has already
authenticated the device and verified the batch signature — it holds evidence,
it does not decide whether to trust it. Authentication, signature verification
and enrolment authorization belong to the service that will sit above it.

`coverage()` is the point of the package. It returns `never_seen` (enrolled,
never shipped), `contact_without_evidence` (talking, but producing nothing),
`reporting`, `devices_without_checkpoint`, `seq_gaps`, and a `blind_spot`
naming the limit of its own claim — all computed against enrolments, not
against whoever happened to ship. See
[CONFIGURATION.md](./CONFIGURATION.md#ingest-read-model).

---

## ⚡ Why ByoAI Runtime?

Most AI frameworks force engineering teams to adapt their database schemas, re-embed millions of vectors, and rewrite state management logic. **ByoAI Runtime adapts to your existing stack instead.**

* 🔌 **Zero Vector Re-indexing (Schema Mapping):** Connect directly to existing vector tables using declarative column mapping.
* 🌲 **Cross-Provider AST Filter Parser:** Pass unified JSON filters; ByoAI translates them on the fly into native target dialects (`pgvector` JSONB, Pinecone `$eq`, Qdrant payload filters).
* 🔒 **Non-Invasive Cache Isolation:** Isolates internal runtime keys (`byoai:*`) while using pattern-mapped readers to read existing chat histories safely.
* 🛡️ **Resilient Provider Routing & Fallbacks:** Native rate-limit management, retries, and dynamic model failovers (e.g., OpenAI ➔ Azure OpenAI ➔ Ollama).
* 📊 **Zero-SaaS Telemetry:** Native OpenTelemetry (OTLP) trace emission directly to your existing Grafana, Datadog, or Honeycomb collectors.

---

## 🏗️ Architecture & Execution Loop

ByoAI Runtime executes as an unopinionated, process-level orchestrator sitting above your existing production data layers:

```
                          ┌──────────────────────────┐
                          │   runtime.execute()      │
                          └─────────────┬────────────┘
                                        │
           ┌────────────────────────────┼────────────────────────────┐
           ▼                            ▼                            ▼
┌──────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
│  Redis Key Reader    │    │  AST Filter Parser   │    │ Dynamic Model Router │
│ (App Chat Ingestion) │    │ (Schema Mapping DB)  │    │ (Failover / Retry)   │
└──────────┬───────────┘    └───────────┬──────────┘    └───────────┬──────────┘
           │                            │                            │
           ▼                            ▼                            ▼
   Existing Redis DB            Existing Vector DB           LLM APIs / Inference
 (No keys overwritten)       (No vector re-indexing)      (Existing API keys)
```

---

## 🚀 Quickstart

### 1. Installation

```bash
pip install --pre byoai-runtime
```

`--pre` is required while the package is in pre-release (`0.1.0a7`).

### 2. Hello world

```python
import asyncio
from byoai import Runtime

async def main():
    runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"})  # reads $OPENAI_API_KEY
    result = await runtime.execute("What are our enterprise SLA terms?")
    print(result.content, result.usage.total_tokens, result.cached)
    await runtime.close()

asyncio.run(main())
```

No cache, no vector store, no telemetry — just a provider. Everything else in this README is
opt-in: add `cache=`/`vector_store=`/`semantic_cache=`/`telemetry=` only for what you need, when
you need it. The next example shows all of them wired up against real infrastructure. See
[Getting Started](https://ravikings.github.io/byoai-runtime/getting-started/) for environment
variables, `system_prompt=`, and `async with Runtime(...)`.

### 3. Execution Example (Production Setup)

```python
from byoai import Runtime

# Connect to existing infrastructure without altering schemas or keys
runtime = Runtime(
    cache={
        "provider": "redis",
        "url": "redis://redis.internal:6379",
        "namespace": "byoai:",  # Isolates ByoAI state
        "session_reader": {
            "pattern": "app:users:{user_id}:chat_history", # Ingests existing history
            "format": "json"
        }
    },
    vector_store={
        "provider": "pgvector",
        "dsn": "postgresql://user:pass@localhost:5432/production_db",
        "table": "document_embeddings",
        "schema_map": {
            "id": "doc_id",
            "embedding": "embedding_v2",
            "content": "raw_text",
            "metadata": "payload_json"
        }
    },
    llm={
        "provider": "openai",
        "model": "gpt-4o",
        "fallback": {
            "provider": "azure_openai",
            "endpoint": "https://prod.openai.azure.com",
            "deployment": "gpt-4-prod",
        }
    },
    telemetry={
        "provider": "opentelemetry",
        "endpoint": "http://otel-collector.internal:4317"  # your existing collector
    },
)

# Execute through the runtime (async, from any async framework)
result = await runtime.execute(
    "What are our enterprise SLA terms?",
    user_id="usr_9912",
    filters={"department": {"$eq": "legal"}}  # Translated automatically to JSONB SQL
)

print(result.content, result.usage.total_tokens, result.cached)
```

### 4. Drop into an existing FastAPI app

```python
from fastapi import Depends, FastAPI
from byoai import Runtime
from byoai.integrations.fastapi import attach, get_runtime, stream_response

app = FastAPI()               # your existing app
attach(app, Runtime(llm={"provider": "openai", "model": "gpt-4o"}))

@app.post("/ask")
async def ask(body: dict, rt: Runtime = Depends(get_runtime)):
    result = await rt.execute(body["query"])
    return {"content": result.content, "usage": result.usage.__dict__}

@app.post("/ask/stream")      # Server-Sent Events token streaming
async def ask_stream(body: dict, rt: Runtime = Depends(get_runtime)):
    return stream_response(rt, body["query"])
```

See `examples/fastapi_app/` for a runnable app with events, caching, and fallback.

### 4b. MCP connector + capture shield (demo layer)

Run an unauthenticated MCP surface plus its behavioral-capture sidecar:

```bash
pip install 'byoai-runtime[mcp]' mitmproxy
python examples/mcp_capture/server.py --http          # MCP over :8800/mcp
python examples/mcp_capture/client.py                 # real session → ledger
npm --prefix web install && npm --prefix web run build   # once, from a checkout
byoai-shield                                          # Shield → :17831/shield (ledger: ~/.byoai/shield/captures.jsonl)
byoai-shield examples/mcp_capture/captures.jsonl      # or point it at another ledger
```

| Example | What it wires |
|---|---|
| `examples/mcp_server/` | ByoAI-over-MCP tool server (stdio or streamable HTTP). |
| `examples/mcp_capture/` | Real MCP session (`client.py`) against an echo-backed `server.py`: tool calls, cache hits, stream deltas, and client identity from the `initialize` handshake land in `captures.jsonl`. |
| `examples/desktop_proxy_capture.py` | Loads the packaged capture proxy (`byoai.integrations.shield_proxy`) against the example ledger: Claude Desktop chat sends and replies go through the same rules → redact → seal path (admin-consented CA + `NODE_EXTRA_CA_CERTS`). |
| `src/byoai/browser_extension/` | Chrome (MV3) extension that records agent chat sends in the browser (claude.ai, chatgpt.com, gemini.google.com, copilot.microsoft.com) into the same ledger. Load it unpacked from `chrome://extensions` → Developer mode → Load unpacked; it ships length-only facts to `POST /api/browser` on `127.0.0.1:17831` (endpoint configurable, enforced local-only, in the popup). The extension never reads message text — rule evaluation happens server-side, and what lands in the ledger is "a message was sent, N characters", sealed like every other row. Rows obey the per-app policy toggles, the queue survives service-worker restarts (session storage, 50-row cap), and retries are deduped by row id. Unpacked, it has a fixed extension ID (`jbbongpiablbbmfflcaafjbeejeododc`, set by the `key` in its manifest), so `BYOAI_SHIELD_EXTENSION_IDS` can be set once for every install; a Chrome Web Store copy gets the store's own ID, which the popup shows under Details. `python scripts/package_extension.py` builds the store zip. Nothing is recorded until you agree: installing opens a welcome page that says exactly what is noted, and capture stays off until you choose *Agree and turn on capture* (the popup's *Turn capture off* reverses it and clears anything waiting). It pairs with your Shield the first time it connects (Shield signs a fresh nonce with its device key; the extension pins the public key), and afterwards refuses to send anything to a different program listening on the same port. If Shield's sealed count is ever lower than the extension remembers, the popup warns and keeps warning (badge `!`) until you press Accept, even when the count later catches up. A Shield too old to answer the pairing check is retried every 30 seconds and recovers on its own once updated and restarted. Its popup shows reachability, whether it is talking to *your* Shield, when the last message on the current tab was noted, the seal chain height, the id to pin trust, and a direct link to the Shield UI (`/shield`), which is where the full experience lives (the extension itself has no timeline by design — the local server already has one). See its data-protection record in `src/byoai/browser_extension/PRIVACY.md`; real-browser end-to-end tests live in `web/tests/extension/` (they skip themselves unless a Chromium is available — see `CHROMIUM_PATH` in CONFIGURATION.md). |
| `examples/ui/keepalive.sh` | launchd-friendly supervisor for the three surfaces (MCP gateway, capture proxy, shield): runs them as a group and exits nonzero if any dies, so `com.coriqo.keepalive` (see the script header) restarts what's missing — survives crashes and reboots. |

### `byoai-shield` — packaged capture shield (source promotion)

The demo's analyzer, seal chain, and UI backend are promoted into the package
as `byoai.integrations.shield` with a console script (see `pyproject.toml`):

```bash
pip install 'byoai-runtime[mcp]'   # includes the mcp extra
byoai-shield ./examples/mcp_capture/captures.jsonl --host 127.0.0.1 --port 17831
byoai-shield --install-login-item   # start at login (macOS launchd, Linux systemd user unit, Windows Task Scheduler), low priority; --remove-login-item undoes it
```

API: `/api/feed` (limit/offset pagination), `/api/verify`, `/api/policy`
GET+POST (validated; a bad value is a 400 that names the field),
`/api/privacy` (what the ledger holds right now), `POST /api/privacy/scrub`,
and `/api/receipt/<seal>`, plus `POST /api/browser` for the Chrome
extension (bulk rows, length-only), driven by
recorder-core primitives: RFC 6962 MerkleTree, device Ed25519 keys in
`~/.byoai/shield/keys` (mode 0600 — see `byoai.recorder.keys`), and
`policy.json` verdict modes (`observe`/`redact`/`block`) + per-app toggles
that the capture proxy reads live.

### What protects the record, and what does not
Shield's files (ledger, seal log, state, policy, keys) are owner-only (`0600`
on Linux and macOS; on Windows the user-profile ACL) and written safely, with
each platform's own file lock. The seal chain is an append-only log
(`sealchain.log.jsonl`, one entry per line) plus a small state file
(`sealchain.json`). Nothing is trimmed, and every entry back to the first is
covered by a signed checkpoint, one per seal. A row is sealed before the
request that carried it returns, and sealing is idempotent, so a restart adds
nothing.

`/api/verify` reports a break when an entry no longer matches its hash, when
the signed checkpoint no longer matches the entries (so recomputing an entry's
hash does not hide an edit), when entries were cut off the end, or when the log
changes while Shield runs. A damaged log or state file is kept under a new
name, the intact part carries over, and the event is recorded as an incident
that stays in `/api/verify`. The browser extension keeps one number, the most
sealed entries Shield has reported, so a deleted or rolled-back record shows in
its popup.

What this cannot do: anything running as your user can still edit or delete
the files. That is detected in the cases above, not prevented, and a wipe of
everything, including the extension's profile, leaves no trace. Copies held off
the machine (enrol Shield in Coriqo, which receives signed checkpoints) are the
defence against a local wipe.

### Privacy-first by default

Shield polices what goes to AI apps, so it keeps **what happened, not what
was said**. With no settings changed:

* **`redact` is the default mode.** Emails, card numbers, phone numbers,
  SSN-like numbers, wallet addresses and API keys inside the message fields
  of the outgoing JSON (`prompt`, `messages[].content`) become labels such as
  `[redacted-email]` before the request leaves the Mac. `block` also stops
  credential and executable-file sends locally (the app gets a 403);
  `observe` only records.
* **No message text on disk.** Ledger rows and seal payloads hold the app,
  verdict, rule ids, message length and an HMAC-SHA256 fingerprint keyed by
  a per-device secret (`~/.byoai/shield/fingerprint.key`, mode 0600), so a
  short message can't be recovered by hashing guesses. Replies get the same
  treatment.
* **Opt-in previews.** `keep_text: true` stores up to 200 characters with
  personal details removed.
* **Retention.** `retention_days` (7 / 30 / 90 / 365, default 30): older
  ledger rows are deleted at startup and on cleanup. Sealed entries stay, so
  receipts keep verifying.
* **Notice.** `notice: true` shows a strip in the console telling whoever
  uses the Mac what Shield checks and keeps.
* **Cleanup.** `POST /api/privacy/scrub` (Settings → Privacy → Remove now)
  replaces text in rows written before these defaults with the fingerprint
  and applies retention. Existing sealed entries are not rewritten: that is
  the change the seal exists to detect.

**Which apps.** The proxy reads Claude (claude.ai, the Claude desktop app,
the Anthropic API) and ChatGPT (chatgpt.com, the OpenAI Chat Completions and
Responses APIs; off until turned on). Gemini and Copilot are listed but not
covered: their clients don't send JSON the proxy can read, so their toggles
can't be turned on and say so.

All of these are keys in `policy.json`, edited from Shield's Settings tab.

**Privacy-first is enforced, not just defaulted.** A `policy.json` from
before these defaults (no `policy_version`) is upgraded when `byoai-shield`
starts, and read that way by the proxy until then: `observe` becomes
`redact` (`block` stays, it is stricter), `keep_text` goes off, app toggles
are kept. Afterwards, any change that makes Shield less private than it is
now (switching to `observe`, turning `keep_text` on) must carry
`"acknowledge": "less_private"` or `POST /api/policy` refuses it with a 400;
the Settings screen asks for that in a confirmation dialog, and the Trust
page flags a weaker setup with a one-click "Restore defaults". The MCP
capture gateway (`examples/mcp_capture/server.py`) writes rows the same way.

### The Shield screen — `/shield` in `web/`

Shield has one UI: the `/shield` route of the React app in `web/`. It is the
screen for the person at one Mac, so it renders outside the admin console
(`/console/{tenant}/…` is the fleet view; `/console/{tenant}/shield` now
redirects here). It calls the shield API as `/shield-api/*`: Vite rewrites
that to `:17831/api/*` in dev, and `byoai-shield` answers it directly, so the
same build works both ways. `byoai-shield` serves the built app
(`byoai/console_static/`) and sends `/` to `/shield`; from a source checkout,
build it once with `npm --prefix web run build`.

The tab and the focused interaction are in the URL (`?tab=timeline&focus=…`).

* **Trust** — status card; today's counters (checked, caught, high risk,
  tool calls, in browser), each a filter; the Activity list with show / when
  filters, search and paging; "What Shield keeps on this Mac" in the rail,
  read from `/api/privacy` and the saved policy.
* **Ledger** — sealed entries with per-row receipts, export of the chain
  state, and **Check a receipt**: paste a receipt and the browser recomputes
  the seal and the Merkle path with no request (`web/src/lib/receipt.ts`,
  tested against a receipt the Python seal chain exported).
* **Timeline** — every record grouped by day; "Open in Timeline" from a row
  shows how that interaction became a record and highlights it.
* **Settings** — Privacy (previews, retention, notice, remove stored text),
  what happens before a message leaves, apps (with which are installed), the
  optional Coriqo connection (save, ship seal), and where each file lives.

Any row opens a detail drawer: what happened, which rules matched, the seal,
a receipt download and the record as stored.

The proxy (auth: admin) always enforces; the shield UI only configures — its
own consent flow is the CA install and the toggles themselves, so nothing can
block traffic it didn't see the user place it inline on. The consequence for
copy: Coriqo never asks the user to take its word for anything. The receipt
math (`byoai.receipt.v2`, sha256 + Merkle proof + device checkpoint) is what
settles it.

### Sending the Shield seal to Coriqo

Shield can send this Mac's seal to a Coriqo tenant, so it sits with the rest
of that tenant's AI evidence. What leaves the Mac is only the chain's signed
checkpoint (Merkle root, entry count, Ed25519 signature); no messages, no rule
matches, no ledger rows.

**Setup, once.** A Coriqo admin creates an enrolment token (the same kind
agent hosts use). In Shield's Settings → Coriqo, paste it with the Coriqo
address and press *Connect this Mac*. Shield enrols its existing device key
(`POST /v1/enroll`) and keeps nothing secret afterwards: every send is signed
by the Mac's key. (`POST /api/coriqo/enrol` does the same without the UI.)

**After that, nothing to do.** A background publisher
(`byoai.integrations.shield_publish`):

* sends only when the chain has grown, and at most every 6 hours, to Coriqo's
  existing device checkpoint route (`POST /v1/checkpoints/batch`, the one the
  agent recorder uses), plus *Send now* in Settings;
* is idempotent: each checkpoint has a stable id, so a resend after a crash is
  a duplicate on Coriqo's side, never a second row;
* backs off on failure (1 min, doubling, capped at 6 h, honouring
  `Retry-After`) and retries on its own; state survives restarts;
* never blocks, slows or stops checking.

The only state that needs a person is Coriqo refusing the Mac (HTTP 401/403,
e.g. the device was revoked): Settings says so and asks for a new token. One
401 is different: when Coriqo says the request is stale or dated in the
future, the Mac's clock is wrong. Settings then reads "This Mac's clock is
wrong. Fix the date and time, and Shield will send again on its own.", and
Shield keeps retrying with the usual backoff.
`GET /api/coriqo` reports the connection, last and next send, and any error.

**What each send tells Coriqo.** Besides the checkpoint, every request body
(signed as a whole by the Mac's key) carries:

* `sent_at`: when this request left, in UTC. Coriqo refuses one older than
  10 minutes or more than 5 minutes ahead, so a captured request can't be
  replayed later.
* `shield`: `{"protecting", "reasons", "mode"}`, whether Shield is checking
  traffic right now. It is protecting when the capture proxy is running, the
  Mac's system HTTPS proxy points at it (checked with `scutil --proxy`; if
  that can't be read, it isn't counted against), the policy redacts or blocks,
  and at least one app Shield can read is switched on. Otherwise `reasons`
  names what is missing: `capture_stopped`, `proxy_off`,
  `policy_monitor_only`, `no_apps_enabled`. The browser extension doesn't
  count towards protection: it records that a chat happened and never reads
  or changes what is sent. Coriqo alerts on a Mac that reports it is not
  protecting. Shield knows the proxy is running from
  `shield_proxy.alive`, a small file (pid and listen port) the proxy writes
  next to the ledger on start and removes on a clean stop. A file left by a
  crashed proxy is ignored.

A heartbeat changes only those two fields; its checkpoint entry is the one
Coriqo already holds, byte for byte. Each new entry also carries:

* `record_id`: a random id for this Mac's local record, stored in the seal
  file when it is first created and kept across restarts and the 512-entry
  window trim. A different id means the record was deleted and started again.
* `prev_chain_hash`: the chain hash of the last entry Coriqo accepted from
  this Mac (`null` for the first one after connecting).

With these Coriqo can tell a record that grew from one that was wiped or
skipped, and raises its record-contradicted alert on the latter. Older Shield
versions send none of these fields and are accepted as before.

### 5. Semantic (intent) caching

Serve *similar* questions from cache — not just identical ones. One embedding
call (~15ms) replaces the whole LLM round-trip when intent matches:

```python
runtime = Runtime(
    llm={"provider": "openai", "model": "gpt-4o"},
    cache={"provider": "redis", "url": "redis://redis.internal:6379"},  # exact match
    semantic_cache={"provider": "memory", "threshold": 0.92},           # intent match
    embedder={"provider": "openai", "model": "text-embedding-3-small"},
)

await runtime.execute("What are our enterprise SLA terms?")   # LLM call (~800ms)
await runtime.execute("Tell me about our enterprise SLAs")    # intent hit (~16ms)
```

Measured ~50× faster on intent hits; lookups stay sub-millisecond to ~30k
cached answers (`benchmarks/RESULTS.md`).

For production, back the intent cache with your existing Redis so hits are
**shared across every worker/replica and survive restarts**:

```python
semantic_cache={"provider": "redis", "url": "redis://redis.internal:6379",
                "threshold": 0.92, "capacity": 10_000, "ttl": 3600}
```

Entries live in one `byoai:`-namespaced Redis stream; each worker keeps a
local numpy mirror and syncs incrementally, so similarity math never leaves
the process. Redis Cluster and Sentinel are supported everywhere Redis is
(`"mode": "cluster"` or `"mode": "sentinel"` + `sentinels`/`service_name`).

---

## 🛠️ Core Capabilities

### 1. Zero-Migration Schema Mapping
No need to run migration scripts or duplicate tables. Define a `schema_map` during initialization to bridge ByoAI to your existing table structures:

```python
vector_config = {
    "provider": "pgvector",
    "dsn": "...",
    "table": "enterprise_knowledge",
    "schema_map": {
        "id": "uuid",
        "embedding": "vector_768",
        "content": "body_text",
        "metadata": "attributes_json"
    }
}
```

### 2. AST Filter Translation
Avoid provider-specific query lock-in. Pass standard logical filter expressions and ByoAI compiles them into native query dialects:

```
                      [ AST Filter Parser ]
                                │
       ┌────────────────────────┼────────────────────────┐
       ▼                        ▼                        ▼
pgvector (SQL / JSONB)    Pinecone (JSON Dict)     Qdrant (Payload Filter)
`attributes_json->>'dept'  `{"dept": {"$eq":      `FieldCondition(key="dept",
 = 'legal'`                "legal"}}`              match=MatchValue("legal"))`
```

### 3. Non-Invasive State Management
ByoAI writes operational artifacts (semantic cache, execution traces, intent plans) under its isolated key namespace while reading existing user sessions read-only:

```python
cache_config = {
    "provider": "redis",
    "url": "redis://localhost:6379",
    "namespace": "byoai:",  # All writes go to byoai:cache:*, byoai:planner:*
    "session_reader": {
        "pattern": "session:{user_id}:messages",
        "format": "json"
    }
}
```

---

## 📊 Framework Comparison

| Architectural Criteria | LangChain / LlamaIndex | LiteLLM / Portkey | **ByoAI Runtime** |
| :--- | :--- | :--- | :--- |
| **Primary Focus** | Framework Abstractions | API Gateway / Proxy | **Unopinionated Agent Engine** |
| **Schema Migration** | ❌ Required / Enforced | N/A | **✅ Zero-Migration (Schema Mapped)** |
| **Vector Re-indexing** | ❌ Required | N/A | **✅ Direct Query over Existing Vectors** |
| **AST Metadata Translator** | ❌ Provider-specific | N/A | **✅ Cross-Provider Dialect Translation** |
| **Existing Redis Reader** | ❌ Overwrites / Requires SDK | ❌ N/A | **✅ Read-only Key Pattern Mapping** |
| **Observability** | ⚠️ Pushes Proprietary SaaS | ✅ OTel Supported | **✅ OpenTelemetry Native (OTLP)** |
| **License** | MIT | MIT / Commercial | **Apache 2.0 (Enterprise Patent Shield)** |

---

## 📦 Supported Adapter Ecosystem

### Cache & Memory
* **Redis** (Standalone, Cluster, Sentinel)
* **Valkey**
* **In-Memory** (Dev/Testing)

### Vector Databases
* **PostgreSQL + pgvector**
* **Pinecone**
* **Qdrant**
* Anything else via a `byoai.vector_stores` plugin — see [Vector stores](docs/guides/vector-stores.md#custom-adapters-via-plugins).

### LLM Providers
* **OpenAI**
* **Anthropic** (direct API, AWS Bedrock, or Google Vertex AI)
* **Azure OpenAI**
* **Google Gemini**
* **Ollama / vLLM / LiteLLM**
* **OpenRouter** / Any OpenAI-compatible REST endpoint

### Observability
* **OpenTelemetry** (Datadog, Grafana, Honeycomb, Jaeger, New Relic) — gRPC or HTTP OTLP.

### Transports
One execution, five ways in — all share the same payload/result dialect:
* **FastAPI** — `byoai.integrations.fastapi` (HTTP, SSE, WebSocket)
* **Robyn** (Rust-powered) — `byoai.integrations.robyn` (HTTP, SSE, WebSocket)
* **MCP** — `byoai.integrations.mcp`: expose the runtime as a tool any MCP client (Claude Desktop, another agent) can call, over stdio or streamable HTTP — with a streaming tool variant (live token deltas as progress notifications)
* **Queue workers** — `byoai.workers`: `RuntimeWorker` + `RedisStreamQueue`/`MemoryJobQueue`
* Or embed `Runtime` directly in any async Python process

### Configuration
Every adapter's every setting — timeouts, retry classification, connection
pooling, TTLs, capacity bounds, batch sizes, and more — is documented in
**[CONFIGURATION.md](CONFIGURATION.md)**.

---

## 🧩 Agent Context Cache

A standalone proxy, separate from `Runtime`, that sits in front of the
Anthropic API. Point Claude Code (or any Anthropic API client) at it and it
injects prompt-cache breakpoints, truncates oversized tool output, and
collapses repeated large tool results within a request, cutting token spend
without any client-side changes.

```bash
pip install --pre "byoai-runtime[agent-context-cache]"
byoai-cache                                    # runs in the foreground
export ANTHROPIC_BASE_URL=http://localhost:8787
```

Prefer to keep it running without holding a terminal open? Start it detached:

```bash
byoai-cache start      # background; survives closing the terminal
byoai-cache console    # same, and prints http://localhost:8787/console/
byoai-cache status     # running (pid …) → http://localhost:8787
byoai-cache stop
```

`start` writes its pid and logs under `~/.byoai/` (`proxy.pid`, `proxy.log`).
Both `byoai-cache` and the longer `byoai-agent-context-cache` are the same
command. Override the bind address/port with `--host` / `--port` (or the
`BYOAI_HOST` / `BYOAI_PORT` env vars). It listens on `:8787` by default and
uses Redis for session/dedup state if `REDIS_URL` is set (falls back to an
in-process store otherwise). Full env var reference in
**[CONFIGURATION.md](CONFIGURATION.md#agent-context-cache--byoai-agent-context-cache)**.

### Reaching the proxy from a remote client (ngrok)

`localhost` only works for a client on the *same* machine. To route a remote
client — Claude's web/mobile apps, a phone, a cloud agent — through the proxy,
expose it with a tunnel such as [ngrok](https://ngrok.com):

```bash
BYOAI_PROXY_TOKEN=$(openssl rand -hex 16) byoai-cache start   # gate it first
ngrok http 8787                                               # public https URL
```

**Set `BYOAI_PROXY_TOKEN` before exposing the proxy.** Without it, a public URL
is an open relay to `api.anthropic.com` (and, if you configured the OpenAI-compat
backend, anyone could spend your `BYOAI_OPENAI_COMPAT_API_KEY`). With it set,
every request must carry the token, supplied either way:

- **Header** — `x-byoai-proxy-token: <token>` (for clients that allow custom headers).
- **URL path** — put the token in the base URL, for clients that only let you set
  one: `ANTHROPIC_BASE_URL=https://<id>.ngrok-free.app/<token>`. The leading
  `/<token>` segment is stripped before routing.

`/health` stays reachable without the token so a tunnel can probe liveness. For a
private, always-on setup between your own devices, [Tailscale](https://tailscale.com)
(point clients at the proxy host's tailnet IP) avoids a public endpoint entirely.

### Inspecting token-savings data

The proxy keeps a durable SQLite log of per-request usage and tokenizer-verified
benchmark samples at `BYOAI_SQLITE_PATH` (default `~/.byoai/byoai_runtime.db`).
The `/v1/stats`, `/v1/stats/benchmark`, `/v1/stats/permanent`, and
`/v1/stats/history` endpoints expose these numbers as JSON. To browse the raw
tables without writing SQL, open the file in
[`sqlite-web`](https://github.com/coleifer/sqlite-web), a small browser-based
SQLite viewer:

```bash
pip install sqlite-web
sqlite-web ~/.byoai/byoai_runtime.db   # opens a UI at http://localhost:8080
```

`sqlite-web` is an optional dev convenience, not a dependency of `byoai-runtime`.

### Keeping a long-running proxy small

Two things would otherwise grow with uptime, and both are now bounded.

The SQLite log is pruned to the last `BYOAI_RETENTION_DAYS` (default 90) once on
every start. A day of heavy Claude Code use adds a few thousand rows and under a
megabyte, so without a window the file reaches a few hundred MB a year and the
unfiltered
`SUM()` queries behind `/v1/stats/permanent` slow down as the table grows. To
prune a proxy that has been up for months without restarting it:

```bash
byoai-cache prune              # delete old rows, then reclaim the freed space
byoai-cache prune --days 30
byoai-cache prune --no-vacuum  # delete only; safe against a running proxy
```

Reclaiming space is skipped when fewer than 1,000 rows were deleted — rewriting
the whole file costs more than the space it returns. The command says so when it
skips.

The delete itself is safe to run against a live proxy: WAL mode keeps in-flight
readers unblocked. Reclaiming the freed space is not — `VACUUM` rewrites the
whole file under an exclusive lock, and a concurrent write from the running
proxy can fail with "database is locked". Either stop the proxy first, or pass
`--no-vacuum` and let the space be reused by future rows. Startup pruning never
vacuums for this reason.

### Tamper-evident recording (opt-in)

Turn on `BYOAI_RECORDER_ENABLED` and the proxy also seals every `tool_use` /
`tool_result` pair the agent exchanges with the model into a local
hash-chained ledger, signed in checkpoints by a device-held Ed25519 key. This
is off by default and separate from the caching/dedup behavior above — the
usage/benchmark stats keep working whether or not it's on.

```bash
pip install --pre "byoai-runtime[recorder]"
BYOAI_RECORDER_ENABLED=1 byoai-cache
coriqo-verify ~/.byoai/recorder/ledger.db   # check it offline, anytime
```

`coriqo-verify` re-derives every hash from the stored ledger rather than
trusting what's on disk, so it catches a tampered row, a deleted one, or a
forged checkpoint signature. It also flags a `tool_use` the agent sent that
never got a matching `tool_result` — and, the sharper case, a `tool_result`
with no `tool_use` behind it.

Capture so far assumes the proxy is in the path. A managed agent — an AWS
Bedrock Agent, say — offers no path to sit in: the caller invokes it and
AWS runs the entire orchestration loop inside the service, so no model request
ever comes past. `byoai.recorder.bedrock_agent` is the third seam for that
case, sealing the agent's own `InvokeAgent` trace instead of an interception.
Rationales, action-group calls, knowledge-base lookups, collaborator handoffs,
`returnControl` calls and guardrail interventions all become the same
hash-chained events, through the same extractor. Its limit is honest and worth
repeating: it sees exactly what the trace says, and a caller who invokes
without `enableTrace` leaves nothing behind — see
**[CONFIGURATION.md](CONFIGURATION.md#managed-agents-you-cannot-intercept-byoairecorderbedrock_agent)**.

The ledger is designed to also sync to a Coriqo instance rather than staying
local-only. **The client side of that is built; the server side does not exist
yet** — no released Coriqo serves the `/v1/enroll` and `/v1/ingest/batch`
endpoints `byoai-recorder-enroll` and the background shipper are written
against, so enrollment has nothing to talk to today. The wire contract is
exercised only against the mock server under `tests/recorder/`. Until a real
server ships, the recorder stays local-only — no loss for verification, since
`coriqo-verify` never needed the network. Details and the full env var
reference in
**[CONFIGURATION.md](CONFIGURATION.md#agent-recorder--tamper-evident-capture-byoairecorder)**.

What does work against Coriqo today is publishing runs to its agent API, which
is a different thing from copying the ledger: each agent is registered once and
each run becomes a governed trajectory plus one decision trace per sealed step,
so Coriqo holds the mandate an agent may act under and flags anything outside
it.

```python
from byoai.recorder.coriqo_agents import (
    AgentRegistration, CoriqoAgentsClient, CoriqoCredentials,
    ensure_registered, publish_session,
)
```

Only digests cross the wire, and they're the ledger's own — so a hash off a
Coriqo trace resolves to the sealed row behind it, and `coriqo-verify` still
checks the ledger offline. Setup, roles, and env vars in
**[CONFIGURATION.md](CONFIGURATION.md#publishing-runs-to-coriqos-agent-api-byoairecordercoriqo_agents)**;
`examples/agent_showcase/` is a working end-to-end wiring.

Two credentials can reach Coriqo from an agent host — the enrolled device key
and a static `BYOAI_CORIQO_API_KEY` — and `byoai.recorder.identity` is the one
place that picks between them:

```python
from byoai.recorder.identity import resolve_identity

identity = resolve_identity()          # None when neither is configured
if identity is not None and identity.enforcement_capable:
    signature = identity.sign(request_bytes)
```

The device key wins when both are present. The static key still publishes, but
it can't sign, so it can't be used for anything that decides what an agent is
allowed to do — a bearer secret that lives in the agent's own environment and
carries `governance:approve` is the agent holding the key to its own cage.
`identity.require_enforcement()` raises `EnforcementIdentityUnavailableError`
(a `ByoAIError`) naming the `byoai-recorder-enroll` command to run.

Enforcing a mandate is a different shape of call from publishing a finished
run: the runtime refreshes a cached policy snapshot on a background interval
while the agent is mid-turn. `byoai.recorder.coriqo_async` is the async,
retrying client for that path — device-signed on the enforcement endpoints, and
retrying reads only, since a resent trace or verdict is a second decision in a
record whose whole value is that it is accurate.

```python
from byoai.recorder.coriqo_async import AsyncCoriqoAgentsClient

async with AsyncCoriqoAgentsClient(resolve_identity()) as client:
    mandate = await client.fetch_mandate(agent_id)
```

Enrolling with `--tenant-slug` records the device's tenant in
`enrollment.json`, so signed enforcement requests carry `X-Tenant-Slug` from
the enrolled identity rather than needing the legacy
`BYOAI_CORIQO_TENANT_SLUG` in the agent's environment. Devices enrolled before
that field existed keep working on the env var, with one warning saying so.

The synchronous `CoriqoAgentsClient` is unchanged and stays the one to use for
publishing runs. Retry policy, the signed-request format, and which calls are
retryable are in
**[CONFIGURATION.md](CONFIGURATION.md#async-publishing-and-enforcement-byoairecordercoriqo_async)**.

`byoai.recorder.mandate` is what enforces the mandate in the agent's own
process. `MandateGate.decide()` answers *may this agent call this tool?* from
the cached snapshot — no network call on the hot path, so the agent does not
stop working while Coriqo is redeploying — and the snapshot refreshes on a
background interval inside the staleness budget Coriqo sets per agent.

```python
from byoai.recorder.mandate import Allow, mandate_gate

gate = mandate_gate(agent_id)
async with gate:
    if isinstance(gate.decide("send_payment"), Allow):
        ...
```

A verdict is `Allow`, `Flag` (allowed, and recorded as off-mandate — that is
what `mandate_enforcement: observe` is for) or `Deny`, which is terminal and
tells the model nothing it could route around. A stale snapshot allows and
flags under `fail_open` and denies under `fail_closed`; a suspended agent
denies under both. Without a Coriqo identity the gate is a no-op, so the code
path can go in before a device is enrolled. The full behavior table, the
`allowed_tools` null-vs-empty rule and the two env vars are in
**[CONFIGURATION.md](CONFIGURATION.md#enforcing-the-mandate-locally-byoairecordermandate)**.

`@governed_tool` is where that verdict stops something. It wraps your own tool
functions — sync or async, same decorator — consults the gate, and on a `Deny`
raises `MandateDeniedError` without entering the function:

```python
from byoai.recorder.governed_tool import governed_tool, set_default_gate

set_default_gate(gate)

@governed_tool(name="payments.send")
async def send_payment(iban: str, amount: str) -> str:
    return await bank.transfer(iban, amount)
```

`str(MandateDeniedError)` is the one fixed sentence and nothing else, because
that is the string a framework feeds back into the model; the tool, the mandate
version and the reason are on `exc.verdict` and in the logs. It is not
retryable and does not look it. The gate comes from `set_default_gate()` /
`use_gate()` (a `ContextVar`, so two agents in one process don't share a
mandate) or per-tool via `gate=`, and with no gate bound the decorator just runs
the function — see
**[CONFIGURATION.md](CONFIGURATION.md#governed_tool--enforcing-at-the-call-site-byoairecordergoverned_tool)**.

`@governed_tool` asks you to decorate your own tool functions, which is a source
change to code you may not own. The proxy is the second seam and needs nothing
from the agent: `BYOAI_PROXY_ENFORCEMENT=1` plus `BYOAI_MANDATE_AGENT_ID` and
every `tool_use` block in a model response is decided by the same gate before
the agent sees it. A denied block is withheld and replaced by a synthesized
`tool_result` carrying the same fixed sentence, so the agent's loop handles it
as an ordinary tool failure and never learns which tool it asked for. Streaming
holds back only the frames of the unfinished `tool_use` block; text frames are
never delayed. This covers tools the model requests through the intercepted API
and nothing else — a tool the agent calls directly in its own code is what the
decorator is for — see
**[CONFIGURATION.md](CONFIGURATION.md#enforcing-at-the-proxy-byoairecorderproxy_gate)**.

A denial stops one call, which is not enough on its own: an agent that ignores
the refusal can go at the same tool until its loop stops. The denial latch counts
those attempts per run and per tool, refuses every repeat straight from memory
without re-running the scope check, and at the third attempt halts the run —
subsequent calls raise `MandateRunHaltedError`, a subclass of
`MandateDeniedError`, so a supervising loop can tell "this tool is refused" from
"this run is over". The model reads the same fixed sentence throughout. Latch
state is per-process: a run that spans processes starts counting again.

When one agent hands work to another, the second agent has no mandate of its own
for that run. `delegated_gate(parent, child)` gives it the intersection of its
own mandate with the delegator's effective scope, pinned to the delegator's
`mandate_version_id`, so delegation can only narrow — spawning a sub-agent is
never a route to a tool the parent was refused. Both are documented in
**[CONFIGURATION.md](CONFIGURATION.md#the-denial-latch--repeats-and-the-halt-byoairecorderdenial_latch)**.

Every verdict — allowed, flagged and blocked alike — is written to the local
hash-chained ledger and shipped to Coriqo in batches of up to 200, sealed as one
governance event per batch.

```python
from byoai.recorder.verdicts import VerdictOutbox, VerdictRecorder, VerdictShipper
from byoai.recorder.verdicts import set_verdict_recorder

outbox = VerdictOutbox("~/.byoai/recorder/verdicts.db")
set_verdict_recorder(VerdictRecorder(ledger=ledger, outbox=outbox))
await VerdictShipper(client, outbox).drain()
```

The ledger is the record and Coriqo being unreachable does not change that;
shipping is downstream of the write, and `decide()` stays free of I/O. A latched
repeat and a halt are distinguishable from a first denial — `repeat_denied` and
`run_halted` rather than three identical rows — and captured tool arguments are
counted, never recorded or shipped, because nothing redacts them yet.
**[CONFIGURATION.md](CONFIGURATION.md#recording-verdicts-byoairecorderverdicts)**.

Set `BYOAI_RETENTION_DAYS=0` to keep every row.

Dedup itself no longer uses this state. It compares occurrences inside a single
request body and remembers nothing between calls, which is what makes a retried
request reach the API unchanged. The hash stores remain for other callers,
capped on two axes: 500 concurrent sessions and 5,000 content hashes per
session, oldest evicted first. Redis, when configured, holds the same state; the
in-process copy is kept as a complete mirror so an outage can't break requests.
Those two caps are what bound it — before them, a single long conversation could
accumulate hashes for its entire 8-hour lifetime.

---
# Contributing

ByoAI welcomes AI-assisted development as well as human contributions. See
[CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, checks, the AI-assisted-development policy,
and the PR process, and our [Code of Conduct](CODE_OF_CONDUCT.md). To report a vulnerability,
see [SECURITY.md](SECURITY.md) rather than opening a public issue.

---

## 📄 License & Enterprise Security

`byoai-runtime` is distributed under the **[Apache License 2.0](LICENSE)**.

* **Enterprise Safe:** Permissive license with explicit patent grants and trademark protections. Pre-approved for enterprise compliance scanners (Snyk, FOSSA, Mend).
* **Data Privacy:** Runs strictly in-process within your infrastructure. Zero data is transmitted to external servers beyond your configured model and database providers.

---

## 🌐 Community & Documentation

* **Documentation:** [ravikings.github.io/byoai-runtime](https://ravikings.github.io/byoai-runtime/)
* **GitHub Repository:** [github.com/ravikings/byoai-runtime](https://github.com/ravikings/byoai-runtime)
* **PyPI Package:** [pypi.org/project/byoai-runtime](https://pypi.org/project/byoai-runtime)
* **Changelog:** [CHANGELOG.md](CHANGELOG.md)
* **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md)
* **Security:** [SECURITY.md](SECURITY.md)