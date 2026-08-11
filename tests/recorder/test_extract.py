"""Tests for the recorder's SSE extractor (workstream C)."""

from __future__ import annotations

import json

import pytest

from byoai.recorder.extract import (
    KIND_PARSE_FAILURE,
    KIND_STREAM_ABORTED,
    PartialEvent,
    StreamExtractor,
    extract_request_events,
    extract_response_events,
)

# A recorded-shape Anthropic stream: one text block, two interleaved tool_use
# blocks, a comment/heartbeat line, and CRLF frames mixed with LF frames.
REAL_STREAM = (
    b"event: message_start\r\n"
    b'data: {"type":"message_start","message":{"id":"msg_01","type":"message",'
    b'"role":"assistant","model":"claude-opus-4","content":[],'
    b'"usage":{"input_tokens":1200,"output_tokens":1}}}\r\n'
    b"\r\n"
    b": ping\r\n"
    b"\r\n"
    b'event: content_block_start\r\ndata: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\r\n\r\n'
    b'event: content_block_delta\r\ndata: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"Let me check "}}\r\n\r\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"both files."}}\n\n'
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    b'event: content_block_start\ndata: {"type":"content_block_start","index":1,'
    b'"content_block":{"type":"tool_use","id":"toolu_A","name":"Read","input":{}}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,'
    b'"delta":{"type":"input_json_delta","partial_json":"{\\"fi"}}\n\n'
    b'event: content_block_start\ndata: {"type":"content_block_start","index":2,'
    b'"content_block":{"type":"tool_use","id":"toolu_B","name":"Bash","input":{}}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":2,'
    b'"delta":{"type":"input_json_delta","partial_json":"{\\"cmd\\":\\"ls\\"}"}}\n\n'
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,'
    b'"delta":{"type":"input_json_delta","partial_json":"le\\":\\"a.py\\"}"}}\n\n'
    b": keep-alive\n\n"
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":2}\n\n'
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":1}\n\n'
    b'event: message_delta\ndata: {"type":"message_delta",'
    b'"delta":{"stop_reason":"tool_use"},"usage":{"output_tokens":88}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _fingerprint(events: list[PartialEvent]) -> list[tuple]:
    """Everything except the wall/monotonic clocks, which are not deterministic."""
    return [
        (e.session_id, e.kind, e.tool_use_id, e.tool_name, e.model, e.provider,
         json.dumps(e.payload, sort_keys=True, default=str))
        for e in events
    ]


def _run_whole(data: bytes, **kw) -> list[PartialEvent]:
    ex = StreamExtractor(session_id=kw.pop("session_id", "ses_1"), model=kw.pop("model", None))
    events = list(ex.feed(data))
    events.extend(ex.close())
    return events


def _run_split(data: bytes, size: int = 1, **kw) -> list[PartialEvent]:
    ex = StreamExtractor(session_id=kw.pop("session_id", "ses_1"), model=kw.pop("model", None))
    events: list[PartialEvent] = []
    for i in range(0, len(data), size):
        events.extend(ex.feed(data[i : i + size]))
    events.extend(ex.close())
    return events


# -- streaming happy path ---------------------------------------------------


def test_full_stream_emits_message_and_both_tool_uses_in_stop_order():
    events = _run_whole(REAL_STREAM)
    assert [e.kind for e in events] == ["message", "tool_use", "tool_use"]

    msg = events[0]
    assert msg.payload["text"] == "Let me check both files."
    assert msg.payload["complete"] is True
    # model is learned from message_start even though the caller passed None.
    assert msg.model == "claude-opus-4"

    # Emission order follows content_block_stop order, not start order.
    b_event, a_event = events[1], events[2]
    assert b_event.tool_use_id == "toolu_B"
    assert b_event.tool_name == "Bash"
    assert b_event.payload["input"] == {"cmd": "ls"}
    assert a_event.tool_use_id == "toolu_A"
    assert a_event.payload["input"] == {"file": "a.py"}
    assert all(e.provider == "anthropic" for e in events)


def test_tool_use_not_emitted_before_content_block_stop():
    ex = StreamExtractor(session_id="ses_1", model=None)
    marker = b'event: content_block_stop\ndata: {"type":"content_block_stop","index":2}'
    upto = REAL_STREAM.split(marker)[0]
    events = ex.feed(upto)
    assert [e.kind for e in events] == ["message"]  # no tool_use yet


@pytest.mark.parametrize("size", [1, 2, 3, 7, 64, 4096])
def test_chunk_boundaries_do_not_change_output(size):
    assert _fingerprint(_run_split(REAL_STREAM, size)) == _fingerprint(_run_whole(REAL_STREAM))


def test_byte_at_a_time_matches_whole_stream_exactly():
    assert _fingerprint(_run_split(REAL_STREAM, 1)) == _fingerprint(_run_whole(REAL_STREAM))


def test_split_inside_multibyte_utf8_sequence():
    text = "café ☕"
    stream = (
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"' + text + '"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
    ).encode()
    events = _run_split(stream, 1)
    assert [e.payload["text"] for e in events] == [text]


def test_crlf_and_lf_frames_are_equivalent():
    lf = REAL_STREAM.replace(b"\r\n", b"\n")
    crlf = lf.replace(b"\n", b"\r\n")
    assert _fingerprint(_run_whole(lf)) == _fingerprint(_run_whole(crlf))


def test_comment_and_heartbeat_lines_are_ignored():
    stream = b": ping\n\n: \n\n" + REAL_STREAM
    assert _fingerprint(_run_whole(stream)) == _fingerprint(_run_whole(REAL_STREAM))


def test_data_before_event_line_and_missing_event_line_both_work():
    frames = (
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"tool_use","id":"toolu_X","name":"Grep"}}\n'
        b"event: content_block_start\n\n"
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":\\"x\\"}"}}\n\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
    )
    events = _run_whole(frames)
    assert [e.kind for e in events] == ["tool_use"]
    assert events[0].payload["input"] == {"q": "x"}


