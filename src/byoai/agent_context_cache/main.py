import os
import json
import time
import copy
import random
import asyncio
import hashlib
import httpx
from contextlib import asynccontextmanager
from pydantic import BaseModel
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, Response
import redis.asyncio as redis

from . import db, openai_compat
from ..stages import PromptCacheInjection, SessionDedup
from ..stages import _count_cache_control_markers as _stage_count_cache_control_markers
from ..session_hash import RedisHashStore

# Configuration
ANTHROPIC_UPSTREAM = "https://api.anthropic.com"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Route specific model names/prefixes to an OpenAI-spec-compatible backend
# (self-hosted vLLM/Ollama, Groq, OpenRouter, Together, etc.) instead of
# Anthropic. Client keeps sending Anthropic-shaped requests either way;
# openai_compat.py handles translating both directions. Configure via env
# so this doesn't require a code change per deployment:
#   BYOAI_OPENAI_COMPAT_MODELS="local-llama-3,my-vllm-model"
#   BYOAI_OPENAI_COMPAT_BASE_URL="http://localhost:8000/v1"
#   BYOAI_OPENAI_COMPAT_API_KEY="..."
OPENAI_COMPAT_MODELS = {
    m.strip() for m in os.getenv("BYOAI_OPENAI_COMPAT_MODELS", "").split(",") if m.strip()
}
OPENAI_COMPAT_BASE_URL = os.getenv("BYOAI_OPENAI_COMPAT_BASE_URL", "")
OPENAI_COMPAT_API_KEY = os.getenv("BYOAI_OPENAI_COMPAT_API_KEY", "")

# How long a conversation's dedup hash set survives with no new requests.
# There is no explicit "session end" signal in the Anthropic Messages API
# (it's a stateless HTTP endpoint), so we approximate "discard when the
# session ends" with an idle TTL: every time the session is touched, the
# TTL is refreshed; if nobody calls in with that session for this long,
# Redis reaps the key on its own.
SESSION_TTL_SECONDS = int(os.getenv("BYOAI_SESSION_TTL_SECONDS", str(8 * 60 * 60)))  # 8h default

# httpx defaults to a 5-SECOND total timeout when none is set. That's fine
# for a typical REST call but far too aggressive for LLM streaming
# responses, where gaps between SSE chunks (thinking time, tool execution,
# slow generation) routinely exceed 5s. Without an explicit override here,
# any such gap raises httpx.ReadTimeout mid-stream, and since nothing
# catches it, it crashes the whole ASGI response instead of failing
# gracefully. Read timeout is intentionally generous; connect/write stay
# tight since those really should be fast.
REQUEST_READ_TIMEOUT_SECONDS = float(os.getenv("BYOAI_READ_TIMEOUT_SECONDS", "600"))
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, write=300.0, pool=10.0, read=REQUEST_READ_TIMEOUT_SECONDS)

# Real-tokenizer benchmarking. estimate_tokens() (len(json)//4) is a rough
# heuristic — fine for a live console readout, not defensible as a public
# claim of "X% saved". Anthropic's own /v1/messages/count_tokens endpoint
# returns an authoritative token count from their real tokenizer, and does
# NOT generate a completion, so it isn't billed like a normal request.
# Calling it on the pre- and post-optimization payload for a sampled
# fraction of real traffic gives a number backed by Anthropic's own count,
# not our guess — that's the number worth publishing, not the estimate.
# Off by default: this adds two extra round trips per sampled request, so
# it must be deliberately enabled and rate-limited via sampling.
BENCHMARK_SAMPLE_RATE = float(os.getenv("BYOAI_BENCHMARK_SAMPLE_RATE", "0.1"))

# Only genuinely-retired model IDs should be remapped, and only onto
# models that are currently active. Do not remap models that already work.
MODEL_MAP = {
    "claude-3-5-haiku-20241022": "claude-haiku-4-5-20251001",
}

# Unsafe response headers that cause Starlette/client decoding errors
UNSAFE_RESPONSE_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "server",
    "keep-alive",
}

# Redis keys, centralized so the same string literal isn't repeated at
# every call site.
REDIS_KEY_ENABLED = "byoai:config:enabled"
REDIS_KEY_TOKENS_SAVED = "byoai:stats:tokens_saved"

JSON_MEDIA_TYPE = "application/json"

# Redis Connection Pool & in-memory backup (used only if Redis is unreachable)
r = redis.from_url(REDIS_URL, decode_responses=True)

# Fallback store when Redis is down: per-session {hash_set, last_touched_ts}.
# Bounded/pruned so it can't grow forever during an extended Redis outage.
_local_session_hashes: dict[str, dict] = {}
_LOCAL_SESSION_MAX = 500  # cap on distinct sessions kept in memory

# Runtime primitives are the single source of truth for request-side
# optimization on the live /v1/messages path. The legacy inline functions
# (ensure_cache_control / optimize_payload / is_duplicate_hash / add_hash)
# remain defined for the step-1 golden tests but are no longer called here.
# One module-level store + dedup stage so per-session dedup state persists
# across requests exactly like the legacy global did. The store resolves the
# module-level `r` lazily (via _LazyRedisClient) rather than capturing it at
# import time, so a test that monkeypatches `acc_main.r` — or a runtime redis
# reconnect — is honored on the live path, matching the legacy inline logic
# that referenced the global `r` on every call.
class _LazyRedisClient:
    def __getattr__(self, name):
        return getattr(r, name)


_session_hash_store = RedisHashStore(_LazyRedisClient(), ttl_seconds=SESSION_TTL_SECONDS)
_session_dedup_stage = SessionDedup(_session_hash_store)

