"""Example: ByoAI Runtime inside an existing Flask app, reusing existing infra.

Run:

    pip install 'byoai-runtime[flask,anthropic]' gunicorn
    export ANTHROPIC_API_KEY=sk-ant-...
    gunicorn --worker-class gthread --workers 1 --threads 8 'examples.flask_app.main:app'

    # or, for local dev only (Flask's dev server, port 8000 to match the curl
    # commands below — its own default is 5000):
    flask --app examples.flask_app.main run --port 8000

Try it:

    curl -s localhost:8000/ask -X POST -H 'content-type: application/json' \
        -d '{"query": "What is an execution runtime?", "user_id": "usr_1"}'

    curl -N localhost:8000/ask/stream -X POST -H 'content-type: application/json' \
        -d '{"query": "Stream me a haiku about runtimes"}'

Everything below the runtime construction is a normal Flask app — ByoAI adds execution
(routing, retries, caching, events, cost tracking) without owning any of your routes, auth,
or business logic. See docs/guides/flask.md for why this is a factory function (not a
module-level `app = Flask(__name__)`) and why it must not run under `gunicorn --preload`.
"""

from __future__ import annotations

import os

from flask import Flask, jsonify, request

from byoai import Runtime
from byoai.errors import ConfigurationError
from byoai.integrations.flask import attach, execute, stream_response


def create_app() -> Flask:
    app = Flask(__name__)

    # Registered unconditionally, before the ConfigurationError branch below —
    # a health/readiness probe must keep working even when AI features are
    # degraded, not vanish along with them.
    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    # --- Runtime wired to YOUR existing environment ---------------------------

    llm_config: dict = {
        "provider": "anthropic",
        "model": os.environ.get("BYOAI_MODEL", "claude-sonnet-5"),
        # Wraps the system prompt in Anthropic's cache_control ephemeral block —
        # restores prompt caching for a compliance-gateway-style fixed system
        # prompt, at the cost of a boolean flag instead of hand-built blocks.
        "cache_system": True,
    }

    cache_config = (
        {"provider": "redis", "url": os.environ["REDIS_URL"], "namespace": "byoai:"}
        if os.environ.get("REDIS_URL")
        else {"provider": "memory"}
    )

    # A missing/invalid API key is a ConfigurationError raised at construction
    # (fail-fast, not a lazy per-request check) — decide here what "misconfigured"
    # means for this app rather than letting it crash the whole process at boot.
    try:
        runtime = Runtime(
            llm=llm_config,
            cache=cache_config,
            system_prompt="You are a concise, helpful assistant.",
        )
    except ConfigurationError as exc:
        app.logger.error("ByoAI runtime misconfigured: %s", exc)
        runtime = None

    if runtime is None:

        @app.post("/ask")
        def ask_unavailable():
            return jsonify({"error": "AI features unavailable"}), 503

        return app

    attach(app, runtime)

    # Observe the execution lifecycle without touching request handlers.
    def _log_provider_event(event, payload):
        usage = payload.get("usage")
        print(
            f"[byoai] {event} provider={payload.get('provider')} "
            f"usage={getattr(usage, '__dict__', None)}"
        )

    runtime.on("provider.*", _log_provider_event)
    runtime.on("cache.*", lambda event, payload: print(f"[byoai] {event}"))

    # --- Your existing Flask routes --------------------------------------------

    @app.post("/ask")
    def ask():
        body = request.get_json()
        result = execute(
            body["query"],
            user_id=body.get("user_id"),
            # Anthropic's native metadata.user_id, for audit correlation on
            # Anthropic's side — distinct from user_id= above, which only
            # affects byoai's own session-history/cache keying.
            provider_metadata={"user_id": body["user_id"]} if body.get("user_id") else None,
        )
        return jsonify(
            {
                "content": result.content,
                "cached": result.cached,
                "model": result.model,
                "provider": result.provider,
                "usage": result.usage.__dict__,
            }
        )

    @app.post("/ask/stream")
    def ask_stream():
        body = request.get_json()
        return stream_response(body["query"], user_id=body.get("user_id"))

    return app


app = create_app()
