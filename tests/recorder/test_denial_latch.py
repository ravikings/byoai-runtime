"""The denial latch: repeats, counting, and the halt.

No network — gates are seeded with ``apply_snapshot``, the same way
``test_governed_tool.py`` does it. The recurring claim under test is not "the
call was refused" but *how* it was refused: from memory rather than from a
fresh scope check, with a count, and with the model's sentence unchanged.
"""

from __future__ import annotations

import asyncio

import pytest

from byoai.errors import MandateDeniedError, MandateRunHaltedError
from byoai.recorder.denial_latch import (
    DEFAULT_HALT_THRESHOLD,
    DEFAULT_MAX_RUNS,
    DenialLatch,
    resolve_principal,
    resolve_run_id,
    run_scope,
    use_denial_latch,
)
from byoai.recorder.governed_tool import governed_tool, use_gate
from byoai.recorder.mandate import (
    MODEL_MESSAGE,
    MandateGate,
    Posture,
    ProposedAction,
    Reason,
)

_AGENT = "coriqo-agent-1"


class CountingGate(MandateGate):
    """A gate that says how many times it was actually asked.

    The latch's central claim is that it does *not* ask again, which a return
    value cannot show and a counter can.
    """

    def __init__(self, **kwargs) -> None:
        async def never_called(_etag):  # pragma: no cover - decide does no I/O
            raise AssertionError("the decide path must not refresh")

        super().__init__(never_called, agent_id=kwargs.pop("agent_id", _AGENT), **kwargs)
        self.decisions = 0

    def decide(self, action):
        self.decisions += 1
        return super().decide(action)


def snapshot_payload(**overrides) -> dict:
    payload = {
        "mandate_version_id": "mv_1",
        "allowed_tools": ["search"],
        "status": "approved",
        "mandate_enforcement": "enforce",
        "enforcement_posture": Posture.FAIL_OPEN,
        "max_staleness_s": 300,
    }
    payload.update(overrides)
    return payload


def gate_with(agent_id: str = _AGENT, **overrides) -> CountingGate:
    gate = CountingGate(agent_id=agent_id)
    gate.apply_snapshot(snapshot_payload(**overrides))
    return gate


@governed_tool
def rm(path: str) -> str:
    return "gone"


@governed_tool
def search(query: str) -> str:
    return "found"


@pytest.fixture(autouse=True)
def _fresh_latch():
    """A latch per test, so one test's halted run is not the next one's."""
    with use_denial_latch(DenialLatch()):
        yield


def deny(fn=rm, *, arg: str = "/etc") -> MandateDeniedError:
    with pytest.raises(MandateDeniedError) as caught:
        fn(arg)
    return caught.value


# -- repeats are refused from memory -----------------------------------------


def test_a_repeat_of_a_denied_tool_is_refused_without_re_evaluating_scope():
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        deny()
        assert gate.decisions == 1
        second = deny()

    # The gate was asked exactly once. The second refusal came from the latch.
    assert gate.decisions == 1
    assert second.verdict.reason == Reason.REPEAT_DENIED


def test_the_repeat_verdict_keeps_the_mandate_version_the_denial_was_made_against():
    """A relabelled denial, not a freshly invented one — so a sealed repeat
    points at the mandate that actually refused it."""
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        first = deny()
        second = deny()
    assert first.verdict.mandate_version_id == "mv_1"
    assert second.verdict.mandate_version_id == "mv_1"
    assert second.verdict.tool == "rm"


def test_an_allowed_tool_is_untouched_by_another_tools_latch():
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        deny()
        assert search("anything") == "found"


# -- counting ----------------------------------------------------------------


def test_the_attempt_count_increments():
    latch = DenialLatch(threshold=10)
    gate = gate_with()
    with use_denial_latch(latch), use_gate(gate), run_scope("run-1"):
        for _ in range(4):
            deny()
    assert latch.attempts("run-1", _AGENT, "rm") == 4


def test_the_count_is_on_the_error_a_supervisor_catches():
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        deny()
        deny()
        halted = deny()
    assert isinstance(halted, MandateRunHaltedError)
    assert halted.attempts == DEFAULT_HALT_THRESHOLD
    assert halted.run_id == "run-1"


# -- the halt ----------------------------------------------------------------


def test_the_threshold_halts_the_run():
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        assert not isinstance(deny(), MandateRunHaltedError)
        assert not isinstance(deny(), MandateRunHaltedError)
        assert isinstance(deny(), MandateRunHaltedError)


