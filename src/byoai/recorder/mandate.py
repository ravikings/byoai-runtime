"""The local mandate gate: does this agent's approved scope permit this tool?

Coriqo holds the approved mandate. This module enforces it *here*, inside the
agent's own process, from a cached snapshot refreshed on a background interval.

The one constraint everything else follows from: **the decide path performs no
network I/O.** Asking Coriqo synchronously before every tool call would make
the agent's availability a function of Coriqo's, and no regulated buyer ships
an agent that stops working because a governance service is redeploying. So
:meth:`MandateGate.decide` reads an immutable in-memory snapshot and returns;
:meth:`MandateGate.refresh` is what talks to the network, on a schedule
comfortably inside the snapshot's own ``max_staleness_s``.

Two server fields, two different questions
------------------------------------------
``mandate_enforcement`` (``enforce`` | ``observe``) answers *does a scope
breach count as a violation* — it is the tenant's rollout dial, and ``observe``
is how a bank watches what an agent would have been blocked from doing before
turning the block on.

``enforcement_posture`` (``fail_open`` | ``fail_closed``) answers a question
about this runtime, not about the agent: *what happens when we cannot
evaluate*. They are not the same dial and collapsing them loses the ability to
run an observing agent under a fail-closed posture, which is exactly the
configuration a first rollout wants.

============================================  ==============  ===============
Situation                                     fail_open       fail_closed
============================================  ==============  ===============
fresh, tool in scope                          allow           allow
fresh, out of scope, ``enforce``              deny            deny
fresh, out of scope, ``observe``              allow + flag    allow + flag
stale past ``max_staleness_s``                allow + flag    **deny**
agent suspended                               **deny**        **deny**
no snapshot ever fetched                      allow + flag    **deny**
============================================  ==============  ===============

Suspension denies under both postures: a suspension is a decision Coriqo
already made and this runtime read, not a failure to evaluate. And what trips
the fail-closed branch is *staleness*, not network reachability — a bounded,
configured budget rather than an incidental one. A gate that denied the moment
a request failed would turn every blip into an outage; this one keeps serving
the snapshot it has until that snapshot is older than the tenant said it may
be.

``allowed_tools``: null is not empty
------------------------------------
``null`` means **unrestricted** — no scope constraint on this agent.
``[]`` means **nothing is permitted**. Both are valid on the wire and they are
opposite instructions, so :class:`MandateSnapshot` keeps them apart (``None``
vs ``()``) and never uses a falsy test to tell them apart. Coercing ``null``
into ``[]`` blocks a legitimately unrestricted agent; coercing the other way
opens an agent that was explicitly permitted nothing.

Denials are terminal, and quiet
-------------------------------
A :class:`Deny` is not a tool error. If a denial reaches the model as an
ordinary failure — worse, one naming the tool and the scope — the model does
what a competent agent should do with a failure: rephrase, try an adjacent
tool, and route around the control. So a ``Deny`` carries a single fixed
:data:`MODEL_MESSAGE` for anything the model may see, and everything an
operator needs (which tool, which mandate version, how stale, why) stays in
:attr:`Verdict.detail` and the logs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from byoai.errors import ByoAIError

from .identity import CoriqoIdentity, resolve_identity

__all__ = [
    "BLOCKING_STATUSES",
    "DEFAULT_MAX_STALENESS_S",
    "MODEL_MESSAGE",
    "Allow",
    "Deny",
    "Enforcement",
    "Flag",
    "MandateGate",
    "MandateSnapshot",
    "Posture",
    "ProposedAction",
    "Reason",
    "Verdict",
    "coriqo_mandate_source",
    "mandate_gate",
]

log = logging.getLogger(__name__)

#: Staleness budget used until a snapshot names its own ``max_staleness_s``.
DEFAULT_MAX_STALENESS_S = 300.0

#: Fraction of the staleness budget the refresh interval targets. Half leaves
#: room for one whole failed refresh (plus its retries) before the snapshot
#: a fail-closed agent depends on ages out.
REFRESH_FRACTION = 0.5

#: Floor on the refresh interval, so a tenant setting a very small staleness
#: budget cannot turn the loop into a busy poll against Coriqo.
MIN_REFRESH_INTERVAL_S = 1.0

#: The only thing a denial ever says where a model can read it. Fixed, and
#: deliberately uninformative: anything richer is a hint sheet for routing
#: around the control.
MODEL_MESSAGE = "This action is not permitted."

#: Agent states that deny outright, under either posture. These are decisions
#: Coriqo made, not failures to evaluate.
BLOCKING_STATUSES = frozenset({"suspended", "retired", "revoked", "decommissioned"})


class Posture:
    """What happens when the gate cannot evaluate."""

    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


class Enforcement:
    """Whether a scope breach counts as a violation."""

    ENFORCE = "enforce"
    OBSERVE = "observe"


class Reason:
    """Operator-facing reason codes. Stable strings — they end up in records."""

    IN_SCOPE = "in_scope"
    UNRESTRICTED = "unrestricted"
    OUT_OF_SCOPE = "out_of_scope"
    OUT_OF_SCOPE_OBSERVED = "out_of_scope_observed"
    SNAPSHOT_STALE = "snapshot_stale"
    NO_SNAPSHOT = "no_snapshot"
    AGENT_SUSPENDED = "agent_suspended"
    ENFORCEMENT_UNCONFIGURED = "enforcement_unconfigured"


class MandateError(ByoAIError):
    """A mandate snapshot could not be understood."""


# -- what is being asked -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposedAction:
    """One tool call, before it runs.

    ``trajectory_id``/``step_index`` are carried through to the verdict so the
    packet that records verdicts can seal one onto the right run without
    re-deriving where it came from.
    """

    tool: str
    trajectory_id: str | None = None
    step_index: int | None = None
    arguments: Mapping[str, Any] | None = None


# -- the verdicts ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    """Base of the three verdicts. Never returned directly.

    ``Allow`` and its subclass ``Flag`` both permit the call — a flagged allow
    is "this ran, and it was off-mandate" — so ``isinstance(v, Allow)`` is the
    honest test for *may this proceed*, and :attr:`allowed` says the same thing
    without an import.
    """

    reason: str
    mandate_version_id: str | None = None
    snapshot_age_s: float | None = None
    tool: str | None = None
    posture: str = Posture.FAIL_OPEN
    enforcement: str | None = None
    trajectory_id: str | None = None
    step_index: int | None = None
    #: Operator-facing only. Never put this in a string bound for the model.
    detail: str | None = None

    @property
    def allowed(self) -> bool:
        raise NotImplementedError  # pragma: no cover - abstract

    @property
    def flagged(self) -> bool:
        """Whether this verdict is worth an operator's attention."""
        return False

    @property
    def verdict(self) -> str:
        """The word Coriqo records: ``allowed``, ``flagged`` or ``blocked``."""
        raise NotImplementedError  # pragma: no cover - abstract


