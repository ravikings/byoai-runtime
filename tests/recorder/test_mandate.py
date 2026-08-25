"""The mandate gate's behavior table, row by row, under both postures.

No network: the refresh source is a plain async callable, and where a real
client is exercised it is wired to ``httpx.MockTransport`` the way
``test_coriqo_async.py`` does it. Time is injected too — a staleness test that
actually slept would either be slow or lie about the boundary.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from byoai.errors import ByoAIError, EnforcementIdentityUnavailableError
from byoai.recorder.coriqo_agents import CoriqoCredentials
from byoai.recorder.coriqo_async import AsyncCoriqoAgentsClient
from byoai.recorder.identity import CoriqoIdentity
from byoai.recorder.keys import load_or_create_device_key
from byoai.recorder.mandate import (
    MODEL_MESSAGE,
    Allow,
    Deny,
    Flag,
    MandateGate,
    MandateSnapshot,
    Posture,
    ProposedAction,
    Reason,
    mandate_gate,
)

_AGENT = "coriqo-agent-1"
_BASE = "https://coriqo.test"

POSTURES = [Posture.FAIL_OPEN, Posture.FAIL_CLOSED]


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def snapshot_payload(**overrides) -> dict:
    payload = {
        "mandate_version_id": "mv_1",
        "allowed_tools": ["search"],
        "status": "approved",
        "mandate_enforcement": "enforce",
        "enforcement_posture": Posture.FAIL_OPEN,
        "max_staleness_s": 60,
        "delegation_policy": "none",
        "max_delegation_depth": 0,
        "served_at": "2026-08-25T10:00:00Z",
    }
    payload.update(overrides)
    return payload


def gate_with(clock: FakeClock | None = None, **overrides) -> MandateGate:
    """A started gate holding one snapshot, without a refresh loop."""
    clock = clock or FakeClock()

    async def never_called(_etag):  # pragma: no cover - decide does no I/O
        raise AssertionError("decide must not refresh")

    gate = MandateGate(never_called, agent_id=_AGENT, clock=clock)
    gate.apply_snapshot(snapshot_payload(**overrides))
    return gate


# -- the table -------------------------------------------------------------


@pytest.mark.parametrize("posture", POSTURES)
def test_fresh_and_in_scope_allows(posture: str):
    verdict = gate_with(enforcement_posture=posture).decide("search")
    assert type(verdict) is Allow
    assert verdict.reason == Reason.IN_SCOPE
    assert verdict.mandate_version_id == "mv_1"
    assert verdict.snapshot_age_s == 0.0


@pytest.mark.parametrize("posture", POSTURES)
def test_fresh_out_of_scope_under_enforce_denies_under_both_postures(posture: str):
    verdict = gate_with(enforcement_posture=posture).decide("rm")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.OUT_OF_SCOPE
    assert not verdict.allowed


@pytest.mark.parametrize("posture", POSTURES)
def test_fresh_out_of_scope_under_observe_allows_and_flags(posture: str):
    gate = gate_with(enforcement_posture=posture, mandate_enforcement="observe")
    verdict = gate.decide("rm")
    assert isinstance(verdict, Flag)
    assert verdict.allowed and verdict.flagged
    assert verdict.reason == Reason.OUT_OF_SCOPE_OBSERVED


def test_stale_snapshot_flags_under_fail_open():
    clock = FakeClock()
    gate = gate_with(clock, enforcement_posture=Posture.FAIL_OPEN)
    clock.advance(61)
    verdict = gate.decide("search")
    assert isinstance(verdict, Flag)
    assert verdict.allowed
    assert verdict.reason == Reason.SNAPSHOT_STALE
    assert verdict.snapshot_age_s == pytest.approx(61)


def test_stale_snapshot_denies_under_fail_closed():
    clock = FakeClock()
    gate = gate_with(clock, enforcement_posture=Posture.FAIL_CLOSED)
    clock.advance(61)
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.SNAPSHOT_STALE


def test_a_snapshot_exactly_at_the_budget_is_still_fresh():
    """The budget is what the tenant said it may be, not one tick less."""
    clock = FakeClock()
    gate = gate_with(clock, enforcement_posture=Posture.FAIL_CLOSED)
    clock.advance(60)
    assert isinstance(gate.decide("search"), Allow)


@pytest.mark.parametrize("posture", POSTURES)
def test_suspension_denies_under_both_postures(posture: str):
    """Including fail_open: a suspension is a decision Coriqo made and this
    runtime read, not a failure to evaluate."""
    gate = gate_with(enforcement_posture=posture, status="suspended")
    verdict = gate.decide("search")  # in scope, and still denied
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.AGENT_SUSPENDED


@pytest.mark.parametrize("posture", POSTURES)
def test_suspension_denies_even_while_only_observing(posture: str):
    gate = gate_with(
        enforcement_posture=posture, status="suspended", mandate_enforcement="observe"
    )
    assert isinstance(gate.decide("search"), Deny)


def test_no_snapshot_flags_under_fail_open():
    gate = MandateGate(
        _source_returning(snapshot_payload()), default_posture=Posture.FAIL_OPEN
    )
    verdict = gate.decide("search")
    assert isinstance(verdict, Flag)
    assert verdict.allowed
    assert verdict.reason == Reason.NO_SNAPSHOT
    assert verdict.snapshot_age_s is None
    assert verdict.mandate_version_id is None


def test_no_snapshot_denies_under_fail_closed():
    gate = MandateGate(
        _source_returning(snapshot_payload()), default_posture=Posture.FAIL_CLOSED
    )
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.NO_SNAPSHOT


def test_the_posture_from_the_snapshot_beats_the_default():
    """The default posture is a bootstrap value, not a policy: once the tenant
    has told this runtime what its posture is, that wins."""
    gate = MandateGate(
        _source_returning(snapshot_payload()), default_posture=Posture.FAIL_CLOSED
    )
    gate.apply_snapshot(snapshot_payload(enforcement_posture=Posture.FAIL_OPEN))
    assert gate.posture == Posture.FAIL_OPEN


# -- null vs empty ---------------------------------------------------------


@pytest.mark.parametrize("posture", POSTURES)
def test_null_allowed_tools_is_unrestricted(posture: str):
    """`null` is no scope constraint. Coercing it to [] would fail closed on an
    agent nobody restricted."""
    gate = gate_with(allowed_tools=None, enforcement_posture=posture)
    assert gate.snapshot is not None and gate.snapshot.unrestricted
    for tool in ("search", "rm", "anything-at-all"):
        verdict = gate.decide(tool)
        assert type(verdict) is Allow
        assert verdict.reason == Reason.UNRESTRICTED


@pytest.mark.parametrize("posture", POSTURES)
def test_empty_allowed_tools_permits_nothing(posture: str):
    """`[]` is the opposite instruction from `null`, and the one a falsy check
    silently turns into 'unrestricted'."""
    gate = gate_with(allowed_tools=[], enforcement_posture=posture)
    assert gate.snapshot is not None and not gate.snapshot.unrestricted
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.OUT_OF_SCOPE


def test_empty_allowed_tools_under_observe_still_flags_rather_than_denies():
    gate = gate_with(allowed_tools=[], mandate_enforcement="observe")
    assert isinstance(gate.decide("search"), Flag)


def test_the_snapshot_keeps_null_and_empty_distinguishable():
    now = 0.0
    unrestricted = MandateSnapshot.from_payload({"allowed_tools": None}, received_at=now)
    nothing = MandateSnapshot.from_payload({"allowed_tools": []}, received_at=now)
    assert unrestricted.allowed_tools is None
    assert nothing.allowed_tools == ()
    assert unrestricted.permits("rm") and not nothing.permits("rm")


def test_a_non_list_allowed_tools_is_rejected_rather_than_guessed_at():
    with pytest.raises(ByoAIError):
        MandateSnapshot.from_payload({"allowed_tools": "search"}, received_at=0.0)


# -- denial semantics ------------------------------------------------------


def test_a_denial_tells_the_model_nothing_it_could_route_around():
    gate = gate_with()
    action = ProposedAction(tool="wire_transfer", trajectory_id="tr_1", step_index=4)
    verdict = gate.decide(action)
    assert isinstance(verdict, Deny)
    assert verdict.model_message == MODEL_MESSAGE
    for leak in ("wire_transfer", "search", "mv_1", "mandate", "scope"):
        assert leak not in verdict.model_message
    # Everything an operator needs is still on the verdict, for the record.
    assert verdict.tool == "wire_transfer"
    assert verdict.trajectory_id == "tr_1" and verdict.step_index == 4
    assert verdict.detail and "wire_transfer" in verdict.detail


def test_every_denial_reason_shares_the_same_model_message():
    """A model that could tell 'suspended' from 'out of scope' from the message
    could tell when waiting or rephrasing is worth trying."""
    clock = FakeClock()
    stale = gate_with(clock, enforcement_posture=Posture.FAIL_CLOSED)
    clock.advance(999)
    denials = [
        gate_with().decide("rm"),
        gate_with(status="suspended").decide("search"),
        stale.decide("search"),
        MandateGate(
            _source_returning(snapshot_payload()), default_posture=Posture.FAIL_CLOSED
        ).decide("search"),
    ]
    assert all(isinstance(d, Deny) for d in denials)
    assert {d.model_message for d in denials} == {MODEL_MESSAGE}
    assert len({d.reason for d in denials}) == 4


def test_a_flagged_allow_is_an_allow():
    verdict = gate_with(mandate_enforcement="observe").decide("rm")
    assert isinstance(verdict, Allow)
    assert verdict.allowed and verdict.flagged
    assert verdict.verdict == "flagged"


def test_verdict_words_match_what_coriqo_records():
    assert gate_with().decide("search").verdict == "allowed"
    assert gate_with().decide("rm").verdict == "blocked"


# -- refresh ---------------------------------------------------------------


def _source_returning(*payloads):
    """An async source yielding each payload in turn, then repeating the last."""
    queue = list(payloads)

    async def fetch(_etag):
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return payload, "etag-1"

    return fetch


async def test_refresh_installs_a_snapshot_and_a_304_keeps_it():
    calls: list[str | None] = []

    async def fetch(etag):
        calls.append(etag)
        if etag is None:
            return snapshot_payload(), 'W/"v1"'
        return None, None  # 304: unchanged, no body

    clock = FakeClock()
    gate = MandateGate(fetch, agent_id=_AGENT, clock=clock)

    await gate.refresh()
    assert gate.snapshot is not None
    assert gate.snapshot.allowed_tools == ("search",)

    clock.advance(30)
    await gate.refresh()

    assert calls == [None, 'W/"v1"'], "the second refresh sent If-None-Match"
    assert gate.snapshot is not None
    assert gate.snapshot.allowed_tools == ("search",), "a 304 is not an empty mandate"
    assert gate.snapshot_age_s() == 0.0, "a 304 means current, not merely recent"
    assert isinstance(gate.decide("search"), Allow)


async def test_a_304_before_any_snapshot_does_not_invent_one():
    async def fetch(_etag):
        return None, None

    gate = MandateGate(fetch, agent_id=_AGENT, default_posture=Posture.FAIL_CLOSED)
    await gate.refresh()
    assert gate.snapshot is None
    assert gate.decide("search").reason == Reason.NO_SNAPSHOT


async def test_a_failed_refresh_keeps_a_still_valid_snapshot():
    """One blip must not become a fail-closed outage: the cached mandate stays
    until staleness — not reachability — says otherwise."""
    fail = False

    async def fetch(_etag):
        if fail:
            raise httpx.ConnectError("no route to host")
        return snapshot_payload(enforcement_posture=Posture.FAIL_CLOSED), "etag-1"

    clock = FakeClock()
    gate = MandateGate(fetch, agent_id=_AGENT, clock=clock)
    await gate.refresh()

    fail = True
    clock.advance(10)
    assert await gate.refresh_safely() is False

    assert gate.snapshot is not None
    assert gate.snapshot.mandate_version_id == "mv_1"
    assert isinstance(gate.last_refresh_error, httpx.ConnectError)
    assert isinstance(gate.decide("search"), Allow)

    # ...and only staleness ends it — the snapshot survives the outage, then
    # ages out of it, under the strictest posture there is.
    clock.advance(100)
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.SNAPSHOT_STALE


async def test_ageing_past_the_budget_flips_the_verdict():
    clock = FakeClock()
    gate = MandateGate(
        _source_returning(snapshot_payload(enforcement_posture=Posture.FAIL_CLOSED)),
        agent_id=_AGENT,
        clock=clock,
    )
    await gate.refresh()
    assert isinstance(gate.decide("search"), Allow)
    clock.advance(60.5)
    assert isinstance(gate.decide("search"), Deny)


async def test_the_refresh_interval_follows_the_agents_own_staleness_budget():
    gate = MandateGate(_source_returning(snapshot_payload(max_staleness_s=600)))
    assert gate.refresh_interval_s() == 150.0  # the default budget, halved
    await gate.refresh()
    assert gate.refresh_interval_s() == 300.0
    assert gate.refresh_interval_s() < 600, "a refresh must land inside the budget"


async def test_a_tiny_staleness_budget_does_not_become_a_busy_poll():
    gate = MandateGate(_source_returning(snapshot_payload(max_staleness_s=0.2)))
    await gate.refresh()
    assert gate.refresh_interval_s() == 1.0


async def test_the_loop_refreshes_on_its_interval_and_stops_cleanly():
    calls = 0

    async def fetch(_etag):
        nonlocal calls
        calls += 1
        return snapshot_payload(max_staleness_s=0.02), None

    gate = MandateGate(fetch, agent_id=_AGENT, min_refresh_interval_s=0.01)
    async with gate:
        assert calls == 1, "start() fetches once before returning"
        for _ in range(50):
            if calls > 1:
                break
            await asyncio.sleep(0.01)
    assert calls > 1
    before = calls
    await asyncio.sleep(0.03)
    assert calls == before, "stopping ends the loop"


async def test_the_loop_survives_a_failing_refresh():
    calls = 0

    async def fetch(_etag):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down")

    gate = MandateGate(
        fetch,
        agent_id=_AGENT,
        default_max_staleness_s=0.02,
        min_refresh_interval_s=0.01,
    )
    async with gate:
        for _ in range(50):
            if calls > 1:
                break
            await asyncio.sleep(0.01)
    assert calls > 1, "a refresh loop that dies on one failure never recovers"


# -- thread safety ---------------------------------------------------------


async def test_concurrent_decides_during_a_refresh_never_see_a_torn_snapshot():
    """Publishing already runs on worker threads here, so decide() is called
    off the event loop while the loop is swapping snapshots underneath it."""
    versions = [snapshot_payload(mandate_version_id=f"mv_{i}") for i in range(200)]

    async def fetch(_etag):
        return versions.pop(0) if versions else snapshot_payload(), None

    gate = MandateGate(fetch, agent_id=_AGENT)
    await gate.refresh()

    stop = threading.Event()
    verdicts: list[object] = []
    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            while not stop.is_set():
                verdicts.append(gate.decide("search"))
                verdicts.append(gate.decide("rm"))
        except BaseException as exc:  # pragma: no cover - only on a real bug
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(100):
            await gate.refresh()
            await asyncio.sleep(0)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

    assert not errors
    assert len(verdicts) > 100
    allows = [v for v in verdicts if isinstance(v, Allow)]
    denies = [v for v in verdicts if isinstance(v, Deny)]
    assert allows and denies
    assert all(v.mandate_version_id and v.snapshot_age_s is not None for v in verdicts)


# -- construction ----------------------------------------------------------


def test_no_identity_gives_a_no_op_gate_that_logs_once(monkeypatch, caplog):
    """Adoptable before enrolment: the code path exists, it just doesn't gate."""
    monkeypatch.setattr("byoai.recorder.mandate.resolve_identity", lambda: None)
    gate = mandate_gate(_AGENT, default_posture=Posture.FAIL_CLOSED)

    assert not gate.enabled
    with caplog.at_level("INFO", logger="byoai.recorder.mandate"):
        verdicts = [gate.decide("rm"), gate.decide("anything")]

    assert all(type(v) is Allow for v in verdicts), "fail_closed must not brick a host"
    assert all(v.reason == Reason.ENFORCEMENT_UNCONFIGURED for v in verdicts)
    assert not any(v.flagged for v in verdicts)
    assert len([r for r in caplog.records if "mandate gate" not in r.message]) <= 1
    assert sum("not gated" in r.message for r in caplog.records) == 1


