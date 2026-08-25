"""``@governed_tool`` — the seam where a mandate denial actually stops something.

:mod:`byoai.recorder.mandate` decides; this decorator is where the decision has
teeth. The integrator puts it on their own tool functions, and from then on a
:class:`~byoai.recorder.mandate.Deny` means the wrapped function is never
called — not that it is called and its result discarded, and not that the model
is handed an error it can work around.

Three decisions worth stating, because each has a plausible-looking alternative
that quietly breaks enforcement.

**The denial is terminal by construction.** It raises
:class:`~byoai.errors.MandateDeniedError`, whose ``str()`` is the single fixed
:data:`~byoai.recorder.mandate.MODEL_MESSAGE` — because ``str(exc)`` is exactly
what an agent framework feeds back into the model's context. Operator detail
goes to this module's logger at ``WARNING``, on the raising path, so a blocked
call is never silent in the record even before the ledger packet lands. Nothing
about the error invites a retry: no ``retry_after``, ``retryable = False``, and
no provider-error ancestry.

**A flag is not a block.** :class:`~byoai.recorder.mandate.Flag` subclasses
:class:`~byoai.recorder.mandate.Allow`, so the test here is ``verdict.allowed``
and an off-mandate call under ``mandate_enforcement: observe`` runs exactly as
it would have. That is the whole value of ``observe``: a bank sees what would
have been blocked without a rollout that breaks the agent.

**One decorator, both colors.** The tool name, the gate lookup, the verdict
handling and the denial contract are identical for sync and async tools; only
the ``await`` differs. Two decorators would mean two copies of the enforcement
rule and a real chance of them drifting — and the integrator would have to know
which one to reach for, which is a question about their function that
:func:`inspect.iscoroutinefunction` can answer for them.

**Repeats are latched, not re-decided.** The first denial of a tool goes to
:mod:`byoai.recorder.denial_latch`, and every later attempt at that tool in the
same run is refused straight from there — no scope check — until the run passes
the halt threshold and stops entirely with
:class:`~byoai.errors.MandateRunHaltedError`. The model's sentence never
changes through any of it. Bind the run with
:func:`~byoai.recorder.denial_latch.run_scope` when your tool functions do not
receive a trajectory id.

Getting a gate to the decorator
-------------------------------
A tool function is usually defined at import time, far from wherever the gate
gets built and started, and threading a gate parameter through every tool (and
everything that calls one) is the kind of change a team declines to make. So
resolution is layered, most specific first:

1. ``@governed_tool(gate=...)`` — an explicit :class:`MandateGate`, or a
   zero-argument callable returning one (or ``None``), evaluated per call so a
   decorator applied at import time can still see a gate built later;
2. the gate bound to the current context by :func:`use_gate` or
   :func:`set_default_gate`.

The default lives in a :class:`~contextvars.ContextVar`, not a module global.
A ContextVar is naturally per-task and per-thread, so two agents in one process
do not share a mandate, and :func:`use_gate` restores the previous value on
exit — which is what keeps tests from leaking state into each other without a
fixture that remembers to tear anything down.

With no gate anywhere the decorator runs the function and logs one line. That
is the same shape as :func:`~byoai.recorder.mandate.mandate_gate` returning a
no-op gate on an unenrolled host: adopting the decorator is never the thing
that breaks a build.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeAlias, TypeVar, overload

from byoai.errors import MandateDeniedError, MandateRunHaltedError

from .denial_latch import (
    LatchedDenial,
    current_run_id,
    denial_latch,
    resolve_principal,
    resolve_run_id,
)
from .mandate import Deny, MandateGate, ProposedAction, Verdict

__all__ = [
    "GateSource",
    "MandateDeniedError",
    "MandateRunHaltedError",
    "default_gate",
    "governed_tool",
    "set_default_gate",
    "use_gate",
]

log = logging.getLogger(__name__)

#: A gate, something that produces one, or nothing.
GateSource: TypeAlias = "MandateGate | Callable[[], MandateGate | None] | None"

F = TypeVar("F", bound=Callable[..., Any])

_default_gate: ContextVar[MandateGate | None] = ContextVar(
    "byoai_default_mandate_gate", default=None
)

_ungated_logged = False


def set_default_gate(gate: MandateGate | None) -> Any:
    """Bind ``gate`` as the default for :func:`governed_tool` in this context.

    Returns the :class:`~contextvars.Token` so a caller can ``reset()`` it.
    Application startup is the place for this; tests should prefer
    :func:`use_gate`, which resets for them.
    """
    return _default_gate.set(gate)


def default_gate() -> MandateGate | None:
    """The gate currently bound in this context, if any."""
    return _default_gate.get()


@contextmanager
def use_gate(gate: MandateGate | None) -> Iterator[MandateGate | None]:
    """Bind ``gate`` for the duration of the block, then restore the previous one."""
    token = _default_gate.set(gate)
    try:
        yield gate
    finally:
        _default_gate.reset(token)


def _resolve_gate(gate: GateSource) -> MandateGate | None:
    if gate is None:
        return _default_gate.get()
    if isinstance(gate, MandateGate):
        return gate
    resolved = gate()
    if resolved is None:
        return _default_gate.get()
    return resolved


def _log_ungated_once() -> None:
    global _ungated_logged
    if _ungated_logged:
        return
    _ungated_logged = True
    log.info(
        "coriqo: @governed_tool has no mandate gate bound, so tool calls are not "
        "gated. Build one with byoai.recorder.mandate.mandate_gate(agent_id) and "
        "bind it with byoai.recorder.governed_tool.set_default_gate()."
    )


def _arguments(
    signature: inspect.Signature | None, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any] | None:
    """Best-effort named arguments for the record. Never fails a call."""
    if signature is None:
        return None
    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError:
        # Let the wrapped function raise the real, well-worded TypeError.
        return None
    return dict(bound.arguments)


def _check(gate: MandateGate | None, action: ProposedAction) -> Verdict | None:
    """The one place a verdict becomes a consequence.

    Order matters here. The latch is consulted *before* the gate, so a tool
    already denied in this run — and every tool once the run is halted — is
    refused without a scope check. The same action against the same snapshot
    cannot decide differently, so re-evaluating would buy nothing and would let
    a model turn the control into a benchmark.

    The mandate version in hand goes to the latch with the question, because a
    new version is the one thing that *can* change the answer: the latch drops
    the run's buckets and lets the gate decide again.
    """
    if gate is None:
        _log_ungated_once()
        return None

    latch = denial_latch()
    run_id = resolve_run_id(action, gate)
    principal = resolve_principal(gate)
    version = gate.latch_version
    latched = latch.check(run_id, principal, action.tool, version)
    if latched is not None:
        raise _denial(latched.verdict, run_id=run_id, latched=latched)

    verdict = gate.decide(action)
    if verdict.allowed or not isinstance(verdict, Deny):
        return verdict
    raise _denial(
        verdict,
        run_id=run_id,
        latched=latch.record(run_id, principal, verdict, version),
    )


def _denial(
    verdict: Deny, *, run_id: str, latched: LatchedDenial
) -> MandateDeniedError:
    """Build the exception a denial raises — halting or not.

    Both carry the same fixed sentence for the model. The difference is the
    type, which is for the loop above: ``MandateRunHaltedError`` says stop
    scheduling turns for this run, an ordinary denial says only that this tool
    is refused.
    """
    if latched.halted:
        error: MandateDeniedError = MandateRunHaltedError(
            verdict, run_id=run_id, attempts=latched.attempts
        )
    else:
        error = MandateDeniedError(verdict)
    # Logged here rather than left to the caller: a blocked call that nobody
    # writes down is indistinguishable from one that never happened. The
    # attempt count rides along, because "the fourth try at the same denied
    # tool" is the fact worth having and a bare denial line does not carry it.
    log.warning(
        "coriqo: blocked tool call - %s run_id=%s attempts=%d%s",
        error.operator_detail,
        run_id,
        latched.attempts,
        " HALTED" if latched.halted else "",
    )
    return error


@overload
def governed_tool(fn: F) -> F: ...


@overload
def governed_tool(
    *,
    name: str | None = ...,
    gate: GateSource = ...,
    capture_arguments: bool = ...,
) -> Callable[[F], F]: ...


def governed_tool(
    fn: F | None = None,
    *,
    name: str | None = None,
    gate: GateSource = None,
    capture_arguments: bool = False,
) -> Any:
    """Enforce the agent's approved mandate around a tool function.

    Usable bare (``@governed_tool``) or called (``@governed_tool(name="rm")``),
    on sync or async functions alike.

    Args:
        name: the tool name Coriqo knows this by. Defaults to ``fn.__name__``,
            which is right whenever the Python function and the tool the model
            sees are named the same thing — and overridable for when they are
            not, which is common once a framework prefixes or namespaces them.
        gate: a :class:`~byoai.recorder.mandate.MandateGate`, or a zero-argument
            callable returning one, for this tool only. Omit it to use whatever
            :func:`use_gate` / :func:`set_default_gate` bound.
        capture_arguments: bind the call's arguments onto the
            :class:`~byoai.recorder.mandate.ProposedAction` so a later packet
            can record *what* was attempted, not only which tool.

            Off by default, deliberately. A tool's arguments hold whatever the
            caller passed it, and for a governed tool that routinely includes
            account numbers, customer identifiers and credentials. Capturing
            them by default would mean a package whose entire premise is a
            defensible record quietly collecting secrets that nothing has
            redacted yet. Turn it on per tool once the arguments are known to
            be safe, or once verdict recording routes them through
            ``recorder/redact.py`` — until then the verdict carries the tool
            name, which is what the mandate decision is actually about.

    Raises:
        MandateDeniedError: the mandate denied the call. The wrapped function
            was not invoked.
    """

    def decorate(target: F) -> F:
        tool = name or target.__name__
        signature: inspect.Signature | None = None
        if capture_arguments:
            try:
                signature = inspect.signature(target)
            except (TypeError, ValueError):  # pragma: no cover - exotic callables
                signature = None

        if inspect.iscoroutinefunction(target):

            @functools.wraps(target)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _check(
                    _resolve_gate(gate),
                    ProposedAction(
                        tool=tool,
                        trajectory_id=current_run_id(),
                        arguments=_arguments(signature, args, kwargs),
                    ),
                )
                return await target(*args, **kwargs)

            wrapper: Any = async_wrapper
        else:

            @functools.wraps(target)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                _check(
                    _resolve_gate(gate),
                    ProposedAction(
                        tool=tool,
                        trajectory_id=current_run_id(),
                        arguments=_arguments(signature, args, kwargs),
                    ),
                )
                return target(*args, **kwargs)

            wrapper = sync_wrapper

        # Introspection-based frameworks read these off the wrapper to build the
        # tool schema they hand the model, so the governed tool has to look
        # exactly like the ungoverned one.
        wrapper.__signature__ = signature or inspect.signature(target)
        wrapper.byoai_tool_name = tool
        return wrapper  # type: ignore[return-value]

    if fn is not None:
        return decorate(fn)
    return decorate