# Shared, connection-pooled HTTP client reused across every request to
# Anthropic. Creating a fresh httpx.AsyncClient() per request (the prior
# behavior) meant a new TCP+TLS handshake to api.anthropic.com on every
# single call — tens of milliseconds of pure connection setup, wasted, on
# every request. One long-lived client with keep-alive avoids that: this
# is the single biggest real latency win available here (see note in
# conversation — everything else is dwarfed by network + model generation
# time regardless of language).
http_client: httpx.AsyncClient | None = None

# Fire-and-forget durable-record writes (db.record_usage_event,
# db.record_benchmark_sample) hold no other reference once scheduled, so
# asyncio would be free to garbage-collect them mid-write. Keeping them in
# this set until they finish prevents that.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def get_http_client() -> httpx.AsyncClient:
    """Return the shared client, set up by `lifespan` before any request is served."""
    assert http_client is not None, "http_client used before FastAPI lifespan startup ran"
    return http_client


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=50, max_connections=200),
        timeout=HTTP_TIMEOUT,
    )
    try:
        if not await r.exists(REDIS_KEY_ENABLED):
            await r.set(REDIS_KEY_ENABLED, "1")
    except Exception as e:
        print(f"[byoai-runtime Warning] Redis offline on startup ({e}). Operating in memory mode.")
    await db.init_db()
    print(f">>> byoai-runtime control plane initialized. Durable record: {db.DB_PATH}")
    yield
    await http_client.aclose()
    await r.close()


app = FastAPI(title="byoai-runtime Context Optimizer", lifespan=lifespan)


def estimate_tokens(data: dict) -> int:
    """Quick Token Estimator (~4 chars per token average)."""
    return len(json.dumps(data)) // 4


def clean_response_headers(headers: dict) -> dict:
    """Strips transfer/encoding headers so FastAPI/Starlette doesn't corrupt streams."""
    return {k: v for k, v in headers.items() if k.lower() not in UNSAFE_RESPONSE_HEADERS}