@dataclass(frozen=True, slots=True)
class Allow(Verdict):
    """The call may proceed."""

    @property
    def allowed(self) -> bool:
        return True

    @property
    def verdict(self) -> str:
        return "allowed"


@dataclass(frozen=True, slots=True)
class Flag(Allow):
    """The call may proceed, and something about it needs recording.

    A subclass of :class:`Allow` on purpose: an off-mandate call under
    ``observe``, or any call decided from a snapshot the gate is not sure of,
    still runs. Only the record differs.
    """

    @property
    def flagged(self) -> bool:
        return True

    @property
    def verdict(self) -> str:
        return "flagged"


@dataclass(frozen=True, slots=True)
class Deny(Verdict):
    """The call must not proceed. Terminal, and non-retryable by construction.

    There is nothing here for a caller to vary and try again: the same action
    against the same snapshot denies again, and the only text the model may see
    is the fixed :attr:`model_message`.
    """

    @property
    def allowed(self) -> bool:
        return False

    @property
    def flagged(self) -> bool:
        return True

    @property
    def verdict(self) -> str:
        return "blocked"

    @property
    def model_message(self) -> str:
        """The one sentence a denial may put in front of a model.

        Fixed regardless of why the denial happened. A denial that explained
        itself — "tool ``rm`` is not in mandate v3, which allows ``search``" —
        would be a working set of instructions for getting the job done another
        way.
        """
        return MODEL_MESSAGE


