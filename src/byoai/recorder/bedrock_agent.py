"""Seal an AWS Bedrock Agent's run into the ledger, from its own trace stream.

Why this exists as a third seam
-------------------------------
The two seams that came before both need to be *in the path*. ``@governed_tool``
(Seam A) is a source change; the proxy (Seam B) sits at ``ANTHROPIC_BASE_URL``
and tees the response stream. Neither reaches a Bedrock Agent. When a caller
invokes an agent, AWS runs the orchestration loop — prompt construction, model
turns, action-group dispatch, knowledge-base retrieval — entirely inside the
service. No LLM request ever leaves the customer's account for something of
ours to intercept, so there is nothing to sit in front of.

What AWS gives back instead is a *trace*: with ``enableTrace=True``,
``InvokeAgent``'s response stream carries a ``TracePart`` for every step the
agent took, and that is the evidence. This module is the seam that turns those
parts into the same sealed ``AgentEvent`` rows the other two produce.

The translation, and what it is faithful to
-------------------------------------------
Nothing here invents a second event schema. Bedrock's orchestration parts are
rewritten into the Anthropic-shaped request/response bodies that
:mod:`byoai.recorder.extract` already understands, and that one extractor
produces the events — the same trick the agent showcase uses to seal OpenAI
runs. One extractor, one digest rule, one ledger.

============================  ==========================================
Bedrock orchestration part    Sealed as
============================  ==========================================
``rationale``                 ``message`` — the agent's stated reasoning
``invocationInput``           ``tool_use`` — action group, KB lookup,
                              code interpreter, or collaborator handoff
``observation.*Output``       ``tool_result``, paired by ``traceId``
``observation.finalResponse``  ``message`` — the decision text
``returnControl``             ``tool_use`` — a call the *caller* executes
``guardrailTrace`` INTERVENED  ``guardrail_intervention``
``failureTrace``              ``api_error``
============================  ==========================================

``modelInvocationInput`` is deliberately not sealed as an event. It carries the
constructed prompt, not an action; the proxy seam does not seal prompts either,
and making this seam the one that does would mean two seams disagreeing about
what an event is. Its ``foundationModel`` is kept — it is how a sealed row
learns which model actually ran. ``modelInvocationOutput``'s raw response (the
ReAct scratchpad) is off by default for the same reason and because it is
large; ``include_raw_model_output=True`` seals it for anyone who wants it.

Stated limits
-------------
This seam sees exactly what the trace says. A trace disabled at invoke time
produces no evidence, and unlike the proxy — which cannot be switched off from
inside the agent — that flag is the caller's to set. An agent invoked without
it leaves no record here, and this module cannot tell that case apart from an
agent that was never invoked at all. Say so to an examiner rather than letting
an empty ledger imply a quiet agent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from time import monotonic_ns
from typing import Any, Literal

from .extract import (
    PartialEvent,
    extract_request_events,
    extract_response_events,
)
from .schema import EventKind, new_span_id, new_trace_id, now_ts_device

__all__ = [
    "PROVIDER",
    "TOOL_NAME_SEPARATOR",
    "Capture",
    "NormalizedRun",
    "normalize_run",
    "seal_run",
    "tool_names",
]

#: Recorded on every event this seam produces. Not "anthropic" even when the
#: agent's foundation model is a Claude: the thing being governed is the AWS
#: agent runtime that chose the tool calls, and provenance should name what
#: actually made the decision.
PROVIDER = "bedrock_agent"

#: Joins an action group to the operation inside it, so one sealed tool name
#: identifies the action the way Bedrock's own console does. This is the name
#: Coriqo matches against an agent's ``allowed_tools``, so it has to be stable
#: and it has to be what a reviewer would recognise.
TOOL_NAME_SEPARATOR = "::"

# Synthetic action-group names for the orchestration steps that are real
# actions but are executed by Bedrock rather than by an action group.
_KNOWLEDGE_BASE_GROUP = "knowledge_base"
_CODE_INTERPRETER_TOOL = "code_interpreter"
_COLLABORATOR_GROUP = "agent_collaborator"

#: A Bedrock guardrail that actually fired: an enforcement decision taken by a
#: third party mid-run — not an error, not a model message, and not one of our
#: own mandate verdicts (those carry reason codes this has no way to supply).
KIND_GUARDRAIL_INTERVENTION = EventKind.GUARDRAIL_INTERVENTION.value


@dataclass(frozen=True, slots=True)
class Capture:
    """One thing to seal, already attributed to a span.

    ``direction`` says which recorder path it takes. ``request``/``response``
    hand ``body`` to the Anthropic extractors; ``direct`` carries a
    ready-made :class:`PartialEvent` for the kinds those extractors have no
    concept of.
    """

    direction: Literal["request", "response", "direct"]
    span_id: str
    parent_span_id: str | None = None
    body: dict[str, Any] | None = None
    event: PartialEvent | None = None
    # The tool this capture concerns, for a caller rendering a timeline. A
    # tool_result body carries only the tool_use_id it answers, so without
    # this a consumer would have to re-pair the captures itself to name the
    # step — which is exactly the pairing the normalizer already did.
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedRun:
    """A Bedrock agent run, ready to seal and to publish.

    ``final_text`` is the run's decision — the one field the Coriqo publisher
    ships as readable prose. ``guardrail_interventions`` and ``failures`` are
    surfaced separately because a caller will usually want to act on them
    without walking every capture.
    """

    session_id: str
    trace_id: str
    span_id: str
    captures: list[Capture] = field(default_factory=list)
    agent_id: str | None = None
    agent_alias_id: str | None = None
    agent_version: str | None = None
    foundation_model: str | None = None
    final_text: str = ""
    collaborator_spans: dict[str, str] = field(default_factory=dict)
    guardrail_interventions: int = 0
    failures: list[str] = field(default_factory=list)


def tool_names(run: NormalizedRun) -> list[str]:
    """Distinct tool names the run actually called, in first-call order.

    Useful for the first registration of an agent nobody has declared yet:
    it is the observed surface, which is a starting point for a mandate, not
    a substitute for one.
    """
    seen: list[str] = []
    for capture in run.captures:
        if capture.direction != "response" or not capture.body:
            continue
        for block in capture.body.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            if isinstance(name, str) and name not in seen:
                seen.append(name)
    return seen


def _parameters_to_dict(parameters: Any) -> dict[str, Any]:
    """Bedrock's ``[{name, type, value}]`` -> ``{name: value}``.

    Values arrive as strings whatever the declared ``type``; they are left as
    strings rather than coerced, because a digest over a coerced value no
    longer commits to what AWS actually sent.
    """
    out: dict[str, Any] = {}
    if not isinstance(parameters, list):
        return out
    for param in parameters:
        if isinstance(param, dict) and isinstance(param.get("name"), str):
            out[param["name"]] = param.get("value")
    return out


def _request_body_to_dict(request_body: Any) -> dict[str, Any]:
    """``{content: {"application/json": [{name,type,value}, ...]}}`` flattened
    per content type, keeping the content type so a reviewer can see how the
    payload was framed."""
    if not isinstance(request_body, dict):
        return {}
    content = request_body.get("content")
    if not isinstance(content, dict):
        return {}
    return {ctype: _parameters_to_dict(params) for ctype, params in content.items()}


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drops keys AWS omitted. An absent field and a null field are different
    claims, and only the first one is true here."""
    return {k: v for k, v in data.items() if v is not None and v != {} and v != []}