def log_usage(session_id: str, usage: dict | None):
    """
    Prints Anthropic's *actual* billed token accounting for a response,
    including prompt-cache hits/writes — this is the real cost signal,
    separate from the rough estimate_tokens() heuristic used elsewhere.
    """
    if not usage:
        return
    input_tok = usage.get("input_tokens", 0)
    output_tok = usage.get("output_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)

    total_context = input_tok + cache_read + cache_write
    cache_hit_pct = (cache_read / total_context * 100) if total_context > 0 else 0.0

    print(f"[byoai-runtime 💰 USAGE] session={session_id}")
    print(f" ├─ input={input_tok:,} output={output_tok:,}")
    print(f" ├─ cache_read={cache_read:,}  cache_write={cache_write:,}")
    if cache_read == 0 and cache_write == 0:
        print(" └─ No prompt caching detected on this call (no cache_control hit or the request doesn't use it).")
    else:
        print(f" └─ Cache hit rate: {cache_hit_pct:.1f}% of context served from cache (cheap) vs fresh (full price).")


# Anthropic allows at most 4 cache_control breakpoints per request. We
# leave headroom for whatever the client itself may have already set
# (e.g. on message content) rather than assuming all 4 are ours to use.
MAX_CACHE_CONTROL_BREAKPOINTS = 4
OUR_CACHE_CONTROL_BUDGET = 2  # system + tools, at most


def _count_cache_control_markers(node) -> int:
    """Recursively count existing cache_control occurrences anywhere in the body."""
    count = 0
    if isinstance(node, dict):
        if "cache_control" in node:
            count += 1
        for v in node.values():
            count += _count_cache_control_markers(v)
    elif isinstance(node, list):
        for item in node:
            count += _count_cache_control_markers(item)
    return count


def ensure_cache_control(body: dict) -> dict:
    """
    Inject cache_control breakpoints on the system prompt and tool
    definitions if the client didn't already set any.

    Why this matters: Anthropic's prompt cache is keyed purely on exact
    content match — it has no concept of "session" at all. Subagent calls
    routed through this proxy typically share the same fixed system
    prompt and tool schema across many otherwise-unrelated invocations.
    If those requests never carry a cache_control breakpoint, none of
    that shared prefix is ever cached, and every subagent call pays full
    price for it — which is exactly the cache_read=0, cache_write=0
    pattern seen in production logs. Adding a breakpoint here lets
    separate subagent calls that share the same system+tools prefix
    reuse each other's cache entry, even though this proxy tracks them
    as unrelated sessions for dedup purposes.

    Safe no-op if the client already set cache_control anywhere (we
    don't want to blow past Anthropic's 4-breakpoint limit or clash with
    intentional client-side cache strategy).
    """
    if _count_cache_control_markers(body) > 0:
        return body  # client already manages its own cache_control; don't interfere

    breakpoints_used = 0

    system = body.get("system")
    if system and breakpoints_used < OUR_CACHE_CONTROL_BUDGET:
        if isinstance(system, str):
            body["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
            breakpoints_used += 1
        elif isinstance(system, list) and system:
            system[-1] = {**system[-1], "cache_control": {"type": "ephemeral"}}
            breakpoints_used += 1

    tools = body.get("tools")
    if tools and isinstance(tools, list) and breakpoints_used < OUR_CACHE_CONTROL_BUDGET:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
        breakpoints_used += 1

    return body


def derive_session_id(request: Request, body: dict) -> str:
    """
    Scope dedup state to a single conversation.

    Preferred: an explicit client-supplied session/conversation id header,
    if the caller sends one (e.g. Claude Code or another agent harness that
    tracks its own conversation ids). Falls back to a stable hash of the
    caller's API key plus the first message in the array plus the system
    prompt — in practice, agentic frameworks append to conversation history
    turn over turn but don't rewrite the opening message, so this stays
    constant for the life of one conversation and changes for a new one.

    The API key is included deliberately: without it, the fallback hash is a
    pure function of request *content*, with no notion of which caller sent
    it. Two unrelated, concurrent conversations under different accounts that
    happen to start with a byte-identical system prompt and first message
    (common for subagent/Task-tool calls, which reuse a fixed boilerplate
    opening message) would otherwise collide onto the same session id — and
    since dedup state controls whether optimize_payload deletes content as
    "already seen", a collision means one conversation's genuinely-first-seen
    file content gets silently replaced with a stale-duplicate placeholder.
    Scoping by API key makes that cross-account collision impossible. The one
    remaining case — two terminals under the *same* key opening with
    byte-identical content within the same SESSION_TTL_SECONDS window — is a
    narrower, honest residual risk; callers who need a hard guarantee should
    send X-BYOAI-Session-Id explicitly.
    """
    explicit = request.headers.get("x-byoai-session-id") or request.headers.get("x-session-id")
    if explicit:
        return f"hdr:{explicit}"

    api_key = request.headers.get("x-api-key", "")
    messages = body.get("messages", [])
    system = body.get("system", "")
    seed = json.dumps(
        {"key": api_key, "system": system, "first": messages[0] if messages else None},
        sort_keys=True,
    )
    return "auto:" + hashlib.sha256(seed.encode()).hexdigest()


async def safe_redis_get(key: str, default: str = "0") -> str:
    try:
        val = await r.get(key)
        # decode_responses=True on the client makes this always str at
        # runtime; redis-py's stubs still type it as bytes | str | None.
        return str(val) if val is not None else default
    except Exception:
        return default


async def safe_redis_incrby(key: str, val: int):
    try:
        await r.incrby(key, val)
    except Exception:
        pass


async def real_token_count(count_token_headers: dict, body: dict | None) -> int | None:
    """
    Ask Anthropic's actual tokenizer for the real input token count of a
    request body, via /v1/messages/count_tokens. This does not generate a
    completion and is not billed as one — it exists specifically for this
    kind of pre-flight measurement. Returns None on any failure so a
    benchmarking call can never break or delay the real request it rides
    alongside; callers must treat None as "sample discarded", not zero.
    """
    if body is None:
        return None
    try:
        # /v1/messages/count_tokens has a STRICTER schema than /v1/messages —
        # it rejects unknown fields outright (confirmed via a real 400:
        # "metadata: Extra inputs are not permitted") rather than ignoring
        # them. Anthropic's docs confirm only these fields are accepted;
        # generation/sampling params (max_tokens, temperature, top_p, top_k)
        # and metadata/stream are NOT, so allow-list rather than exclude a
        # few — an exclude-list breaks again the next time the client sends
        # some other field this endpoint doesn't recognize.
        COUNT_TOKENS_ALLOWED_FIELDS = {"model", "messages", "system", "tools", "tool_choice", "thinking"}
        count_body = {k: v for k, v in body.items() if k in COUNT_TOKENS_ALLOWED_FIELDS}
        res = await get_http_client().post(
            f"{ANTHROPIC_UPSTREAM}/v1/messages/count_tokens",
            json=count_body,
            headers=count_token_headers,
            timeout=30.0,
        )
        if res.status_code != 200:
            print(f"[byoai-runtime 🔬 BENCHMARK ERROR] count_tokens returned {res.status_code}: {res.content[:500]!r}")
            return None
        return json.loads(res.content).get("input_tokens")
    except Exception as e:
        print(f"[byoai-runtime 🔬 BENCHMARK ERROR] count_tokens call raised {type(e).__name__}: {e}")
        return None


def _prune_local_sessions():
    if len(_local_session_hashes) <= _LOCAL_SESSION_MAX:
        return
    # Drop the oldest-touched sessions first.
    ordered = sorted(_local_session_hashes.items(), key=lambda kv: kv[1]["touched"])
    for sid, _ in ordered[: len(ordered) - _LOCAL_SESSION_MAX]:
        _local_session_hashes.pop(sid, None)


def _local_get_session(session_id: str) -> set:
    now = time.time()
    entry = _local_session_hashes.get(session_id)
    if entry is None or (now - entry["touched"]) > SESSION_TTL_SECONDS:
        entry = {"hashes": set(), "touched": now}
        _local_session_hashes[session_id] = entry
        _prune_local_sessions()
    entry["touched"] = now
    return entry["hashes"]


async def is_duplicate_hash(session_id: str, doc_hash: str) -> bool:
    key = f"byoai:hashes:{session_id}"
    try:
        is_member = bool(await r.sismember(key, doc_hash))
        # Refresh idle TTL on every touch so an active conversation never
        # loses its dedup state mid-stream, while a conversation that goes
        # quiet gets reaped automatically.
        await r.expire(key, SESSION_TTL_SECONDS)
        return is_member
    except Exception:
        return doc_hash in _local_get_session(session_id)


async def add_hash(session_id: str, doc_hash: str):
    _local_get_session(session_id).add(doc_hash)
    key = f"byoai:hashes:{session_id}"
    try:
        await r.sadd(key, doc_hash)
        await r.expire(key, SESSION_TTL_SECONDS)
    except Exception:
        pass


# Tools whose output is genuinely log-shaped (test runners, shell
# commands) where "keep only the error lines" is a reasonable lossy
# summary. Everything else — file reads, greps, edits, anything that
# returns source/document content — must never be classified by keyword
# guessing, since ordinary code very plausibly contains the substrings
# "ERROR", "Exception", etc. with zero relation to a test failure.
# Match is case-insensitive and case-insensitive-substring on tool name so
# this survives minor naming differences (e.g. "Bash", "bash_command").
NOISY_LOG_TOOLS = {"bash", "shell", "execute", "run", "runtests", "runcommand", "terminal"}


def _tool_name_is_noisy_log_source(tool_name: str) -> bool:
    name = (tool_name or "").lower()
    return any(marker in name for marker in NOISY_LOG_TOOLS)


def _build_tool_use_name_map(messages: list) -> dict:
    """tool_use_id -> tool name, so a tool_result can be traced back to
    the tool that produced it (the tool_result block itself only carries
    tool_use_id, not the tool's name)."""
    id_to_name = {}
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id")
                    if tid:
                        id_to_name[tid] = block.get("name", "")
    return id_to_name


def _truncate_generic(text: str, limit: int = 4000) -> str:
    """
    Safe fallback for non-log tool output that's still oversized: keep a
    head and tail slice rather than guessing at "errors" or discarding the
    middle blind. Never applies keyword reclassification — this is purely
    length-based, so it can't mislabel or gut ordinary file content the
    way the old logic could.
    """
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n[...byoai-runtime: {len(text) - limit:,} chars omitted from middle (non-log tool output, safe length cap only)...]\n\n{tail}"


def _truncate_log_like(text: str) -> str:
    """Only for known log/test-runner tool output: safe to keep just the
    error-shaped lines since that's the pattern this class of tool output
    actually follows (pytest, npm, git, CI logs)."""
    if len(text) <= 1200:
        return text
    lines = text.split("\n")
    errors = [l for l in lines if any(k in l for k in ["FAIL", "ERROR", "Traceback", "Exception"])]
    if errors:
        return "\n".join(errors[-25:]) + "\n\n[...byoai-runtime: Verbose test/log output pruned to error lines...]"
    return text[:600] + "\n\n[...byoai-runtime: Large log-like tool output truncated...]"


async def optimize_payload(body: dict, session_id: str) -> tuple[dict, int, int]:
    """
    Cleans tool output noise and deduplicates repeated file snapshots
    *within this same conversation only*.
    Returns: (optimized_body, orig_tokens, opt_tokens)
    """
    orig_tokens = estimate_tokens(body)
    messages = body.get("messages", [])
    tool_name_by_id = _build_tool_use_name_map(messages)

    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                # 1. Truncate Excessive Tool Outputs — but only apply the
                # error-keyword heuristic to tools we know produce
                # log-shaped output. Anything else gets a safe, purely
                # length-based head/tail truncation that can't misfire on
                # ordinary source code.
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content", "")
                    tool_name = tool_name_by_id.get(block.get("tool_use_id"), "")
                    is_log_source = _tool_name_is_noisy_log_source(tool_name)

                    if isinstance(content, str) and len(content) > 1200:
                        block["content"] = (
                            _truncate_log_like(content) if is_log_source else _truncate_generic(content)
                        )

                    elif isinstance(content, list):
                        for sub_block in content:
                            if isinstance(sub_block, dict) and sub_block.get("type") == "text":
                                sub_text = sub_block.get("text", "")
                                if len(sub_text) > 1200:
                                    sub_block["text"] = (
                                        _truncate_log_like(sub_text) if is_log_source else _truncate_generic(sub_text)
                                    )

                # 2. SHA-256 Deduplication for Repeated Large File Snapshots
                # Scoped to this conversation's session_id — never cross-
                # conversation, so we can't reference content the model in
                # THIS conversation never actually saw.
                elif isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if len(text) > 2000:
                        doc_hash = hashlib.sha256(text.encode()).hexdigest()
                        if await is_duplicate_hash(session_id, doc_hash):
                            block["text"] = f"[byoai-runtime: Duplicate file snapshot detected (SHA: {doc_hash[:8]}). Content retained in earlier turns.]"
                        else:
                            await add_hash(session_id, doc_hash)

    opt_tokens = estimate_tokens(body)
    return body, orig_tokens, opt_tokens


@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "byoai-runtime is operational."}


@app.api_route("/v1/messages/count_tokens", methods=["GET", "POST"])
async def proxy_count_tokens(request: Request):
    """
    Plain pass-through for Anthropic's token-counting endpoint. This has
    no optimizer/dedup logic — it's a lightweight pre-flight call clients
    make to estimate tokens before sending the real request, and if it
    404s, the client can end up retrying aggressively. Just relay it.

    Models routed to an OpenAI-compat backend (Ollama, vLLM, etc.) have no
    Anthropic tokenizer to call and may have no real Anthropic API key
    configured at all — passing through unconditionally would always
    401/404 for those. Answer with our own estimate_tokens() heuristic
    instead; it's the same estimate already used for the /v1/stats
    savings numbers, so it's consistent even if not tokenizer-exact.
    """
    body_bytes = await request.body()

    if body_bytes:
        try:
            raw_body = json.loads(body_bytes)
        except json.JSONDecodeError:
            raw_body = None
        if raw_body and raw_body.get("model") in OPENAI_COMPAT_MODELS:
            return Response(
                content=json.dumps({"input_tokens": estimate_tokens(raw_body)}),
                status_code=200,
                media_type=JSON_MEDIA_TYPE,
            )

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "accept-encoding", "connection")
    }
    upstream_url = f"{ANTHROPIC_UPSTREAM}/v1/messages/count_tokens"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    res = await get_http_client().request(
        request.method,
        upstream_url,
        content=body_bytes if body_bytes else None,
        headers=headers,
        timeout=30.0,
    )
    if res.status_code >= 400:
        print(f"[byoai-runtime ❌ UPSTREAM {res.status_code}] count_tokens")
        print(f" └─ body: {res.content[:500]!r}")
    return Response(
        content=res.content,
        status_code=res.status_code,
        headers=clean_response_headers(dict(res.headers)),
        )