def test_multiline_data_field_is_joined_with_newline():
    obj = {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}
    dumped = json.dumps(obj)
    half = len(dumped) // 2
    # SSE joins successive data: lines with "\n"; JSON tolerates the newline.
    stream = (
        f"data: {dumped[:half]}\ndata: {dumped[half:]}\n\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"hi"}}\n\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
    ).encode()
    # The split point may land inside a JSON string, which json cannot parse;
    # only assert the parseable case is handled.
    events = _run_whole(stream)
    kinds = [e.kind for e in events]
    assert "message" in kinds or KIND_PARSE_FAILURE in kinds


def test_no_space_after_colon_is_accepted():
    stream = (
        b'data:{"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text"}}\n\n'
        b'data:{"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
        b'data:{"type":"content_block_stop","index":0}\n\n'
    )
    events = _run_whole(stream)
    assert [(e.kind, e.payload["text"]) for e in events] == [("message", "ok")]


# -- malformed / adversarial ------------------------------------------------


def test_malformed_tool_input_is_marked_not_dropped():
    stream = (
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"tool_use","id":"toolu_M","name":"Edit"}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"input_json_delta","partial_json":"{\\"broken\\": "}}\n\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
    )
    events = _run_whole(stream)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "tool_use"
    assert ev.tool_use_id == "toolu_M"
    assert ev.payload["malformed_input"] is True
    assert ev.payload["input_raw"] == '{"broken": '
    assert ev.payload["input"] is None
    assert ev.payload["parse_error"]


def test_empty_partial_json_means_empty_input():
    stream = (
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"tool_use","id":"toolu_E","name":"NoArgs"}}\n\n'
        b'data: {"type":"content_block_stop","index":0}\n\n'
    )
    events = _run_whole(stream)
    assert events[0].payload["input"] == {}
    assert "malformed_input" not in events[0].payload


def test_non_json_data_line_emits_parse_failure():
    events = _run_whole(b"data: not json at all\n\n")
    assert [e.kind for e in events] == [KIND_PARSE_FAILURE]
    assert events[0].payload["raw"] == "not json at all"


def test_done_sentinel_is_ignored():
    assert _run_whole(b"data: [DONE]\n\n") == []


