"""Tests for the AWS-facing half of Bedrock agent ingest.

Every test injects a fake client, so the suite exercises the real call shape
without boto3, credentials, or a network. That split is the point of keeping
this layer thin: the translation is tested against recorded traces in
``test_bedrock_agent.py``, and what is left to check here is that we ask AWS
for the right thing.
"""

from __future__ import annotations

import json

import pytest

from byoai.recorder.bedrock_agent_source import (
    BedrockAgentSourceError,
    iter_cloudwatch_invocations,
    record_invocation,
    stream_invocation,
)


class FakeAgentRuntime:
    def __init__(self, completion: list, *, raises: Exception | None = None) -> None:
        self.completion = completion
        self.raises = raises
        self.calls: list[dict] = []

    def invoke_agent(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return {"completion": self.completion, "sessionId": kwargs["sessionId"]}


class FakeRecorder:
    def __init__(self) -> None:
        self.events: list = []

    def record_many(self, partials: list) -> None:
        self.events.extend(partials)


TRACE_CHUNK = {
    "trace": {
        "agentId": "A1",
        "agentAliasId": "AL1",
        "sessionId": "s-1",
        "trace": {
            "orchestrationTrace": {
                "observation": {"traceId": "t-1", "finalResponse": {"text": "Done."}}
            }
        },
    }
}


def test_tracing_is_forced_on() -> None:
    """An untraced invocation seals to an empty run, which reads as a quiet
    agent rather than as a missing flag. Not a default a caller can forget."""
    client = FakeAgentRuntime([TRACE_CHUNK])
    list(stream_invocation(agent_id="A1", agent_alias_id="AL1", prompt="hello", client=client))
    assert client.calls[0]["enableTrace"] is True
    assert client.calls[0]["inputText"] == "hello"


def test_session_id_is_generated_when_the_caller_has_none() -> None:
    client = FakeAgentRuntime([TRACE_CHUNK])
    list(stream_invocation(agent_id="A1", agent_alias_id="AL1", prompt="hi", client=client))
    assert client.calls[0]["sessionId"].startswith("byoai-")


def test_extra_invoke_kwargs_reach_boto3() -> None:
    client = FakeAgentRuntime([TRACE_CHUNK])
    list(
        stream_invocation(
            agent_id="A1",
            agent_alias_id="AL1",
            prompt="hi",
            client=client,
            sessionState={"sessionAttributes": {"tenant": "acme"}},
        )
    )
    assert client.calls[0]["sessionState"] == {"sessionAttributes": {"tenant": "acme"}}


def test_aws_failure_surfaces_as_our_error_type() -> None:
    client = FakeAgentRuntime([], raises=RuntimeError("AccessDeniedException"))
    with pytest.raises(BedrockAgentSourceError, match="invoke_agent failed"):
        list(stream_invocation(agent_id="A1", agent_alias_id="AL1", prompt="hi", client=client))


def test_record_invocation_seals_and_returns_the_run() -> None:
    recorder = FakeRecorder()
    client = FakeAgentRuntime([TRACE_CHUNK])
    run = record_invocation(
        recorder,
        agent_id="A1",
        agent_alias_id="AL1",
        prompt="triage txn_8841",
        session_id="s-1",
        client=client,
    )
    assert run.final_text == "Done."
    assert run.session_id == "s-1"
    assert [e.session_id for e in recorder.events] == ["s-1", "s-1"]


def test_record_invocation_without_a_recorder_still_returns_the_run() -> None:
    """The recorder being off is a configuration state, not an error — the
    same way the proxy keeps serving with recording disabled."""
    run = record_invocation(
        None,
        agent_id="A1",
        agent_alias_id="AL1",
        prompt="hi",
        client=FakeAgentRuntime([TRACE_CHUNK]),
    )
    assert run.final_text == "Done."


class FakeLogs:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.paginate_kwargs: dict = {}

    def get_paginator(self, name: str):
        assert name == "filter_log_events"
        outer = self

        class _Paginator:
            def paginate(self, **kwargs):
                outer.paginate_kwargs = kwargs
                return outer.pages

        return _Paginator()


def _event(payload: object) -> dict:
    return {"message": json.dumps(payload)}


def test_cloudwatch_records_are_grouped_by_session() -> None:
    logs = FakeLogs(
        [
            {
                "events": [
                    _event({"sessionId": "s-1", "trace": {"orchestrationTrace": {}}}),
                    _event({"sessionId": "s-2", "trace": {"orchestrationTrace": {}}}),
                    _event({"sessionId": "s-1", "trace": {"orchestrationTrace": {}}}),
                ]
            }
        ]
    )
    grouped = dict(
        iter_cloudwatch_invocations(
            log_group_name="/aws/bedrock/agents", start_time_ms=0, client=logs
        )
    )
    assert sorted(grouped) == ["s-1", "s-2"]
    assert len(grouped["s-1"]) == 2


def test_a_malformed_record_does_not_abort_the_sweep() -> None:
    logs = FakeLogs(
        [
            {
                "events": [
                    {"message": "not json {{{"},
                    _event({"no_session": True}),
                    _event({"sessionId": "s-good"}),
                ]
            }
        ]
    )
    grouped = dict(
        iter_cloudwatch_invocations(
            log_group_name="/aws/bedrock/agents", start_time_ms=0, client=logs
        )
    )
    assert list(grouped) == ["s-good"]


def test_end_time_and_filter_are_only_sent_when_given() -> None:
    logs = FakeLogs([{"events": []}])
    list(
        iter_cloudwatch_invocations(
            log_group_name="/aws/bedrock/agents", start_time_ms=100, client=logs
        )
    )
    assert "endTime" not in logs.paginate_kwargs
    assert "filterPattern" not in logs.paginate_kwargs

    list(
        iter_cloudwatch_invocations(
            log_group_name="/aws/bedrock/agents",
            start_time_ms=100,
            end_time_ms=200,
            filter_pattern='{ $.sessionId = "s-1" }',
            client=logs,
        )
    )
    assert logs.paginate_kwargs["endTime"] == 200
    assert logs.paginate_kwargs["filterPattern"].startswith("{ $.sessionId")


def test_nested_session_spellings_are_found() -> None:
    logs = FakeLogs([{"events": [_event({"input": {"session_id": "s-nested"}})]}])
    grouped = dict(
        iter_cloudwatch_invocations(
            log_group_name="/aws/bedrock/agents", start_time_ms=0, client=logs
        )
    )
    assert list(grouped) == ["s-nested"]


def test_the_session_aws_is_called_under_is_the_session_events_seal_under() -> None:
    """These used to be picked independently — the invoke generated one, the
    normalizer was handed the caller's ``None`` — and only agreed because the
    trace echoes the session back. An untraced or truncated stream does not.
    """
    recorder = FakeRecorder()
    client = FakeAgentRuntime([{"chunk": {"bytes": b"done"}}])  # no trace parts
    run = record_invocation(
        recorder, agent_id="A1", agent_alias_id="AL1", prompt="hi", client=client
    )
    assert run.session_id == client.calls[0]["sessionId"]
    assert {e.session_id for e in recorder.events} == {client.calls[0]["sessionId"]}