async def proxy_openai_compat_request(request: Request, raw_body: dict) -> Response | StreamingResponse:
    """
    Handle a request whose model is configured to route to an OpenAI-spec
    backend instead of Anthropic. Client still sent (and expects back) the
    Anthropic Messages shape; openai_compat.py does the translation both
    ways. Anthropic-only concerns (MODEL_MAP retirement remap, cache_control
    injection) don't apply here — Anthropic's cache_control has no meaning
    once translated to an OpenAI request, and OpenAI-compatible backends
    have their own model catalog, not Anthropic's.
    """
    requested_model = raw_body.get("model", "")
    is_stream = raw_body.get("stream", False)
    session_id = derive_session_id(request, raw_body)

    if not OPENAI_COMPAT_BASE_URL:
        return Response(
            content=json.dumps({"error": {"type": "invalid_request_error",
                                           "message": f"Model '{requested_model}' is configured for OpenAI-compat "
                                                      f"routing but BYOAI_OPENAI_COMPAT_BASE_URL is not set."}}),
            status_code=500,
            media_type=JSON_MEDIA_TYPE,
        )

    print(f"[byoai-runtime 🌉 OPENAI-COMPAT] session={session_id} model={requested_model} -> {OPENAI_COMPAT_BASE_URL}")

    try:
        res = await openai_compat.forward_to_openai_compatible(
            http_client, OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY, raw_body
        )
    except httpx.HTTPError as e:
        print(f"[byoai-runtime ❌ OPENAI-COMPAT ERROR] session={session_id}: {e}")
        return Response(
            content=json.dumps({"error": {"type": "api_error", "message": f"Upstream OpenAI-compat backend unreachable: {e}"}}),
            status_code=502,
            media_type=JSON_MEDIA_TYPE,
        )

    if res.status_code >= 400:
        body_preview = res.content if not is_stream else b"<streamed>"
        print(f"[byoai-runtime ❌ UPSTREAM {res.status_code}] openai-compat session={session_id}")
        print(f" └─ body: {body_preview[:1000]!r}")
        return Response(content=res.content if not is_stream else b"", status_code=res.status_code,
                         media_type=JSON_MEDIA_TYPE)

    if is_stream:
        async def openai_line_iter():
            buffer = ""
            async for chunk in res.aiter_bytes():
                buffer += chunk.decode("utf-8", errors="ignore")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        yield line

        async def stream_translated():
            try:
                async for anthropic_chunk in openai_compat.translate_openai_stream_to_anthropic_sse(
                    openai_line_iter(), requested_model, raw_body.get("stop_sequences")
                ):
                    yield anthropic_chunk
            finally:
                await res.aclose()

        return StreamingResponse(stream_translated(), status_code=200, media_type="text/event-stream")
    else:
        try:
            openai_resp = json.loads(res.content)
        except json.JSONDecodeError:
            return Response(content=res.content, status_code=502, media_type=JSON_MEDIA_TYPE)
        anthropic_resp = openai_compat.openai_to_anthropic_response(openai_resp, requested_model, raw_body.get("stop_sequences"))
        log_usage(session_id, anthropic_resp.get("usage"))
        _fire_and_forget(db.record_usage_event(session_id, "openai_compat", requested_model, anthropic_resp.get("usage")))
        return Response(content=json.dumps(anthropic_resp), status_code=200, media_type=JSON_MEDIA_TYPE)


