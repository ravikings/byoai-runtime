"""Case pools and the history backfill.

Every case's cached transcript has to agree with the mock bank's data: a
transcript that looks up an id the mocks don't have would seal an ``error``
result, and a backfilled quarter would be full of them.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest
from examples.agent_showcase import backfill
from examples.agent_showcase.agents.registry import list_agents
from examples.agent_showcase.runner import FALLBACKS_DIR, AgentRunner

from byoai.recorder import coriqo_agents
from byoai.recorder import integration as recorder_integration
from byoai.recorder.coriqo_agents import AgentRegistration, CoriqoAgentsClient, CoriqoCredentials
from byoai.recorder.schema import now_ts_device, set_device_clock

CASE_AGENTS = [a for a in list_agents() if a.cases]
ALL_CASES = [(agent, case) for agent in CASE_AGENTS for case in agent.cases]


@pytest.fixture
def recorder_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BYOAI_RECORDER_ENABLED", "1")
    monkeypatch.setenv("BYOAI_RECORDER_DIR", str(tmp_path / "ledger"))
    recorder_integration.reset_recorder_for_tests()
    yield
    recorder_integration.reset_recorder_for_tests()


def test_every_banking_agent_except_bedrock_has_a_varied_pool():
    pools = {a.id: a.cases for a in list_agents() if a.domain == "banking"}
    for agent_id, cases in pools.items():
        if agent_id == "b6-bedrock-sanctions-review":
            assert cases == ()
            continue
        assert 8 <= len(cases) <= 12, agent_id
        assert len({c.id for c in cases}) == len(cases)
        assert len({c.scenario_message for c in cases}) == len(cases)


def test_misfire_is_a_minority_of_its_pool():
    b5 = next(a for a in list_agents() if a.id == "b5-misfire-demo")
    wires = [
        c for c in b5.cases
        if any(
            call["name"] == "initiate_wire_transfer"
            for step in json.loads((FALLBACKS_DIR / c.fallback_file).read_text())["steps"]
            for call in step["tool_calls"]
        )
    ]
    assert 1 <= len(wires) <= 2


@pytest.mark.parametrize(("agent", "case"), ALL_CASES, ids=[f"{a.id}:{c.id}" for a, c in ALL_CASES])
async def test_each_case_replays_without_tool_errors(recorder_env, agent, case):
    results = []
    outcome = None
    async for event in AgentRunner(agent, case=case).run(replay=True):
        if event.kind == "tool_result":
            results.append((event.tool_name, event.data["result"]))
        if event.kind == "run_complete":
            outcome = event
    assert outcome is not None and outcome.data["mode"] == "cached"
    assert results
    for name, result in results:
        assert not (isinstance(result, dict) and "error" in result), (name, result)
    assert case.id in outcome.text


def test_for_case_binds_sub_agents_to_the_same_case():
    b2 = next(a for a in list_agents() if a.id == "b2-kyc-onboarding")
    case = b2.cases[3]
    bound = b2.for_case(case)
    sub = bound.sub_agent_tools["sanctions_screen"]
    assert bound.fallback_file == case.fallback_file
    assert sub.fallback_file == case.sub_cases[sub.id][1]
    assert b2.sub_agent_tools["sanctions_screen"].fallback_file != sub.fallback_file


def test_plan_is_deterministic_business_hours_and_domain_scoped():
    banking = [a for a in list_agents() if a.domain == "banking"]
    first = backfill.plan_runs(banking, days=30, seed=7, today=date(2026, 9, 12))
    second = backfill.plan_runs(banking, days=30, seed=7, today=date(2026, 9, 12))
    assert [(r.at, r.agent.id, r.case and r.case.id) for r in first] == [
        (r.at, r.agent.id, r.case and r.case.id) for r in second
    ]
    eastern = ZoneInfo("America/New_York")
    for run in first:
        local = run.at.astimezone(eastern)
        assert local.weekday() < 5
        assert 8 <= local.hour < 18
        assert run.agent.domain == "banking"
    assert [r.at for r in first] == sorted(r.at for r in first)
    assert {r.agent.id for r in first} >= {"b1-fraud-triage", "b4-loan-prequalification"}


def test_device_clock_is_injectable_and_restorable():
    fixed = datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)
    previous = set_device_clock(lambda: fixed)
    try:
        assert now_ts_device() == "2026-06-01T13:30:00.000000Z"
    finally:
        set_device_clock(previous)
    assert now_ts_device() != "2026-06-01T13:30:00.000000Z"


def _client(handler, monkeypatch):
    real = httpx.Client

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(coriqo_agents.httpx, "Client", factory)
    return CoriqoAgentsClient(CoriqoCredentials(base_url="http://coriqo.test", api_key="k", tenant_slug="t"))


def test_trajectory_times_are_sent_only_when_given(monkeypatch):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"trajectory_id": "tr"})

    client = _client(handler, monkeypatch)
    client.open_trajectory("a", goal="g")
    client.open_trajectory("a", goal="g", started_at="2026-06-01T13:30:00.000000Z")
    client.complete_trajectory("a", "tr")
    client.complete_trajectory("a", "tr", ended_at="2026-06-01T13:31:00.000000Z")
    assert "started_at" not in bodies[0]
    assert bodies[1]["started_at"] == "2026-06-01T13:30:00.000000Z"
    assert bodies[2] == {"status": "completed"}
    assert bodies[3]["ended_at"] == "2026-06-01T13:31:00.000000Z"


def test_registration_sends_use_case_only_when_set():
    assert "use_case" not in AgentRegistration(name="x").to_body()
    assert AgentRegistration(name="x", use_case="fraud-detection").to_body()["use_case"] == "fraud-detection"
