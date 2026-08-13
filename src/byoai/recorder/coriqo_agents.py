"""Publishing recorded runs to Coriqo's agent governance API.

The local ledger proves a run happened as recorded. Coriqo is where that
becomes governance: an agent registry, the mandate each agent is allowed to
act under, and a hash-chained trail of what every agent actually did. This
module is the bridge, for any agent anywhere — register once, then publish each
run as a trajectory plus one decision trace per sealed step.

Distinct from :mod:`byoai.recorder.shipper`, which is worth being clear about
since both talk to something called Coriqo:

* ``shipper.py`` syncs the raw device ledger itself (``/v1/ingest/batch``),
  signed and batched, as a verbatim copy of the hash chain. No Coriqo serves
  those endpoints yet.
* this module publishes *governed decisions* to Coriqo's shipped agent API
  (``/api/v1/agents/…``), which exists today. It sends digests and step
  metadata, never raw payloads.

Two properties make what lands in Coriqo evidence rather than telemetry:

* **Ledger-first.** :func:`read_tool_steps` reads sealed rows back out of the
  ledger, so nothing reaches Coriqo that wasn't recorded first.
* **Shared digests.** Each step's ``args_hash``/``result_hash`` are the
  ledger's own ``payload_hash`` values, which commit to the raw, unredacted
  payload whatever ``BYOAI_RECORDER_PAYLOAD_MODE`` is set to, and each trace
  cites its ledger row's ``entry_hash`` as an external grounding anchor. So both
  stores commit to the same bytes: a hash from a Coriqo trace resolves to the
  sealed row it came from, and ``coriqo-verify`` still checks the ledger
  offline. Neither store has to be trusted on its own.

Unlike the shipper, nothing here runs automatically. Session boundaries and
agent identity are application concepts the recorder can't infer — it never
sees where a "run" ends, and under the default redacted payload mode it can't
even read which agent a session belonged to. So the caller drives this
explicitly, and functions here raise on failure rather than swallowing, leaving
the log-and-continue decision to the application that knows whether Coriqo
being down should matter.

Typical wiring::

    creds = CoriqoCredentials.from_env()
    if creds is not None:
        with CoriqoAgentsClient(creds) as client:
            agent_ids = ensure_registered(client, {
                "my-app:my-agent": AgentRegistration(
                    name="My Agent",
                    allowed_tools=("search_docs", "summarize"),
                ),
            })
            # ... after a run completes ...
            publish_session(
                client,
                coriqo_agent_id=agent_ids["my-app:my-agent"],
                ledger=recorder.ledger,
                session_id=run_id,
            )
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import httpx

from .ledger import Ledger
from .schema import EventKind

__all__ = [
    "GROUNDING_SYSTEM",
    "MAX_TRACE_BATCH",
    "AgentRegistration",
    "AgentSuspendedError",
    "CoriqoAgentsClient",
    "CoriqoAgentsError",
    "CoriqoCredentials",
    "PublishResult",
    "ToolStep",
    "ensure_registered",
    "publish_session",
    "read_tool_steps",
]

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10.0
_LIST_PAGE_SIZE = 200  # Coriqo caps `limit` at 200 on GET /api/v1/agents.

#: Coriqo's cap on ``/traces/batch``. Longer runs are split across batches.
MAX_TRACE_BATCH = 200

#: The ``system`` value on the external grounding anchors this module writes, so
#: an auditor reading a Coriqo trace knows which store the cited hash lives in.
GROUNDING_SYSTEM = "byoai-recorder"


class CoriqoAgentsError(RuntimeError):
    """Any non-2xx from Coriqo, or a response that isn't usable JSON.

    ``status_code`` and ``detail`` let a caller branch on a specific failure
    (409 = the agent has no mandate version yet, 403 = the service account is
    missing a role, 422 = a field Coriqo's strict schemas rejected) without
    matching on message text.
    """

    def __init__(self, status_code: int | None, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Coriqo API error {status_code}: {detail}")


class AgentSuspendedError(CoriqoAgentsError):
    """423: governance has suspended this agent, so Coriqo refuses to record
    anything further for it.

    Worth catching separately, and worth being honest about what it is: a
    governance-level stop, not a live circuit breaker. It prevents new traces
    from being recorded; it cannot stop an agent that has already acted. A
    caller that wants a real kill switch has to enforce it in its own loop.
    """


@dataclass(frozen=True, slots=True)
class CoriqoCredentials:
    """How a machine identity authenticates to Coriqo.

    Coriqo service accounts use two headers rather than a JWT and cannot log in
    to its web UI. The account needs ``governance:approve`` to register agents
    and ``model:write`` to record traces. A deployment that only publishes to
    already-registered agents can hold ``model:write`` alone and skip
    :func:`ensure_registered`, passing known agent ids to
    :func:`publish_session` directly.
    """

    base_url: str
    api_key: str
    tenant_slug: str

    @classmethod
    def from_env(cls) -> CoriqoCredentials | None:
        """Reads ``BYOAI_CORIQO_URL``/``_API_KEY``/``_TENANT_SLUG``.

        Returns ``None`` when the URL is unset, which is how a caller keeps
        Coriqo publishing opt-in. A URL set without the other two is treated as
        a misconfiguration and warned about rather than silently ignored, since
        the likely cause is a typo'd variable name.
        """
        base_url = os.environ.get("BYOAI_CORIQO_URL")
        if not base_url:
            return None
        api_key = os.environ.get("BYOAI_CORIQO_API_KEY")
        tenant_slug = os.environ.get("BYOAI_CORIQO_TENANT_SLUG")
        if not api_key or not tenant_slug:
            log.warning(
                "BYOAI_CORIQO_URL is set but BYOAI_CORIQO_API_KEY/"
                "BYOAI_CORIQO_TENANT_SLUG are not — Coriqo publishing disabled"
            )
            return None
        return cls(base_url=base_url.rstrip("/"), api_key=api_key, tenant_slug=tenant_slug)


@dataclass(frozen=True, slots=True)
class AgentRegistration:
    """What Coriqo needs to put an agent in its registry.

    ``allowed_tools`` is the agent's mandate surface and is enforced on every
    trace: Coriqo flags a recorded call outside it and opens a mandate Finding.
    It has to be the agent's real declared tool set, or ordinary runs will be
    flagged as out-of-mandate. Set ``mandate_enforcement="observe"`` to have
    violations sealed and reported without flagging the trace — useful while
    learning an agent's real tool surface, readable afterwards via Coriqo's
    ``/mandate/observed-tools``.

    ``external_id`` is what makes registration idempotent: re-registering with
    the same one returns the existing agent instead of creating a second copy.
    :func:`ensure_registered` fills it in from the caller's own key when it
    isn't set, so it is rarely worth passing by hand.
    """

    name: str
    mandate: str | None = None
    system: str | None = None
    risk_tier: str | None = None
    allowed_tools: tuple[str, ...] = ()
    owner_id: str | None = None
    external_id: str | None = None
    mandate_enforcement: str | None = None

    def to_body(self) -> dict[str, Any]:
        """Coriqo's request schemas are strict (``extra="forbid"``), so this
        sends only fields it declares — never a spare key that would 422."""
        body: dict[str, Any] = {
            "name": self.name,
            "mandate": self.mandate,
            "system": self.system,
            "risk_tier": self.risk_tier,
            "allowed_tools": list(self.allowed_tools),
            "owner_id": self.owner_id,
        }
        if self.external_id is not None:
            body["external_id"] = self.external_id
        if self.mandate_enforcement is not None:
            body["mandate_enforcement"] = self.mandate_enforcement
        return body


@dataclass(frozen=True, slots=True)
class ToolStep:
    """One sealed ``tool_use``/``tool_result`` pair, ready to publish.

    ``args_hash``/``result_hash`` are the ledger's ``payload_hash`` values
    verbatim, and ``entry_hash`` is the ledger row's own chain link. Those
    shared digests are what tie a Coriqo trace back to the sealed row behind
    it. ``result_hash`` is ``None`` for a tool call the ledger has no result
    for, which the offline verifier reports as an unpaired tool_use.
    """

    index: int
    tool_name: str
    args_hash: str
    result_hash: str | None
    entry_hash: str
    occurred_at: str
    latency_ms: int | None


@dataclass(frozen=True, slots=True)
class PublishResult:
    trajectory_id: str
    total_steps: int
    recorded: int
    flagged: int
    status: str


# -- reading a run back out of the ledger -----------------------------------


def read_tool_steps(ledger: Ledger, session_id: str) -> list[ToolStep]:
    """Pairs one session's sealed tool calls, in ledger order.

    Pairing is by ``tool_use_id``, since ``tool_name`` is only set on the
    ``tool_use`` side. Latency comes from the monotonic capture clock rather
    than ``ts_device``, which is an untrusted host wall clock that may have
    jumped mid-run.
    """
    entries = ledger.read_session(session_id)
    # Only events that actually carry an id can be paired. extract.py stores
    # tool_use_id=None when a transcript's id field wasn't a string, and
    # without this guard every such event would collide on the None key: the
    # results dict would keep only the last one, and an unrelated malformed
    # tool_use would then be published carrying that result's hash and latency
    # as its own evidence. verify.py's pairing walk guards the same way.
    results = {
        e.event.tool_use_id: e.event
        for e in entries
        if e.event.kind == EventKind.TOOL_RESULT.value and e.event.tool_use_id
    }

    steps: list[ToolStep] = []
    for entry in entries:
        event = entry.event
        if event.kind != EventKind.TOOL_USE.value:
            continue
        result = results.get(event.tool_use_id) if event.tool_use_id else None
        latency_ms = None
        if result is not None:
            elapsed_ns = result.ts_monotonic_ns - event.ts_monotonic_ns
            # A negative delta means the two rows came from different processes
            # and so different monotonic epochs. Report nothing rather than a
            # nonsense duration.
            if elapsed_ns >= 0:
                latency_ms = elapsed_ns // 1_000_000
        steps.append(
            ToolStep(
                index=len(steps),
                tool_name=event.tool_name or "unknown",
                args_hash=event.payload_hash,
                result_hash=result.payload_hash if result is not None else None,
                entry_hash=entry.entry_hash,
                occurred_at=event.ts_device,
                latency_ms=latency_ms,
            )
        )
    return steps


def _as_utc_isoformat(ts_device: str) -> str | None:
    """Ledger timestamps are RFC3339 UTC with microsecond precision.

    Coriqo reads a naive timestamp as already-UTC, so an unparseable value is
    dropped rather than risk sealing a decision hours off.
    """
    try:
        parsed = datetime.strptime(ts_device, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


# -- the client ------------------------------------------------------------


class CoriqoAgentsClient:
    """Thin, synchronous client for Coriqo's agent governance API.

    One client per process rather than a shared global: it holds an
    ``httpx.Client`` and offers no more thread safety than that already does.
    Every method raises :class:`CoriqoAgentsError` on a non-2xx response and
    does not retry.
    """

    def __init__(
        self,
        credentials: CoriqoCredentials,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        """Pass ``http_client`` to supply your own transport, retry policy, or
        a test double. A caller-supplied client is never closed by
        :meth:`close`, since closing something shared out from under its owner
        would break its next use elsewhere."""
        if http_client is not None:
            self._client = http_client
            self._owns_client = False
        else:
            self._client = httpx.Client(
                base_url=credentials.base_url,
                headers={
                    "X-API-Key": credentials.api_key,
                    "X-Tenant-Slug": credentials.tenant_slug,
                    "content-type": "application/json",
                },
                timeout=timeout,
            )
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> CoriqoAgentsClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _send(self, method: str, path: str, **kwargs: Any) -> tuple[Any, int]:
        """Performs the request and returns ``(parsed_body, status_code)``.

        Separate from :meth:`_request` because registration needs the status
        code itself: Coriqo distinguishes "created" from "already existed" by
        201 vs 200, not by anything in the body.
        """
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CoriqoAgentsError(None, f"request to {path} failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text
            if response.headers.get("content-type", "").startswith("application/json"):
                try:
                    detail = response.json().get("detail", response.text)
                except ValueError:
                    pass
            if response.status_code == 423:
                raise AgentSuspendedError(response.status_code, detail)
            raise CoriqoAgentsError(response.status_code, detail)

        if not response.content:
            return None, response.status_code
        try:
            return response.json(), response.status_code
        except ValueError as exc:
            # A 2xx carrying a non-JSON body isn't a Coriqo response at all —
            # usually a proxy or maintenance page. Surface it as the same error
            # type every other failure uses so one `except` covers it.
            raise CoriqoAgentsError(
                response.status_code, f"non-JSON response body: {exc}"
            ) from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        body, _status = self._send(method, path, **kwargs)
        return body

    # -- agents ------------------------------------------------------------

    def list_agents(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Every agent in the tenant, paging until the reported total is reached.

        Coriqo caps ``limit`` at 200 per request, so a single call cannot see a
        tenant with more agents than that. Paging matters because a caller using
        this to decide whether an agent already exists would otherwise register
        a duplicate for every agent past the first page. ``limit`` caps the
        total returned; omit it for all of them.
        """
        collected: list[dict[str, Any]] = []
        offset = 0
        while True:
            page_size = _LIST_PAGE_SIZE
            if limit is not None:
                page_size = min(page_size, limit - len(collected))
                if page_size <= 0:
                    return collected
            page = self._request(
                "GET", "/api/v1/agents", params={"limit": page_size, "offset": offset}
            )
            if not isinstance(page, dict):
                return collected
            items = page.get("items") or []
            collected.extend(items)
            total = page.get("total")
            offset += len(items)
            # Stop on an empty page even if `total` disagrees, so a miscounted
            # total can't spin this loop forever.
            if not items or (isinstance(total, int) and offset >= total):
                return collected

    def register_agent(self, registration: AgentRegistration) -> tuple[dict[str, Any], bool]:
        """Registers an agent, returning ``(record, created)``.

        With an ``external_id`` this is idempotent: Coriqo answers ``201`` the
        first time and ``200`` with the existing agent afterwards, so
        ``created`` distinguishes the two. Note that a repeat call does **not**
        apply changes to ``mandate``/``allowed_tools`` — amend those through
        Coriqo's mandate endpoint, so the change is versioned rather than
        silently rewriting what earlier decisions were judged against.

        Without an ``external_id`` every call creates a new agent.

        Registering always lands the agent at ``in_review``: Coriqo never
        pre-approves. That is what makes self-registration safe — it files a
        governance to-do rather than granting the agent any standing.
        """
        body, status = self._send("POST", "/api/v1/agents", json=registration.to_body())
        if not isinstance(body, dict):
            raise CoriqoAgentsError(status, "registration response was not an object")
        return body, status == 201

    def get_agent(self, coriqo_agent_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/agents/{coriqo_agent_id}")

    # -- runs --------------------------------------------------------------

    def open_trajectory(
        self,
        coriqo_agent_id: str,
        *,
        goal: str | None = None,
        use_case: str | None = None,
        parent_trajectory_id: str | None = None,
    ) -> dict[str, Any]:
        """Opens a run.

        ``parent_trajectory_id`` nests this run under another, which Coriqo
        uses to roll a flagged step up through every ancestor. The parent must
        belong to the **same** agent and still be open — Coriqo refuses
        cross-agent nesting, so a sub-agent that is its own registered agent
        cannot be nested this way and is better represented as a tool call on
        its parent's trace.
        """
        body: dict[str, Any] = {"goal": goal}
        if use_case is not None:
            body["use_case"] = use_case
        if parent_trajectory_id is not None:
            body["parent_trajectory_id"] = parent_trajectory_id
        return self._request(
            "POST", f"/api/v1/agents/{coriqo_agent_id}/trajectories", json=body
        )

    def complete_trajectory(
        self, coriqo_agent_id: str, trajectory_id: str, *, status: str = "completed"
    ) -> dict[str, Any]:
        """Closes a run. Coriqo answers 409 if it still has open sub-runs, so
        nested children have to be completed first."""
        return self._request(
            "POST",
            f"/api/v1/agents/{coriqo_agent_id}/trajectories/{trajectory_id}/complete",
            json={"status": status},
        )

    def list_trajectories(
        self, coriqo_agent_id: str, *, status: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """This agent's runs, depth-first with each run followed by its
        children, each carrying ``depth``/``step_count``/``child_count``."""
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        return self._request(
            "GET", f"/api/v1/agents/{coriqo_agent_id}/trajectories", params=params
        )

    def record_trace(
        self,
        coriqo_agent_id: str,
        *,
        inputs: Any | None = None,
        output: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        grounding_refs: list[Any] | None = None,
        trajectory_id: str | None = None,
        step_index: int | None = None,
        latency_ms: int | None = None,
        cost_usd: float | None = None,
        token_count: int | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """Records one decision. See :meth:`record_traces` for the batch form.

        ``inputs`` is hashed server-side and never stored, but it *is*
        transmitted — so send metadata or a digest, not raw payloads you can't
        have Coriqo see even in transit. Prefer pre-computed
        ``args_hash``/``result_hash`` in ``tool_calls`` (which is what
        :func:`publish_session` does) so raw arguments never leave the process
        at all.

        ``grounding_refs`` entries are either ids Coriqo can resolve itself, or
        anchors into another store, written as
        ``{"type": "external", "id": …, "system": …}``. External anchors are
        held outside the integrity calculation — neither verified nor counted
        against the score — which is what makes it safe to cite a hash from a
        store Coriqo has no copy of.
        """
        return self._request(
            "POST",
            f"/api/v1/agents/{coriqo_agent_id}/traces",
            json=_trace_body(
                inputs=inputs,
                output=output,
                tool_calls=tool_calls,
                grounding_refs=grounding_refs,
                trajectory_id=trajectory_id,
                step_index=step_index,
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                token_count=token_count,
                occurred_at=occurred_at,
            ),
        )

    def record_traces(
        self, coriqo_agent_id: str, traces: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Records up to :data:`MAX_TRACE_BATCH` decisions in one request.

        All-or-nothing: Coriqo rejects the whole batch if any single trace is
        invalid, and records none of them. Mandate checks still run per trace,
        so the returned ``traces`` array carries each one's own
        ``status``/``flag_reason`` and ``recorded``/``flagged`` are counts.
        """
        if not traces:
            raise ValueError("traces must not be empty")
        if len(traces) > MAX_TRACE_BATCH:
            raise ValueError(
                f"a batch holds at most {MAX_TRACE_BATCH} traces, got {len(traces)}"
            )
        return self._request(
            "POST",
            f"/api/v1/agents/{coriqo_agent_id}/traces/batch",
            json={"traces": [dict(t) for t in traces]},
        )

    def authorize(
        self,
        coriqo_agent_id: str,
        *,
        tool: str,
        trajectory_id: str | None = None,
        step_index: int | None = None,
    ) -> dict[str, Any]:
        """Pre-act mandate check, returning a sealed ``allow``/``deny``
        verdict. Advisory: Coriqo records the verdict, it does not enforce it —
        honoring a deny is the caller's responsibility."""
        return self._request(
            "POST",
            f"/api/v1/agents/{coriqo_agent_id}/authorize",
            json={"tool": tool, "trajectory_id": trajectory_id, "step_index": step_index},
        )


def _close_quietly(
    client: CoriqoAgentsClient, coriqo_agent_id: str, trajectory_id: str, session_id: str
) -> None:
    """Best-effort completion of a trajectory whose steps failed to publish.

    Deliberately swallows its own errors: it runs while a real failure is
    already propagating, and masking that failure with a cleanup error would
    hide the thing the caller actually needs to see.
    """
    try:
        client.complete_trajectory(coriqo_agent_id, trajectory_id, status="flagged")
    except CoriqoAgentsError as exc:
        log.warning(
            "coriqo: could not close trajectory %s after session %s failed to "
            "publish, it will stay open: %s",
            trajectory_id,
            session_id,
            exc.detail,
        )


def _required(body: Any, key: str, path: str) -> Any:
    """Reads a required field off a 2xx body, or raises.

    A 2xx whose body is missing the field it promised (a proxy rewriting
    responses, an API version that renamed it) is a Coriqo-side problem, not a
    bug in the caller — so it surfaces as the error type callers already handle
    instead of a bare ``KeyError`` escaping this module and breaking a run that
    was told a Coriqo outage would never do that.
    """
    if not isinstance(body, dict) or body.get(key) is None:
        raise CoriqoAgentsError(None, f"response from {path} had no {key}")
    return body[key]


def _trace_body(
    *,
    inputs: Any | None,
    output: str | None,
    tool_calls: list[dict[str, Any]] | None,
    grounding_refs: list[Any] | None,
    trajectory_id: str | None,
    step_index: int | None,
    latency_ms: int | None,
    cost_usd: float | None,
    token_count: int | None,
    occurred_at: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "inputs": inputs,
        "output": output,
        "tool_calls": tool_calls,
        "grounding_refs": grounding_refs,
        "trajectory_id": trajectory_id,
        "step_index": step_index,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd,
        "token_count": token_count,
    }
    if occurred_at is not None:
        body["occurred_at"] = occurred_at
    return body


# -- orchestration ---------------------------------------------------------


def ensure_registered(
    client: CoriqoAgentsClient,
    registrations: Mapping[str, AgentRegistration],
    *,
    external_id_prefix: str = "",
) -> dict[str, str]:
    """Resolves every local agent key to a Coriqo agent id, registering as needed.

    Safe to call on every startup, and safe to call concurrently from several
    processes. Idempotency is Coriqo's: each agent is registered with an
    ``external_id`` (the registration's own, or the mapping key prefixed by
    ``external_id_prefix``), and re-registering the same one returns the
    existing agent rather than creating a second copy. That means there is no
    local cache to keep in sync, no listing to page through, and no matching on
    display names — the server is the single source of truth.

    Pick an ``external_id_prefix`` that names your application (``"my-app:"``)
    if the mapping keys are generic enough to collide with another publisher
    sharing the tenant.

    Raises :class:`CoriqoAgentsError` if Coriqo refuses a registration — most
    often 403 for a service account without ``governance:approve``. A
    deployment that shouldn't create agents at all can skip this entirely and
    pass known agent ids straight to :func:`publish_session`.
    """
    resolved: dict[str, str] = {}
    for key, registration in registrations.items():
        if registration.external_id is None:
            registration = replace(
                registration, external_id=f"{external_id_prefix}{key}"
            )
        record, created = client.register_agent(registration)
        agent_id = record.get("agent_id")
        if not agent_id:
            raise CoriqoAgentsError(
                None, f"registration of {key!r} returned no agent_id"
            )
        resolved[key] = agent_id
        if created:
            log.info(
                "coriqo: registered %r as agent %s (%s)",
                key,
                agent_id,
                record.get("validation_status", "in_review"),
            )
        else:
            log.debug("coriqo: %r already registered as agent %s", key, agent_id)
    return resolved


def publish_session(
    client: CoriqoAgentsClient,
    *,
    coriqo_agent_id: str,
    ledger: Ledger,
    session_id: str,
    goal: str | None = None,
    use_case: str | None = None,
    final_output: str | None = None,
    inputs_extra: Mapping[str, Any] | None = None,
    parent_trajectory_id: str | None = None,
    ground_in_ledger: bool = True,
) -> PublishResult | None:
    """Publishes one recorded session as a trajectory plus a trace per step.

    Returns ``None`` when the session sealed no tool calls, so there is
    nothing to publish. ``final_output`` is attached to the last step, which is
    where a run's decision text belongs; earlier steps carry their results by
    hash only.

    Steps go up in batches of :data:`MAX_TRACE_BATCH`, so an ordinary run costs
    one request. Each batch is atomic on Coriqo's side: one invalid trace
    rejects that whole batch, which is why a rejection raises rather than
    reporting a partial success. If a batch does fail, the trajectory is closed
    as ``flagged`` on the way out rather than left open — an abandoned open run
    would sit in Coriqo as permanently in-progress and would block its parent
    from ever completing.

    With ``ground_in_ledger`` (the default) every trace cites its ledger row's
    ``entry_hash`` as an external grounding anchor, so a Coriqo trace names the
    exact sealed row behind it. Coriqo holds external anchors outside its
    integrity scoring, so this adds provenance without distorting the score.

    ``parent_trajectory_id`` nests this run under another of the **same**
    agent's runs; Coriqo refuses cross-agent nesting. A nested run has to be
    completed before its parent can be.

    A trajectory containing a flagged step is completed as ``flagged`` rather
    than ``completed`` — a run that went outside its mandate should not close
    looking clean.

    Raises :class:`CoriqoAgentsError` on any rejection, and
    :class:`AgentSuspendedError` if governance has stopped this agent.
    """
    steps = read_tool_steps(ledger, session_id)
    if not steps:
        return None

    trajectory = client.open_trajectory(
        coriqo_agent_id,
        goal=goal,
        use_case=use_case,
        parent_trajectory_id=parent_trajectory_id,
    )
    trajectory_id = _required(
        trajectory, "trajectory_id", f"/api/v1/agents/{coriqo_agent_id}/trajectories"
    )

    last_index = steps[-1].index
    bodies = []
    for step in steps:
        inputs: dict[str, Any] = {
            "session_id": session_id,
            "step": step.index,
            "tool": step.tool_name,
        }
        if inputs_extra:
            inputs.update(inputs_extra)
        grounding_refs = (
            [{"type": "external", "id": step.entry_hash, "system": GROUNDING_SYSTEM}]
            if ground_in_ledger
            else None
        )
        bodies.append(
            _trace_body(
                inputs=inputs,
                output=final_output if step.index == last_index else None,
                tool_calls=[
                    {
                        "step": step.index,
                        "tool": step.tool_name,
                        "args_hash": step.args_hash,
                        "result_hash": step.result_hash,
                    }
                ],
                grounding_refs=grounding_refs,
                trajectory_id=trajectory_id,
                step_index=step.index,
                latency_ms=step.latency_ms,
                cost_usd=None,
                token_count=None,
                occurred_at=_as_utc_isoformat(step.occurred_at),
            )
        )

    recorded = 0
    flagged = 0
    try:
        for start in range(0, len(bodies), MAX_TRACE_BATCH):
            batch = bodies[start : start + MAX_TRACE_BATCH]
            response = client.record_traces(coriqo_agent_id, batch)
            recorded += int(response.get("recorded", 0))
            flagged += int(response.get("flagged", 0))
            for trace in response.get("traces") or []:
                if trace.get("status") == "flagged":
                    log.warning(
                        "coriqo: flagged step %s of session %s: %s",
                        trace.get("step_index"),
                        session_id,
                        trace.get("flag_reason"),
                    )
    except CoriqoAgentsError:
        # The trajectory is already open at this point. Leaving it that way
        # would strand the run as permanently in-progress in Coriqo — and
        # since a parent can't be completed while a child is open, it would
        # block that parent too. Callers of this function typically log and
        # carry on, so nothing else would ever come back to close it. Mark it
        # flagged (the closest Coriqo has to "this didn't finish cleanly")
        # before re-raising the real failure.
        _close_quietly(client, coriqo_agent_id, trajectory_id, session_id)
        raise

    status = "flagged" if flagged else "completed"
    client.complete_trajectory(coriqo_agent_id, trajectory_id, status=status)

    log.info(
        "coriqo: published session %s to agent %s (trajectory %s, %s/%s steps, "
        "%s flagged, status=%s)",
        session_id,
        coriqo_agent_id,
        trajectory_id,
        recorded,
        len(steps),
        flagged,
        status,
    )
    return PublishResult(
        trajectory_id=trajectory_id,
        total_steps=len(steps),
        recorded=recorded,
        flagged=flagged,
        status=status,
    )
