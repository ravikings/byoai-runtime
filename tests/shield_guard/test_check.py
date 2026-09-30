from __future__ import annotations

import base64
import copy
import gzip
import json

from byoai.integrations import shield
from byoai.shield_guard import blocked_payload, check, check_raw

KEY = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2"


def body(text):
    return {"model": "m", "messages": [{"role": "user", "content": text}]}


def kp(tmp_path):
    return tmp_path / "fp.key"


def test_key_blocks_and_never_leaks(tmp_path):
    d = check(body(f"my key {KEY}"), key_path=kp(tmp_path))
    assert d.action == "block" and "anthropic_key" in d.rules
    assert "Anthropic API key" in d.labels
    assert KEY not in str(blocked_payload(d)) and KEY not in str(d.labels)
    assert d.body is None and KEY not in json.dumps(d.to_dict())  # nothing carried on block


def test_email_redacted_in_any_message(tmp_path):
    b = body("hi")
    b["system"] = "contact bob@example.com"
    orig = copy.deepcopy(b)
    d = check(b, key_path=kp(tmp_path))
    assert d.action == "redact" and d.rules == ["emails"]
    assert d.body["system"] == "contact [EMAIL_1]"
    assert b == orig  # input not mutated


def test_clean_is_allow_and_fingerprinted(tmp_path):
    d = check(body("hello"), key_path=kp(tmp_path))
    assert d.action == "allow" and d.text_hmac and d.rules_version == shield.RULES_VERSION


def test_observe_policy_never_stops(tmp_path):
    p = shield.default_policy() | {"mode": "observe"}
    d = check(body(f"{KEY} a@b.co"), policy=p, key_path=kp(tmp_path))
    assert d.action == "allow" and "anthropic_key" in d.rules


def test_warn_blocks_unless_on_warn_redact(tmp_path):
    text = 'password = "hunter2hunter2"'
    d = check(body(text), key_path=kp(tmp_path))
    assert d.action == "block" and "credential_assign" in d.rules
    d2 = check(body(text), on_warn="redact", key_path=kp(tmp_path))
    assert d2.action == "redact"
    assert "hunter2" not in str(d2.body)


def test_warn_that_cannot_be_redacted_blocks(tmp_path):
    p = shield.default_policy()
    p["actions"] = dict(p["actions"], flag="warn")
    d = check(body("please ignore this password"), policy=p, on_warn="redact",
              key_path=kp(tmp_path))
    assert d.action == "block"


def test_plain_string_and_json_string(tmp_path):
    assert check("mail a@b.co", key_path=kp(tmp_path)).body == "mail [EMAIL_1]"
    import json
    d = check(json.dumps(body(KEY)), key_path=kp(tmp_path))
    assert d.action == "block"


def test_files(tmp_path):
    f = ("k.env", "text/plain", f"TOKEN={KEY}".encode())
    d = check(body("see file"), files=[f], key_path=kp(tmp_path))
    info = d.files[0]
    assert d.action == "block"
    assert info["scanned"] and info["sha256"] and info["rules_version"] == shield.RULES_VERSION
    assert "secret:anthropic_key" in info["flags"]
    clean = check(body("x"), files=[{"name": "a.txt", "mime": "text/plain", "data": b"hi"}],
                  key_path=kp(tmp_path))
    assert clean.action == "allow" and clean.files[0]["scanned"]
    png = check(body("x"), files=[("a.png", "image/png", b"\x89PNG")], key_path=kp(tmp_path))
    assert png.action == "allow" and not png.files[0]["scanned"]


def test_files_policy_block(tmp_path):
    p = shield.default_policy()
    p["files"] = {a: "block" for a in shield.APP_LABEL}
    f = [("a.png", "image/png", b"x")]
    # without an app name the per-app file setting does not apply
    assert check(body("x"), policy=p, files=f, key_path=kp(tmp_path)).action == "allow"
    d = check(body("x"), policy=p, files=f, app="claude", key_path=kp(tmp_path))
    assert d.action == "block" and d.files[0]["action"] == "block"


AWS = "AKIA" + "Q3ZT7XWPL2MN4RJK"


