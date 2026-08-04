"""Regression tests for derive_session_id's collision-safety guarantees.

See src/byoai/agent_context_cache/main.py:derive_session_id for the
correctness argument: the auto-derived fallback must be scoped by API key,
not just by content, or two unrelated conversations that happen to open with
byte-identical system+first-message content collide onto the same dedup
state and corrupt each other's context (main.py's optimize_payload deletes
"already seen" text based on session_id).
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.requests import Request

from byoai.agent_context_cache.main import derive_session_id


def make_request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "headers": Headers(headers).raw,
    }
    return Request(scope)


BODY = {
    "system": "You are a helpful assistant.",
    "messages": [{"role": "user", "content": "hello"}],
}


def test_explicit_session_header_wins_over_derived_content():
    req = make_request({"x-byoai-session-id": "abc123", "x-api-key": "key-a"})
    assert derive_session_id(req, BODY) == "hdr:abc123"


def test_explicit_session_header_ignores_api_key_and_content():
    req_a = make_request({"x-session-id": "same-convo", "x-api-key": "key-a"})
    req_b = make_request({"x-session-id": "same-convo", "x-api-key": "key-b"})
    assert derive_session_id(req_a, BODY) == derive_session_id(req_b, BODY)


def test_derived_session_id_stable_across_calls_with_same_input():
    req1 = make_request({"x-api-key": "key-a"})
    req2 = make_request({"x-api-key": "key-a"})
    assert derive_session_id(req1, BODY) == derive_session_id(req2, BODY)


def test_derived_session_id_differs_by_api_key():
    """
    The core regression test: two different accounts sending byte-identical
    system prompt + first message must NOT share dedup state, or one
    account's conversation can silently corrupt another's (content it never
    actually saw gets replaced with a "duplicate, already seen" placeholder).
    """
    req_a = make_request({"x-api-key": "key-a"})
    req_b = make_request({"x-api-key": "key-b"})
    assert derive_session_id(req_a, BODY) != derive_session_id(req_b, BODY)


def test_derived_session_id_differs_by_first_message():
    req = make_request({"x-api-key": "key-a"})
    other_body = {**BODY, "messages": [{"role": "user", "content": "goodbye"}]}
    assert derive_session_id(req, BODY) != derive_session_id(req, other_body)


def test_derived_session_id_differs_by_system_prompt():
    req = make_request({"x-api-key": "key-a"})
    other_body = {**BODY, "system": "You are a different assistant."}
    assert derive_session_id(req, BODY) != derive_session_id(req, other_body)


def test_derived_session_id_works_without_api_key_header():
    """Absent header degrades to empty-string scoping — still a function, not a crash."""
    req = make_request({})
    assert derive_session_id(req, BODY).startswith("auto:")
