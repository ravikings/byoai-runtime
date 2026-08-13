# ByoAI Demo Agent Showcase

Client-facing demo: enterprise agents captured live by byoai-runtime, sealed
by the Coriqo agent recorder, verifiable after the fact. Full spec:
`internal_doc/demo_agent_showcase_spec.md` (gitignored, internal).

**Status: M5 — feature-complete.** All milestones (M1-M5) landed: 9 agents
(8 real + 1 deliberate misfire demo), sub-agent spans, the UI, verify,
sealed replay, the tamper demo, and a Coriqo shipping smoke test. See
[`DEMO.md`](./DEMO.md) for the sales walkthrough script.

**Status detail — M2, all 8 agents.** All four banking agents (B1 Fraud Triage, B2
KYC Onboarding, B3 Dispute Resolution, B4 Loan Prequalification) and all four
healthcare agents (H1 Prior Authorization, H2 Clinical Documentation, H3
Patient Intake, H4 Claims Denial Appeal) run end-to-end and are sealed by the
recorder, with a verify endpoint. B2, B4, and H1 each delegate one step to a
sub-agent (Sanctions Screener, Document Extractor, Criteria Matcher
respectively); the recorder attributes each sub-agent's events to a distinct
span sharing the parent run's `trace_id`, with `parent_span_id` set to the
parent's `span_id`.

H1-H4 (`provider="openai"`) make real OpenAI chat-completions calls via
`OpenAICompatProvider` when `OPENAI_API_KEY` is set. The recorder's extractor
only understands Anthropic-shaped request/response bodies, so the OpenAI live
path normalizes each turn into that shape (text/tool_use content blocks)
before sealing it — the actual wire call is genuine OpenAI; only what's fed
to the recorder is re-shaped so one extractor covers both providers.

**M3 — UI.** A static single-page UI (`ui/`, served at `/`) lists the agent
gallery, runs an agent, streams its timeline live over SSE, renders the span
tree (root + sub-agent spans), and calls `/verify` on demand.

**Misfire demo (B5).** `b5-misfire-demo` has the exact same declared tool
schema as B1 Fraud Triage, but its fallback transcript calls
`initiate_wire_transfer` — a tool that was never offered to the model,
simulating a mis-fired or prompt-injected action. The recorder seals that
event exactly like any other (capture doesn't care about scope); the demo
app then flags it after the fact by diffing the tool call against the
agent's declared schema (`AgentDef.is_out_of_scope`, `runner.py`
`_tool_use_data`, exposed as `policy_violations`/`flagged` on
`/api/runs/{run_id}`) and highlights it in the UI. `/verify` still reports a
clean chain — the point isn't that the ledger breaks, it's that the audit
trail makes the off-scope action undeniable and inspectable.

**M4 — replay, tamper demo.** `GET /api/runs/{run_id}/replay` reconstructs a
run purely from the sealed ledger (queries `agent_events` by `session_id`,
no `_RUNS` in-memory state involved) — proves the ledger alone is sufficient
to audit a run, e.g. after a process restart. `POST
/api/demo/tamper/{run_id}` (gated behind `DEMO_TAMPER=1`; 403 otherwise)
flips a sealed row's payload in place without recomputing its hash, so a
follow-up `/verify` visibly flips from green to red for that run — the UI's
verify panel has a "Tamper demo" button that does exactly this live.

**M5 — Coriqo shipping smoke test, polish.**
`tests/test_coriqo_shipping.py` runs a real agent through the app (sealing
real events into the real local ledger), then hands that ledger + device key
to byoai-runtime's public `Shipper` class against a local double standing in
for Coriqo's `/v1/ingest/batch` (`httpx.MockTransport` — no live Coriqo
credentials needed to prove the wiring). Confirms the batch is signed
(`ed25519:`-prefixed device signature), gzip-encoded, accepted, and the
ledger's sync watermark advances — and that a second ship attempt finds
nothing left to send. Acceptance criteria (spec §9) all verified: 8 agents
complete end-to-end with correct span parenting, event counts match the
ledger, verify flips red on tamper and names the exact seq, replay survives
`_RUNS` being wiped, fallback engages on API outage, and cold start is well
under a second (`uvicorn ... ` to first successful request).

All data is synthetic — no real customers, patients, or institutions.

