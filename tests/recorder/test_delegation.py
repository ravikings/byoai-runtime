"""Delegated scope attenuation: a sub-agent can only ever be narrower.

No network — gates are seeded with ``apply_snapshot``. The claim under test
throughout is one-directional: every delegation produces a scope that is a
subset of the delegator's, and none of the ways a caller can get it wrong
(a missing list, an empty list, an undeclared hand-off) produce a wider one.
"""

from __future__ import annotations

import pytest

from byoai.errors import MandateDeniedError, MandateRunHaltedError
from byoai.recorder.delegation import (
    DelegatedGate,
    DelegationRefusedError,
    EffectiveScope,
    delegate,
    delegated_gate,
    intersect_tools,
)
from byoai.recorder.denial_latch import DenialLatch, run_scope, use_denial_latch
from byoai.recorder.governed_tool import governed_tool, use_gate
from byoai.recorder.mandate import (
    MODEL_MESSAGE,
    Allow,
    DelegationPolicy,
    Deny,
    Flag,
    MandateGate,
    MandateSnapshot,
    Posture,
    Reason,
)

_PARENT = "agent-parent"
_CHILD = "agent-child"


def snapshot_payload(**overrides) -> dict:
    payload = {
        "mandate_version_id": "mv_parent",
        "allowed_tools": ["search", "read_file"],
        "status": "approved",
        "mandate_enforcement": "enforce",
        "enforcement_posture": Posture.FAIL_OPEN,
        "max_staleness_s": 300,
        "delegation_policy": DelegationPolicy.ATTENUATED,
    }
    payload.update(overrides)
    return payload


def snapshot(**overrides) -> MandateSnapshot:
    return MandateSnapshot.from_payload(snapshot_payload(**overrides), received_at=0.0)


def gate_with(agent_id: str, **overrides) -> MandateGate:
    """A gate whose mandate version is its own by default.

    Distinct versions per agent on purpose: fixtures that give the delegator and
    the delegated child the same ``mandate_version_id`` cannot tell "the runtime
    read the right version" from "both versions happened to be equal", and that
    is exactly the class of bug this file has to catch.
    """

    async def never_called(_etag):  # pragma: no cover - decide does no I/O
        raise AssertionError("the decide path must not refresh")

    overrides.setdefault("mandate_version_id", "mv_" + agent_id.rsplit("-", 1)[-1])
    gate = MandateGate(never_called, agent_id=agent_id)
    gate.apply_snapshot(snapshot_payload(**overrides))
    return gate


def parent_scope(**overrides) -> EffectiveScope:
    return EffectiveScope.from_snapshot(snapshot(**overrides), agent_id=_PARENT)


@pytest.fixture(autouse=True)
def _fresh_latch():
    """Repeated denials in one test must not halt the run under test."""
    with use_denial_latch(DenialLatch(threshold=1000)):
        yield


# -- the intersection itself -------------------------------------------------


def test_delegated_scope_is_the_intersection():
    child = delegate(
        parent_scope(),
        parent_snapshot=snapshot(),
        child_agent_id=_CHILD,
        child_tools=["read_file", "wire_transfer"],
    )
    assert child.allowed_tools == ("read_file",)


def test_delegation_cannot_widen():
    """The child's own mandate names a tool the parent was never approved for.
    It does not come along."""
    child = delegate(
        parent_scope(allowed_tools=["search"]),
        parent_snapshot=snapshot(allowed_tools=["search"]),
        child_agent_id=_CHILD,
        child_tools=["search", "wire_transfer", "delete_account"],
    )
    assert child.allowed_tools == ("search",)
    assert not child.permits("wire_transfer")


def test_an_unrestricted_parent_yields_the_childs_own_list():
    """``None`` is *no restriction from this side*, not *nothing permitted*."""
    child = delegate(
        parent_scope(allowed_tools=None),
        parent_snapshot=snapshot(allowed_tools=None),
        child_agent_id=_CHILD,
        child_tools=["search"],
    )
    assert child.allowed_tools == ("search",)
    assert not child.unrestricted