def test_a_halted_run_refuses_tools_that_were_never_out_of_scope():
    """The run is over. ``search`` is in the mandate and still does not run."""
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        deny()
        deny()
        deny()
        after = deny(search, arg="anything")
    assert isinstance(after, MandateRunHaltedError)
    assert after.verdict.reason == Reason.RUN_HALTED


def test_the_halt_is_distinguishable_from_an_ordinary_denial():
    """Same base class, so every existing handler still stops the call; a
    different type, so a supervising loop can tell the two apart."""
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        first = deny()
        deny()
        halted = deny()

    assert isinstance(first, MandateDeniedError)
    assert not isinstance(first, MandateRunHaltedError)
    assert first.halted is False

    assert isinstance(halted, MandateDeniedError)
    assert isinstance(halted, MandateRunHaltedError)
    assert halted.halted is True


def test_the_threshold_is_configurable():
    latch = DenialLatch(threshold=2)
    gate = gate_with()
    with use_denial_latch(latch), use_gate(gate), run_scope("run-1"):
        assert not isinstance(deny(), MandateRunHaltedError)
        assert isinstance(deny(), MandateRunHaltedError)


def test_the_default_threshold_is_three():
    assert DEFAULT_HALT_THRESHOLD == 3
    assert DenialLatch().threshold == 3
    assert DEFAULT_MAX_RUNS >= 1


def test_the_threshold_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("BYOAI_MANDATE_HALT_THRESHOLD", "5")
    assert DenialLatch().threshold == 5


def test_a_nonsense_threshold_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("BYOAI_MANDATE_HALT_THRESHOLD", "nope")
    assert DenialLatch().threshold == DEFAULT_HALT_THRESHOLD
    monkeypatch.setenv("BYOAI_MANDATE_HALT_THRESHOLD", "0")
    assert DenialLatch().threshold == DEFAULT_HALT_THRESHOLD


# -- the model learns nothing ------------------------------------------------


def test_the_model_facing_message_is_identical_across_denial_repeat_and_halt():
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        messages = [str(deny()) for _ in range(4)]
        model_messages = [MODEL_MESSAGE] * 4

    assert messages == model_messages
    # Byte-identical, not merely equal-looking.
    assert {m.encode() for m in messages} == {MODEL_MESSAGE.encode()}


def test_the_halt_does_not_leak_the_reason_into_anything_the_model_reads():
    """A distinctly named tool, because "rm" hides inside the word "permitted"
    and would pass this assertion for the wrong reason."""

    @governed_tool
    def wire_transfer(account: str) -> str:  # pragma: no cover - never runs
        return "sent"

    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        halted = None
        for _ in range(DEFAULT_HALT_THRESHOLD):
            halted = deny(wire_transfer, arg="ACC-1")

    assert isinstance(halted, MandateRunHaltedError)
    assert "wire_transfer" not in str(halted)
    assert "mandate" not in str(halted).lower()
    # The verdict's repr already redacts `detail`, which is where the reason is.
    assert "<redacted>" in repr(halted.verdict)
    # The operator's copy still has all of it.
    assert "wire_transfer" in halted.operator_detail


# -- buckets do not bleed ----------------------------------------------------


def test_latch_buckets_are_per_run():
    gate = gate_with()
    with use_gate(gate):
        with run_scope("run-1"):
            deny()
            deny()
            assert isinstance(deny(), MandateRunHaltedError)
        with run_scope("run-2"):
            second = deny()
    assert not isinstance(second, MandateRunHaltedError)
    assert second.verdict.reason == Reason.OUT_OF_SCOPE


def test_a_halted_run_does_not_halt_another_run():
    gate = gate_with()
    with use_gate(gate):
        with run_scope("run-1"):
            deny()
            deny()
            deny()
        with run_scope("run-2"):
            assert search("anything") == "found"


def test_the_trajectory_id_on_the_action_is_the_run():
    latch = DenialLatch(threshold=10)
    gate = gate_with()
    with use_denial_latch(latch), use_gate(gate):
        for _ in range(2):
            gate_check(gate, "rm", trajectory_id="traj-9")
    assert latch.attempts("traj-9", _AGENT, "rm") == 2
    assert latch.attempts("traj-8", _AGENT, "rm") == 0


def gate_check(gate: MandateGate, tool: str, *, trajectory_id: str) -> None:
    """Drive the seam directly, for the cases where the action carries its own
    run id rather than inheriting one from :func:`run_scope`."""
    from byoai.recorder.governed_tool import _check

    with pytest.raises(MandateDeniedError):
        _check(gate, ProposedAction(tool=tool, trajectory_id=trajectory_id))


