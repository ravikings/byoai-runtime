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
    RequireApproval,
    approval_request_id,
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


# -- AD-9: on_suspend_observed fires once, on the transition ----------------

def test_on_suspend_observed_fires_on_the_transition_into_suspended():
    seen = []

    async def never_called(_etag):  # pragma: no cover
        raise AssertionError("apply_snapshot is called directly in this test")

    gate = MandateGate(
        never_called, agent_id=_AGENT, on_suspend_observed=lambda snap: seen.append(snap)
    )
    gate.apply_snapshot(snapshot_payload(status="approved"))
    assert seen == []  # not suspended yet — no callback

    gate.apply_snapshot(snapshot_payload(status="suspended"))
    assert len(seen) == 1
    assert seen[0].mandate_version_id == "mv_1"


def test_on_suspend_observed_does_not_refire_on_a_repeat_suspended_fetch():
    seen = []

    async def never_called(_etag):  # pragma: no cover
        raise AssertionError

    gate = MandateGate(
        never_called, agent_id=_AGENT, on_suspend_observed=lambda snap: seen.append(snap)
    )
    gate.apply_snapshot(snapshot_payload(status="suspended"))
    gate.apply_snapshot(snapshot_payload(status="suspended"))
    gate.apply_snapshot(snapshot_payload(status="suspended"))
    assert len(seen) == 1  # one transition, not one per fetch


def test_on_suspend_observed_refires_after_a_resume_and_re_suspend():
    seen = []

    async def never_called(_etag):  # pragma: no cover
        raise AssertionError

    gate = MandateGate(
        never_called, agent_id=_AGENT, on_suspend_observed=lambda snap: seen.append(snap)
    )
    gate.apply_snapshot(snapshot_payload(status="suspended"))
    gate.apply_snapshot(snapshot_payload(status="approved"))
    gate.apply_snapshot(snapshot_payload(status="suspended"))
    assert len(seen) == 2  # two distinct suspend cycles


def test_a_raising_on_suspend_observed_callback_does_not_break_apply_snapshot():
    def boom(_snap):
        raise RuntimeError("network is down")

    async def never_called(_etag):  # pragma: no cover
        raise AssertionError

    gate = MandateGate(never_called, agent_id=_AGENT, on_suspend_observed=boom)
    # Must not raise — a failed ack callback is not allowed to break the
    # fetch that installed the (already-applied) suspended snapshot.
    snapshot = gate.apply_snapshot(snapshot_payload(status="suspended"))
    assert snapshot.suspended is True
    assert gate.snapshot.suspended is True


# -- AD-10: approval-required tools -----------------------------------------

def test_approval_required_tool_is_denied_for_now_not_a_scope_violation():
    gate = gate_with(approval_required_tools=["send_payment"])
    verdict = gate.decide("send_payment")
    assert isinstance(verdict, RequireApproval)
    assert verdict.allowed is False
    assert verdict.reason == Reason.APPROVAL_REQUIRED
    assert verdict.request_id == approval_request_id("send_payment", None, None)


def test_approval_required_verdict_carries_the_fixed_model_message():
    gate = gate_with(approval_required_tools=["send_payment"])
    verdict = gate.decide("send_payment")
    assert verdict.model_message == MODEL_MESSAGE


def test_a_retried_identical_call_computes_the_same_request_id():
    gate = gate_with(approval_required_tools=["send_payment"])
    a = gate.decide(ProposedAction(tool="send_payment", trajectory_id="t1", step_index=0))
    b = gate.decide(ProposedAction(tool="send_payment", trajectory_id="t1", step_index=0))
    assert a.request_id == b.request_id


def test_a_different_call_to_the_same_tool_gets_a_different_request_id():
    gate = gate_with(approval_required_tools=["send_payment"])
    a = gate.decide(ProposedAction(tool="send_payment", trajectory_id="t1", step_index=0))
    b = gate.decide(ProposedAction(tool="send_payment", trajectory_id="t1", step_index=1))
    assert a.request_id != b.request_id


