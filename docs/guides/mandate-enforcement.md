# Mandate enforcement

Your agent has an approved list of tools it is allowed to call. This guide covers how the
runtime holds it to that list, and what happens when it tries something outside it.

The decision is made locally. The runtime keeps a cached copy of the agent's mandate and
refreshes it in the background, so a tool call never waits on a network round trip to
Coriqo, and Coriqo being slow or unreachable never stalls your agent.

## Before you start

You need an enrolled device. Enrolment gives this host an Ed25519 key that signs every
governance request, which is what ties a decision to a machine rather than to a shared
secret in an environment variable:

```bash
pip install "byoai-runtime[recorder]"
byoai-recorder-enroll --coriqo-url https://coriqo.example.com --token cik_live_xxxxx
```

The static `BYOAI_CORIQO_API_KEY` credential still works for publishing records, but it
cannot enforce. If you try, you get `EnforcementIdentityUnavailableError` naming the
enrolment command rather than a request that fails later for an unclear reason.

## Decorating a tool

```python
from byoai.recorder.governed_tool import governed_tool, set_default_gate
from byoai.recorder.mandate import mandate_gate

gate = mandate_gate("agt_7f3c")      # the agent id Coriqo registered
set_default_gate(gate)
await gate.start()                    # begins the background refresh

@governed_tool
def wire_transfer(account: str, amount: int) -> str:
    ...
```

`wire_transfer` now checks the mandate before it runs. If the mandate does not cover it,
the function body never executes and the call raises `MandateDeniedError`.

The decorator works on sync and async functions, keeps the wrapped function's name,
signature and docstring, and takes `name=` when your framework prefixes tool names.

If no gate is bound, or no device is enrolled, the decorator logs one line and calls the
function. You can add it to your codebase before anyone has enrolled anything.

### Binding a gate per task

`set_default_gate` binds a `ContextVar`, so two agents running in one process do not share
a mandate. For a scoped binding, use the context manager:

```python
with use_gate(other_agents_gate):
    result = wire_transfer("ACC-1", 500)
```

## What the gate decides

Two separate settings, controlled in Coriqo, answer two different questions.

`mandate_enforcement` answers *does an out-of-scope call count as a violation*:

- `enforce` — deny it
- `observe` — allow it, and record it as flagged

`enforcement_posture` answers *what to do when the runtime cannot tell*, which happens when
the cached snapshot has aged past its staleness budget or was never fetched at all:

- `fail_open` — allow, and flag
- `fail_closed` — deny

Together:

| Situation | `fail_open` | `fail_closed` |
|---|---|---|
| Snapshot fresh, tool in scope | Allow | Allow |
| Snapshot fresh, out of scope, `enforce` | Deny | Deny |
| Snapshot fresh, out of scope, `observe` | Allow + flag | Allow + flag |
| Snapshot stale past its budget | Allow + flag | **Deny** |
| Agent suspended | **Deny** | **Deny** |
| No snapshot ever fetched | Allow + flag | **Deny** |

Two rows are worth reading twice. A suspended agent is denied under both postures, because
a suspension you know about outranks a staleness you are unsure about. And it is *staleness*
that triggers the fail-closed branch, not network reachability: a failed refresh leaves a
still-valid snapshot in place, so one blip does not take your agent offline. The failure is
bounded by a number you configured rather than by whatever the network did.

## Reading a verdict directly

Most code should use the decorator. When you want the decision without the raise:

```python
verdict = gate.decide("wire_transfer")
if verdict.allowed:
    ...
```

`Allow`, `Flag` and `Deny` all carry `reason`, `mandate_version_id` and `snapshot_age_s`.
`Flag` subclasses `Allow`, so `verdict.allowed` and `isinstance(verdict, Allow)` agree: a
flagged call still runs, and only the record differs.

## Denials do not come back as tool errors

A denied call raises `MandateDeniedError`, which is deliberately not shaped like a failure
worth retrying. It carries no `retry_after`, is not a `ProviderError`, and its message is
one fixed sentence that is identical whatever the reason was.

That last part matters more than it looks. If a denial reaches the model as an ordinary
tool error carrying detail, the model will read the detail, rephrase, try an adjacent tool,
and work its way around the control, because that is what a capable model does with a
failure it understands. So the model gets one opaque sentence, and everything an operator
needs sits on `exc.verdict` and `exc.operator_detail` for your logs.

Keep that separation when you handle the error. This one line undoes the design:

```python
except MandateDeniedError as e:
    return f"error: {e.verdict.detail}"     # don't: hands the model the reason
```

The reprs of `Verdict` and `ProposedAction` redact `detail` and `arguments` for the same
reason, so an accidental f-string or a traceback does not leak them either.

## Tool arguments are not captured by default

The verdict records which tool was attempted, not what it was called with. Turn capture on
per tool when the arguments are safe to keep:

```python
@governed_tool(capture_arguments=True)
def search(query: str, limit: int = 10) -> list[str]:
    ...
```

