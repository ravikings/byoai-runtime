"""ASGI middleware that runs Shield's rules on JSON chat requests.

    from byoai.shield_guard.fastapi import ShieldGuardMiddleware
    app.add_middleware(ShieldGuardMiddleware, policy="~/.byoai/shield/policy.json")

Only POSTs with a JSON object body on ``paths`` are read; everything else
passes untouched. A blocked request gets a ``403`` in the provider's error
shape (Anthropic for ``/messages`` paths, OpenAI otherwise); a redacted one is
handed to your app with the rewritten body. Streaming responses are not
touched. Needs no web framework import, so it also works on Starlette.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from . import Decision, blocked_payload, check_raw, load_policy_from

DEFAULT_PATHS = ("/v1/chat/completions", "/v1/messages", "/v1/responses")


class ShieldGuardMiddleware:
    def __init__(self, app, *, paths=DEFAULT_PATHS, policy: dict | str | Path | None = None,
                 mode: str = "enforce", on_warn: str = "block",
                 max_body: int = 8 * 1024 * 1024, key_path: Path | None = None,
                 app_name: str | None = None):
        if mode not in ("observe", "enforce"):
            raise ValueError("mode must be 'observe' or 'enforce'")
        self.app, self.mode = app, mode
        self.paths = tuple(_norm(p) for p in paths)
        self.on_warn, self.max_body, self.key_path = on_warn, max_body, key_path
        self.app_name = app_name
        self.policy = policy if isinstance(policy, dict) else load_policy_from(policy)
        #: The last 100 decisions that were not a plain allow: rules, labels,
        #: length and keyed fingerprint. Never text or the request body.
        self.decisions: deque = deque(maxlen=100)

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH")
                or _norm(scope["path"]) not in self.paths):
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            chunks.append(msg.get("body", b""))
            size += len(chunks[-1])
            if size > self.max_body:  # too big to read: fail closed
                return await self._reject(send, scope, Decision(
                    action="block", rules=["unreadable"], labels=["request body that could not be checked"]))
            if not msg.get("more_body"):
                break
        raw = b"".join(chunks)
        enc = next((v.decode("latin-1") for k, v in scope["headers"] if k == b"content-encoding"), None)
        try:
            import anyio
            res = await anyio.to_thread.run_sync(lambda: check_raw(
                raw, enc, policy=self.policy, on_warn=self.on_warn,
                key_path=self.key_path, app=self.app_name))
        except ImportError:  # no anyio: check inline
            res = check_raw(raw, enc, policy=self.policy, on_warn=self.on_warn,
                            key_path=self.key_path, app=self.app_name)
        d = res.decision
        if d.action != "allow" or d.rules:
            self.decisions.append({"action": d.action, "rules": d.rules, "labels": d.labels,
                                   "chars": d.chars, "text_hmac": d.text_hmac})
        if self.mode == "enforce" and d.action == "block":
            return await self._reject(send, scope, d)
        if self.mode == "enforce" and res.rewritten is not None:
            fwd = res.rewritten
        else:  # unchanged: hand the app the original bytes and headers
            return await self.app(scope, _replay(raw, receive), send)
        scope = dict(scope)
        scope["headers"] = [(k, v) for k, v in scope["headers"]
                            if k not in (b"content-length", b"content-encoding")] \
            + [(b"content-length", str(len(fwd)).encode())]
        return await self.app(scope, _replay(fwd, receive), send)

    async def _reject(self, send, scope, d):
        shape = "anthropic" if _norm(scope["path"]).endswith("/messages") else "openai"
        out = json.dumps(blocked_payload(d, shape)).encode()
        await send({"type": "http.response.start", "status": 403, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(out)).encode())]})
        await send({"type": "http.response.body", "body": out})


def _norm(path: str) -> str:
    return "/" + path.strip("/").lower()


def _replay(raw: bytes, receive):
    sent = False

    async def rx():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return await receive()
    return rx
