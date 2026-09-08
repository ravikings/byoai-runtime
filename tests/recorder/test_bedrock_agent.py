"""Tests for the Bedrock Agent ingest seam.

The trace parts below are shaped against botocore's own service model for
``bedrock-agent-runtime`` (``TracePart`` -> ``Trace`` -> ``OrchestrationTrace``),
not from memory: field names here are the field names AWS sends, so a rename on
their side shows up as a failing test rather than as silently empty evidence.
"""

from __future__ import annotations

import json

import pytest

from byoai.recorder.bedrock_agent import (
    KIND_GUARDRAIL_INTERVENTION,
    PROVIDER,
    Capture,
    NormalizedRun,
    normalize_run,
    seal_run,
    tool_names,
)
from byoai.recorder.schema import EventKind

AGENT_ID = "AGENT123456"
ALIAS_ID = "ALIAS7890"
SESSION = "sess-fraud-001"


def _trace(body: dict, *, collaborator: str | None = None) -> dict:
    part = {
        "agentId": AGENT_ID,
        "agentAliasId": ALIAS_ID,
        "agentVersion": "3",
        "sessionId": SESSION,
        "trace": body,
    }
    if collaborator is not None:
        part["collaboratorName"] = collaborator
    return {"trace": part}


# One full orchestration loop: prompt in, reasoning, an action-group call, its
# observation, a knowledge-base lookup, then the final response.
REAL_TRACE: list[dict] = [
    _trace(
        {
            "orchestrationTrace": {
                "modelInvocationInput": {
                    "traceId": "t-0",
                    "type": "ORCHESTRATION",
                    "foundationModel": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "text": "You are a fraud triage agent...",
                }
            }
        }
    ),
    _trace(
        {
            "orchestrationTrace": {
                "rationale": {
                    "traceId": "t-0",
                    "text": "I should pull the flagged transaction first.",
                }
            }
        }
    ),
    _trace(
        {
            "orchestrationTrace": {
                "invocationInput": {
                    "traceId": "t-1",
                    "invocationType": "ACTION_GROUP",
                    "actionGroupInvocationInput": {
                        "actionGroupName": "TransactionActions",
                        "function": "get_transaction",
                        "executionType": "LAMBDA",
                        "parameters": [{"name": "txn_id", "type": "string", "value": "txn_8841"}],
                    },
                }
            }
        }
    ),
    _trace(
        {
            "orchestrationTrace": {
                "observation": {
                    "traceId": "t-1",
                    "type": "ACTION_GROUP",
                    "actionGroupInvocationOutput": {"text": '{"amount": 2450.0, "city": "Leeds"}'},
                }
            }
        }
    ),
    _trace(
        {
            "orchestrationTrace": {
                "invocationInput": {
                    "traceId": "t-2",
                    "invocationType": "KNOWLEDGE_BASE",
                    "knowledgeBaseLookupInput": {
                        "knowledgeBaseId": "KB99",
                        "text": "card-not-present fraud thresholds",
                    },
                }
            }
        }
    ),
    _trace(
        {
            "orchestrationTrace": {
                "observation": {
                    "traceId": "t-2",
                    "type": "KNOWLEDGE_BASE",
                    "knowledgeBaseLookupOutput": {
                        "retrievedReferences": [{"content": {"text": "Threshold is GBP 3,000."}}]
                    },
                }
            }
        }
    ),
    _trace(
        {
            "orchestrationTrace": {
                "observation": {
                    "traceId": "t-3",
                    "type": "FINISH",
                    "finalResponse": {"text": "Cleared txn_8841: below threshold, home city."},
                }
            }
        }
    ),
]


class FakeRecorder:
    """Captures what would have been sealed, without a ledger or a device key."""

    def __init__(self) -> None:
        self.events: list = []

    def record_many(self, partials: list) -> None:
        self.events.extend(partials)