def test_an_unrestricted_child_yields_the_parents_list():
    child = delegate(
        parent_scope(),
        parent_snapshot=snapshot(),
        child_agent_id=_CHILD,
        child_tools=None,
    )
    assert child.allowed_tools == ("search", "read_file")


def test_both_unrestricted_stays_unrestricted():
    child = delegate(
        parent_scope(allowed_tools=None),
        parent_snapshot=snapshot(allowed_tools=None),
        child_agent_id=_CHILD,
        child_tools=None,
    )
    assert child.allowed_tools is None
    assert child.unrestricted


@pytest.mark.parametrize("parent_tools", [None, [], ["search"]])
def test_intersection_with_an_empty_list_is_empty(parent_tools):
    """``[]`` permits nothing, and no partner widens it — including ``None``."""
    assert intersect_tools(parent_tools, []) == ()


def test_an_empty_parent_scope_cannot_be_widened_by_the_child():
    child = delegate(
        parent_scope(allowed_tools=[]),
        parent_snapshot=snapshot(allowed_tools=[]),
        child_agent_id=_CHILD,
        child_tools=["search"],
    )
    assert child.allowed_tools == ()
    assert not child.permits("search")


def test_empty_and_unrestricted_are_never_confused():
    assert intersect_tools(None, None) is None
    assert intersect_tools(None, []) == ()
    assert intersect_tools([], None) == ()
    assert EffectiveScope(allowed_tools=()).permits("search") is False
    assert EffectiveScope(allowed_tools=None).permits("search") is True


def test_a_child_tool_list_given_as_a_string_is_refused_not_iterated():
    with pytest.raises(Exception, match="list or None"):
        delegate(
            parent_scope(),
            parent_snapshot=snapshot(),
            child_agent_id=_CHILD,
            child_tools="search",  # type: ignore[arg-type]
        )


# -- pinning and chaining ----------------------------------------------------


def test_the_delegated_scope_is_pinned_to_the_delegators_mandate_version():
    child = delegate(
        parent_scope(),
        parent_snapshot=snapshot(),
        child_agent_id=_CHILD,
        child_tools=["search"],
    )
    assert child.mandate_version_id == "mv_parent"
    assert child.delegated is True
    assert child.chain == (_PARENT, _CHILD)


def test_a_chain_narrows_monotonically():
    first = delegate(
        parent_scope(allowed_tools=["a", "b", "c"]),
        parent_snapshot=snapshot(allowed_tools=["a", "b", "c"], max_delegation_depth=3),
        child_agent_id="agent-b",
        child_tools=["a", "b"],
    )
    second = delegate(
        first,
        parent_snapshot=snapshot(allowed_tools=["a", "b", "c"], max_delegation_depth=3),
        child_agent_id="agent-c",
        child_tools=["a", "b", "c"],
    )
    assert second.allowed_tools == ("a", "b")
    assert second.depth == 2
    assert second.chain == (_PARENT, "agent-b", "agent-c")


def test_the_childs_standing_mandate_is_untouched():
    child_gate = gate_with(_CHILD, allowed_tools=["search", "wire_transfer"])
    delegated = delegated_gate(gate_with(_PARENT), child_gate)
    assert delegated.scope.allowed_tools == ("search",)
    # The child's own snapshot still says what it always said.
    assert child_gate.snapshot is not None
    assert child_gate.snapshot.allowed_tools == ("search", "wire_transfer")


# -- policy and depth --------------------------------------------------------


def test_delegation_policy_none_refuses():
    with pytest.raises(DelegationRefusedError, match="may not delegate"):
        delegate(
            parent_scope(delegation_policy=DelegationPolicy.NONE),
            parent_snapshot=snapshot(delegation_policy=DelegationPolicy.NONE),
            child_agent_id=_CHILD,
            child_tools=["search"],
        )