def test_calls_with_no_trajectory_tracking_still_distinguish_by_arguments():
    # trajectory_id/step_index are both optional — without folding arguments
    # into the id, two unrelated calls with neither set would collide and an
    # approval for one would silently approve the other.
    gate = gate_with(approval_required_tools=["send_payment"])
    a = gate.decide(ProposedAction(tool="send_payment", arguments={"amount": 100}))
    b = gate.decide(ProposedAction(tool="send_payment", arguments={"amount": 200}))
    assert a.request_id != b.request_id


def test_calls_with_no_trajectory_and_no_arguments_still_dedupe_a_retry():
    gate = gate_with(approval_required_tools=["send_payment"])
    a = gate.decide(ProposedAction(tool="send_payment"))
    b = gate.decide(ProposedAction(tool="send_payment"))
    assert a.request_id == b.request_id


def test_an_approved_request_id_allows_that_exact_call():
    request_id = approval_request_id("send_payment", None, None)
    gate = gate_with(
        approval_required_tools=["send_payment"],
        resolved_approvals={request_id: "approved"},
    )
    verdict = gate.decide("send_payment")
    assert isinstance(verdict, Allow)
    assert verdict.reason == Reason.APPROVAL_GRANTED


def test_a_denied_request_id_denies_that_exact_call():
    request_id = approval_request_id("send_payment", None, None)
    gate = gate_with(
        approval_required_tools=["send_payment"],
        resolved_approvals={request_id: "denied"},
    )
    verdict = gate.decide("send_payment")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.APPROVAL_REFUSED


def test_approval_required_is_checked_before_allowed_tools_scope():
    # A tool in BOTH lists is unusual (Coriqo's own job to keep them
    # disjoint), but the gate must still resolve deterministically —
    # approval-required wins, since it's the stricter of the two answers.
    gate = gate_with(allowed_tools=["send_payment"], approval_required_tools=["send_payment"])
    assert isinstance(gate.decide("send_payment"), RequireApproval)


def test_suspension_still_wins_over_approval_required():
    gate = gate_with(status="suspended", approval_required_tools=["send_payment"])
    verdict = gate.decide("send_payment")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.AGENT_SUSPENDED


def test_report_approval_request_invokes_the_callback_once_per_request_id():
    async def never_called(_etag):  # pragma: no cover
        raise AssertionError

    seen = []
    gate = MandateGate(
        never_called, agent_id=_AGENT, report_approval_request=lambda v: seen.append(v.request_id)
    )
    gate.apply_snapshot(snapshot_payload(approval_required_tools=["send_payment"]))
    verdict = gate.decide("send_payment")
    gate.report_approval_request(verdict)
    gate.report_approval_request(verdict)  # a retry must not double-report
    assert seen == [verdict.request_id]


def test_a_failed_report_is_not_marked_reported_so_a_retry_can_succeed():
    async def never_called(_etag):  # pragma: no cover
        raise AssertionError

    calls = []

    def flaky(v):
        calls.append(v.request_id)
        if len(calls) == 1:
            raise RuntimeError("transient network failure")

    gate = MandateGate(never_called, agent_id=_AGENT, report_approval_request=flaky)
    gate.apply_snapshot(snapshot_payload(approval_required_tools=["send_payment"]))
    verdict = gate.decide("send_payment")

    gate.report_approval_request(verdict)  # fails, must not be marked reported
    gate.report_approval_request(verdict)  # the host's own retry — must actually try again
    assert calls == [verdict.request_id, verdict.request_id]


def test_report_approval_request_swallows_a_raising_callback():
    async def never_called(_etag):  # pragma: no cover
        raise AssertionError

    def boom(_verdict):
        raise RuntimeError("network is down")

    gate = MandateGate(never_called, agent_id=_AGENT, report_approval_request=boom)
    gate.apply_snapshot(snapshot_payload(approval_required_tools=["send_payment"]))
    verdict = gate.decide("send_payment")
    gate.report_approval_request(verdict)  # must not raise