## Quick start

```bash
./start.sh
```

Installs `byoai-runtime` editable (`fastapi` + `recorder` extras) from the
repo root and launches uvicorn with the recorder on by default
(`BYOAI_RECORDER_ENABLED=1`), so every run is sealed to the ledger. Check
with `curl -s localhost:8000/api/runs/<run_id>/verify | python -m json.tool`.
Set `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` first for live model calls; without
them agents fall back to cached transcripts. See `start.sh` for the rest of
the env vars it reads.

## Setup (manual)

```bash
pip install 'byoai-runtime[fastapi,recorder]' uvicorn
export ANTHROPIC_API_KEY=sk-ant-...        # optional — powers B1-B4 live;
                                            # falls back to a cached transcript
                                            # if unset/unavailable
export OPENAI_API_KEY=sk-...               # optional — same, for H1-H4
export BYOAI_RECORDER_ENABLED=1
export BYOAI_RECORDER_DIR=~/.byoai/demo    # optional, defaults to ~/.byoai/recorder
export BYOAI_DEMO_AUTOPILOT=1              # optional — pings a random agent
                                            # every 1.5-4min to keep the demo alive
```

## Run

```bash
uvicorn examples.agent_showcase.app:app --reload
```

## Try it

Open http://localhost:8000/ for the UI, or drive the API directly:

```bash
curl -s localhost:8000/api/agents | python -m json.tool

run_id=$(curl -s -X POST localhost:8000/api/agents/b2-kyc-onboarding/run | python -c 'import json,sys;print(json.load(sys.stdin)["run_id"])')

curl -s localhost:8000/api/runs/$run_id | python -m json.tool

curl -s localhost:8000/api/runs/$run_id/verify | python -m json.tool
```

## Env vars

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Live model calls for B1-B4. If unset or the call fails, the agent replays its recorded fallback transcript (`fallbacks/`) instead — the run still completes and the recorder still seals real events. |
| `OPENAI_API_KEY` | Live model calls for H1-H4, same fallback behavior as above. |
| `BYOAI_RECORDER_ENABLED` | `1` to capture/seal every run to the local ledger. Without it, agents still run but nothing is sealed and `/verify` reports the recorder as disabled. |
| `BYOAI_RECORDER_DIR` | Ledger/device-key directory. Defaults to `~/.byoai/recorder`. |
| `BYOAI_DEMO_AUTOPILOT` | `1` to start a background loop that runs a random agent every 90-240s, mimicking real bank/healthcare traffic. Off by default so importing the app (tests, one-off runs) never spends API credits on its own. |
| `DEMO_TAMPER` | `1` to enable `/api/demo/tamper/{run_id}`, which flips a byte in a sealed row to demonstrate `/verify` catching it. |
| `BYOAI_DEMO_STEP_DELAY_MS` | Milliseconds to pause between fallback-transcript replay steps. Fallback replay has no real network latency, so without this every step fires in milliseconds — too fast for the UI's heartbeat/active-card/workflow indicators to be seen. `start.sh` sets this to `400` by default; `0` (the library default) disables pacing, which is what the test suite runs with. |

`/api/agents` reports each agent's current `"live"` status — `true` if its provider's API key is set, `false` if it's running off the cached transcript.

### Live-call TTL and misfire injection

`POST /api/agents/{agent_id}/run` takes two optional query params:

- `force_live=true` — bypass the 1-day live-call TTL (below) and always attempt a live call for this run.
- `inject_misfire=true` — skip the live call entirely and force the fallback path, simulating a provider misfire (timeout, bad response) on demand. The run's events include a `"misfire"` event and `run_complete.data.mode` is `"misfire"`.

Once an agent has been called live, repeat runs of that same agent within **24 hours** replay its cached fallback transcript instead of spending API credits again (`runner.LIVE_CALL_TTL_SECONDS`, in-memory per process, resets on restart). `run_complete.data.mode` reports which path a run took: `"live"`, `"replay"` (live call failed), `"cached"` (TTL cooldown), or `"misfire"` (injected).

```bash
curl -s -X POST "localhost:8000/api/agents/b1-fraud-triage/run?inject_misfire=true" | python -m json.tool
```

## Tests

```bash
pytest examples/agent_showcase/tests
```