def test_a_snapshot_that_does_not_mention_delegation_refuses():
    """Absent is not permission. A tenant that never approved delegation for
    this agent has not approved it."""
    with pytest.raises(DelegationRefusedError):
        delegate(
            parent_scope(delegation_policy=None),
            parent_snapshot=snapshot(delegation_policy=None),
            child_agent_id=_CHILD,
            child_tools=["search"],
        )


def test_the_depth_limit_is_enforced():
    snap = snapshot(max_delegation_depth=1)
    first = delegate(
        parent_scope(max_delegation_depth=1),
        parent_snapshot=snap,
        child_agent_id="agent-b",
        child_tools=None,
    )
    assert first.depth == 1
    with pytest.raises(DelegationRefusedError, match="max_delegation_depth"):
        delegate(
            first, parent_snapshot=snap, child_agent_id="agent-c", child_tools=None
        )


def test_a_depth_limit_of_zero_forbids_delegation_entirely():
    """Zero is a real limit, which is why the check is ``is not None`` and not a
    truth test."""
    with pytest.raises(DelegationRefusedError, match="max_delegation_depth"):
        delegate(
            parent_scope(max_delegation_depth=0),
            parent_snapshot=snapshot(max_delegation_depth=0),
            child_agent_id=_CHILD,
            child_tools=None,
        )


def test_no_depth_limit_means_the_policy_is_the_only_gate():
    scope = parent_scope(max_delegation_depth=None)
    for index in range(4):
        scope = delegate(
            scope,
            parent_snapshot=snapshot(max_delegation_depth=None),
            child_agent_id=f"agent-{index}",
            child_tools=None,
        )
    assert scope.depth == 4


# -- undeclared --------------------------------------------------------------


def test_an_undeclared_delegation_gets_the_empty_scope():
    child = delegate(
        parent_scope(),
        parent_snapshot=snapshot(),
        child_agent_id=_CHILD,
        child_tools=["search"],
        declared=False,
    )
    assert child.allowed_tools == ()
    assert not child.unrestricted
    assert not child.permits("search")


def test_an_undeclared_delegation_denies_by_rule_not_by_accident():
    parent = gate_with(_PARENT)
    child = gate_with(_CHILD)
    gate = delegated_gate(parent, child, declared=False)
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.DELEGATED_OUT_OF_SCOPE


# -- the gate ----------------------------------------------------------------


def test_a_delegated_gate_denies_what_the_delegator_could_not_call():
    parent = gate_with(_PARENT, allowed_tools=["search"])
    child = gate_with(_CHILD, allowed_tools=["search", "wire_transfer"])
    gate = delegated_gate(parent, child)

    assert isinstance(gate.decide("search"), Allow)
    denied = gate.decide("wire_transfer")
    assert isinstance(denied, Deny)
    # The live delegator refuses it first, which is why the reason is its own.
    assert denied.reason == Reason.OUT_OF_SCOPE
    assert denied.model_message == MODEL_MESSAGE
    # Pinned to the delegator's approval, not the child's own version.
    assert denied.mandate_version_id == "mv_parent"


def test_a_delegated_gate_still_honours_the_childs_own_denial():
    """Narrowing composes with the child's gate; it does not replace it."""
    parent = gate_with(_PARENT, allowed_tools=None)
    child = gate_with(_CHILD, allowed_tools=["search"], status="suspended")
    gate = delegated_gate(parent, child)
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.AGENT_SUSPENDED


def test_a_child_in_observe_rollout_cannot_run_what_its_delegator_was_refused():
    """The dial that decides whether the *narrowing* blocks belongs to the
    delegator. Otherwise picking a sub-agent still in observe rollout would be a
    working route around the parent's mandate."""
    parent = gate_with(_PARENT, allowed_tools=["search"], mandate_enforcement="enforce")
    child = gate_with(
        _CHILD, allowed_tools=["search", "wire_transfer"], mandate_enforcement="observe"
    )
    gate = delegated_gate(parent, child)
    verdict = gate.decide("wire_transfer")
    assert isinstance(verdict, Deny)
    assert not verdict.allowed