async def _apply_optimizer(
    raw_body: dict, session_id: str, is_enabled: bool
) -> tuple[dict, bool, dict | None]:
    """
    Run (or skip) the context optimizer for one request, and decide whether
    it's sampled for tokenizer-verified benchmarking.

    Returns (body, is_benchmark_sample, pre_optimize_snapshot). `body` is
    `raw_body` unchanged when the optimizer is off, or optimize_payload's
    mutated-in-place result when it's on.
    """
    # Snapshot BEFORE optimize_payload runs — it mutates the body's nested
    # blocks in place, so by the time it returns, the "original" is already
    # gone. Only pay the deepcopy cost when this request is actually
    # sampled for benchmarking.
    # random() picks whether this request gets sampled for benchmarking —
    # not security-sensitive, so the default PRNG is fine here.
    is_benchmark_sample = is_enabled and BENCHMARK_SAMPLE_RATE > 0 and random.random() < BENCHMARK_SAMPLE_RATE  # noqa: S311
    pre_optimize_snapshot = copy.deepcopy(raw_body) if is_benchmark_sample else None

    if not is_enabled:
        print("\n[byoai-runtime 🔴 OPTIMIZER OFF - Direct Pass-Through]\n")
        return raw_body, is_benchmark_sample, pre_optimize_snapshot

    body, orig_tok, opt_tok = await _session_dedup_stage.optimize(raw_body, session_id)
    saved_tok = max(0, orig_tok - opt_tok)

    await safe_redis_incrby("byoai:stats:tokens_original", orig_tok)
    await safe_redis_incrby("byoai:stats:tokens_sent", opt_tok)
    await safe_redis_incrby(REDIS_KEY_TOKENS_SAVED, saved_tok)

    cum_saved = await safe_redis_get(REDIS_KEY_TOKENS_SAVED, default="0")
    pct_saved = (saved_tok / orig_tok * 100) if orig_tok > 0 else 0

    print(f"\n[byoai-runtime ⚡ OPTIMIZER ON] session={session_id}")
    print(f" ├─ Payload: {orig_tok:,} tok  ➔  {opt_tok:,} tok (estimate)")
    print(f" ├─ Turn Savings: {saved_tok:,} tok ({pct_saved:.1f}%) (estimate — see /v1/stats/benchmark for tokenizer-verified numbers)")
    print(f" └─ Lifetime Saved: {int(cum_saved):,} tokens (estimate)\n")

    return body, is_benchmark_sample, pre_optimize_snapshot


