# ByoAI Demo Agent Showcase — sales walkthrough

A ~10-minute script for demonstrating that byoai-runtime + the Coriqo agent
recorder capture and seal *every* tool call and model message an agent
makes — tamper-evidently, verifiably, and even when the agent misbehaves.

All data is synthetic. No real customers, patients, or institutions.

## Setup (before the room fills up)

```bash
cd examples/agent_showcase
export BYOAI_RECORDER_ENABLED=1
export DEMO_TAMPER=1          # enables the live tamper demo — leave this off outside a demo
# ANTHROPIC_API_KEY optional: unset means every run uses the cached fallback
# transcript, which is actually the more reliable choice for a live demo —
# no dependency on network or a model API being up mid-pitch.
uvicorn examples.agent_showcase.app:app --port 8001
```

Open http://localhost:8001/ in one browser tab. Cold start is under a
second — there's no setup delay to fill with small talk.

## 1. Frame the problem (30s)

"Every one of these agents can call tools that touch real money or real
patient records. If something goes wrong — a bad decision, a rogue tool
call, a compliance question six months later — can you *prove* what the
agent actually did? Most agent stacks can't. This one can."

## 2. Run a normal agent end-to-end (2 min)

Click **B1 Fraud Triage** in the gallery (banking).

- Point out the live timeline populating over SSE as the agent pulls the
  transaction, checks history, runs a geo/velocity check, and calls
  `flag_decision`.
- Point out the **span tree** panel — one root span, this agent's whole run.
- Point out the **Outcome** panel — the final decision with cited rationale.

"Every one of those steps — every tool call, every tool result, every
message — was sealed into a local, hash-chained ledger as it happened, not
after the fact."

## 3. Show sub-agent attribution (2 min)

Click **B2 KYC Onboarding**.

- This agent delegates sanctions screening to a separate sub-agent. Point
  out the **span tree** now shows a root span plus a child span, correctly
  parented (`parent_span_id` = the root's `span_id`).
- "This is what lets you audit not just 'the agent did X' but 'this
  *specific sub-agent*, invoked by this parent, did X' — which matters the
  moment you have agents calling agents, which is where this is all
  heading."

(B4 Loan Pre-Qualification and H1 Prior Authorization show the same
pattern if you want a second example, including a mixed-provider one — H1
is labeled as an OpenAI-backed agent.)

## 4. Verify the ledger (1 min)

Still on any completed run, click **Verify sealed ledger**.

- "Chain intact, digests ok — this recomputes every hash link in the ledger
  independently. It doesn't trust what's stored; it re-derives it."

## 5. The misfire — the moment that lands (3 min, the centerpiece)

Click **Fraud Triage (misfire demo)** (B5, tagged "misfire demo").

- Let it run. The timeline shows the usual triage steps, then an
  **amber-highlighted** entry: `initiate_wire_transfer`, marked "⚠
  off-scope."
- Open the **Policy violations** panel: it names the exact tool, and the
  exact reason — that tool was never in this agent's declared tool schema.
  The agent (or something that hijacked its output — a prompt injection is
  the realistic version of this in production) fired a real, consequential
  action it was never granted.
- "Here's the part that matters: the recorder didn't stop it, and it
  didn't miss it either. It sealed it — exactly like every other event.
  Detection and flagging happen *after* capture, by diffing against the
  agent's declared contract, which means this works even for failure modes
  nobody anticipated when the agent was built. Click Verify — the chain is
  still green. This isn't a system that breaks when something goes wrong;
  it's a system that makes 'something went wrong' undeniable and
  inspectable, in a scenario where most stacks would have no record at
  all."

## 6. Prove tampering doesn't hide (2 min)

On any completed run, click **Tamper demo**.

- The button mutates one sealed row's payload directly in the ledger file,
  without touching its hash — simulating an insider or an attacker editing
  the log after the fact.
- Click **Verify sealed ledger** again: it flips from green to **red**,
  naming the exact broken sequence number.
- "This is the actual value proposition: not 'we log things,' but 'you
  cannot rewrite history without us knowing exactly where you did it.'"

## 7. Sealed replay (1 min, optional — technical audiences)

`curl localhost:8001/api/runs/<run_id>/replay | python -m json.tool`

"This reconstructs the entire run from the ledger alone — no dependency on
the app process's memory. Restart the server, kill the demo, come back a
year later: the ledger is the source of truth, not some in-memory cache."

## 8. The agent you don't run (3 min — the AWS conversation)

Run **B6 Sanctions Review (AWS Bedrock Agent)**.

"Everything so far was our loop. This one is a managed AWS Bedrock Agent —
AWS builds the prompts, picks the tools, calls the Lambdas, decides when to
stop. We are not in the path and there is no path to be in. So where does
evidence come from?"

Point at the timeline as it fills:

"From the agent's own trace. AWS emits one for every orchestration step, and
we normalize it into the same sealed events you just watched — same ledger,
same hash chain, same verify."

Then the two events at the end, in this order:

1. **The guardrail intervention.** "That's AWS's own control firing. We record
   that it fired. Good — that's their control working, and now it's in the
   same evidence chain as everything else."
2. **The flagged wire transfer.** "And this one the guardrail let straight
   through. The agent asked the caller to run `initiate_wire_transfer` — for
   the payment it had just decided to hold — using a tool outside its declared
   action groups. A guardrail scores content. It does not check authority."

The line to land: "That call is a `returnControl` action, which means it runs
in *your* process, not in AWS's. It is in no CloudWatch log. If we weren't
sealing this trace, there would be no record anywhere that it was ever
requested."

If they ask whether it works on their agents: yes, `enableTrace=True` on the
invoke and their agent id — no change to the agent, the Lambdas, or the
prompts. Say the limit too, because their security team will find it: we see
what the trace says, and a caller who invokes without tracing leaves nothing
behind. That flag belongs to them, unlike the proxy, which cannot be switched
off from inside an agent.

## Closing line

"Ten production-shaped agents, two industries, real tool-calling loops, one
deliberately misbehaving, and one running inside AWS where we never touch it
— and in every case, the full record survives, verifies, and tells you
exactly what happened. That's what you're buying: not fewer agent failures,
but zero blind spots when they happen."

## If something goes wrong live

- No network / model API down: this is actually fine — every agent falls
  back to its cached transcript automatically (you'll see an amber
  "model API unavailable" event in the timeline) and the run still
  completes and still seals real events. Mention this as a feature, not an
  apology.
- Forgot `DEMO_TAMPER=1`: the tamper button returns a 403 with a clear
  message; restart with the env var set, or skip §6 for this run.
- No AWS account for §8: that is the default and nothing needs saying. B6
  replays a recorded Bedrock trace through the same normalizer the live path
  uses, so the timeline, the guardrail event and the flagged transfer are all
  real output from real code. Only the input is recorded. If asked, say so
  plainly — it holds up better than hedging.
- Want a clean ledger for the next room: stop the server, delete the
  `BYOAI_RECORDER_DIR` (defaults to `~/.byoai/recorder`), restart.
