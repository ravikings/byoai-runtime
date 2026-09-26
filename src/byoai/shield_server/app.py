"""``create_app(config) -> FastAPI`` for the self-hosted Shield server.

Wires together the ingest store (enrolments + checkpoints, shared with the
rest of ByoAI), this package's own store (tokens, signed policies, per-device
shield state), the Ed25519 policy-signing key, the device routes, the admin
API, the read-only fleet console router (mounted behind the admin token),
and the built console SPA at ``/console/``.
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from byoai.agent_context_cache import console as console_assets
from byoai.console import build_console_router
from byoai.ingest import IngestStore
from byoai.shield_server import auth
from byoai.shield_server.config import ShieldServerConfig
from byoai.shield_server.policy import load_or_create_signing_key
from byoai.shield_server.routes import build_admin_router, build_device_router
from byoai.shield_server.store import ServerStore

__all__ = ["BodySizeLimitMiddleware", "create_app"]

#: Request bodies larger than this are refused outright — the security floor
#: the plan asks for. Generous enough for a full checkpoint batch or policy
#: poll, far below anything a message-text payload would need (this server
#: never receives message text at all).
MAX_BODY_BYTES = 16 * 1024 * 1024


class BodySizeLimitMiddleware:
    """Refuses a request body over ``max_bytes`` — enforced on the actual
    bytes read off the wire, not on a client-supplied ``Content-Length``.

    A ``Content-Length`` check alone (what this used to be) never sees a
    chunked request that omits the header entirely: the app would happily
    buffer an unbounded body before any route or auth check ever runs. This
    is a raw ASGI middleware (not ``@app.middleware("http")``, which is
    Starlette's ``BaseHTTPMiddleware`` and only inspects headers before
    ``call_next`` — it doesn't see the body stream either) that wraps
    ``receive`` and counts every chunk as it arrives. Once the running total
    exceeds the cap, it stops handing bytes to the app (a synthetic
    ``http.disconnect`` — the standard way to abort a request an ASGI app is
    mid-way through reading) and sends the 413 itself once the app gives up
    on it, so the cap holds no matter how the app tries to read the body
    (``request.body()``, streaming, form parsing, ...).
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = {"total": 0, "over_limit": False, "started": False}

        async def guarded_receive():
            message = await receive()
            if message["type"] == "http.request":
                state["total"] += len(message.get("body") or b"")
                if state["total"] > self.max_bytes:
                    state["over_limit"] = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message) -> None:
            # Once the app has genuinely started responding, the response is
            # already committed to the real client — swallowing anything past
            # that point would corrupt the stream (a body with no start, or
            # one that simply stops mid-way). Let it finish; a body-size
            # refusal discovered only after the app already answered is too
            # late to turn into a different response.
            if state["started"]:
                await send(message)
                return
            if state["over_limit"]:
                # Not yet responded for real: swallow app messages and send
                # our own 413 once it gives up on the (now-disconnected) body.
                return
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        await self.app(scope, guarded_receive, guarded_send)

        if state["over_limit"] and not state["started"]:
            await send({
                "type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            })
            await send({
                "type": "http.response.body",
                "body": f"Request body too large (over {self.max_bytes} bytes).\n".encode(),
            })


def create_app(config: ShieldServerConfig) -> FastAPI:
    ingest = IngestStore(config.ingest_db_path)
    ingest.ensure_tenant(config.org)
    store = ServerStore(config.server_db_path)
    signer = load_or_create_signing_key(config.signing_key_path)

    app = FastAPI(title="ByoAI Shield server", version="1.5")
    app.state.ingest_store = ingest
    app.state.server_store = store
    app.state.signer = signer
    app.state.config = config
    # Wraps the ASGI app itself (below FastAPI's own middleware stack), so the
    # streaming cap applies to every request regardless of route or whether
    # it declares a body model at all.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)

    app.include_router(build_device_router(config, ingest, store, signer))
    app.include_router(build_admin_router(config, ingest, store, signer))

    # The read-only fleet console API, mounted behind the SAME admin token —
    # per the plan, "the existing read-only /v1/console/* fleet router is
    # mounted too, behind the admin token." A router-level dependency (not a
    # post-hoc mutation of each route's `.dependencies`) so the gate can never
    # be dropped by a route added to `build_console_router` later.
    def _require_admin(authorization: str | None = Header(None)) -> None:
        auth.check_admin_token(authorization, expected=config.admin_token)

    app.include_router(build_console_router(ingest), dependencies=[Depends(_require_admin)])

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/console", methods=["GET", "HEAD"])
    async def console_root_redirect():
        return RedirectResponse(url="/console/", status_code=307)

    @app.api_route("/console/{path:path}", methods=["GET", "HEAD"])
    async def console_spa(path: str):
        if console_assets.env_flag_disabled():
            return PlainTextResponse(content="The console is disabled (BYOAI_CONSOLE=0).\n",
                                     status_code=404)
        if not console_assets.assets_available():
            return PlainTextResponse(content=console_assets.MISSING_BUILD_MESSAGE, status_code=503)
        asset = console_assets.resolve_asset(path)
        if asset is not None:
            return FileResponse(asset, headers=console_assets.cache_headers(path))
        return FileResponse(console_assets.INDEX_FILE,
                            headers=console_assets.cache_headers("index.html"))

    @app.on_event("shutdown")
    def _close_stores() -> None:
        ingest.close()
        store.close()

    return app