def test_observe_on_the_delegator_flags_a_delegated_breach_instead_of_blocking_it():
    parent = gate_with(_PARENT, allowed_tools=["search"], mandate_enforcement="observe")
    child = gate_with(_CHILD, allowed_tools=["search"], mandate_enforcement="observe")
    # Undeclared, so the pinned scope is empty and the breach is the delegated
    # one rather than the parent's own.
    gate = delegated_gate(parent, child, declared=False)
    verdict = gate.decide("search")
    assert isinstance(verdict, Flag)
    assert verdict.allowed
    assert verdict.reason == Reason.DELEGATED_OUT_OF_SCOPE_OBSERVED


def test_a_delegated_gate_plugs_into_governed_tool_unchanged():
    @governed_tool
    def wire_transfer(account: str) -> str:  # pragma: no cover - never runs
        return "sent"

    parent = gate_with(_PARENT, allowed_tools=["search"])
    child = gate_with(_CHILD, allowed_tools=["search", "wire_transfer"])
    gate = delegated_gate(parent, child)

    assert isinstance(gate, MandateGate)
    with use_gate(gate), run_scope("run-1"), pytest.raises(MandateDeniedError) as caught:
        wire_transfer("ACC-1")
    assert str(caught.value) == MODEL_MESSAGE


def test_delegating_from_a_gate_with_no_snapshot_is_refused():
    parent = MandateGate(None, agent_id=_PARENT)
    with pytest.raises(DelegationRefusedError, match="no mandate snapshot"):
        delegated_gate(parent, gate_with(_CHILD))


def test_the_delegated_gate_reports_the_childs_liveness_not_its_own():
    parent = gate_with(_PARENT)
    child = gate_with(_CHILD)
    gate = delegated_gate(parent, child)
    assert isinstance(gate, DelegatedGate)
    assert gate.enabled is True
    assert gate.snapshot is child.snapshot
    assert gate.inner is child
    assert gate.agent_id == _CHILD


def test_suspending_the_delegator_stops_the_agent_it_lent_authority_to():
    """The pinned scope is a photograph; a revocation has to reach the child."""
    parent = gate_with(_PARENT, allowed_tools=["search"])
    child = gate_with(_CHILD, allowed_tools=["search"])
    gate = delegated_gate(parent, child)
    assert isinstance(gate.decide("search"), Allow)

    parent.apply_snapshot(snapshot_payload(status="suspended", mandate_version_id="mv_parent"))
    verdict = gate.decide("search")
    assert isinstance(verdict, Deny)
    assert verdict.reason == Reason.AGENT_SUSPENDED


def test_narrowing_the_delegators_mandate_narrows_the_delegated_run():
    parent = gate_with(_PARENT, allowed_tools=["search", "read_file"])
    child = gate_with(_CHILD, allowed_tools=["search", "read_file"])
    gate = delegated_gate(parent, child)
    assert isinstance(gate.decide("read_file"), Allow)

    parent.apply_snapshot(
        snapshot_payload(allowed_tools=["search"], mandate_version_id="mv_parent")
    )
    assert isinstance(gate.decide("read_file"), Deny)
    assert isinstance(gate.decide("search"), Allow)


def test_a_middle_agents_own_depth_limit_cannot_loosen_the_roots():
    root_snapshot = snapshot(max_delegation_depth=1)
    middle_snapshot = snapshot(max_delegation_depth=10)
    first = delegate(
        EffectiveScope.from_snapshot(root_snapshot, agent_id=_PARENT),
        parent_snapshot=root_snapshot,
        child_agent_id="agent-b",
        child_tools=None,
    )
    assert first.max_depth == 1
    with pytest.raises(DelegationRefusedError, match="max_delegation_depth"):
        delegate(
            first,
            parent_snapshot=middle_snapshot,
            child_agent_id="agent-c",
            child_tools=None,
        )


