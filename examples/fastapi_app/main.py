"""Example: ByoAI Runtime inside an existing FastAPI app, reusing existing infra.

Run:

    pip install 'byoai-runtime[fastapi,redis]' uvicorn
    export OPENAI_API_KEY=sk-...          # or point BYOAI_BASE_URL at Ollama/vLLM
    uvicorn examples.fastapi_app.main:app --reload

Try it:

    curl -s localhost:8000/ask -X POST -H 'content-type: application/json' \
        -d '{"query": "What is an execution runtime?", "user_id": "usr_1"}'

    curl -N localhost:8000/ask/stream -X POST -H 'content-type: application/json' \
        -d '{"query": "Stream me a haiku about runtimes"}'

Everything below the runtime construction is a normal FastAPI app — ByoAI adds
execution (routing, retries, fallback, caching, events, cost tracking) without
owning any of your routes, auth, or business logic.
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from byoai import Runtime
from byoai.integrations.fastapi import attach, get_runtime, stream_response

# --- Runtime wired to YOUR existing environment ------------------------------

llm_config: dict = {
    # Any OpenAI-compatible endpoint: OpenAI, Azure, Ollama, vLLM, OpenRouter...
    "provider": os.environ.get("BYOAI_PROVIDER", "openai"),
    "model": os.environ.get("BYOAI_MODEL", "gpt-4o-mini"),
}
if os.environ.get("BYOAI_BASE_URL"):
    llm_config["provider"] = "openai_compatible"
    llm_config["base_url"] = os.environ["BYOAI_BASE_URL"]
if os.environ.get("BYOAI_FALLBACK_MODEL"):
    llm_config["fallback"] = {
        "provider": "ollama",
        "model": os.environ["BYOAI_FALLBACK_MODEL"],
    }

cache_config = (
    {
        "provider": "redis",
        "url": os.environ["REDIS_URL"],
        "namespace": "byoai:",  # writes isolated; your keys untouched
        "session_reader": {
            # read your app's existing chat history, read-only
            "pattern": "app:users:{user_id}:chat_history",
            "format": "json",
        },
    }
    if os.environ.get("REDIS_URL")
    else {"provider": "memory"}
)

runtime = Runtime(
    llm=llm_config,
    cache=cache_config,
    system_prompt="You are a concise, helpful assistant.",
)


# Observe the execution lifecycle without touching request handlers.
def _log_provider_event(event, payload):
    usage = payload.get("usage")
    print(f"[byoai] {event} provider={payload.get('provider')} "
          f"usage={getattr(usage, '__dict__', None)}")


runtime.on("provider.*", _log_provider_event)
runtime.on("cache.*", lambda event, payload: print(f"[byoai] {event}"))


# --- Your existing FastAPI app ------------------------------------------------

app = FastAPI(title="my-existing-app")
attach(app, runtime)


class AskRequest(BaseModel):
    query: str
    user_id: str | None = None


@app.post("/ask")
async def ask(body: AskRequest, rt: Runtime = Depends(get_runtime)):
    result = await rt.execute(body.query, user_id=body.user_id)
    return {
        "content": result.content,
        "cached": result.cached,
        "model": result.model,
        "provider": result.provider,
        "usage": result.usage.__dict__,
    }


@app.post("/ask/stream")
async def ask_stream(body: AskRequest, rt: Runtime = Depends(get_runtime)):
    return stream_response(rt, body.query, user_id=body.user_id)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