def test_an_unnamed_run_falls_back_to_the_gate_not_to_one_shared_bucket():
    """Two gates, no trajectory ids, no run_scope: still two buckets."""
    latch = DenialLatch(threshold=10)
    one, two = gate_with(), gate_with()
    with use_denial_latch(latch):
        with use_gate(one):
            deny()
            deny()
        with use_gate(two):
            deny()

    assert resolve_run_id(ProposedAction(tool="rm"), one) != resolve_run_id(
        ProposedAction(tool="rm"), two
    )
    assert latch.attempts(resolve_run_id(ProposedAction(tool="rm"), one), _AGENT, "rm") == 2
    assert latch.attempts(resolve_run_id(ProposedAction(tool="rm"), two), _AGENT, "rm") == 1


def test_an_unnamed_run_keeps_one_bucket_across_calls_on_the_same_gate():
    gate = gate_with()
    with use_gate(gate):
        deny()
        deny()
        assert isinstance(deny(), MandateRunHaltedError)


def test_two_concurrent_agents_do_not_halt_each_other():
    """The failure this design is meant to avoid: one agent grinding against a
    control taking an unrelated agent's run down with it."""
    results: dict[str, object] = {}

    async def agent(label: str, gate: MandateGate, tool) -> None:
        with use_gate(gate), run_scope(f"run-{label}"):
            await asyncio.sleep(0)
            try:
                results[label] = tool("x")
            except MandateDeniedError as exc:
                results[label] = exc

    async def main() -> None:
        strict = gate_with(agent_id="agent-strict")
        for _ in range(DEFAULT_HALT_THRESHOLD):
            with use_gate(strict), run_scope("run-strict"):
                deny()
        await asyncio.gather(
            agent("strict", strict, rm),
            agent("open", gate_with(agent_id="agent-open", allowed_tools=None), rm),
        )

    asyncio.run(main())
    assert isinstance(results["strict"], MandateRunHaltedError)
    assert results["open"] == "gone"


def test_a_principal_is_its_own_bucket_within_one_run():
    """A delegated child and its delegator share a run and not a scope, so a
    child's denial must not latch a tool the parent may still call."""
    latch = DenialLatch(threshold=10)
    parent = gate_with(agent_id="agent-parent", allowed_tools=None)
    child = gate_with(agent_id="agent-child", allowed_tools=["search"])
    with use_denial_latch(latch), run_scope("run-1"):
        with use_gate(child):
            deny()
        with use_gate(parent):
            assert rm("/etc") == "gone"
    assert latch.attempts("run-1", "agent-child", "rm") == 1
    assert latch.attempts("run-1", "agent-parent", "rm") == 0


# -- housekeeping ------------------------------------------------------------


def test_reset_forgets_one_run_and_leaves_the_others():
    latch = DenialLatch(threshold=10)
    gate = gate_with()
    with use_denial_latch(latch), use_gate(gate):
        with run_scope("run-1"):
            deny()
        with run_scope("run-2"):
            deny()
        latch.reset("run-1")
    assert latch.attempts("run-1", _AGENT, "rm") == 0
    assert latch.attempts("run-2", _AGENT, "rm") == 1


def test_halted_by_names_the_tool_that_ended_the_run():
    gate = gate_with()
    latch = DenialLatch()
    with use_denial_latch(latch), use_gate(gate), run_scope("run-1"):
        for _ in range(DEFAULT_HALT_THRESHOLD):
            deny()
    assert latch.halted_by("run-1") == (_AGENT, "rm", DEFAULT_HALT_THRESHOLD)
    assert latch.is_halted("run-1")
    assert not latch.is_halted("run-2")


def test_the_principal_is_the_agent_id_when_the_gate_knows_one():
    assert resolve_principal(gate_with(agent_id="agent-7")) == "agent-7"
    assert resolve_principal(None) == "unidentified-agent"


def test_an_ungated_call_is_not_latched():
    """No gate bound is the adopt-before-enrolment path; it must stay a no-op."""
    with use_gate(None):
        for _ in range(DEFAULT_HALT_THRESHOLD + 2):
            assert rm("/etc") == "gone"


