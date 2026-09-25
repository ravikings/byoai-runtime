"""The capture proxy addon, driven with stand-in flows (no network)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("mitmproxy")

from byoai.integrations.shield_proxy import ShieldProxy, reply_text  # noqa: E402

SECRET = "email j.rivers@gmail.com card 4242424242424242"


class _Req:
    def __init__(self, host, path, body):
        self.pretty_host = host
        self.path = path
        self.path_components = tuple(p for p in path.split("/") if p)
        self.pretty_url = f"https://{host}{path}"
        self._body = body

    def get_text(self, strict=False):
        return self._body

    def set_text(self, text):
        self._body = text


class _Resp:
    def __init__(self, text, status=200):
        self._text = text
        self.status_code = status

    def get_text(self, strict=False):
        return self._text


class _Flow:
    def __init__(self, host, path, body):
        self.request = _Req(host, path, body)
        self.response = None
        self.metadata: dict = {}


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    import byoai.integrations.shield as sh
    real = sh._fingerprint_key
    monkeypatch.setattr(sh, "_fingerprint_key",
                        lambda p=None: real(tmp_path / "fp.key"))
    return ShieldProxy(ledger=tmp_path / "captures.jsonl",
                       policy_path=tmp_path / "policy.json")


def _rows(proxy):
    return [json.loads(x) for x in proxy.ledger.read_text().splitlines()]


def _chat(body):
    return _Flow("claude.ai", "/api/organizations/o1/chat_conversations/c1/completion",
                 json.dumps(body))


def test_default_redacts_before_send_and_logs_no_text(proxy):
    flow = _chat({"prompt": SECRET, "timezone": "America/Chicago"})
    proxy.request(flow)
    sent = json.loads(flow.request.get_text())
    assert sent["prompt"] == "email [redacted-email] card [redacted-card]"
    (row,) = _rows(proxy)
    assert row["kind"] == "desktop.chat.request"
    assert row["verdict"] == "redacted(2)"
    assert "j.rivers" not in json.dumps(row) and "4242" not in json.dumps(row)
    assert "prompt" not in row and "preview" not in row
    assert row["chars"] == len(SECRET)


def test_observe_sends_unchanged_but_still_logs_no_text(proxy):
    proxy.policy_path.write_text(json.dumps({"policy_version": 2, "mode": "observe"}))
    flow = _chat({"prompt": SECRET})
    proxy.request(flow)
    assert json.loads(flow.request.get_text())["prompt"] == SECRET
    assert "j.rivers" not in proxy.ledger.read_text()


def test_block_stops_credentials_locally(proxy):
    proxy.policy_path.write_text(json.dumps({"mode": "block"}))
    flow = _chat({"prompt": "password: hunter2"})
    proxy.request(flow)
    assert flow.response is not None and flow.response.status_code == 403
    (row,) = _rows(proxy)
    assert row["kind"] == "desktop.verdict.denied"
    assert "hunter2" not in json.dumps(row)


def test_app_turned_off_is_pure_pass_through(proxy):
    proxy.policy_path.write_text(json.dumps({"apps": {"claude": False}}))
    flow = _chat({"prompt": SECRET})
    proxy.request(flow)
    assert json.loads(flow.request.get_text())["prompt"] == SECRET
    assert not proxy.ledger.exists()


def test_reply_flags_without_reply_text(proxy):
    flow = _chat({"prompt": "hi"})
    proxy.request(flow)
    sse = "\n".join([
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"sent to "}}',
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"j.rivers@gmail.com"}}',
    ])
    flow.response = _Resp(sse)
    proxy.response(flow)
    reply = _rows(proxy)[-1]
    assert reply["kind"] == "desktop.chat.response"
    assert reply["flags"] == ["pii:reply_pii_echo"]
    assert "j.rivers" not in json.dumps(reply)


def test_reply_text_reads_sse_and_json():
    assert reply_text('data: {"completion":"a"}\ndata: {"completion":"b"}') == "ab"
    assert reply_text(json.dumps({"content": [{"type": "text", "text": "x"}]})) == "x"


def test_chatgpt_is_governed_once_turned_on(proxy):
    body = {"messages": [{"content": {"content_type": "text", "parts": [SECRET]}}]}
    flow = _Flow("chatgpt.com", "/backend-api/conversation", json.dumps(body))
    proxy.request(flow)                       # off by default: untouched
    assert not proxy.ledger.exists()
    proxy.policy_path.write_text(json.dumps({"apps": {"chatgpt": True}}))
    proxy._policy = None
    flow = _Flow("chatgpt.com", "/backend-api/conversation", json.dumps(body))
    proxy.request(flow)
    sent = json.loads(flow.request.get_text())
    assert "j.rivers" not in json.dumps(sent)
    (row,) = _rows(proxy)
    assert row["app"] == "chatgpt" and "j.rivers" not in json.dumps(row)


def test_uncovered_app_is_never_inspected_even_if_toggled(proxy):
    proxy.policy_path.write_text(json.dumps({"apps": {"gemini": True}}))
    flow = _Flow("gemini.google.com", "/v1/messages", json.dumps({"prompt": SECRET}))
    proxy.request(flow)
    assert not proxy.ledger.exists()


def test_reply_text_reads_openai_streams():
    chat = 'data: {"choices":[{"delta":{"content":"hi "}}]}\ndata: {"choices":[{"delta":{"content":"there"}}]}'
    assert reply_text(chat) == "hi there"
    resp = 'data: {"type":"response.output_text.delta","delta":"ok"}'
    assert reply_text(resp) == "ok"