def test_full_loop_normalizes_to_the_expected_events() -> None:
    run = normalize_run(REAL_TRACE)

    assert run.session_id == SESSION
    assert run.agent_id == AGENT_ID
    assert run.agent_alias_id == ALIAS_ID
    assert run.foundation_model == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert run.final_text == "Cleared txn_8841: below threshold, home city."

    recorder = FakeRecorder()
    count = seal_run(recorder, run)
    assert count == len(recorder.events)

    kinds = [e.kind for e in recorder.events]
    assert kinds == [
        EventKind.SESSION_START.value,
        EventKind.MESSAGE.value,  # rationale
        EventKind.TOOL_USE.value,  # action group
        EventKind.TOOL_RESULT.value,
        EventKind.TOOL_USE.value,  # knowledge base
        EventKind.TOOL_RESULT.value,
        EventKind.MESSAGE.value,  # final response
    ]


def test_every_sealed_event_names_bedrock_as_the_provider() -> None:
    """The bodies are Anthropic-shaped so one extractor can read them, but the
    thing that chose these tool calls was the AWS agent runtime. A row that
    said ``anthropic`` would be provenance that is confidently wrong."""
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(REAL_TRACE))
    assert {e.provider for e in recorder.events} == {PROVIDER}


def test_tool_names_use_action_group_and_operation() -> None:
    run = normalize_run(REAL_TRACE)
    assert tool_names(run) == [
        "TransactionActions::get_transaction",
        "knowledge_base::KB99",
    ]


def test_tool_result_pairs_with_its_call_by_bedrock_trace_id() -> None:
    """verify.py reports an unpaired tool_use or tool_result as a finding, so
    getting this wrong turns every clean Bedrock run into a false alarm."""
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(REAL_TRACE))

    uses = {e.tool_use_id for e in recorder.events if e.kind == EventKind.TOOL_USE.value}
    results = {e.tool_use_id for e in recorder.events if e.kind == EventKind.TOOL_RESULT.value}
    assert uses == results == {"bda_t-1", "bda_t-2"}


def test_action_group_parameters_are_preserved_as_sent() -> None:
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(REAL_TRACE))
    call = next(
        e
        for e in recorder.events
        if e.kind == EventKind.TOOL_USE.value and e.tool_name.endswith("get_transaction")
    )
    # Value stays the string AWS sent — coercing it would break the digest's
    # claim to commit to what actually crossed the wire.
    assert call.payload["input"]["parameters"] == {"txn_id": "txn_8841"}
    assert call.payload["input"]["executionType"] == "LAMBDA"


def test_api_path_agents_get_a_readable_tool_name() -> None:
    chunks = [
        _trace(
            {
                "orchestrationTrace": {
                    "invocationInput": {
                        "traceId": "t-9",
                        "actionGroupInvocationInput": {
                            "actionGroupName": "Payments",
                            "apiPath": "/transfers",
                            "verb": "post",
                            "parameters": [],
                        },
                    }
                }
            }
        )
    ]
    assert tool_names(normalize_run(chunks)) == ["Payments::post /transfers"]


def test_prompt_is_not_sealed_as_an_event() -> None:
    """The proxy seam seals actions, not prompts. If this seam sealed prompts
    the two would disagree about what an event is."""
    only_prompt = [REAL_TRACE[0]]
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(only_prompt), record_session_start=False)
    assert recorder.events == []


def test_raw_model_output_is_opt_in() -> None:
    chunks = [
        REAL_TRACE[0],
        _trace(
            {
                "orchestrationTrace": {
                    "modelInvocationOutput": {
                        "traceId": "t-0",
                        "rawResponse": {"content": "<thinking>scratchpad</thinking>"},
                    }
                }
            }
        ),
    ]
    off = FakeRecorder()
    seal_run(off, normalize_run(chunks), record_session_start=False)
    assert off.events == []

    on = FakeRecorder()
    seal_run(on, normalize_run(chunks, include_raw_model_output=True), record_session_start=False)
    assert [e.kind for e in on.events] == [EventKind.MESSAGE.value]
    assert "scratchpad" in on.events[0].payload["text"]


def test_guardrail_intervention_is_its_own_kind() -> None:
    chunks = [
        _trace(
            {
                "guardrailTrace": {
                    "traceId": "t-4",
                    "action": "INTERVENED",
                    "outputAssessments": [{"topicPolicy": {"topics": [{"name": "Legal advice"}]}}],
                }
            }
        )
    ]
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(chunks), record_session_start=False)
    assert [e.kind for e in recorder.events] == [KIND_GUARDRAIL_INTERVENTION]
    assert normalize_run(chunks).guardrail_interventions == 1


