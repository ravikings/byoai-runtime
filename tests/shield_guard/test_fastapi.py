from __future__ import annotations

import gzip
import json

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from byoai.shield_guard.fastapi import ShieldGuardMiddleware

KEY = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2"


def make(tmp_path, **kw):
    app = FastAPI()
    app.add_middleware(ShieldGuardMiddleware, key_path=tmp_path / "k", **kw)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await request.json()

    @app.post("/other")
    async def other(request: Request):
        return await request.json()

    return TestClient(app)


def msg(t):
    return {"messages": [{"role": "user", "content": t}]}


def test_block(tmp_path):
    r = make(tmp_path).post("/v1/chat/completions", json=msg(KEY))
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "coriqo_shield_blocked" and KEY not in r.text


def test_redact_reaches_handler(tmp_path):
    r = make(tmp_path).post("/v1/chat/completions", json=msg("a@b.co"))
    assert r.status_code == 200 and r.json()["messages"][0]["content"] == "[EMAIL_1]"


def test_other_paths_and_clean_pass(tmp_path):
    c = make(tmp_path)
    assert c.post("/other", json=msg(KEY)).status_code == 200
    assert c.post("/v1/chat/completions", json=msg("hi")).json() == msg("hi")


def test_observe(tmp_path):
    r = make(tmp_path, mode="observe").post("/v1/chat/completions", json=msg(KEY))
    assert r.status_code == 200 and r.json()["messages"][0]["content"] == KEY


def test_oversize_fails_closed(tmp_path):
    r = make(tmp_path, max_body=10).post("/v1/chat/completions", json=msg("x" * 50))
    assert r.status_code == 403


def test_variants_gzip_and_decisions_are_safe(tmp_path):
    c = make(tmp_path)
    b = json.dumps(msg(KEY)).encode()
    assert c.post("/V1/Chat/Completions/", content=b).status_code == 403
    assert c.post("/v1/chat/completions", content=gzip.compress(b),
                  headers={"content-encoding": "gzip"}).status_code == 403
    assert c.put("/v1/chat/completions", content=b).status_code == 403
    r = c.post("/v1/chat/completions", json={**msg("hi"), "metadata": {"user_id": KEY}})
    assert r.status_code == 403
    ok = c.post("/v1/chat/completions", content=gzip.compress(json.dumps(msg("a@b.co")).encode()),
                headers={"content-encoding": "gzip"})
    assert ok.json()["messages"][0]["content"] == "[EMAIL_1]"


def test_decisions_bounded_and_textless(tmp_path):
    from collections import deque
    mw = ShieldGuardMiddleware(None, key_path=tmp_path / "k")
    assert isinstance(mw.decisions, deque) and mw.decisions.maxlen == 100


def test_unchanged_body_reaches_app_as_sent(tmp_path):
    seen = []
    app = FastAPI()
    app.add_middleware(ShieldGuardMiddleware, key_path=tmp_path / "k")

    @app.post("/v1/chat/completions")
    async def h(request: Request):
        seen.append((request.headers.get("content-encoding"), await request.body()))
        return {}
    body = gzip.compress(json.dumps(msg("hi")).encode())
    TestClient(app).post("/v1/chat/completions", content=body, headers={"content-encoding": "gzip"})
    assert seen == [("gzip", body)]