# -- AD-11: enforced budgets -------------------------------------------------

def test_call_rate_ceiling_denies_once_the_window_is_full():
    clock = FakeClock()
    gate = gate_with(clock=clock, max_calls_per_minute=2)
    assert isinstance(gate.decide("search"), Allow)
    assert isinstance(gate.decide("search"), Allow)
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.BUDGET_CALL_RATE_EXCEEDED


def test_call_rate_ceiling_recovers_once_the_window_slides_past():
    clock = FakeClock()
    gate = gate_with(clock=clock, max_calls_per_minute=1)
    assert isinstance(gate.decide("search"), Allow)
    assert isinstance(gate.decide("search"), Deny)
    clock.advance(61)
    assert isinstance(gate.decide("search"), Allow)


def test_step_ceiling_denies_a_new_step_past_the_limit():
    gate = gate_with(max_run_steps=2)
    assert isinstance(gate.decide(ProposedAction(tool="search", trajectory_id="t1", step_index=0)), Allow)
    assert isinstance(gate.decide(ProposedAction(tool="search", trajectory_id="t1", step_index=1)), Allow)
    verdict = gate.decide(ProposedAction(tool="search", trajectory_id="t1", step_index=2))
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.BUDGET_STEP_LIMIT_EXCEEDED


def test_step_ceiling_does_not_count_a_repeated_step_index_twice():
    gate = gate_with(max_run_steps=1)
    assert isinstance(gate.decide(ProposedAction(tool="search", trajectory_id="t1", step_index=0)), Allow)
    # Same step retried — must not be treated as a second distinct step.
    assert isinstance(gate.decide(ProposedAction(tool="search", trajectory_id="t1", step_index=0)), Allow)


def test_step_ceiling_is_tracked_per_trajectory_not_globally():
    gate = gate_with(max_run_steps=1)
    assert isinstance(gate.decide(ProposedAction(tool="search", trajectory_id="t1", step_index=0)), Allow)
    # A different trajectory has its own budget.
    assert isinstance(gate.decide(ProposedAction(tool="search", trajectory_id="t2", step_index=0)), Allow)


def test_cost_ceiling_denies_once_spent_meets_the_limit():
    gate = gate_with(max_run_cost_usd=10.0)
    gate.decide(ProposedAction(tool="search", trajectory_id="t1"))
    gate.record_actual_cost("t1", 10.0)
    verdict = gate.decide(ProposedAction(tool="search", trajectory_id="t1"))
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.BUDGET_COST_EXCEEDED


def test_cost_ceiling_allows_while_under_the_limit():
    gate = gate_with(max_run_cost_usd=10.0)
    gate.record_actual_cost("t1", 5.0)
    assert isinstance(gate.decide(ProposedAction(tool="search", trajectory_id="t1")), Allow)


def test_no_budgets_configured_never_denies_on_their_account():
    gate = gate_with()
    for _ in range(500):
        assert isinstance(gate.decide("search"), Allow)


def test_suspension_still_wins_over_a_budget_breach():
    gate = gate_with(status="suspended", max_calls_per_minute=1)
    gate.decide("search")
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.AGENT_SUSPENDED


def test_a_denied_out_of_scope_call_does_not_consume_call_rate_budget():
    # Regression: budget checks used to run (and record) before the scope
    # check, so retrying a disallowed tool burned rate-window slots the
    # trajectory's real, in-scope calls needed.
    gate = gate_with(allowed_tools=["search"], max_calls_per_minute=1)
    for _ in range(5):
        assert isinstance(gate.decide("not_allowed_tool"), Deny)
    # The rate ceiling must still be untouched — a single in-scope call fits.
    assert isinstance(gate.decide("search"), Allow)