def test_guardrail_that_did_not_fire_is_not_an_event() -> None:
    chunks = [_trace({"guardrailTrace": {"traceId": "t-5", "action": "NONE"}})]
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(chunks), record_session_start=False)
    assert recorder.events == []


def test_failure_trace_seals_as_an_api_error() -> None:
    chunks = [
        _trace(
            {
                "failureTrace": {
                    "traceId": "t-6",
                    "failureCode": 400,
                    "failureReason": "Lambda returned an unparseable response",
                }
            }
        )
    ]
    run = normalize_run(chunks)
    assert run.failures == ["Lambda returned an unparseable response"]
    recorder = FakeRecorder()
    seal_run(recorder, run, record_session_start=False)
    assert [e.kind for e in recorder.events] == [EventKind.API_ERROR.value]
    assert recorder.events[0].payload["failure_code"] == 400


def test_return_control_is_sealed_because_nothing_else_records_it() -> None:
    """A returnControl action runs in the caller's own process, outside
    anything AWS logs. If this seam dropped it, the call would exist nowhere."""
    chunks = [
        {
            "returnControl": {
                "invocationId": "inv-77",
                "invocationInputs": [
                    {
                        "functionInvocationInput": {
                            "actionGroup": "PaymentActions",
                            "function": "initiate_wire_transfer",
                            "parameters": [{"name": "amount", "type": "number", "value": "50000"}],
                        }
                    }
                ],
            }
        }
    ]
    run = normalize_run(chunks, session_id=SESSION)
    assert tool_names(run) == ["PaymentActions::initiate_wire_transfer"]

    recorder = FakeRecorder()
    seal_run(recorder, run, record_session_start=False)
    assert recorder.events[0].payload["input"]["return_control"] is True
    assert recorder.events[0].payload["input"]["parameters"] == {"amount": "50000"}


def test_collaborator_steps_get_their_own_span_under_the_root() -> None:
    chunks = REAL_TRACE[:2] + [
        _trace(
            {
                "orchestrationTrace": {
                    "rationale": {"traceId": "t-7", "text": "Checking sanctions lists."}
                }
            },
            collaborator="SanctionsScreener",
        )
    ]
    run = normalize_run(chunks)
    assert list(run.collaborator_spans) == ["SanctionsScreener"]

    recorder = FakeRecorder()
    seal_run(recorder, run)
    sub = [e for e in recorder.events if e.parent_span_id]
    assert len(sub) == 1
    assert sub[0].span_id == run.collaborator_spans["SanctionsScreener"]
    assert sub[0].parent_span_id == run.span_id
    assert sub[0].trace_id == run.trace_id


def test_completion_bytes_are_the_decision_text_only_when_the_trace_had_none() -> None:
    streamed = [{"chunk": {"bytes": b"Cleared, "}}, {"chunk": {"bytes": b"no action needed."}}]
    assert normalize_run(streamed).final_text == "Cleared, no action needed."
    # With a finalResponse present, the trace wins — it is the part that is
    # attributable to an orchestration step.
    assert normalize_run(REAL_TRACE + streamed).final_text.startswith("Cleared txn_8841")


def test_truncated_trace_still_normalizes() -> None:
    """A stream that dies mid-loop should leave the evidence it produced, not
    raise and lose all of it."""
    run = normalize_run(REAL_TRACE[:4])
    recorder = FakeRecorder()
    seal_run(recorder, run)
    assert EventKind.TOOL_RESULT.value in [e.kind for e in recorder.events]
    assert run.final_text == ""


def test_unknown_stream_members_are_ignored_not_fatal() -> None:
    """AWS adds members to the response stream; ingest of the ones we do
    understand must not break when it does."""
    chunks = REAL_TRACE + [{"somethingNew": {"foo": 1}}, {"files": {"files": []}}, "nonsense"]
    assert normalize_run(chunks).final_text.startswith("Cleared txn_8841")


def test_orphan_observation_still_seals_rather_than_vanishing() -> None:
    chunks = [
        _trace(
            {
                "orchestrationTrace": {
                    "observation": {
                        "traceId": "t-orphan",
                        "actionGroupInvocationOutput": {"text": "{}"},
                    }
                }
            }
        )
    ]
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(chunks), record_session_start=False)
    assert [e.kind for e in recorder.events] == [EventKind.TOOL_RESULT.value]


