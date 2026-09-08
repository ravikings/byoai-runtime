"""End-to-end test for B6, the managed AWS Bedrock Agent.

No AWS account and no credentials: the Bedrock env vars are cleared, so the
run replays ``fallbacks/b6_bedrock_sanctions_review.json``. That file holds
real ``InvokeAgent`` response-stream events, and the replay path hands them to
the same ``byoai.recorder.bedrock_agent`` normalizer the live path uses — so
this is a test of the production translation, not of a stub that resembles it.
"""

from __future__ import annotations

import pytest
from examples.agent_showcase.agents.bedrock import B6_BEDROCK_SANCTIONS_REVIEW
from examples.agent_showcase.app import app
from fastapi.testclient import TestClient

from byoai.recorder import integration as recorder_integration

AGENT = "b6-bedrock-sanctions-review"


@pytest.fixture
def demo_client(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOAI_RECORDER_ENABLED", "1")
    monkeypatch.setenv("BYOAI_RECORDER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("BYOAI_BEDROCK_AGENT_ID", raising=False)
    monkeypatch.delenv("BYOAI_BEDROCK_AGENT_ALIAS_ID", raising=False)
    recorder_integration.reset_recorder_for_tests()
    with TestClient(app) as client:
        yield client
    recorder_integration.reset_recorder_for_tests()


def _run(client) -> dict:
    run_id = client.post(f"/api/agents/{AGENT}/run").json()["run_id"]
    for _ in range(200):
        summary = client.get(f"/api/runs/{run_id}").json()
        if summary["done"]:
            return summary
    raise AssertionError("B6 run did not complete")


def test_b6_is_in_the_gallery_and_reports_not_live_without_aws(demo_client):
    agent = next(a for a in demo_client.get("/api/agents").json() if a["id"] == AGENT)
    assert agent["provider"] == "bedrock_agent"
    assert agent["live"] is False


def test_half_configured_aws_is_not_live(demo_client, monkeypatch):
    """An agent id with no alias cannot be invoked. Reporting that as live
    would promise a run that is going to fail."""
    monkeypatch.setenv("BYOAI_BEDROCK_AGENT_ID", "AGENT123")
    agent = next(a for a in demo_client.get("/api/agents").json() if a["id"] == AGENT)
    assert agent["live"] is False


def test_b6_run_seals_the_bedrock_trace(demo_client):
    summary = _run(demo_client)
    kinds = [e["kind"] for e in summary["events"]]
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert "guardrail_intervention" in kinds
    assert summary["events"][-1]["kind"] == "run_complete"
    assert "OFSI" in (summary["events"][-1]["text"] or "")


def test_no_api_error_is_sealed_when_aws_is_simply_not_configured(demo_client):
    """The other agents' fallbacks stand in for a call that failed and seal an
    api_error saying so. B6's stands in for an AWS account the demo box does
    not have, which is not a failure and must not be recorded as one."""
    summary = _run(demo_client)
    assert "api_error" not in [e["kind"] for e in summary["events"]]


def test_the_ledger_verifies(demo_client):
    run_id = demo_client.post(f"/api/agents/{AGENT}/run").json()["run_id"]
    for _ in range(200):
        if demo_client.get(f"/api/runs/{run_id}").json()["done"]:
            break
    verdict = demo_client.get(f"/api/runs/{run_id}/verify").json()
    assert verdict["chain_ok"] is True, verdict
    assert verdict["digests_ok"] is True, verdict
    assert verdict["tampered_events"] == []


def test_replay_reconstructs_the_run_from_the_ledger_alone(demo_client):
    run_id = demo_client.post(f"/api/agents/{AGENT}/run").json()["run_id"]
    for _ in range(200):
        if demo_client.get(f"/api/runs/{run_id}").json()["done"]:
            break
    replay = demo_client.get(f"/api/runs/{run_id}/replay").json()
    kinds = [e["kind"] for e in replay["events"]]
    assert "tool_use" in kinds
    assert "guardrail_intervention" in kinds


def test_the_return_control_wire_transfer_is_flagged_as_out_of_scope(demo_client):
    """The heart of the demo. AWS ran this agent end to end; its guardrail
    fired on something else entirely and let this through. The call is outside
    the agent's declared action groups, and because ``returnControl`` executes
    in the caller's own process it appears in no AWS log — so this event is
    the only written record that it was ever requested."""
    summary = _run(demo_client)
    assert summary["flagged"] is True
    violations = summary["policy_violations"]
    assert [v["tool_name"] for v in violations] == ["PaymentActions::initiate_wire_transfer"]


def test_the_declared_tools_are_not_flagged(demo_client):
    """A padded or trimmed declared surface would flag every ordinary call and
    make the finding above meaningless."""
    summary = _run(demo_client)
    called = {e["tool_name"] for e in summary["events"] if e["kind"] == "tool_use"}
    declared = B6_BEDROCK_SANCTIONS_REVIEW.declared_tool_names
    assert declared <= called
    flagged = {v["tool_name"] for v in summary["policy_violations"]}
    assert flagged & declared == set()


def test_collaborator_steps_land_on_their_own_span(demo_client):
    """Bedrock's multi-agent collaboration is the same shape as this demo's
    sub-agents, and has to render as a branch in the span tree rather than as
    the supervisor's own work."""
    summary = _run(demo_client)
    spans = {e["span_id"] for e in summary["events"]}
    parented = {e["span_id"] for e in summary["events"] if e.get("parent_span_id")}
    assert parented
    assert parented < spans