def test_a_middle_agents_observe_dial_cannot_loosen_the_roots():
    root_snapshot = snapshot(mandate_enforcement="enforce", max_delegation_depth=None)
    middle_snapshot = snapshot(
        mandate_enforcement="observe", max_delegation_depth=None
    )
    first = delegate(
        EffectiveScope.from_snapshot(root_snapshot, agent_id=_PARENT),
        parent_snapshot=root_snapshot,
        child_agent_id="agent-b",
        child_tools=None,
    )
    second = delegate(
        first,
        parent_snapshot=middle_snapshot,
        child_agent_id="agent-c",
        child_tools=None,
    )
    assert second.enforcement == "enforce"
    assert not second.observing


def test_an_unreadable_depth_limit_is_read_as_no_delegation_not_as_unlimited():
    """A typo in a payload must not lift a limit."""
    snap = MandateSnapshot.from_payload(
        snapshot_payload(max_delegation_depth="two"), received_at=0.0
    )
    assert snap.max_delegation_depth is None
    with pytest.raises(DelegationRefusedError, match="max_delegation_depth"):
        delegate(
            EffectiveScope.from_snapshot(snap, agent_id=_PARENT),
            parent_snapshot=snap,
            child_agent_id=_CHILD,
            child_tools=None,
        )


def test_the_latch_still_halts_a_delegated_run():
    """Regression: the delegator and the delegated child hold different mandate
    versions, and the latch has to be asked one question with one answer. Fed two
    version sources it would see a version flapping on every call, wipe the
    buckets it had just written, and never reach the threshold — which would hand
    any agent with a sub-agent a permanently unreachable halt."""
    parent = gate_with(_PARENT, allowed_tools=["search"])
    child = gate_with(_CHILD, allowed_tools=["search", "wire_transfer"])
    assert parent.snapshot is not None and child.snapshot is not None
    assert parent.snapshot.mandate_version_id != child.snapshot.mandate_version_id

    @governed_tool
    def wire_transfer(account: str) -> str:  # pragma: no cover - never runs
        return "sent"

    latch = DenialLatch()
    gate = delegated_gate(parent, child)
    halts = 0
    with use_denial_latch(latch), use_gate(gate), run_scope("run-1"):
        for _ in range(10):
            with pytest.raises(MandateDeniedError) as caught:
                wire_transfer("ACC-1")
            if isinstance(caught.value, MandateRunHaltedError):
                halts += 1
    assert halts == 8
    assert latch.is_halted("run-1")


def test_two_principals_on_different_mandate_versions_each_still_halt():
    """Regression: one principal's mandate version must not wipe another's
    buckets, or a second agent in the run is all it takes to defeat the latch."""

    @governed_tool
    def wire_transfer(account: str) -> str:  # pragma: no cover - never runs
        return "sent"

    latch = DenialLatch()
    one = gate_with("agent-one", allowed_tools=["search"])
    two = gate_with("agent-two", allowed_tools=["search"])
    with use_denial_latch(latch), run_scope("run-1"):
        for _ in range(6):
            for gate in (one, two):
                with use_gate(gate), pytest.raises(MandateDeniedError):
                    wire_transfer("ACC-1")
    assert latch.is_halted("run-1")
    assert latch.attempts("run-1", "agent-one", "wire_transfer") >= 3


def test_a_version_change_clears_only_that_principals_buckets():
    @governed_tool
    def wire_transfer(account: str) -> str:  # pragma: no cover - never runs
        return "sent"

    latch = DenialLatch(threshold=10)
    one = gate_with("agent-one", allowed_tools=["search"])
    two = gate_with("agent-two", allowed_tools=["search"])
    with use_denial_latch(latch), run_scope("run-1"):
        for gate in (one, two):
            for _ in range(3):
                with use_gate(gate), pytest.raises(MandateDeniedError):
                    wire_transfer("ACC-1")
        one.apply_snapshot(
            snapshot_payload(allowed_tools=["search"], mandate_version_id="mv_new")
        )
        with use_gate(one), pytest.raises(MandateDeniedError):
            wire_transfer("ACC-1")

    assert latch.attempts("run-1", "agent-one", "wire_transfer") == 1
    assert latch.attempts("run-1", "agent-two", "wire_transfer") == 3