It is off by default because a governed tool's arguments tend to be exactly the things you
would not want sitting in a record: account numbers, customer identifiers, credentials.
Nothing redacts them yet.

## An empty tool list is not the same as no tool list

On the wire, `allowed_tools: null` means the agent is unrestricted, and `allowed_tools: []`
means it may call nothing at all. Opposite meanings, and both are valid.

The runtime distinguishes them structurally: it branches on `is None` rather than on
truthiness, and rejects a bare string instead of iterating it into single characters. If
you write your own client against Coriqo's mandate endpoint, do the same. A falsy check
turns "permitted nothing" into "permitted everything".

## Repeat attempts are latched

A denial stops one call. On its own that is not much of a control: an agent that
ignores the message can attempt the same refused tool for as long as its loop
keeps turning, and your logs fill with identical single denials that nothing ties
together. The interesting fact — *this agent went at a control it had already
been refused, four times in ninety seconds* — is the one a risk committee asks
about, so the runtime keeps it.

The latch is keyed on the run, the agent asking, and the tool:

- the first denial is remembered;
- every later attempt at that tool in that run is refused **from the latch**,
  without re-running the scope check — the same tool against the same snapshot
  cannot answer differently, so re-evaluating would be theatre;
- at the third attempt (counting the first denial) the run is halted. Every
  subsequent call in that run, including tools that were in scope all along,
  raises `MandateRunHaltedError`.

```python
from byoai.errors import MandateDeniedError, MandateRunHaltedError
from byoai.recorder.denial_latch import run_scope

with run_scope(trajectory_id):
    try:
        result = wire_transfer("ACC-1", 500)
    except MandateRunHaltedError as exc:
        stop_the_run(exc.run_id, attempts=exc.attempts)
    except MandateDeniedError:
        try_something_else()
```

`MandateRunHaltedError` derives from `MandateDeniedError`, so a handler you have
already written keeps stopping the call. The distinction is for the loop above
it, not for the model: `isinstance` (or `exc.halted`) separates "this tool is
refused, try another" from "stop scheduling turns for this run".

The model's sentence does not change through any of it. First denial, fourth
repeat, halt — the same fixed sentence every time. Escalating the detail as the
agent tries harder would be handing it a hint sheet at exactly the moment it is
probing the control. The count goes to the operator instead, in the `WARNING`
logged on the raising path.

Set the threshold with `BYOAI_MANDATE_HALT_THRESHOLD`, or `DenialLatch(threshold=…)`
in code. The process-wide latch reads the variable at import, so set it before
importing the runtime.

### What is not latched

Only denials about *scope*. A suspension is lifted, a stale snapshot refreshes, a
first snapshot arrives — all three deny under `fail_closed` and all three are
temporary. Remembering them would turn one refresh blip into a permanently halted
run, which is the failure the refresh path is built to avoid.

And a new mandate version clears that agent's buckets, its halt included. The
latch's licence to refuse without looking is that the answer cannot have changed;
a new version is exactly the case where somebody changed it. It is tracked per
agent rather than per run, because a run holds a delegator and its delegated
child at two different versions, and tracking it per run would have each of them
wipe the other's count on every call — which would hand any agent with a
sub-agent a halt it can never reach.

### Which run, and which agent

The run is `ProposedAction.trajectory_id` when your tools carry one. Failing
that, `run_scope()` binds one for a block of calls. Failing that, the runtime
falls back to an id belonging to the `MandateGate` itself — one gate is built per
agent per run in every wiring this package encourages, and two gate objects are
two different mandates, so unrelated agents in one process never halt each other.
The imprecision runs the safe way round: an application that reuses one
long-lived gate across several sequential runs shares one bucket until it names
the runs, which latches too eagerly rather than too late.

Buckets carry the agent as well as the run, because a delegated sub-agent and its
delegator share a run and do not share a scope. A child's denial must not latch a
tool the parent may still legitimately call. The halt is the exception, and is
deliberately run-wide.

### The limit worth knowing

Latch state lives in process memory. A run that spans two processes, and a host
that restarts mid-run, both start counting from zero. Nothing persists it yet, so
an agent that can get itself restarted can reset its own count — treat the halt
as a control against a model grinding away inside one run, not as one against an
orchestrator that respawns workers.

A latch also holds a bounded number of runs (1024 by default) and drops the
oldest first, so a long-lived process does not accumulate a bucket per run. A run
that has been idle for that many runs and then comes back starts counting again.

## Delegation narrows, it never widens

When agent A hands work to agent B, B has no mandate of its own for that run —
nobody approved B to act inside A's task. Its effective scope is the intersection
of B's own mandate with A's effective scope at the moment of delegation, pinned
to A's `mandate_version_id`.

```python
from byoai.recorder.delegation import delegated_gate
from byoai.recorder.governed_tool import use_gate

child = delegated_gate(parent_gate, child_gate)
with use_gate(child):
    await sub_agent.run(task)
```