def test_a_latch_forgets_the_oldest_runs_rather_than_growing_forever():
    """A process that runs for months cannot remember every run it ever saw."""
    latch = DenialLatch(threshold=10, max_runs=2)
    gate = gate_with()
    with use_denial_latch(latch), use_gate(gate):
        for index in range(3):
            with run_scope(f"run-{index}"):
                deny()
    assert latch.attempts("run-0", _AGENT, "rm") == 0  # evicted
    assert latch.attempts("run-1", _AGENT, "rm") == 1
    assert latch.attempts("run-2", _AGENT, "rm") == 1


def test_a_run_that_keeps_calling_is_not_evicted_by_its_own_traffic():
    latch = DenialLatch(threshold=10, max_runs=2)
    gate = gate_with()
    with use_denial_latch(latch), use_gate(gate), run_scope("busy"):
        for _ in range(5):
            deny()
    assert latch.attempts("busy", _AGENT, "rm") == 5


# -- what must not be latched ------------------------------------------------


def test_a_stale_snapshot_denial_is_not_latched():
    """Staleness is transient. Latching it would turn one refresh blip into a
    permanently halted run — the outage the refresh path was written to avoid."""
    clock = _Clock()
    gate = CountingGate(agent_id=_AGENT, clock=clock)
    gate.apply_snapshot(
        snapshot_payload(enforcement_posture=Posture.FAIL_CLOSED, max_staleness_s=10)
    )
    with use_gate(gate), run_scope("run-1"):
        clock.advance(60)
        for _ in range(DEFAULT_HALT_THRESHOLD + 1):
            denied = deny(search, arg="anything")
            assert denied.verdict.reason == Reason.SNAPSHOT_STALE
            assert not isinstance(denied, MandateRunHaltedError)

        # The snapshot comes back. The run is not halted, and the gate decides.
        gate.apply_snapshot(snapshot_payload())
        assert search("anything") == "found"


def test_a_suspension_is_not_latched_so_lifting_it_takes_effect():
    gate = gate_with(status="suspended")
    with use_gate(gate), run_scope("run-1"):
        for _ in range(DEFAULT_HALT_THRESHOLD + 1):
            assert deny(search, arg="anything").verdict.reason == Reason.AGENT_SUSPENDED
        gate.apply_snapshot(snapshot_payload())
        assert search("anything") == "found"


def test_a_new_mandate_version_clears_the_latch():
    """The latch's licence to refuse without re-evaluating is that the answer
    cannot have changed. A new version is exactly when it can have."""
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        deny()
        deny()
        assert gate.decisions == 1

        gate.apply_snapshot(
            snapshot_payload(mandate_version_id="mv_2", allowed_tools=["search", "rm"])
        )
        assert rm("/etc") == "gone"
        assert gate.decisions == 2


def test_a_new_mandate_version_lifts_a_halt():
    gate = gate_with()
    with use_gate(gate), run_scope("run-1"):
        for _ in range(DEFAULT_HALT_THRESHOLD):
            deny()
        gate.apply_snapshot(
            snapshot_payload(mandate_version_id="mv_2", allowed_tools=["search", "rm"])
        )
        assert rm("/etc") == "gone"


def test_a_halted_run_does_not_grow_a_bucket_per_tool_name():
    """A halted loop calling a fresh tool name every turn must not accumulate."""
    latch = DenialLatch()
    gate = gate_with()
    with use_denial_latch(latch), use_gate(gate), run_scope("run-1"):
        for _ in range(DEFAULT_HALT_THRESHOLD):
            deny()
        for index in range(50):
            fresh = governed_tool(name=f"tool_{index}")(lambda: "ran")
            with pytest.raises(MandateRunHaltedError):
                fresh()
    assert latch.attempts("run-1", _AGENT, "tool_7") == 0
    assert latch.halted_by("run-1") == (_AGENT, "rm", DEFAULT_HALT_THRESHOLD)


def test_the_halt_verdict_does_not_claim_another_agents_mandate():
    """The halting denial belongs to one agent and one tool; a later call by
    another agent must not be sealed as decided against that agent's mandate."""
    latch = DenialLatch()
    one = gate_with(agent_id="agent-one")
    two = gate_with(agent_id="agent-two")
    with use_denial_latch(latch), run_scope("run-1"):
        with use_gate(one):
            for _ in range(DEFAULT_HALT_THRESHOLD):
                deny()
        with use_gate(two):
            halted = deny(search, arg="anything")
    assert halted.verdict.reason == Reason.RUN_HALTED
    assert halted.verdict.mandate_version_id is None
    assert halted.verdict.step_index is None


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