async def _maybe_run_benchmark_sample(
    is_benchmark_sample: bool,
    pre_optimize_snapshot: dict | None,
    body: dict,
    headers: dict,
    session_id: str,
    requested_model: str,
) -> None:
    """
    For a request sampled for benchmarking, ask Anthropic's real tokenizer
    (via /v1/messages/count_tokens) for the pre- and post-optimization token
    counts, update the running stats, and persist a durable sample row.
    """
    if not is_benchmark_sample:
        return

    count_token_headers = {
        k: v for k, v in headers.items()
        if k.lower() in ("x-api-key", "authorization", "anthropic-version", "anthropic-beta", "content-type")
    }
    count_token_headers.setdefault("content-type", JSON_MEDIA_TYPE)
    real_orig, real_opt = await asyncio.gather(
        real_token_count(count_token_headers, pre_optimize_snapshot),
        real_token_count(count_token_headers, body),
    )
    if real_orig is None or real_opt is None:
        print("[byoai-runtime 🔬 BENCHMARK] sample discarded — count_tokens call failed, not counted toward stats\n")
        return

    real_saved = max(0, real_orig - real_opt)
    real_pct = (real_saved / real_orig * 100) if real_orig > 0 else 0.0
    await safe_redis_incrby("byoai:stats:real_tokens_original", real_orig)
    await safe_redis_incrby("byoai:stats:real_tokens_sent", real_opt)
    await safe_redis_incrby("byoai:stats:real_tokens_saved", real_saved)
    await safe_redis_incrby("byoai:stats:real_sample_count", 1)
    # Fire-and-forget: the durable SQLite row is the permanent record; don't
    # make the live response wait on a disk write.
    _fire_and_forget(
        db.record_benchmark_sample(session_id, body.get("model", requested_model), real_orig, real_opt, real_saved)
    )
    print("[byoai-runtime 🔬 REAL BENCHMARK — Anthropic tokenizer, not an estimate]")
    print(f" ├─ real_orig={real_orig:,}  real_opt={real_opt:,}  real_saved={real_saved:,} ({real_pct:.1f}%)")
    print(f" └─ via /v1/messages/count_tokens (not billed as a completion) — persisted to {db.DB_PATH}\n")