class _Normalizer:
    """Walks a trace stream once, holding only what pairing requires.

    Bedrock pairs an ``invocationInput`` with the ``observation`` that answers
    it by sharing a ``traceId``, so that is the only state carried between
    parts. Everything else is derived from the part in hand.
    """

    def __init__(
        self,
        *,
        session_id: str | None,
        trace_id: str | None,
        span_id: str | None,
        include_raw_model_output: bool,
    ) -> None:
        self._session_id = session_id
        self.trace_id = trace_id or new_trace_id()
        self.root_span_id = span_id or new_span_id()
        self._include_raw = include_raw_model_output

        self.captures: list[Capture] = []
        self.collaborator_spans: dict[str, str] = {}
        self.agent_id: str | None = None
        self.agent_alias_id: str | None = None
        self.agent_version: str | None = None
        self.foundation_model: str | None = None
        self.final_text = ""
        self.guardrail_interventions = 0
        self.failures: list[str] = []

        # bedrock traceId -> the tool_use id/name we minted for it, so the
        # observation that answers it seals as a paired tool_result.
        self._pending: dict[str, tuple[str, str]] = {}
        # A traceId is one orchestration step and should hold one invocation,
        # but a suffix keeps ids unique if that ever stops being true rather
        # than silently pairing a result with the wrong call.
        self._id_uses: dict[str, int] = {}
        # Streamed completion bytes, used as the decision text only if the
        # trace carried no finalResponse.
        self._chunk_text: list[str] = []

    # ------------------------------------------------------------- helpers

    @property
    def session_id(self) -> str:
        return self._session_id or "bedrock-agent-session"

    def _span_for(self, collaborator_name: str | None) -> tuple[str, str | None]:
        """A collaborator's steps get their own span under the root span.

        Bedrock's multi-agent collaboration runs a second agent inside the
        same session; giving it a distinct span keeps its work attributable
        instead of flattened into the supervisor's, which is the same shape
        the showcase uses for sub-agents.
        """
        if not collaborator_name:
            return self.root_span_id, None
        span = self.collaborator_spans.get(collaborator_name)
        if span is None:
            span = new_span_id()
            self.collaborator_spans[collaborator_name] = span
        return span, self.root_span_id

    def _tool_use_id(self, bedrock_trace_id: str | None) -> str:
        base = bedrock_trace_id or "untraced"
        uses = self._id_uses.get(base, 0)
        self._id_uses[base] = uses + 1
        return f"bda_{base}" if uses == 0 else f"bda_{base}#{uses}"

    def _emit_response(
        self,
        blocks: list[dict[str, Any]],
        span: str,
        parent: str | None,
        *,
        tool_name: str | None = None,
    ) -> None:
        if not blocks:
            return
        self.captures.append(
            Capture(
                direction="response",
                span_id=span,
                parent_span_id=parent,
                tool_name=tool_name,
                body={"model": self.foundation_model, "content": blocks},
            )
        )

    def _emit_tool_result(
        self,
        tool_use_id: str,
        content: Any,
        span: str,
        parent: str | None,
        *,
        is_error: bool = False,
        tool_name: str | None = None,
    ) -> None:
        self.captures.append(
            Capture(
                direction="request",
                span_id=span,
                parent_span_id=parent,
                tool_name=tool_name,
                body={
                    "model": self.foundation_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use_id,
                                    "content": content
                                    if isinstance(content, str)
                                    else json.dumps(content, sort_keys=True),
                                    "is_error": is_error,
                                }
                            ],
                        }
                    ],
                },
            )
        )

    def _emit_direct(
        self, kind: str, payload: dict[str, Any], span: str, parent: str | None
    ) -> None:
        self.captures.append(
            Capture(
                direction="direct",
                span_id=span,
                parent_span_id=parent,
                event=PartialEvent(
                    session_id=self.session_id,
                    kind=kind,
                    ts_device=now_ts_device(),
                    ts_monotonic_ns=monotonic_ns(),
                    tool_use_id=None,
                    tool_name=None,
                    payload=payload,
                    model=self.foundation_model,
                    provider=PROVIDER,
                    trace_id=self.trace_id,
                    span_id=span,
                    parent_span_id=parent,
                ),
            )
        )

    def _emit_tool_use(
        self,
        *,
        bedrock_trace_id: str | None,
        name: str,
        tool_input: dict[str, Any],
        span: str,
        parent: str | None,
    ) -> None:
        tool_use_id = self._tool_use_id(bedrock_trace_id)
        if bedrock_trace_id:
            self._pending[bedrock_trace_id] = (tool_use_id, name)
        self._emit_response(
            [{"type": "tool_use", "id": tool_use_id, "name": name, "input": tool_input}],
            span,
            parent,
            tool_name=name,
        )

    # --------------------------------------------------------------- parts

    def feed(self, chunk: Any) -> None:
        if not isinstance(chunk, dict):
            return
        if isinstance(chunk.get("trace"), dict):
            self._feed_trace_part(chunk["trace"])
        if isinstance(chunk.get("returnControl"), dict):
            self._feed_return_control(chunk["returnControl"])
        payload = chunk.get("chunk")
        if isinstance(payload, dict):
            raw = payload.get("bytes")
            if isinstance(raw, bytes):
                self._chunk_text.append(raw.decode("utf-8", "replace"))
            elif isinstance(raw, str):
                self._chunk_text.append(raw)

    def _feed_trace_part(self, part: dict[str, Any]) -> None:
        self.agent_id = part.get("agentId") or self.agent_id
        self.agent_alias_id = part.get("agentAliasId") or self.agent_alias_id
        self.agent_version = part.get("agentVersion") or self.agent_version
        if self._session_id is None and isinstance(part.get("sessionId"), str):
            self._session_id = part["sessionId"]

        collaborator = part.get("collaboratorName")
        span, parent = self._span_for(collaborator if isinstance(collaborator, str) else None)

        body = part.get("trace")
        if not isinstance(body, dict):
            return

        # pre/post-processing and the routing classifier run the same shapes as
        # orchestration; a prompt-injection attempt that lands in preprocessing
        # is as much a finding as one that lands mid-loop, so all of them are
        # walked rather than only the orchestration loop.
        for key in (
            "orchestrationTrace",
            "preProcessingTrace",
            "postProcessingTrace",
            "routingClassifierTrace",
            "customOrchestrationTrace",
        ):
            step = body.get(key)
            if isinstance(step, dict):
                self._feed_orchestration(step, span, parent)

        guardrail = body.get("guardrailTrace")
        if isinstance(guardrail, dict):
            self._feed_guardrail(guardrail, span, parent)

        failure = body.get("failureTrace")
        if isinstance(failure, dict):
            reason = failure.get("failureReason")
            self.failures.append(str(reason))
            self._emit_direct(
                EventKind.API_ERROR.value,
                _compact(
                    {
                        "error_type": "bedrock_agent_failure",
                        "message": reason,
                        "failure_code": failure.get("failureCode"),
                        "bedrock_trace_id": failure.get("traceId"),
                    }
                ),
                span,
                parent,
            )

    def _feed_orchestration(self, step: dict[str, Any], span: str, parent: str | None) -> None:
        model_input = step.get("modelInvocationInput")
        if isinstance(model_input, dict):
            model = model_input.get("foundationModel")
            if isinstance(model, str):
                self.foundation_model = model

        if self._include_raw:
            model_output = step.get("modelInvocationOutput")
            if isinstance(model_output, dict):
                raw = (model_output.get("rawResponse") or {}).get("content")
                if isinstance(raw, str) and raw:
                    self._emit_response([{"type": "text", "text": raw}], span, parent)

        rationale = step.get("rationale")
        if isinstance(rationale, dict) and isinstance(rationale.get("text"), str):
            text = rationale["text"]
            if text:
                self._emit_response([{"type": "text", "text": text}], span, parent)

        invocation = step.get("invocationInput")
        if isinstance(invocation, dict):
            self._feed_invocation(invocation, span, parent)

        observation = step.get("observation")
        if isinstance(observation, dict):
            self._feed_observation(observation, span, parent)

    def _feed_invocation(self, invocation: dict[str, Any], span: str, parent: str | None) -> None:
        bedrock_trace_id = invocation.get("traceId")

        action = invocation.get("actionGroupInvocationInput")
        if isinstance(action, dict):
            group = action.get("actionGroupName") or "action_group"
            operation = action.get("function")
            if not operation:
                verb = action.get("verb") or ""
                path = action.get("apiPath") or ""
                operation = f"{verb} {path}".strip() or "invoke"
            self._emit_tool_use(
                bedrock_trace_id=bedrock_trace_id,
                name=f"{group}{TOOL_NAME_SEPARATOR}{operation}",
                tool_input=_compact(
                    {
                        "parameters": _parameters_to_dict(action.get("parameters")),
                        "requestBody": _request_body_to_dict(action.get("requestBody")),
                        "apiPath": action.get("apiPath"),
                        "verb": action.get("verb"),
                        "executionType": action.get("executionType"),
                    }
                ),
                span=span,
                parent=parent,
            )
            return

        kb = invocation.get("knowledgeBaseLookupInput")
        if isinstance(kb, dict):
            kb_id = kb.get("knowledgeBaseId") or "lookup"
            self._emit_tool_use(
                bedrock_trace_id=bedrock_trace_id,
                name=f"{_KNOWLEDGE_BASE_GROUP}{TOOL_NAME_SEPARATOR}{kb_id}",
                tool_input=_compact({"text": kb.get("text")}),
                span=span,
                parent=parent,
            )
            return

        code = invocation.get("codeInterpreterInvocationInput")
        if isinstance(code, dict):
            self._emit_tool_use(
                bedrock_trace_id=bedrock_trace_id,
                name=_CODE_INTERPRETER_TOOL,
                tool_input=_compact({"code": code.get("code"), "files": code.get("files")}),
                span=span,
                parent=parent,
            )
            return

        collaborator = invocation.get("agentCollaboratorInvocationInput")
        if isinstance(collaborator, dict):
            name = collaborator.get("agentCollaboratorName") or "collaborator"
            self._emit_tool_use(
                bedrock_trace_id=bedrock_trace_id,
                name=f"{_COLLABORATOR_GROUP}{TOOL_NAME_SEPARATOR}{name}",
                tool_input=_compact(
                    {
                        "alias_arn": collaborator.get("agentCollaboratorAliasArn"),
                        "input": collaborator.get("input"),
                    }
                ),
                span=span,
                parent=parent,
            )

    def _feed_observation(self, observation: dict[str, Any], span: str, parent: str | None) -> None:
        bedrock_trace_id = observation.get("traceId")

        # The pending tool call is claimed only once we know this observation
        # really is that call's result. A collaborator's own finalResponse
        # shares its traceId with the supervisor's handoff, so popping first
        # let the sub-agent's answer consume the handoff and left the actual
        # handback orphaned — which verify.py reports as a broken run.
        final = observation.get("finalResponse")
        if isinstance(final, dict) and isinstance(final.get("text"), str):
            self.final_text = final["text"]
            if final["text"]:
                self._emit_response([{"type": "text", "text": final["text"]}], span, parent)
            return

        reprompt = observation.get("repromptResponse")
        if isinstance(reprompt, dict) and isinstance(reprompt.get("text"), str):
            # A reprompt is the orchestrator telling the model it got the
            # format wrong. Sealed as a message, because a run that needed
            # three of them is a different run from one that needed none.
            self._emit_response([{"type": "text", "text": reprompt["text"]}], span, parent)
            return

        pending = self._pending.pop(bedrock_trace_id, None) if bedrock_trace_id else None
        result: Any = None
        is_error = False
        for key in (
            "actionGroupInvocationOutput",
            "knowledgeBaseLookupOutput",
            "codeInterpreterInvocationOutput",
            "agentCollaboratorInvocationOutput",
        ):
            output = observation.get(key)
            if not isinstance(output, dict):
                continue
            if key == "actionGroupInvocationOutput":
                result = output.get("text")
            elif key == "knowledgeBaseLookupOutput":
                result = {"retrievedReferences": output.get("retrievedReferences") or []}
            elif key == "codeInterpreterInvocationOutput":
                is_error = bool(output.get("executionError"))
                result = _compact(
                    {
                        "executionOutput": output.get("executionOutput"),
                        "executionError": output.get("executionError"),
                        "executionTimeout": output.get("executionTimeout"),
                        "files": output.get("files"),
                    }
                )
            else:
                payload = output.get("output")
                result = payload.get("text") if isinstance(payload, dict) else payload
            break

        if result is None:
            return
        if pending is None:
            # An observation with nothing to pair to still happened. Sealing
            # it unpaired beats dropping it: verify.py reports an orphan
            # tool_result as a finding, which is the correct signal that the
            # trace stream was incomplete.
            tool_use_id, pending_name = self._tool_use_id(bedrock_trace_id), None
        else:
            tool_use_id, pending_name = pending
        self._emit_tool_result(
            tool_use_id, result, span, parent, is_error=is_error, tool_name=pending_name
        )

    def _feed_guardrail(self, guardrail: dict[str, Any], span: str, parent: str | None) -> None:
        if guardrail.get("action") != "INTERVENED":
            return
        self.guardrail_interventions += 1
        self._emit_direct(
            KIND_GUARDRAIL_INTERVENTION,
            _compact(
                {
                    "action": guardrail.get("action"),
                    "bedrock_trace_id": guardrail.get("traceId"),
                    "input_assessments": guardrail.get("inputAssessments"),
                    "output_assessments": guardrail.get("outputAssessments"),
                }
            ),
            span,
            parent,
        )

    def _feed_return_control(self, payload: dict[str, Any]) -> None:
        """The agent handed a call back for the caller to execute.

        These matter more than ordinary action-group calls, not less: the
        action runs in the customer's own process, outside anything AWS
        records, so this event is the only place the request is written down.
        """
        span, parent = self.root_span_id, None
        invocation_id = payload.get("invocationId")
        for member in payload.get("invocationInputs") or []:
            if not isinstance(member, dict):
                continue
            function = member.get("functionInvocationInput")
            api = member.get("apiInvocationInput")
            if isinstance(function, dict):
                group = function.get("actionGroup") or "action_group"
                name = f"{group}{TOOL_NAME_SEPARATOR}{function.get('function') or 'invoke'}"
                tool_input = _compact(
                    {
                        "parameters": _parameters_to_dict(function.get("parameters")),
                        "actionInvocationType": function.get("actionInvocationType"),
                        "return_control": True,
                    }
                )
            elif isinstance(api, dict):
                group = api.get("actionGroup") or "action_group"
                operation = f"{api.get('httpMethod') or ''} {api.get('apiPath') or ''}".strip()
                name = f"{group}{TOOL_NAME_SEPARATOR}{operation or 'invoke'}"
                tool_input = _compact(
                    {
                        "parameters": _parameters_to_dict(api.get("parameters")),
                        "requestBody": _request_body_to_dict(api.get("requestBody")),
                        "apiPath": api.get("apiPath"),
                        "verb": api.get("httpMethod"),
                        "actionInvocationType": api.get("actionInvocationType"),
                        "return_control": True,
                    }
                )
            else:
                continue
            self._emit_tool_use(
                bedrock_trace_id=f"returnControl:{invocation_id}",
                name=name,
                tool_input=tool_input,
                span=span,
                parent=parent,
            )

    def finish(self) -> NormalizedRun:
        if not self.final_text and self._chunk_text:
            # No finalResponse in the trace (an agent invoked with streaming
            # only, or a truncated stream). The completion bytes are the same
            # answer, so the decision text is recoverable even though the
            # per-step evidence is not.
            self.final_text = "".join(self._chunk_text)
        return NormalizedRun(
            session_id=self.session_id,
            trace_id=self.trace_id,
            span_id=self.root_span_id,
            captures=self.captures,
            agent_id=self.agent_id,
            agent_alias_id=self.agent_alias_id,
            agent_version=self.agent_version,
            foundation_model=self.foundation_model,
            final_text=self.final_text,
            collaborator_spans=dict(self.collaborator_spans),
            guardrail_interventions=self.guardrail_interventions,
            failures=self.failures,
        )