# -- the snapshot ------------------------------------------------------------


def _coerce_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


@dataclass(frozen=True, slots=True)
class MandateSnapshot:
    """One immutable copy of what Coriqo said this agent's mandate is.

    Immutable so :meth:`MandateGate.decide` can read it from any thread without
    a lock held across the evaluation: publishing a new snapshot swaps a
    reference, it never mutates one a decide is mid-way through reading.
    """

    #: ``None`` means *unrestricted*. ``()`` means *nothing is permitted*.
    #: These are opposite instructions; see the module docstring.
    allowed_tools: tuple[str, ...] | None = None
    mandate_version_id: str | None = None
    status: str | None = None
    mandate_enforcement: str = Enforcement.ENFORCE
    enforcement_posture: str = Posture.FAIL_OPEN
    max_staleness_s: float = DEFAULT_MAX_STALENESS_S
    delegation_policy: str | None = None
    max_delegation_depth: int | None = None
    served_at: str | None = None
    #: Monotonic reading from when this snapshot was received. Monotonic, not
    #: wall clock, so an NTP correction mid-run cannot make a snapshot look
    #: fresher (or older) than it is.
    received_at: float = 0.0
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, received_at: float
    ) -> MandateSnapshot:
        if not isinstance(payload, Mapping):
            raise MandateError("mandate snapshot was not an object")

        tools: tuple[str, ...] | None = None
        raw_tools = payload.get("allowed_tools")
        if raw_tools is not None:
            # An explicit list — including an empty one, which permits nothing.
            if isinstance(raw_tools, str) or not isinstance(raw_tools, Iterable):
                raise MandateError(
                    f"allowed_tools must be a list or null, got {type(raw_tools).__name__}"
                )
            tools = tuple(str(tool) for tool in raw_tools)

        return cls(
            allowed_tools=tools,
            mandate_version_id=_opt_str(payload.get("mandate_version_id")),
            status=_opt_str(payload.get("status")),
            mandate_enforcement=(
                _opt_str(payload.get("mandate_enforcement")) or Enforcement.ENFORCE
            ),
            enforcement_posture=(
                _opt_str(payload.get("enforcement_posture")) or Posture.FAIL_OPEN
            ),
            max_staleness_s=_coerce_float(
                payload.get("max_staleness_s"), DEFAULT_MAX_STALENESS_S
            ),
            delegation_policy=_opt_str(payload.get("delegation_policy")),
            max_delegation_depth=_opt_int(payload.get("max_delegation_depth")),
            served_at=_opt_str(payload.get("served_at")),
            received_at=received_at,
            raw=dict(payload),
        )

    @property
    def unrestricted(self) -> bool:
        """True only for ``allowed_tools: null``. Never true for ``[]``."""
        return self.allowed_tools is None

    @property
    def suspended(self) -> bool:
        return (self.status or "").lower() in BLOCKING_STATUSES

    @property
    def observing(self) -> bool:
        return self.mandate_enforcement == Enforcement.OBSERVE

    def age_s(self, now: float) -> float:
        return max(0.0, now - self.received_at)

    def is_stale(self, now: float) -> bool:
        return self.age_s(now) > self.max_staleness_s

    def permits(self, tool: str) -> bool:
        if self.allowed_tools is None:
            return True
        return tool in self.allowed_tools


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


# -- where snapshots come from -----------------------------------------------

#: A refresh: given the ETag of the snapshot in hand (or ``None``), return
#: ``(payload, etag)``. ``payload is None`` means *unchanged* — Coriqo's 304 —
#: and the gate keeps what it has, with its age reset.
MandateSource = Callable[
    [str | None], Awaitable[tuple[Mapping[str, Any] | None, str | None]]
]