@app.api_route("/v1/messages", methods=["GET", "POST"])
async def proxy_claude_messages(request: Request):
    body_bytes = await request.body()

    if not body_bytes or request.method == "GET":
        return Response(
            content=json.dumps({"status": "ok", "message": "byoai-runtime proxy active"}),
            status_code=200,
            media_type=JSON_MEDIA_TYPE
        )

    try:
        raw_body = json.loads(body_bytes)
    except json.JSONDecodeError:
        return Response(
            content=json.dumps({"error": {"type": "invalid_request_error", "message": "Invalid JSON payload"}}),
            status_code=400,
            media_type=JSON_MEDIA_TYPE
        )

    requested_model = raw_body.get("model", "")

    if requested_model in OPENAI_COMPAT_MODELS:
        return await proxy_openai_compat_request(request, raw_body)

    if requested_model in MODEL_MAP:
        remapped = MODEL_MAP[requested_model]
        raw_body["model"] = remapped
        print(f"[byoai-runtime 🔄 REMAP] '{requested_model}' ➔ '{remapped}'")

    had_cache_control_already = _stage_count_cache_control_markers(raw_body) > 0
    raw_body = PromptCacheInjection.inject(raw_body)
    if not had_cache_control_already and _stage_count_cache_control_markers(raw_body) > 0:
        print("[byoai-runtime 📌 CACHE] Injected cache_control breakpoint(s) — client sent none.")

    # NOTE: if this upstream traffic uses prompt caching (cache_control
    # breakpoints — including the ones we may have just injected above),
    # mutating cached blocks below busts the cache for everything after
    # that point in the prompt on every call. optimize_payload only
    # touches tool_result/text blocks inside individual user messages, not
    # the system/tools blocks we cache here, so the two don't conflict —
    # but keep that boundary in mind if optimize_payload is ever extended.
    is_enabled = (await safe_redis_get(REDIS_KEY_ENABLED, default="1")) != "0"
    session_id = derive_session_id(request, raw_body)
    body, is_benchmark_sample, pre_optimize_snapshot = await _apply_optimizer(raw_body, session_id, is_enabled)

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "accept-encoding", "connection")
    }

    await _maybe_run_benchmark_sample(
        is_benchmark_sample, pre_optimize_snapshot, body, headers, session_id, requested_model
    )

    upstream_url = f"{ANTHROPIC_UPSTREAM}/v1/messages"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    is_stream = body.get("stream", False)
    log_session_id = derive_session_id(request, body)

    if is_stream:
        req = get_http_client().build_request("POST", upstream_url, json=body, headers=headers)
        try:
            res = await get_http_client().send(req, stream=True)
        except httpx.HTTPError as e:
            print(f"[byoai-runtime ❌ CONNECT {type(e).__name__}] session={log_session_id}: {e}")
            return Response(
                content=json.dumps({"error": {"type": "api_error", "message": f"byoai-runtime: could not reach upstream: {e}"}}),
                status_code=502,
                media_type=JSON_MEDIA_TYPE,
            )

        async def stream_and_close():
            # SSE events are separated by a blank line ("\n\n"). Anthropic
            # sends usage (incl. cache_read/cache_creation) split across
            # message_start (initial usage) and message_delta (final
            # output_tokens) events, so we buffer decoded text across
            # chunks and parse complete events without altering the bytes
            # we forward downstream.
            buffer = ""
            usage_seen = {}
            stream_error = None
            try:
                async for chunk in res.aiter_bytes():
                    yield chunk
                    try:
                        buffer += chunk.decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    while "\n\n" in buffer:
                        event, buffer = buffer.split("\n\n", 1)
                        for line in event.split("\n"):
                            if not line.startswith("data:"):
                                continue
                            try:
                                payload = json.loads(line[len("data:"):].strip())
                            except json.JSONDecodeError:
                                continue
                            msg_usage = payload.get("message", {}).get("usage") or payload.get("usage")
                            if msg_usage:
                                usage_seen.update({k: v for k, v in msg_usage.items() if v is not None})
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ConnectError) as e:
                # Upstream connection dropped/stalled mid-stream. Don't let
                # this crash the ASGI response (that's what was happening
                # before — an unhandled httpx.ReadTimeout propagating up
                # through StreamingResponse). Emit a well-formed SSE error
                # event so the client sees a clean failure instead of a
                # truncated stream with no explanation, then stop iterating.
                stream_error = e
                print(f"[byoai-runtime ❌ STREAM {type(e).__name__}] session={log_session_id}: {e}")
                error_event = {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"byoai-runtime: upstream connection {type(e).__name__} mid-stream: {e}"},
                }
                yield f"event: error\ndata: {json.dumps(error_event)}\n\n".encode()
            finally:
                await res.aclose()
                # NOTE: do not close http_client here — it's the shared,
                # long-lived pooled client reused across every request.
                if usage_seen:
                    log_usage(log_session_id, usage_seen)
                    _fire_and_forget(db.record_usage_event(log_session_id, "anthropic", body.get("model"), usage_seen))
                if not stream_error and res.status_code >= 400:
                    print(f"[byoai-runtime ❌ UPSTREAM {res.status_code}] session={log_session_id}")
                    print(f" └─ retry-after={dict(res.headers).get('retry-after', 'n/a')}  (see client for error body; streamed responses aren't buffered here)")

        return StreamingResponse(
            stream_and_close(),
            status_code=res.status_code,
            headers=clean_response_headers(dict(res.headers))
        )
    else:
        try:
            res = await get_http_client().post(
                upstream_url,
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            print(f"[byoai-runtime ❌ CONNECT {type(e).__name__}] session={log_session_id}: {e}")
            return Response(
                content=json.dumps({"error": {"type": "api_error", "message": f"byoai-runtime: could not reach upstream: {e}"}}),
                status_code=502,
                media_type=JSON_MEDIA_TYPE,
            )
        if res.status_code >= 400:
            print(f"[byoai-runtime ❌ UPSTREAM {res.status_code}] session={log_session_id}")
            print(f" ├─ retry-after={res.headers.get('retry-after', 'n/a')}")
            print(f" └─ body: {res.content[:1000]!r}")
        else:
            try:
                resp_json = json.loads(res.content)
                log_usage(log_session_id, resp_json.get("usage"))
                _fire_and_forget(db.record_usage_event(log_session_id, "anthropic", body.get("model"), resp_json.get("usage")))
            except (json.JSONDecodeError, AttributeError):
                pass
        return Response(
            content=res.content,
            status_code=res.status_code,
            headers=clean_response_headers(dict(res.headers))
        )


class ConfigRequest(BaseModel):
    enabled: bool


@app.get("/v1/stats")
async def get_stats():
    enabled_val = await safe_redis_get(REDIS_KEY_ENABLED, default="1")
    tokens_saved = int(await safe_redis_get(REDIS_KEY_TOKENS_SAVED, default="0"))
    tokens_orig = int(await safe_redis_get("byoai:stats:tokens_original", default="0"))
    tokens_sent = int(await safe_redis_get("byoai:stats:tokens_sent", default="0"))

    pct_saved = (tokens_saved / tokens_orig * 100) if tokens_orig > 0 else 0.0

    return {
        "optimizer_enabled": enabled_val != "0",
        "tokens_saved": tokens_saved,
        "tokens_original": tokens_orig,
        "tokens_sent": tokens_sent,
        "savings_percentage": f"{pct_saved:.2f}%",
        "methodology": "ESTIMATE ONLY — len(json.dumps(body)) // 4, a rough character-count heuristic. "
                        "Not Anthropic's real tokenizer. Do not use this endpoint's numbers in external/marketing "
                        "claims — use /v1/stats/benchmark instead, which is tokenizer-verified.",
    }


@app.get("/v1/stats/benchmark")
async def get_benchmark_stats():
    """
    Tokenizer-verified savings, safe to cite externally. Every number here
    comes from Anthropic's own /v1/messages/count_tokens endpoint called on
    both the pre- and post-optimization payload for a randomly sampled
    subset of real production requests — never from our own estimate.
    Empty/zero until BYOAI_BENCHMARK_SAMPLE_RATE is set above 0 and some
    sampled traffic has flowed through.
    """
    sample_count = int(await safe_redis_get("byoai:stats:real_sample_count", default="0"))
    real_orig = int(await safe_redis_get("byoai:stats:real_tokens_original", default="0"))
    real_sent = int(await safe_redis_get("byoai:stats:real_tokens_sent", default="0"))
    real_saved = int(await safe_redis_get("byoai:stats:real_tokens_saved", default="0"))
    pct = (real_saved / real_orig * 100) if real_orig > 0 else 0.0

    return {
        "sample_count": sample_count,
        "sample_rate_configured": BENCHMARK_SAMPLE_RATE,
        "real_tokens_original": real_orig,
        "real_tokens_sent": real_sent,
        "real_tokens_saved": real_saved,
        "real_savings_percentage": f"{pct:.2f}%",
        "methodology": "Each sampled request's raw and optimized payload were independently submitted to "
                        "Anthropic's own /v1/messages/count_tokens endpoint (a real tokenizer call, not billed "
                        "as a completion) and compared. This measures payload-size reduction only.",
        "scope_note": "This does NOT include prompt-cache savings, which are typically the larger cost lever "
                       "and are tracked separately per-request in the console log (cache_read/cache_write), not "
                       "aggregated here yet.",
        "sample_size_caveat": "Treat this as directional until sample_count is reasonably large (dozens+ of "
                               "sampled requests spanning varied payload sizes) before citing externally.",
    }


@app.get("/v1/stats/permanent")
async def get_permanent_stats():
    """
    Same measurement as /v1/stats/benchmark, but read directly from the
    durable SQLite log instead of Redis counters. This is the number to
    trust if Redis has ever been restarted or flushed since you started
    sampling — Redis counters reset to zero silently on that; this table
    doesn't. Use this as the source of truth; /v1/stats/benchmark is a
    faster read of (hopefully) the same numbers.
    """
    bench = await db.benchmark_summary()
    usage = await db.usage_summary()
    real_orig = bench["real_tokens_original"]
    real_saved = bench["real_tokens_saved"]
    pct = (real_saved / real_orig * 100) if real_orig > 0 else 0.0

    return {
        "storage": db.DB_PATH,
        "benchmark": {
            "sample_count": bench["sample_count"],
            "real_tokens_original": real_orig,
            "real_tokens_sent": bench["real_tokens_sent"],
            "real_tokens_saved": real_saved,
            "real_savings_percentage": f"{pct:.2f}%",
        },
        "usage_totals": usage,
        "methodology": "Identical measurement methodology to /v1/stats/benchmark (Anthropic's real tokenizer via "
                        "count_tokens), but persisted to disk on every sample rather than kept only in Redis — "
                        "survives restarts, Redis flushes, and infrastructure changes.",
    }


@app.get("/v1/stats/history")
async def get_benchmark_history(limit: int = 50):
    """Individual benchmark data points, most recent first — useful for
    eyeballing variance across samples before trusting an aggregate
    percentage (a single unusually large or small payload can skew an
    average built from only a handful of samples)."""
    samples = await db.recent_benchmark_samples(limit=min(limit, 500))
    return {"count": len(samples), "samples": samples}


@app.post("/v1/config")
async def set_config(config: ConfigRequest):
    val = "1" if config.enabled else "0"
    try:
        await r.set(REDIS_KEY_ENABLED, val)
    except Exception:
        pass
    return {
        "status": "success",
        "optimizer_enabled": config.enabled,
        "message": f"Optimizer is now {'ON' if config.enabled else 'OFF'}",
    }


@app.post("/v1/toggle")
async def toggle_config():
    current = await safe_redis_get(REDIS_KEY_ENABLED, default="1")
    new_val = "0" if current != "0" else "1"
    try:
        await r.set(REDIS_KEY_ENABLED, new_val)
    except Exception:
        pass
    is_on = new_val == "1"
    return {
        "status": "success",
        "optimizer_enabled": is_on,
        "message": f"Optimizer toggled {'ON' if is_on else 'OFF'}",
    }


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_catch_all(request: Request, path: str):
    """
    Generic fallback for any other Anthropic /v1/* endpoint (models list,
    batches, files, etc.) that this proxy doesn't have bespoke logic for.
    Without this, any endpoint the client calls that we haven't explicitly
    wired up 404s locally instead of reaching Anthropic at all — which is
    exactly what happened with count_tokens before it got its own route.

    MUST stay the last route defined in this file: FastAPI/Starlette
    matches routes in registration order, and this path pattern (/v1/*)
    would otherwise shadow every specific route above it (/v1/messages,
    /v1/messages/count_tokens, /v1/stats, /v1/config, /v1/toggle).
    """
    body_bytes = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "accept-encoding", "connection")
    }
    upstream_url = f"{ANTHROPIC_UPSTREAM}/v1/{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    res = await get_http_client().request(
        request.method,
        upstream_url,
        content=body_bytes if body_bytes else None,
        headers=headers,
        timeout=60.0,
    )
    if res.status_code >= 400:
        print(f"[byoai-runtime ❌ UPSTREAM {res.status_code}] passthrough /v1/{path}")
        print(f" └─ body: {res.content[:500]!r}")
    return Response(
        content=res.content,
        status_code=res.status_code,
        headers=clean_response_headers(dict(res.headers)),
    )


def run():
    """Entry point for the `byoai-agent-context-cache` console script."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("BYOAI_HOST", "0.0.0.0"),
        port=int(os.getenv("BYOAI_PORT", "8787")),
    )


if __name__ == "__main__":
    run()