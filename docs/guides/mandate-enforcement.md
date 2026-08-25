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

## Not built yet

Two things you may expect are still missing, and it is better to know now than to discover
them in a demo:

- **Repeat attempts are not counted.** A denial stops one call. A model that ignores the
  message can attempt the same denied tool indefinitely, and nothing yet halts the run or
  raises a finding.
- **Verdicts are logged, not sealed.** They do not reach the local ledger or Coriqo yet, so
  a denial is visible in your process logs and nowhere else.