def test_a_pending_approval_call_does_not_consume_step_budget():
    gate = gate_with(approval_required_tools=["send_payment"], max_run_steps=1)
    for step in range(5):
        verdict = gate.decide(ProposedAction(tool="send_payment", trajectory_id="t1", step_index=step))
        assert isinstance(verdict, RequireApproval)
    # The step ceiling must still be untouched — a single in-scope step fits.
    assert isinstance(gate.decide(ProposedAction(tool="search", trajectory_id="t1", step_index=99)), Allow)


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


# -- W-7: reassessment_required -----------------------------------------------


def test_reassessment_required_denies_under_fail_closed():
    gate = MandateGate(
        _source_returning(
            snapshot_payload(
                enforcement_posture=Posture.FAIL_CLOSED, reassessment_required=True
            )
        )
    )
    asyncio.run(gate.refresh())
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.REASSESSMENT_REQUIRED


def test_reassessment_required_only_flags_under_fail_open():
    gate = MandateGate(
        _source_returning(
            snapshot_payload(
                enforcement_posture=Posture.FAIL_OPEN, reassessment_required=True
            )
        )
    )
    asyncio.run(gate.refresh())
    verdict = gate.decide("search")
    assert isinstance(verdict, Flag)
    assert verdict.allowed
    assert verdict.reason == Reason.REASSESSMENT_REQUIRED


def test_reassessment_required_absent_is_false_by_default():
    gate = MandateGate(_source_returning(snapshot_payload()))
    asyncio.run(gate.refresh())
    assert gate.snapshot.reassessment_required is False
    assert isinstance(gate.decide("search"), Allow)


# -- W-7: capability attestation ----------------------------------------------


def _tool(name: str) -> dict:
    return {"name": name, "description": "d", "input_schema": {"type": "object"}}


def test_attest_capabilities_sends_on_first_call():
    reported: list[dict] = []
    gate = MandateGate(
        None, agent_id=_AGENT, report_capability_snapshot=reported.append
    )
    sent = gate.attest_capabilities([_tool("search")], model_id="m1")
    assert sent is True
    assert len(reported) == 1
    assert reported[0]["model_id"] == "m1"


def test_attest_capabilities_skips_when_digest_unchanged():
    reported: list[dict] = []
    gate = MandateGate(
        None, agent_id=_AGENT, report_capability_snapshot=reported.append
    )
    assert gate.attest_capabilities([_tool("search")], model_id="m1") is True
    assert gate.attest_capabilities([_tool("search")], model_id="m1") is False
    assert len(reported) == 1


def test_attest_capabilities_sends_again_when_digest_changes():
    reported: list[dict] = []
    gate = MandateGate(
        None, agent_id=_AGENT, report_capability_snapshot=reported.append
    )
    assert gate.attest_capabilities([_tool("search")], model_id="m1") is True
    assert gate.attest_capabilities([_tool("search"), _tool("write")], model_id="m1") is True
    assert len(reported) == 2


def test_attest_capabilities_force_bypasses_the_skip():
    reported: list[dict] = []
    gate = MandateGate(
        None, agent_id=_AGENT, report_capability_snapshot=reported.append
    )
    assert gate.attest_capabilities([_tool("search")], model_id="m1") is True
    assert gate.attest_capabilities([_tool("search")], model_id="m1", force=True) is True
    assert len(reported) == 2


def test_attest_capabilities_with_no_callback_is_a_safe_no_op():
    gate = MandateGate(None, agent_id=_AGENT)
    assert gate.attest_capabilities([_tool("search")], model_id="m1") is False


def test_attest_capabilities_callback_failure_is_swallowed():
    def _raise(_snapshot: dict) -> None:
        raise RuntimeError("boom")

    gate = MandateGate(None, agent_id=_AGENT, report_capability_snapshot=_raise)
    assert gate.attest_capabilities([_tool("search")], model_id="m1") is False