def coriqo_mandate_source(client: Any, coriqo_agent_id: str) -> MandateSource:
    """Adapt an :class:`AsyncCoriqoAgentsClient` to :data:`MandateSource`."""

    async def fetch(
        etag: str | None,
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        return await client.fetch_mandate_conditional(coriqo_agent_id, etag=etag)

    return fetch


# -- the gate ----------------------------------------------------------------


class MandateGate:
    """Decides, locally and without I/O, whether a proposed tool call is in
    the agent's approved scope.

    Construct one per agent, start its refresh loop, and call :meth:`decide`
    from wherever tools are dispatched — including from a worker thread, which
    is where this package already does its publishing.

    A gate built with no :data:`MandateSource` is a working no-op: every action
    is allowed, one line is logged the first time, and nothing is flagged. That
    is what makes this adoptable before a device is enrolled — an unenrolled
    host gets the code path, not a crash.
    """

    def __init__(
        self,
        source: MandateSource | None = None,
        *,
        agent_id: str | None = None,
        default_posture: str | None = None,
        default_max_staleness_s: float | None = None,
        min_refresh_interval_s: float = MIN_REFRESH_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """``default_posture`` and ``default_max_staleness_s`` apply only until
        the first snapshot lands; after that the tenant's own values, which
        arrive on every snapshot, win. ``clock`` is monotonic and injectable so
        tests can age a snapshot past its budget without sleeping."""
        self._source = source
        self._agent_id = agent_id
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._default_posture = default_posture or _env_posture()
        self._default_max_staleness_s = (
            default_max_staleness_s
            if default_max_staleness_s is not None
            else _env_max_staleness()
        )
        self._min_refresh_interval_s = min_refresh_interval_s
        self._lock = threading.Lock()
        self._snapshot: MandateSnapshot | None = None
        self._etag: str | None = None
        self._last_refresh_error: Exception | None = None
        self._noop_logged = False
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- state -------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """False for a gate with no source — the adopt-before-enrolment no-op."""
        return self._source is not None

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    @property
    def snapshot(self) -> MandateSnapshot | None:
        with self._lock:
            return self._snapshot

    @property
    def last_refresh_error(self) -> Exception | None:
        with self._lock:
            return self._last_refresh_error

    def snapshot_age_s(self) -> float | None:
        snapshot = self.snapshot
        return None if snapshot is None else snapshot.age_s(self._clock())

    @property
    def posture(self) -> str:
        snapshot = self.snapshot
        if snapshot is None:
            return self._default_posture
        return snapshot.enforcement_posture

    def apply_snapshot(
        self, payload: Mapping[str, Any], *, etag: str | None = None
    ) -> MandateSnapshot:
        """Install a snapshot directly, as a refresh would.

        Public because the alternative is tests (and anyone wiring a different
        transport) reaching into private state to seed a gate.
        """
        snapshot = MandateSnapshot.from_payload(payload, received_at=self._clock())
        if payload.get("max_staleness_s") is None:
            snapshot = replace(
                snapshot, max_staleness_s=self._default_max_staleness_s
            )
        if payload.get("enforcement_posture") is None:
            snapshot = replace(snapshot, enforcement_posture=self._default_posture)
        with self._lock:
            self._snapshot = snapshot
            self._etag = etag
            self._last_refresh_error = None
        return snapshot

    # -- the decide path: no I/O, no awaits, no surprises -------------------

    def decide(self, action: ProposedAction | str) -> Verdict:
        """Return :class:`Allow`, :class:`Flag` or :class:`Deny` for ``action``.

        Reads memory and returns. No network call, no lock held across an
        evaluation, no awaiting — so it is safe to call from a worker thread
        while the refresh loop is mid-flight on the event loop.
        """
        if isinstance(action, str):
            action = ProposedAction(tool=action)

        if self._source is None:
            self._log_noop_once()
            return Allow(
                reason=Reason.ENFORCEMENT_UNCONFIGURED,
                tool=action.tool,
                posture=self._default_posture,
                trajectory_id=action.trajectory_id,
                step_index=action.step_index,
                detail="no Coriqo identity configured; the mandate gate is a no-op",
            )

        with self._lock:
            snapshot = self._snapshot
        now = self._clock()

        if snapshot is None:
            return self._unevaluable(
                action,
                reason=Reason.NO_SNAPSHOT,
                age=None,
                version=None,
                enforcement=None,
                detail=(
                    "no mandate snapshot has been fetched yet for agent "
                    f"{self._agent_id or '?'}"
                ),
            )

        age = snapshot.age_s(now)

        # A suspension is a decision Coriqo made and this runtime read, so it
        # denies under both postures — it is not a failure to evaluate.
        if snapshot.suspended:
            return Deny(
                reason=Reason.AGENT_SUSPENDED,
                mandate_version_id=snapshot.mandate_version_id,
                snapshot_age_s=age,
                tool=action.tool,
                posture=snapshot.enforcement_posture,
                enforcement=snapshot.mandate_enforcement,
                trajectory_id=action.trajectory_id,
                step_index=action.step_index,
                detail=f"agent status is {snapshot.status!r}",
            )

        if snapshot.is_stale(now):
            return self._unevaluable(
                action,
                reason=Reason.SNAPSHOT_STALE,
                age=age,
                version=snapshot.mandate_version_id,
                enforcement=snapshot.mandate_enforcement,
                posture=snapshot.enforcement_posture,
                detail=(
                    f"snapshot is {age:.1f}s old, past the "
                    f"{snapshot.max_staleness_s:.1f}s budget"
                ),
            )

        if snapshot.permits(action.tool):
            return Allow(
                reason=Reason.UNRESTRICTED if snapshot.unrestricted else Reason.IN_SCOPE,
                mandate_version_id=snapshot.mandate_version_id,
                snapshot_age_s=age,
                tool=action.tool,
                posture=snapshot.enforcement_posture,
                enforcement=snapshot.mandate_enforcement,
                trajectory_id=action.trajectory_id,
                step_index=action.step_index,
            )

        # Out of scope. `observe` is the tenant's rollout dial: record what
        # would have been blocked, block nothing.
        common = {
            "mandate_version_id": snapshot.mandate_version_id,
            "snapshot_age_s": age,
            "tool": action.tool,
            "posture": snapshot.enforcement_posture,
            "enforcement": snapshot.mandate_enforcement,
            "trajectory_id": action.trajectory_id,
            "step_index": action.step_index,
        }
        if snapshot.observing:
            return Flag(
                reason=Reason.OUT_OF_SCOPE_OBSERVED,
                detail=f"{action.tool!r} is outside the mandate; enforcement is observe",
                **common,
            )
        return Deny(
            reason=Reason.OUT_OF_SCOPE,
            detail=f"{action.tool!r} is not in the agent's approved scope",
            **common,
        )

    def _unevaluable(
        self,
        action: ProposedAction,
        *,
        reason: str,
        age: float | None,
        version: str | None,
        enforcement: str | None,
        detail: str,
        posture: str | None = None,
    ) -> Verdict:
        """The fail_open / fail_closed fork, in one place.

        Both branches exist for the same situation — the gate does not have a
        mandate it can stand behind — so they are written once and differ only
        in the verdict class.
        """
        effective = posture or self._default_posture
        fields: dict[str, Any] = {
            "reason": reason,
            "mandate_version_id": version,
            "snapshot_age_s": age,
            "tool": action.tool,
            "posture": effective,
            "enforcement": enforcement,
            "trajectory_id": action.trajectory_id,
            "step_index": action.step_index,
            "detail": detail,
        }
        if effective == Posture.FAIL_CLOSED:
            return Deny(**fields)
        return Flag(**fields)

    def _log_noop_once(self) -> None:
        with self._lock:
            if self._noop_logged:
                return
            self._noop_logged = True
        log.info(
            "coriqo: no mandate source configured, so tool calls are not gated. "
            "Enroll this device (byoai-recorder-enroll) to enforce the agent's "
            "approved mandate."
        )

    # -- the refresh path: the only place that touches the network ---------

    async def refresh(self) -> MandateSnapshot | None:
        """Fetch once. Returns the snapshot now in hand, or ``None`` if there
        still isn't one.

        A ``304`` keeps the cached snapshot and resets its age — the mandate
        did not change, so the copy in memory is current, not merely recent.
        """
        if self._source is None:
            return None
        etag = self._etag
        payload, new_etag = await self._source(etag)
        now = self._clock()
        with self._lock:
            if payload is None:
                # Unchanged. Same mandate, fresh as of now.
                if self._snapshot is not None:
                    self._snapshot = replace(self._snapshot, received_at=now)
                if new_etag:
                    self._etag = new_etag
                self._last_refresh_error = None
                return self._snapshot

        snapshot = self.apply_snapshot(payload, etag=new_etag)
        log.debug(
            "coriqo: mandate refreshed for %s (version=%s, tools=%s)",
            self._agent_id,
            snapshot.mandate_version_id,
            "unrestricted" if snapshot.unrestricted else len(snapshot.allowed_tools or ()),
        )
        return snapshot

    async def refresh_safely(self) -> bool:
        """:meth:`refresh`, with a failure recorded rather than raised.

        A failed refresh must never clear a snapshot that is still inside its
        staleness budget. Dropping a good snapshot on one blip is how a network
        hiccup becomes a fail-closed outage — the snapshot stays, and staleness
        alone decides when it stops counting.
        """
        try:
            await self.refresh()
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            with self._lock:
                self._last_refresh_error = exc
                held = self._snapshot
            log.warning(
                "coriqo: mandate refresh failed for %s (%s); %s",
                self._agent_id,
                exc,
                "keeping the cached snapshot" if held else "no snapshot cached yet",
            )
            return False
        return True

    def refresh_interval_s(self) -> float:
        """How long to wait before the next refresh.

        Half the staleness budget the *snapshot in hand* names, so the interval
        follows the agent's own budget once one is known and there is room for
        a whole failed refresh before the snapshot ages out.
        """
        budget = (
            self.snapshot.max_staleness_s
            if self.snapshot is not None
            else self._default_max_staleness_s
        )
        return max(self._min_refresh_interval_s, budget * REFRESH_FRACTION)

    async def start(self) -> None:
        """Fetch once, then keep refreshing in the background.

        The first fetch is awaited so a caller that started the gate before its
        first tool call is not deciding from an empty cache — under
        ``fail_closed`` that first window would deny everything.
        """
        if self._source is None or self._task is not None:
            return
        self._stop = asyncio.Event()
        await self.refresh_safely()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), self.refresh_interval_s())
                return
            except asyncio.TimeoutError:
                pass
            await self.refresh_safely()

    async def __aenter__(self) -> MandateGate:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()