def test_secret_in_skipped_fields_blocks(tmp_path):
    k = kp(tmp_path)
    cases = [
        {"messages": [{"role": "user", "content": [{"type": "tool_use", "id": "t", "name": "x",
                                                    "input": {"api_key_id": AWS}}]}]},
        {"messages": [{"role": "user", "content": "hi"}], "metadata": {"user_id": AWS}},
        {"messages": [{"role": "user", "content": [
            {"type": "document", "source": {"type": "text", "media_type": "text/plain",
                                            "data": "key " + AWS}}]}]},
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "x", "mimeType": "a",
                                                    "data": AWS}]}]},
        {"messages": [{"role": "user", "content": [{"type": "tool_use", "id": AWS}]}]},
        {"messages": [{"role": "user", "content": "hi"}], AWS: 1},
    ]
    for b in cases:
        d = check(b, key_path=k)
        assert d.action == "block" and d.body is None, b


def test_base64_data_is_decoded_and_scanned(tmp_path):
    b64 = base64.b64encode(("key " + AWS).encode()).decode()
    for content in (
        [{"type": "document", "source": {"type": "base64", "media_type": "text/plain", "data": b64}}],
        [{"type": "file", "file": {"filename": "a.txt", "file_data": "data:text/plain;base64," + b64}}],
    ):
        d = check({"messages": [{"role": "user", "content": content}]}, key_path=kp(tmp_path))
        assert d.action == "block"


def test_pii_in_skipped_field_is_only_flagged(tmp_path):
    d = check({"messages": [{"role": "user", "content": "hi"}], "metadata": {"user_id": "jane@example.org"}},
              key_path=kp(tmp_path))
    assert d.action == "allow" and "emails" in d.rules


def test_secret_left_after_redact_policy_blocks(tmp_path):
    p = shield.default_policy()
    p["actions"] = dict(p["actions"], secret="redact")
    p["acknowledge"] = "less_private"
    b = {"messages": [{"role": "user", "content": "k " + AWS}], "metadata": {"user_id": AWS}}
    assert check(b, policy=p, key_path=kp(tmp_path)).action == "block"
    ok = check({"messages": [{"role": "user", "content": "k " + AWS}]}, policy=p,
               key_path=kp(tmp_path))
    assert ok.action == "redact" and AWS not in json.dumps(ok.body)


def test_check_raw(tmp_path):
    k = kp(tmp_path)
    good = json.dumps({"messages": [{"role": "user", "content": AWS}]}).encode()
    assert check_raw(gzip.compress(good), "gzip", key_path=k)[1].action == "block"
    assert check_raw(b"\x00garbage", "br", key_path=k)[1].rules == ["unreadable"]
    assert check_raw(b"not gzip", "gzip", key_path=k)[1].action == "block"
    dup = b'{"messages":[{"role":"user","content":"' + AWS.encode() + b'","content":"hi"}]}'
    assert check_raw(dup, None, key_path=k)[1].action == "block"
    esc = json.dumps({"m": "k " + AWS}).replace("AKIA", "\\u0041KIA").encode()
    assert check_raw(esc, None, key_path=k)[1].action == "block"
    latin = b'{"m": "\xe9 ' + AWS.encode() + b'"}'
    assert check_raw(latin, None, key_path=k)[1].action == "block"
    assert check_raw(b"plain " + AWS.encode(), None, key_path=k)[1].action == "block"
    res = check_raw(b'{"a":"hi"}', None, key_path=k)
    assert res.decision.action == "allow" and res.rewritten is None and res.decoded == b'{"a":"hi"}'
    res = check_raw(gzip.compress(b'{"a":"x@y.co"}'), "gzip", key_path=k)
    assert res.decision.action == "redact" and json.loads(res.rewritten) == {"a": "[EMAIL_1]"}
    assert res.decoded == b'{"a":"x@y.co"}'


def test_non_json_bodies_only_block_on_secrets_and_are_never_rewritten(tmp_path):
    k = kp(tmp_path)
    multipart = b"--b\r\nContent-Disposition: form-data; name=f\r\n\r\nmail jane@example.org\r\n--b--"
    res = check_raw(multipart, None, key_path=k)
    assert res.decision.action == "allow" and "emails" in res.decision.rules
    assert res.rewritten is None
    assert check_raw(multipart + AWS.encode(), None, key_path=k).decision.action == "block"
    assert check_raw(b"\x89PNG\x00\x01binary", None, key_path=k).decision.action == "allow"