def test_a_static_api_key_identity_is_refused_rather_than_silently_ungated():
    identity = CoriqoIdentity.from_credentials(
        CoriqoCredentials(base_url=_BASE, api_key="cq_sa_test", tenant_slug="acme_bank")
    )
    with pytest.raises(EnforcementIdentityUnavailableError):
        mandate_gate(_AGENT, identity=identity)


async def test_a_gate_built_on_the_real_client_refreshes_over_a_signed_request(tmp_path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-none-match") == 'W/"v1"':
            return httpx.Response(304, headers={"etag": 'W/"v1"'})
        return httpx.Response(
            200, json=snapshot_payload(), headers={"etag": 'W/"v1"'}
        )

    key = load_or_create_device_key(tmp_path)
    identity = CoriqoIdentity.from_device(
        base_url=_BASE, device_id=key.device_id, signer=key, tenant_slug="acme_bank"
    )
    client = AsyncCoriqoAgentsClient(
        identity,
        http_client=httpx.AsyncClient(
            base_url=_BASE, transport=httpx.MockTransport(handler)
        ),
    )
    gate = mandate_gate(_AGENT, client=client)
    try:
        await gate.refresh()
        await gate.refresh()
    finally:
        await client.close()

    assert len(seen) == 2
    assert "X-Coriqo-Signature" in seen[0].headers
    assert "if-none-match" not in seen[0].headers
    assert seen[1].headers["if-none-match"] == 'W/"v1"'
    assert gate.snapshot is not None
    assert gate.snapshot.allowed_tools == ("search",), "the 304 kept the snapshot"
    assert isinstance(gate.decide("search"), Allow)


def test_the_posture_env_var_only_covers_the_pre_snapshot_window(monkeypatch):
    monkeypatch.setenv("BYOAI_MANDATE_POSTURE", "fail_closed")
    gate = MandateGate(_source_returning(snapshot_payload()))
    assert gate.posture == Posture.FAIL_CLOSED
    assert isinstance(gate.decide("search"), Deny)
    gate.apply_snapshot(snapshot_payload())
    assert gate.posture == Posture.FAIL_OPEN


def test_an_unreadable_posture_env_var_falls_back_to_fail_open(monkeypatch):
    monkeypatch.setenv("BYOAI_MANDATE_POSTURE", "yes please")
    assert MandateGate(_source_returning(snapshot_payload())).posture == Posture.FAIL_OPEN