# -- construction ------------------------------------------------------------


def _env_posture() -> str:
    value = (os.getenv("BYOAI_MANDATE_POSTURE") or "").strip().lower()
    if value in {Posture.FAIL_OPEN, Posture.FAIL_CLOSED}:
        return value
    return Posture.FAIL_OPEN


def _env_max_staleness() -> float:
    return _coerce_float(
        os.getenv("BYOAI_MANDATE_MAX_STALENESS_S"), DEFAULT_MAX_STALENESS_S
    )


def mandate_gate(
    coriqo_agent_id: str,
    *,
    identity: CoriqoIdentity | None = None,
    client: Any | None = None,
    **kwargs: Any,
) -> MandateGate:
    """Build a gate for ``coriqo_agent_id`` from this host's Coriqo identity.

    With no identity configured on the host — nothing enrolled, no API key —
    this returns the no-op gate rather than raising, so the enforcement code
    path can be adopted before enrolment. A *static API key* identity is
    refused instead, with
    :class:`~byoai.errors.EnforcementIdentityUnavailableError`: that is a
    misconfiguration rather than an absence, and a key carrying
    ``governance:approve`` would let the agent widen its own mandate.
    """
    if client is None:
        if identity is None:
            identity = resolve_identity()
        if identity is None:
            return MandateGate(None, agent_id=coriqo_agent_id, **kwargs)
        identity.require_enforcement()
        from .coriqo_async import AsyncCoriqoAgentsClient

        client = AsyncCoriqoAgentsClient(identity)

    return MandateGate(
        coriqo_mandate_source(client, coriqo_agent_id),
        agent_id=coriqo_agent_id,
        **kwargs,
    )