This is not the same thing as a *nested* run. A sub-run of the same agent is
still that agent, and its one mandate already scopes it; nothing attenuates.
Attenuation is about two different agents, and it is one-directional on purpose.
If delegation could widen, "ask another agent to do it" would be the cheapest way
around a denial, and a model that has just been refused is one prompt away from
trying it.

B's standing mandate is untouched. It is still B, with its own scope, the moment
it runs its own work.

`DelegatedGate` also keeps A's gate and asks it first, so suspending A, or
narrowing A's mandate, stops B within one refresh interval. The pinned scope is a
photograph of the moment of delegation, and a photograph cannot notice a
revocation.

Two dials attenuate along with the tools. Whether a delegated breach blocks or
only flags is read off the *delegator's* `mandate_enforcement`, and any `enforce`
up the chain enforces — otherwise handing work to a sub-agent that happens to be
in observe rollout would be a working way around the parent's mandate. And
`max_delegation_depth` is the tightest limit anywhere up the chain, so a middle
agent cannot raise a bound the root set.

Two fields on the snapshot govern the hand-off. `delegation_policy` must be
`attenuated`; `none` — and anything absent or unrecognised, because a snapshot
that does not say delegation was approved has not approved it — refuses with
`DelegationRefusedError`. `max_delegation_depth` bounds the chain, where `0`
forbids delegation outright and `null` leaves the policy as the only gate.

A delegation Coriqo was never told about gets the empty scope rather than a
refusal: it denies every tool under `enforce`, which is the same practical
outcome, but as a decision the record can explain instead of a crash in your
spawn path.

Intersecting is where the null-vs-empty rule earns its keep. `allowed_tools: null`
is unrestricted and contributes no restriction, so unrestricted ∩ X is X. `[]`
permits nothing and survives every intersection, so anything ∩ `[]` is `[]`. The
runtime branches on `is None`; if you write your own, do the same, because a
falsy check turns "permitted nothing" into "permitted everything".

## Recording what the gate decided

Enforcement without a record is a claim. Bind a `VerdictRecorder` and every gate
decision — `allowed`, `flagged` and `blocked` alike — is appended to the local
hash-chained ledger and queued for Coriqo:

```python
from byoai.recorder.ledger import Ledger
from byoai.recorder.verdicts import (
    VerdictOutbox, VerdictRecorder, VerdictShipper, set_verdict_recorder,
)

ledger = Ledger("~/.byoai/recorder/ledger.db", device_id)
outbox = VerdictOutbox("~/.byoai/recorder/verdicts.db")
set_verdict_recorder(VerdictRecorder(ledger=ledger, outbox=outbox))

await VerdictShipper(client, outbox).drain()   # batches of up to 200
```

Four things about it are worth stating plainly.

**Allows are recorded too.** The denominator is what makes the numerator mean
anything: "4,120 tool calls, 9 outside the mandate" is a sentence a second-line
reviewer can act on, and "9 denials" is not.

**The record does not depend on Coriqo.** The ledger write happens first and
shipping is downstream of it, so a denial during an outage is written down like
any other. A failed batch keeps its `batch_key` and is resent as the same batch,
so retrying cannot produce a second governance record; a repeat replays the
stored result instead.

**Recording is not on the decide path.** `decide()` still reads memory and
returns, with no I/O of any kind — the recorder runs at `@governed_tool`'s
enforcement seam, after the verdict exists. Recording never raises into a tool
call either.

**A repeat reads as a repeat.** The first denial records `out_of_scope`, the
next attempt `repeat_denied` with `attempts: 2`, the one that trips the
threshold `run_halted` with `halted: true`. So *the agent went at a control it
had already been refused, three times, then the run stopped* is legible off the
record rather than being three rows that look the same.

If Coriqo's answer reports `stale_mandate_version_count`, this host's snapshot
has drifted from the version the batch was anchored on. The verdicts are still
recorded — the drift is the finding, not a reason to reject them — and the
shipper logs it so you learn from the reply rather than from the chain.

## Not built yet

Two things you may expect are still missing, and it is better to know now than to
discover them in a demo:

- **The local ledger has no destination yet.** Verdicts reach Coriqo through the
  verdict endpoint, but the ledger underneath them — the hash-chained record the
  shipper posts to `/v1/ingest/batch` — has no server-side implementation, so
  those uploads have nowhere to land. Your host keeps a complete, verifiable
  record; Coriqo currently sees the verdicts and not the events behind them.
  Practically: `coriqo-verify` against a local export works, gap detection from
  the Coriqo console does not.

- **Tool arguments are not part of the record.** `capture_arguments=True` binds
  them onto the `ProposedAction`, but the verdict record keeps only how many
  were captured and never a key or a value, locally or on the wire, because
  nothing redacts them yet. Until it does, the record says which tool was
  attempted, not with what.