def normalize_run(
    chunks: Iterable[Any],
    *,
    session_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    include_raw_model_output: bool = False,
) -> NormalizedRun:
    """Turn one agent invocation's trace stream into captures. Pure.

    ``chunks`` is whatever ``InvokeAgent``'s ``completion`` stream yielded —
    ``trace``, ``chunk`` and ``returnControl`` members, in order. Anything
    else is ignored rather than rejected, so a new member added to the
    response stream by AWS cannot break ingest of the members we do handle.

    No AWS SDK, no clock beyond timestamping, no ledger: give it recorded
    JSON and it produces the same captures it would from a live stream, which
    is what makes the fixtures in the showcase real tests rather than mocks.
    """
    normalizer = _Normalizer(
        session_id=session_id,
        trace_id=trace_id,
        span_id=span_id,
        include_raw_model_output=include_raw_model_output,
    )
    for chunk in chunks:
        normalizer.feed(chunk)
    return normalizer.finish()


def seal_run(recorder: Any, run: NormalizedRun, *, record_session_start: bool = True) -> int:
    """Seal a normalized run into the ledger. Returns the event count.

    Extraction is run here rather than through ``recorder.record_request_body``
    so each event's ``provider`` can be corrected to ``bedrock_agent`` before
    it is sealed. Going through the recorder's own helpers would stamp every
    row ``anthropic``, which is what the extractor defaults to — true of the
    body's shape, false about who ran the agent, and provenance that is false
    is worse than provenance that is missing.
    """
    partials: list[PartialEvent] = []

    if record_session_start:
        partials.append(
            PartialEvent(
                session_id=run.session_id,
                kind=EventKind.SESSION_START.value,
                ts_device=now_ts_device(),
                ts_monotonic_ns=monotonic_ns(),
                tool_use_id=None,
                tool_name=None,
                payload=_compact(
                    {
                        "source": "bedrock_agent_trace",
                        "agent_id": run.agent_id,
                        "agent_alias_id": run.agent_alias_id,
                        "agent_version": run.agent_version,
                    }
                ),
                model=run.foundation_model,
                provider=PROVIDER,
                trace_id=run.trace_id,
                span_id=run.span_id,
            )
        )

    for capture in run.captures:
        if capture.direction == "direct":
            if capture.event is not None:
                partials.append(capture.event)
            continue
        if capture.body is None:
            continue
        extract = (
            extract_request_events if capture.direction == "request" else extract_response_events
        )
        extracted = extract(
            capture.body,
            session_id=run.session_id,
            trace_id=run.trace_id,
            span_id=capture.span_id,
            parent_span_id=capture.parent_span_id,
        )
        partials.extend(replace(partial, provider=PROVIDER) for partial in extracted)

    recorder.record_many(partials)
    return len(partials)
