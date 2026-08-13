"""ByoAI Demo Agent Showcase — FastAPI app.

Run:

    export ANTHROPIC_API_KEY=sk-ant-...       # powers the banking (B1-B4) agents live
    export OPENAI_API_KEY=sk-...              # powers the healthcare (H1-H4) agents live
    export BYOAI_RECORDER_ENABLED=1
    export DEMO_TAMPER=1                      # optional: enables /api/demo/tamper
    export BYOAI_DEMO_AUTOPILOT=1             # optional: pings a random agent every 1.5-4min
    export BYOAI_CORIQO_URL=http://localhost:8000   # optional: publish runs to Coriqo
    export BYOAI_CORIQO_API_KEY=cq_sa_...           #   ...with these credentials
    export BYOAI_CORIQO_TENANT_SLUG=acme_bank
    uvicorn examples.agent_showcase.app:app --reload --port 8001

Either API key is optional independently: an agent whose provider has no key
set transparently replays its cached fallback transcript instead of calling
out live (see runner.py's fallback path) — /api/agents reports each agent's
current live/replay availability under "live".

See internal_doc/demo_agent_showcase_spec.md for the full spec and
examples/agent_showcase/README.md for setup/status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from byoai.recorder.integration import get_recorder
from byoai.recorder.verify import verify_ledger

from . import coriqo_sync
from .agents.registry import get_agent, list_agents
from .runner import AgentRunner, RunEvent

_httpx_log_level = os.environ.get("HTTPX_LOG_LEVEL")
if _httpx_log_level:
    level = getattr(logging, _httpx_log_level.upper(), logging.DEBUG)
    _httpx_handler = logging.StreamHandler()
    _httpx_handler.setLevel(level)
    _httpx_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    for _logger_name in ("httpx", "httpcore"):
        _logger = logging.getLogger(_logger_name)
        _logger.setLevel(level)
        _logger.addHandler(_httpx_handler)

app = FastAPI(title="ByoAI Agent Showcase (demo)")

UI_DIR = Path(__file__).parent / "ui"
app.mount("/ui/static", StaticFiles(directory=UI_DIR), name="ui-static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")

log = logging.getLogger("agent_showcase.app")

# run_id -> {"agent_id", "trace_id", "events": [RunEvent, ...], "done": bool, "source"}
_RUNS: dict[str, dict[str, Any]] = {}

# Keeps fire-and-forget run/autopilot tasks referenced so the event loop
# can't garbage-collect them mid-run; entries are dropped once done.
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# Autopilot: pings a random agent every AUTOPILOT_MIN_SECONDS-AUTOPILOT_MAX_SECONDS
# to mimic real bank/healthcare traffic for a live demo. Opt-in via
# BYOAI_DEMO_AUTOPILOT=1 so importing this module (tests, one-off runs) never
# silently starts spending API credits.
AUTOPILOT_MIN_SECONDS = 90
AUTOPILOT_MAX_SECONDS = 240

# showcase agent id -> Coriqo agent id, populated at startup when Coriqo sync
# is configured (BYOAI_CORIQO_URL). Empty means every run publishes nothing.
_CORIQO_AGENTS: dict[str, str] = {}


def _event_to_dict(event: RunEvent) -> dict[str, Any]:
    return asdict(event)


async def _autopilot_loop() -> None:
    agents = list_agents()
    if not agents:
        return
    while True:
        await asyncio.sleep(random.uniform(AUTOPILOT_MIN_SECONDS, AUTOPILOT_MAX_SECONDS))
        agent = random.choice(agents)
        try:
            run_id = _start_run(agent.id, source="autopilot")
            log.info("autopilot: started %s run_id=%s", agent.id, run_id)
        except Exception:  # noqa: BLE001 - autopilot must never take the app down
            log.exception("autopilot: failed to start a run for %s", agent.id)


@app.on_event("startup")
async def _register_with_coriqo() -> None:
    """Registers every showcase agent with Coriqo, if sync is configured.

    Runs in a worker thread because ``ensure_agents_registered`` is sync httpx
    and would otherwise block the event loop for as long as Coriqo takes to
    answer (or to time out, if it isn't there).
    """
    if not coriqo_sync.enabled():
        log.info("agent_showcase: Coriqo sync off (BYOAI_CORIQO_URL unset)")
        return
    global _CORIQO_AGENTS
    _CORIQO_AGENTS = await asyncio.to_thread(coriqo_sync.ensure_agents_registered)
    log.info("agent_showcase: Coriqo sync on, %d agent(s) mapped", len(_CORIQO_AGENTS))


@app.on_event("shutdown")
async def _close_coriqo_client() -> None:
    await asyncio.to_thread(coriqo_sync.close)


@app.on_event("startup")
async def _start_autopilot() -> None:
    if os.environ.get("BYOAI_DEMO_AUTOPILOT") != "1":
        return
    task = asyncio.create_task(_autopilot_loop())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


_PROVIDER_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _provider_live(provider: str) -> bool:
    env_var = _PROVIDER_KEY_ENV.get(provider)
    return bool(env_var and os.environ.get(env_var))


@app.get("/api/agents")
def api_list_agents() -> list[dict[str, Any]]:
    return [
        {
            "id": a.id,
            "name": a.name,
            "domain": a.domain,
            "description": a.description,
            "tools": [t["name"] for t in a.tools],
            "provider": a.provider,
            "model": a.model,
            "sub_agents": a.sub_agents,
            "live": _provider_live(a.provider),
        }
        for a in list_agents()
    ]


# Caps in-memory _RUNS growth for long-lived processes (e.g. autopilot running
# for days) — evicting the oldest *completed* run's in-memory state doesn't
# lose anything: every sealed event is still in the ledger, just no longer
# reachable via the non-authoritative /api/runs/{run_id} in-memory endpoints
# (use /api/runs/{run_id}/replay, which reads the ledger directly, instead).
_MAX_RETAINED_RUNS = 200


def _evict_old_runs() -> None:
    if len(_RUNS) <= _MAX_RETAINED_RUNS:
        return
    for run_id in list(_RUNS):
        if len(_RUNS) <= _MAX_RETAINED_RUNS:
            break
        if _RUNS[run_id]["done"]:
            del _RUNS[run_id]


def _start_run(
    agent_id: str,
    *,
    source: str = "manual",
    inject_misfire: bool = False,
    force_live: bool = False,
) -> str:
    agent = get_agent(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"unknown agent: {agent_id}")

    _evict_old_runs()
    run_id = "run_" + uuid.uuid4().hex[:12]
    runner = AgentRunner(agent, run_id=run_id)
    state: dict[str, Any] = {
        "agent_id": agent_id,
        "trace_id": "",
        "events": [],
        "done": False,
        "source": source,
    }
    _RUNS[run_id] = state

    async def drive() -> None:
        final_text = ""
        try:
            async for event in runner.run(inject_misfire=inject_misfire, force_live=force_live):
                if not state["trace_id"]:
                    state["trace_id"] = event.trace_id
                if event.kind == "run_complete":
                    final_text = event.text or ""
                state["events"].append(event)
        finally:
            state["done"] = True
            if _CORIQO_AGENTS:
                # Sync httpx in a worker thread, and after `done` is set, so a
                # slow or unreachable Coriqo can't hold up the run's own
                # completion or the SSE stream watching for it.
                await asyncio.to_thread(
                    coriqo_sync.publish_run,
                    run_id,
                    agent_id,
                    agent_map=_CORIQO_AGENTS,
                    final_text=final_text,
                )

    task = asyncio.create_task(drive())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return run_id


@app.post("/api/agents/{agent_id}/run")
async def api_run_agent(
    agent_id: str, inject_misfire: bool = False, force_live: bool = False
) -> dict[str, str]:
    """inject_misfire=true forces a simulated provider failure (skips the live
    call entirely) so the fallback/detection path can be demoed on demand.
    force_live=true bypasses the 1-day live-call TTL for this agent (see
    runner.LIVE_CALL_TTL_SECONDS) and always calls out live."""
    run_id = _start_run(
        agent_id, source="manual", inject_misfire=inject_misfire, force_live=force_live
    )
    return {"run_id": run_id, "status": "started"}


@app.get("/api/runs/{run_id}/events")
async def api_run_events(run_id: str) -> StreamingResponse:
    if run_id not in _RUNS:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")

    async def gen():
        sent = 0
        state = _RUNS[run_id]
        while True:
            events = state["events"]
            while sent < len(events):
                payload = _event_to_dict(events[sent])
                yield f"data: {json.dumps(payload)}\n\n"
                sent += 1
            if state["done"] and sent >= len(state["events"]):
                break
            await asyncio.sleep(0.1)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/runs/{run_id}")
def api_run_summary(run_id: str) -> dict[str, Any]:
    state = _RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    events = state["events"]
    spans = {(e.trace_id, e.span_id, e.parent_span_id) for e in events}
    policy_violations = [
        {
            "span_id": e.span_id,
            "tool_name": e.tool_name,
            "reason": e.data.get("policy_violation_reason"),
            "ts": e.ts,
        }
        for e in events
        if e.kind == "tool_use" and e.data.get("policy_violation")
    ]
    return {
        "run_id": run_id,
        "agent_id": state["agent_id"],
        "trace_id": state["trace_id"],
        "done": state["done"],
        "event_count": len(events),
        "spans": [{"trace_id": t, "span_id": s, "parent_span_id": p} for t, s, p in spans],
        "events": [_event_to_dict(e) for e in events],
        "policy_violations": policy_violations,
        "flagged": len(policy_violations) > 0,
    }


@app.get("/api/runs/{run_id}/verify")
def api_run_verify(run_id: str) -> dict[str, Any]:
    if run_id not in _RUNS:
        raise HTTPException(status_code=404, detail=f"unknown run: {run_id}")
    recorder = get_recorder()
    if recorder is None:
        return {
            "chain_ok": None,
            "digests_ok": None,
            "anchor": {"status": "recorder disabled (BYOAI_RECORDER_ENABLED != 1)"},
            "tampered_events": [],
        }
    result = verify_ledger(recorder.ledger.path)

    conn = sqlite3.connect(f"file:{recorder.ledger.path}?mode=ro", uri=True)
    try:
        run_seqs = {
            row[0]
            for row in conn.execute("SELECT seq FROM agent_events WHERE session_id = ?", (run_id,))
        }
    finally:
        conn.close()
    run_broken_links = [seq for seq in result.broken_links if seq in run_seqs]

    return {
        "chain_ok": not run_broken_links,
        # Hash-chain digests span the whole ledger, so a tamper anywhere still
        # flips this — but tampered_events below is scoped to this run alone.
        "digests_ok": result.ok,
        "anchor": {
            "status": "local checkpoint (no external anchor configured)",
            "checkpoints_checked": result.checkpoints_checked,
            "signatures_verified": result.signatures_verified,
        },
        "tampered_events": run_broken_links,
        "notes": result.notes,
    }


@app.get("/api/coriqo/status")
def api_coriqo_status() -> dict[str, Any]:
    """Whether Coriqo sync is on and which agents it mapped — the quickest way
    to tell a misconfigured key from a Coriqo that simply isn't running."""
    return {
        "enabled": coriqo_sync.enabled(),
        "base_url": os.environ.get("BYOAI_CORIQO_URL"),
        "tenant_slug": os.environ.get("BYOAI_CORIQO_TENANT_SLUG"),
        "mapped_agents": _CORIQO_AGENTS,
    }


@app.get("/api/runs/{run_id}/replay")
def api_run_replay(run_id: str) -> dict[str, Any]:
    """Reconstructs a run purely from the sealed ledger — no in-memory
    ``_RUNS`` state involved. Proves the ledger alone is enough to audit a
    run after the fact, e.g. after a process restart."""
    recorder = get_recorder()
    if recorder is None:
        raise HTTPException(status_code=503, detail="recorder disabled (BYOAI_RECORDER_ENABLED != 1)")

    conn = sqlite3.connect(f"file:{recorder.ledger.path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM agent_events WHERE session_id = ? ORDER BY seq ASC", (run_id,)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        raise HTTPException(status_code=404, detail=f"no sealed events for run: {run_id}")

    events = []
    for row in rows:
        payload = json.loads(row["payload"])
        events.append(
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "ts_device": row["ts_device"],
                "tool_name": row["tool_name"],
                "trace_id": row["trace_id"],
                "span_id": row["span_id"],
                "parent_span_id": row["parent_span_id"],
                "payload": payload,
                "entry_hash": row["entry_hash"],
            }
        )

    return {"run_id": run_id, "source": "ledger (not in-memory state)", "event_count": len(events), "events": events}


@app.post("/api/demo/tamper/{run_id}")
def api_demo_tamper(run_id: str) -> dict[str, Any]:
    """Flips one byte in a sealed row's payload, in place in the ledger file,
    to demonstrate detection: /verify goes from green to red for this run.
    Gated behind DEMO_TAMPER=1 — this mutates a supposedly append-only store,
    which is exactly what production deployments must never allow; it exists
    here only to prove the tamper-evidence claim live in a sales demo."""
    if os.environ.get("DEMO_TAMPER") != "1":
        raise HTTPException(status_code=403, detail="tamper demo disabled; set DEMO_TAMPER=1 to enable")
    recorder = get_recorder()
    if recorder is None:
        raise HTTPException(status_code=503, detail="recorder disabled (BYOAI_RECORDER_ENABLED != 1)")

    conn = sqlite3.connect(str(recorder.ledger.path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT seq, payload FROM agent_events WHERE session_id = ? AND kind = 'tool_result' "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"no tool_result event to tamper for run: {run_id}")

        payload = json.loads(row["payload"])
        payload["_tampered_by_demo"] = True
        conn.execute(
            "UPDATE agent_events SET payload = ? WHERE seq = ?",
            (json.dumps(payload), row["seq"]),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "tampered_seq": row["seq"],
        "note": "payload mutated in place without recomputing entry_hash/payload_hash — "
        "call /api/runs/{run_id}/verify to see the chain break at this seq",
    }