def test_normalize_is_pure_and_repeatable() -> None:
    """Same input, same captures — which is what makes a recorded fixture a
    real test of the live path rather than a mock of it."""
    first = normalize_run(REAL_TRACE, session_id=SESSION, trace_id="tr", span_id="sp")
    second = normalize_run(REAL_TRACE, session_id=SESSION, trace_id="tr", span_id="sp")
    assert [(c.direction, c.span_id, c.body) for c in first.captures] == [
        (c.direction, c.span_id, c.body) for c in second.captures
    ]


def test_preprocessing_trace_is_walked_too() -> None:
    chunks = [
        _trace(
            {
                "preProcessingTrace": {
                    "invocationInput": {
                        "traceId": "t-pre",
                        "actionGroupInvocationInput": {
                            "actionGroupName": "Ops",
                            "function": "escalate",
                            "parameters": [],
                        },
                    }
                }
            }
        )
    ]
    assert tool_names(normalize_run(chunks)) == ["Ops::escalate"]


def test_captures_are_json_serialisable() -> None:
    """Everything sealed has to survive canonicalization into a digest."""
    for capture in normalize_run(REAL_TRACE).captures:
        if capture.body is not None:
            json.dumps(capture.body)


@pytest.mark.parametrize("bad", [None, [], {}, "", 0])
def test_garbage_input_normalizes_to_an_empty_run(bad: object) -> None:
    run = normalize_run([bad])
    assert isinstance(run, NormalizedRun)
    assert run.captures == []


def test_a_collaborators_final_response_does_not_steal_the_handoff() -> None:
    """Bedrock reuses one traceId for the supervisor's collaborator handoff,
    the collaborator's own finalResponse, and the handback. Claiming the
    pending call on the first observation to arrive let the sub-agent's answer
    consume the handoff, so the real handback sealed as an orphan tool_result
    — which verify.py correctly reports as a broken run.
    """
    chunks = [
        _trace(
            {
                "orchestrationTrace": {
                    "invocationInput": {
                        "traceId": "t-c",
                        "invocationType": "AGENT_COLLABORATOR",
                        "agentCollaboratorInvocationInput": {
                            "agentCollaboratorName": "PaymentsSpecialist",
                            "input": {"text": "can we hold?"},
                        },
                    }
                }
            }
        ),
        _trace(
            {
                "orchestrationTrace": {
                    "observation": {
                        "traceId": "t-c",
                        "type": "FINISH",
                        "finalResponse": {"text": "Hold is safe."},
                    }
                }
            },
            collaborator="PaymentsSpecialist",
        ),
        _trace(
            {
                "orchestrationTrace": {
                    "observation": {
                        "traceId": "t-c",
                        "type": "AGENT_COLLABORATOR",
                        "agentCollaboratorInvocationOutput": {
                            "agentCollaboratorName": "PaymentsSpecialist",
                            "output": {"text": "Hold is safe."},
                        },
                    }
                }
            }
        ),
    ]
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(chunks), record_session_start=False)

    uses = [e.tool_use_id for e in recorder.events if e.kind == EventKind.TOOL_USE.value]
    results = [e.tool_use_id for e in recorder.events if e.kind == EventKind.TOOL_RESULT.value]
    assert uses == results == ["bda_t-c"]


def test_tool_names_survives_a_malformed_content_block() -> None:
    run = normalize_run(REAL_TRACE)
    run.captures.append(
        Capture(direction="response", span_id="s", body={"content": ["junk", None]})
    )
    assert tool_names(run) == ["TransactionActions::get_transaction", "knowledge_base::KB99"]


def test_a_collaborator_output_that_is_not_a_dict_still_seals() -> None:
    chunks = [
        _trace(
            {
                "orchestrationTrace": {
                    "invocationInput": {
                        "traceId": "t-x",
                        "agentCollaboratorInvocationInput": {"agentCollaboratorName": "Peer"},
                    }
                }
            }
        ),
        _trace(
            {
                "orchestrationTrace": {
                    "observation": {
                        "traceId": "t-x",
                        "agentCollaboratorInvocationOutput": {"output": "plain string"},
                    }
                }
            }
        ),
    ]
    recorder = FakeRecorder()
    seal_run(recorder, normalize_run(chunks), record_session_start=False)
    assert [e.kind for e in recorder.events] == [
        EventKind.TOOL_USE.value,
        EventKind.TOOL_RESULT.value,
    ]


