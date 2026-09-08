"""Where Bedrock agent traces come from. The only place ``boto3`` is imported.

:mod:`byoai.recorder.bedrock_agent` does the translation and knows nothing
about AWS — you can hand it recorded JSON and get identical output. This module
is the thin layer that fetches the JSON, and it is deliberately thin: the next
managed agent runtime worth ingesting (Azure AI Agent Service threads, say)
needs a sibling of this file, not a rewrite of the normalizer.

Two sources, and they are not equivalent
----------------------------------------
:func:`stream_invocation` is the strong one. The caller invokes the agent with
``enableTrace=True`` and gets every orchestration step in the response, live.
The evidence is as complete as Bedrock's trace, and it is sealed in the same
process that made the request.

:func:`iter_cloudwatch_invocations` is the weak one, and it is offered because
it is often the only one available: it reads Bedrock's model-invocation logs
after the fact. Say the limits out loud to anyone relying on it — the log group
is written by AWS and read by us, so the customer's own logging configuration
decides what is in it (large payloads are dropped to S3, and data delivery can
be disabled per account), and anyone with CloudWatch write access could have
edited a record before we sealed it. The ledger will faithfully attest to what
the log said, which is not the same claim as attesting to what the agent did.

``boto3`` lives behind the ``byoai-runtime[bedrock-agent]`` extra, so both
functions import it lazily: a base install must keep importing the recorder.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from .bedrock_agent import NormalizedRun, normalize_run, seal_run

log = logging.getLogger(__name__)

__all__ = [
    "BedrockAgentSourceError",
    "new_session_id",
    "iter_cloudwatch_invocations",
    "record_invocation",
    "stream_invocation",
]


class BedrockAgentSourceError(RuntimeError):
    """Raised when the AWS side cannot be reached or is misconfigured."""


def new_session_id() -> str:
    """A session id for a caller that has none. Bedrock treats the session as
    the conversation, and the recorder treats it as the run, so the same value
    has to reach both."""
    return f"byoai-{uuid.uuid4().hex[:16]}"


def _client(service: str, client: Any | None, region_name: str | None) -> Any:
    if client is not None:
        return client
    try:
        import boto3  # noqa: PLC0415 - optional extra, imported on use
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise BedrockAgentSourceError(
            "boto3 is required for Bedrock agent ingest: pip install 'byoai-runtime[bedrock-agent]'"
        ) from exc
    return boto3.client(service, region_name=region_name) if region_name else boto3.client(service)


def stream_invocation(
    *,
    agent_id: str,
    agent_alias_id: str,
    prompt: str,
    session_id: str | None = None,
    client: Any | None = None,
    region_name: str | None = None,
    **invoke_kwargs: Any,
) -> Iterator[dict[str, Any]]:
    """Invoke a Bedrock agent with tracing on; yield raw response-stream events.

    ``enableTrace`` is forced on rather than defaulted, because an untraced
    invocation through this function would return a stream that seals to an
    empty run — evidence that looks like a quiet agent instead of like a
    missing flag. If you want an untraced call, call boto3 directly.

    The events are yielded exactly as AWS returned them and are not buffered,
    so a caller can stream ``chunk`` text to a UI while the trace parts flow
    to the normalizer.
    """
    runtime = _client("bedrock-agent-runtime", client, region_name)
    session = session_id or new_session_id()
    try:
        response = runtime.invoke_agent(
            agentId=agent_id,
            agentAliasId=agent_alias_id,
            sessionId=session,
            inputText=prompt,
            enableTrace=True,
            **invoke_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - botocore raises a wide family here
        raise BedrockAgentSourceError(f"invoke_agent failed: {exc}") from exc

    for event in response.get("completion", []):
        if isinstance(event, dict):
            yield event


def record_invocation(
    recorder: Any,
    *,
    agent_id: str,
    agent_alias_id: str,
    prompt: str,
    session_id: str | None = None,
    client: Any | None = None,
    region_name: str | None = None,
    include_raw_model_output: bool = False,
    **invoke_kwargs: Any,
) -> NormalizedRun:
    """Invoke an agent and seal the whole run. The one-call path.

    The stream is drained before anything is sealed, on purpose: the ledger is
    append-only and a half-sealed run cannot be taken back, so a stream that
    dies mid-flight should leave the partial evidence it produced *and*
    whatever the trace said about why — which is what normalizing the drained
    chunks gives, since a truncated trace still normalizes.
    """
    # Resolved here rather than left to stream_invocation, so the id AWS is
    # called under is the id the events are sealed under. Letting each pick
    # its own worked only because the trace echoes the session back — which
    # an untraced or truncated stream does not.
    session = session_id or new_session_id()
    chunks = list(
        stream_invocation(
            agent_id=agent_id,
            agent_alias_id=agent_alias_id,
            prompt=prompt,
            session_id=session,
            client=client,
            region_name=region_name,
            **invoke_kwargs,
        )
    )
    run = normalize_run(
        chunks,
        session_id=session,
        include_raw_model_output=include_raw_model_output,
    )
    if recorder is not None:
        seal_run(recorder, run)
    return run


def iter_cloudwatch_invocations(
    *,
    log_group_name: str,
    start_time_ms: int,
    end_time_ms: int | None = None,
    client: Any | None = None,
    region_name: str | None = None,
    filter_pattern: str = "",
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Replay Bedrock model-invocation logs as ``(session_id, chunks)`` pairs.

    Yields one pair per session so a caller can normalize and seal each run
    independently. Records that are not JSON, or that carry no recognisable
    session, are skipped with a warning rather than aborting the sweep — a
    single malformed line in a log group of thousands is not a reason to
    ingest none of them.

    Records arrive grouped by session but in whatever order CloudWatch
    returned them, which is timestamp order within the window. A run that
    straddles the window boundary will be ingested as two partial runs; widen
    the window rather than stitching them here, because a stitch we invent is
    not something the log actually said.
    """
    logs = _client("logs", client, region_name)
    paginator = logs.get_paginator("filter_log_events")
    pages = paginator.paginate(
        logGroupName=log_group_name,
        startTime=start_time_ms,
        **({"endTime": end_time_ms} if end_time_ms is not None else {}),
        **({"filterPattern": filter_pattern} if filter_pattern else {}),
    )

    by_session: dict[str, list[dict[str, Any]]] = {}
    for page in pages:
        for event in page.get("events", []):
            message = event.get("message")
            if not isinstance(message, str):
                continue
            try:
                record = json.loads(message)
            except json.JSONDecodeError:
                log.warning("bedrock_agent: skipping non-JSON log record in %s", log_group_name)
                continue
            session = _session_of(record)
            if session is None:
                log.warning("bedrock_agent: log record with no session id, skipped")
                continue
            by_session.setdefault(session, []).append(record)

    for session, records in by_session.items():
        yield session, records


def _session_of(record: Any) -> str | None:
    """Bedrock's invocation-log records nest the session differently depending
    on which surface wrote them, so the known spellings are tried in turn
    rather than assuming one."""
    if not isinstance(record, dict):
        return None
    for key in ("sessionId", "session_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    for container in ("trace", "identity", "input", "requestMetadata"):
        nested = record.get(container)
        if isinstance(nested, dict):
            found = _session_of(nested)
            if found is not None:
                return found
    return None