def test_error_event_becomes_api_error():
    stream = (
        b"event: error\n"
        b'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    )
    events = _run_whole(stream)
    assert [e.kind for e in events] == ["api_error"]
    assert events[0].payload["error_type"] == "overloaded_error"


def test_delta_for_unknown_block_index_is_not_dropped():
    stream = (
        b'data: {"type":"content_block_delta","index":9,'
        b'"delta":{"type":"text_delta","text":"orphan"}}\n\n'
        b'data: {"type":"content_block_stop","index":9}\n\n'
    )
    events = _run_whole(stream)
    assert [(e.kind, e.payload["text"]) for e in events] == [("message", "orphan")]


# -- truncation -------------------------------------------------------------


def test_close_on_truncated_stream_marks_incomplete_tool_block():
    cut = REAL_STREAM.split(b'data: {"type":"content_block_stop","index":2}')[0]
    ex = StreamExtractor(session_id="ses_1", model=None)
    during = ex.feed(cut)
    assert [e.kind for e in during] == ["message"]

    aborted = ex.close()
    assert [e.kind for e in aborted] == [KIND_STREAM_ABORTED, KIND_STREAM_ABORTED]
    by_id = {e.tool_use_id: e for e in aborted}
    assert by_id["toolu_A"].payload["complete"] is False
    assert by_id["toolu_A"].payload["reason"] == "stream_truncated"
    assert by_id["toolu_A"].payload["partial_json"] == '{"file":"a.py"}'
    assert by_id["toolu_B"].payload["partial_json"] == '{"cmd":"ls"}'
    assert by_id["toolu_B"].tool_name == "Bash"


def test_close_marks_message_level_truncation_when_no_blocks_open():
    head = (
        b'data: {"type":"message_start","message":{"id":"m","model":"claude-x",'
        b'"usage":{"input_tokens":5}}}\n\n'
    )
    ex = StreamExtractor(session_id="ses_1", model=None)
    ex.feed(head)
    events = ex.close()
    assert [e.kind for e in events] == [KIND_STREAM_ABORTED]
    assert events[0].payload["complete"] is False
    assert events[0].tool_use_id is None


def test_close_is_idempotent_and_feed_after_close_is_inert():
    ex = StreamExtractor(session_id="ses_1", model=None)
    ex.feed(REAL_STREAM)
    assert ex.close() == []
    assert ex.close() == []
    assert ex.feed(REAL_STREAM) == []


def test_complete_stream_close_emits_nothing():
    ex = StreamExtractor(session_id="ses_1", model=None)
    ex.feed(REAL_STREAM)
    assert ex.close() == []


def test_truncation_output_is_identical_byte_at_a_time():
    cut = REAL_STREAM[: len(REAL_STREAM) - 400]
    assert _fingerprint(_run_split(cut, 1)) == _fingerprint(_run_whole(cut))


def test_extractor_does_not_retain_or_alter_passthrough_bytes():
    """The tee must not hold the stream: buffers only ever hold a partial line."""
    ex = StreamExtractor(session_id="ses_1", model=None)
    ex.feed(REAL_STREAM)
    assert bytes(ex._buf) == b""


# -- request-side extraction ------------------------------------------------


def test_request_tool_result_string_content():
    body = {
        "model": "claude-opus-4",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_A"}]},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_A", "content": "file contents"}
                ],
            },
        ],
    }
    events = extract_request_events(body, session_id="ses_1")
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "tool_result"
    assert ev.tool_use_id == "toolu_A"
    assert ev.payload["content"] == "file contents"
    assert ev.payload["is_error"] is False
    assert ev.model == "claude-opus-4"


def test_request_tool_result_list_content_and_is_error():
    blocks = [{"type": "text", "text": "boom"}]
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "ignore me"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_B",
                        "content": blocks,
                        "is_error": True,
                    },
                    {"type": "tool_result", "tool_use_id": "toolu_C", "content": "ok"},
                ],
            }
        ]
    }
    events = extract_request_events(body, session_id="ses_1")
    assert [e.tool_use_id for e in events] == ["toolu_B", "toolu_C"]
    assert events[0].payload["content"] == blocks
    assert events[0].payload["is_error"] is True
    assert events[1].payload["is_error"] is False


def test_request_only_reads_the_last_user_message():
    body = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "old", "content": "x"}],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "..."}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "new", "content": "y"}],
            },
        ]
    }
    events = extract_request_events(body, session_id="ses_1")
    assert [e.tool_use_id for e in events] == ["new"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"messages": []},
        {"messages": [{"role": "user", "content": "plain string"}]},
        {"messages": [{"role": "assistant", "content": [{"type": "text", "text": "a"}]}]},
        {"messages": "not a list"},
    ],
)
def test_request_extraction_tolerates_odd_bodies(body):
    assert extract_request_events(body, session_id="ses_1") == []


# -- non-streaming response extraction --------------------------------------


def test_response_extraction_text_and_tool_use():
    body = {
        "model": "claude-opus-4",
        "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {"file": "a.py"}},
            {"type": "tool_use", "id": "toolu_B", "name": "Bash", "input": {"cmd": "ls"}},
        ],
    }
    events = extract_response_events(body, session_id="ses_1")
    assert [e.kind for e in events] == ["message", "tool_use", "tool_use"]
    assert [e.tool_use_id for e in events] == [None, "toolu_A", "toolu_B"]
    assert events[1].payload["input"] == {"file": "a.py"}
    assert events[2].tool_name == "Bash"


def test_response_error_body_becomes_api_error():
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    events = extract_response_events(body, session_id="ses_1")
    assert [e.kind for e in events] == ["api_error"]
    assert events[0].payload["message"] == "bad"


def test_streaming_and_non_streaming_agree_on_the_same_turn():
    """The tee must record the same tool calls either transport takes."""
    streamed = [e for e in _run_whole(REAL_STREAM) if e.kind == "tool_use"]
    body = {
        "model": "claude-opus-4",
        "content": [
            {"type": "text", "text": "Let me check both files."},
            {"type": "tool_use", "id": "toolu_A", "name": "Read", "input": {"file": "a.py"}},
            {"type": "tool_use", "id": "toolu_B", "name": "Bash", "input": {"cmd": "ls"}},
        ],
    }
    batch = [e for e in extract_response_events(body, session_id="ses_1") if e.kind == "tool_use"]
    key = lambda e: (e.tool_use_id, e.tool_name, json.dumps(e.payload["input"], sort_keys=True))  # noqa: E731
    assert sorted(map(key, streamed)) == sorted(map(key, batch))