def test_a_bedrock_run_publishes_to_coriqo_with_its_action_group_names(tmp_path) -> None:
    """The Coriqo publisher needs nothing Bedrock-specific, but only because
    the names line up: Coriqo matches a step's tool_name against the agent's
    allowed_tools, which coriqo_sync registers from the same declared_tool_names
    this seam seals. Verified against a live local Coriqo on 2026-09-08 — the
    run landed as a flagged trajectory and Coriqo raised its own major finding
    for the out-of-mandate call.
    """
    from byoai.recorder.coriqo_agents import read_tool_steps
    from byoai.recorder.integration import Recorder

    # The real Recorder, not a stand-in: its promotion step is what stamps
    # payload_hash and device_id, and a hand-rolled copy here would drift from
    # it silently — which is exactly the seam this test exists to hold.
    recorder = Recorder(dir=tmp_path)
    try:
        chunks = REAL_TRACE + [
            {
                "returnControl": {
                    "invocationId": "inv-1",
                    "invocationInputs": [
                        {
                            "functionInvocationInput": {
                                "actionGroup": "PaymentActions",
                                "function": "initiate_wire_transfer",
                                "parameters": [],
                            }
                        }
                    ],
                }
            }
        ]
        seal_run(recorder, normalize_run(chunks, session_id=SESSION))

        steps = read_tool_steps(recorder.ledger, SESSION)
        assert [s.tool_name for s in steps] == [
            "TransactionActions::get_transaction",
            "knowledge_base::KB99",
            "PaymentActions::initiate_wire_transfer",
        ]
        # The returnControl call has no result and must still publish. Dropping
        # it for want of one would hide the only call nothing else records.
        assert [s.result_hash is not None for s in steps] == [True, True, False]
        assert all(s.entry_hash for s in steps)
    finally:
        recorder.close()


def test_policy_names_survive_redaction_but_the_matched_text_does_not(tmp_path) -> None:
    """The bug a live pass caught, and the reason `categories` exists.

    Under the default REDACTED payload mode the recorder digests every string
    it ships, because it cannot tell a vendor's vocabulary from a customer's
    text. Deriving a published policy name from the raw assessments therefore
    put `[REDACTED:hash:027cb…]` on a compliance screen where "LegalAdvice"
    belongs — unreadable evidence protecting nothing, since the matched text
    was never in that field.

    So the capture seam normalizes to names at seal time and redaction passes
    that one key through. This pins BOTH halves: the names survive, and the
    matched email in the raw assessment still does not.
    """
    from byoai.recorder.coriqo_agents import read_external_controls
    from byoai.recorder.integration import Recorder
    from byoai.recorder.redact import PayloadMode

    recorder = Recorder(dir=tmp_path, payload_mode=PayloadMode.REDACTED)
    try:
        chunks = [
            _trace(
                {
                    "guardrailTrace": {
                        "traceId": "t-g",
                        "action": "INTERVENED",
                        "outputAssessments": [
                            {
                                "topicPolicy": {
                                    "topics": [{"name": "LegalAdvice", "action": "BLOCKED"}]
                                },
                                "sensitiveInformationPolicy": {
                                    "piiEntities": [
                                        {
                                            "type": "EMAIL",
                                            "match": "kestrel.ops@example.com",
                                            "action": "ANONYMIZED",
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                }
            )
        ]
        seal_run(recorder, normalize_run(chunks, session_id=SESSION))

        control = read_external_controls(recorder.ledger, SESSION)[0]
        assert control.stage == "output"
        assert control.categories == [
            {"policy": "topic", "name": "LegalAdvice", "action": "BLOCKED"},
            # The PII entity's TYPE, never its match.
            {"policy": "pii", "name": "EMAIL", "action": "ANONYMIZED"},
        ]

        shipped = json.dumps([e.event.payload for e in recorder.ledger.read_session(SESSION)])
        assert "kestrel.ops@example.com" not in shipped, (
            "the matched span reached the shipped payload"
        )
        # The raw assessments are still there and still redacted — only the
        # normalized key is trusted.
        assert "[REDACTED" in shipped
    finally:
        recorder.close()
