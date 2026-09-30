"""File evidence: policy, rules_version, inspect_file, proxy, browser rows,
sync privacy and `byoai shield check-file`."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

import byoai.integrations.shield as sh
from byoai.integrations.shield import (
    RULES_VERSION,
    apply_policy_update,
    clean_browser_row,
    default_policy,
    inspect_file,
    merge_policy,
)

AWS = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"


@pytest.fixture(autouse=True)
def _key(tmp_path, monkeypatch):
    real = sh._fingerprint_key
    monkeypatch.setattr(sh, "_fingerprint_key", lambda p=None: real(tmp_path / "fp.key"))


# ---- policy ---------------------------------------------------------------

def test_files_policy_defaults_and_validation():
    p = default_policy()
    assert set(p["files"]) == set(sh.APP_LABEL) and set(p["files"].values()) == {"allow"}
    assert merge_policy({"files": {"claude": "nope", "zzz": "block"}})["files"]["claude"] == "allow"
    out = apply_policy_update(p, {"files": {"claude": "block"}})
    assert out["files"]["claude"] == "block"
    for bad in ({"files": {"claude": "log"}}, {"files": {"nope": "block"}}, {"files": []}):
        with pytest.raises(ValueError):
            apply_policy_update(p, bad)
    with pytest.raises(ValueError, match="less private"):
        apply_policy_update(out, {"files": {"claude": "warn"}})
    lowered = apply_policy_update(out, {"files": {"claude": "warn"},
                                        "acknowledge": "less_private"})
    assert lowered["files"]["claude"] == "warn"
    apply_policy_update(lowered, {"files": {"claude": "block"}})  # stricter needs no ack


def test_files_policy_is_read_from_disk(tmp_path):
    f = tmp_path / "policy.json"
    f.write_text(json.dumps({"policy_version": 2, "files": {"chatgpt": "block", "claude": 7}}))
    got = sh.read_policy(f)["files"]
    assert got["chatgpt"] == "block" and got["claude"] == "allow"


# ---- rules_version --------------------------------------------------------

def test_rules_version_agrees_between_python_and_the_generated_js():
    path = (Path(__file__).resolve().parents[2] / "src/byoai/browser_extension/shield-rules.js")
    text = path.read_text()
    body = text[text.index("const rules = ") + len("const rules = "):text.index("\n  Object.defineProperty")]
    rules = json.loads(body)
    assert re.fullmatch(r"[0-9a-f]{12}", rules["rules_version"])
    assert rules["rules_version"] == RULES_VERSION
    assert sh.rules_version_of({k: v for k, v in rules.items() if k != "rules_version"}) == RULES_VERSION


# ---- inspect_file ---------------------------------------------------------

def test_inspect_file_hashes_scans_and_never_leaks():
    data = AWS.encode()
    f = inspect_file("john_smith_payroll.csv", "text/csv", data, "claude", default_policy())
    assert f["sha256"] == hashlib.sha256(data).hexdigest()
    assert f["scanned"] is True and f["bytes"] == len(data)
    assert "secret:aws_access_key" in f["flags"] and f["action"] == "block"
    assert f["rules_version"] == RULES_VERSION
    dumped = json.dumps(f)
    assert "john_smith" not in dumped and "AKIA" not in dumped
    assert f["name_hash"].startswith("hmac-sha256:")


def test_inspect_file_actions():
    pol = default_policy()
    pii = b"mail j.rivers@gmail.com"
    # pii tier is redact by default: files are never rewritten, so it warns
    assert inspect_file("a.txt", None, pii, "claude", pol)["action"] == "warn"
    assert inspect_file("a.txt", None, b"hello", "claude", pol)["action"] == "log"
    pol["files"]["claude"] = "block"
    assert inspect_file("a.txt", None, b"hello", "claude", pol)["action"] == "block"
    # not text-like: not read, only the file policy applies
    f = inspect_file("a.pdf", "application/pdf", AWS.encode(), "claude", pol)
    assert f["scanned"] is False and f["flags"] == [] and f["action"] == "block"
    # over 5 MB is not read
    big = inspect_file("a.txt", "text/plain", b"x" * (5 * 1024 * 1024 + 1), "claude", default_policy())
    assert big["scanned"] is False
    # text/* by mime with an odd name; UTF-16 with a BOM is decoded
    assert inspect_file("blob", "text/plain", AWS.encode(), "claude", default_policy())["scanned"]
    u16 = b"\xff\xfe" + AWS.encode("utf-16-le")
    assert "secret:aws_access_key" in inspect_file("a.txt", None, u16, "claude", default_policy())["flags"]
    pol["mode"] = "observe"
    assert inspect_file("a.txt", None, AWS.encode(), "claude", pol)["action"] == "log"


# ---- browser rows ---------------------------------------------------------

def test_clean_browser_row_attachment_fields():
    sha = "a" * 64
    row = clean_browser_row({"kind": "browser.chat.attachment", "app": "claude", "mime": "text/csv",
                             "bytes": 5, "sha256": sha, "name": "john_smith.csv", "scanned": True,
                             "rules_version": RULES_VERSION, "verdict": "warned→removed",
                             "flags": ["secret:aws_access_key"]})
    assert row["sha256"] == sha and row["scanned"] is True and row["verdict"] == "warned→removed"
    assert row["rules_version"] == RULES_VERSION and row["name_hash"].startswith("hmac-sha256:")
    assert "john_smith" not in json.dumps(row) and "name" not in row
    bad = clean_browser_row({"kind": "browser.chat.attachment", "sha256": "A" * 64,
                             "name_hash": "sha256:zz", "scanned": "yes",
                             "rules_version": "abc", "verdict": "sent"})
    for k in ("sha256", "name_hash", "scanned", "rules_version", "verdict"):
        assert k not in bad
    for v in ("allowed", "blocked", "cancelled", "warned→uploaded", "warned->removed"):
        assert clean_browser_row({"kind": "browser.chat.attachment", "verdict": v})["verdict"] == v


# ---- proxy ----------------------------------------------------------------

pytest.importorskip("mitmproxy")
from byoai.integrations.shield_proxy import ShieldProxy  # noqa: E402

from .test_shield_proxy import _Flow  # noqa: E402


def _form(name: str, mime: str, data: bytes) -> bytes:
    return (b'--B\r\nContent-Disposition: form-data; name="purpose"\r\n\r\nassistants\r\n'
            b'--B\r\nContent-Disposition: form-data; name="file"; filename="' + name.encode() +
            b'"\r\nContent-Type: ' + mime.encode() + b"\r\n\r\n" + data + b"\r\n--B--\r\n")


def _upload(px, body, ctype="multipart/form-data; boundary=B", path="/backend-api/files"):
    flow = _Flow("claude.ai", path, "")
    flow.request.method = "POST"
    flow.request.headers = {"content-type": ctype}
    flow.request.get_content = lambda strict=False: body
    px.request(flow)
    return flow


@pytest.fixture
def px(tmp_path):
    return ShieldProxy(ledger=tmp_path / "l.jsonl", policy_path=tmp_path / "p.json")


def _rows(px):
    return [json.loads(x) for x in px.ledger.read_text().splitlines()]


def test_proxy_hashes_exact_bytes_of_each_file_part(px):
    data = b"a\r\nb\n\x00\xff\r\n\r\nend"
    flow = _upload(px, _form("Q3 payroll.bin", "application/octet-stream", data))
    (row,) = _rows(px)
    assert flow.response is None and row["verdict"] == "allowed"
    assert row["kind"] == "desktop.chat.attachment"
    assert row["sha256"] == hashlib.sha256(data).hexdigest() and row["bytes"] == len(data)
    assert row["scanned"] is False and row["rules_version"] == RULES_VERSION
    assert "payroll" not in json.dumps(row) and "action" not in row


def test_proxy_blocks_a_text_file_with_a_secret_and_logs_no_content(px):
    flow = _upload(px, _form("creds.txt", "text/plain", AWS.encode()))
    (row,) = _rows(px)
    assert flow.response.status_code == 403
    assert flow.response.headers["x-coriqo-shield"] == "blocked"
    assert row["verdict"] == "blocked" and "secret:aws_access_key" in row["flags"]
    assert "AKIA" not in json.dumps(row) and "creds" not in json.dumps(row)


def test_proxy_file_policy_warn_blocks_and_block_blocks(px):
    px.policy_path.write_text(json.dumps({"policy_version": 2, "files": {"claude": "warn"}}))
    assert _upload(px, _form("a.pdf", "application/pdf", b"%PDF")).response.status_code == 403
    px.policy_path.write_text(json.dumps({"policy_version": 2, "files": {"claude": "block"}}))
    px._policy = None
    assert _upload(px, _form("a.pdf", "application/pdf", b"%PDF")).response.status_code == 403
    assert [r["verdict"] for r in _rows(px)] == ["blocked", "blocked"]


def test_proxy_raw_body_to_upload_path_and_two_files(px):
    flow = _upload(px, AWS.encode(), ctype="text/plain", path="/api/o/upload")
    assert flow.response.status_code == 403
    (row,) = _rows(px)
    assert row["sha256"] == hashlib.sha256(AWS.encode()).hexdigest() and row["name_hash"] is None
    px.ledger.unlink()
    two = (_form("a.txt", "text/plain", b"hello")[:-len(b"--B--\r\n")] +
           b'--B\r\nContent-Disposition: form-data; name="f2"; filename="b.pdf"\r\n\r\n%PDF\r\n--B--\r\n')
    flow = _upload(px, two)
    rows = _rows(px)
    assert flow.response is None and len(rows) == 2
    assert {r["sha256"] for r in rows} == {hashlib.sha256(b"hello").hexdigest(),
                                           hashlib.sha256(b"%PDF").hexdigest()}


def test_proxy_unreadable_form_is_hashed_whole(px):
    body = b"not a form at all"
    _upload(px, body, ctype="multipart/form-data")  # no boundary
    (row,) = _rows(px)
    assert row["sha256"] == hashlib.sha256(body).hexdigest() and row["scanned"] is False


# ---- feed, seal, sync, check-file ------------------------------------------

def _cfg(tmp_path):
    return sh.ShieldConfig(ledger=tmp_path / "captures.jsonl", seal_path=tmp_path / "sealchain.json",
                           key_dir=tmp_path / "keys", policy_path=tmp_path / "policy.json")


def _shield_with_upload(tmp_path, data=b"quarterly numbers\n", name="q3.csv"):
    px = ShieldProxy(ledger=tmp_path / "captures.jsonl", policy_path=tmp_path / "policy.json")
    _upload(px, _form(name, "text/csv", data))
    feed = sh.Feed(_cfg(tmp_path))
    feed.scan_initial()
    return feed


def test_feed_item_seals_the_file_facts_and_sync_never_carries_them(tmp_path):
    from byoai.integrations.shield_sync import EVENT_FIELDS, build_event
    data = b"quarterly numbers\n"
    feed = _shield_with_upload(tmp_path, data)
    (item,) = feed.items
    sha = hashlib.sha256(data).hexdigest()
    assert item["file"]["sha256"] == sha and item["seal"]
    entry = feed.seals._find_entry(item["seal"])
    assert entry["payload"]["file"]["sha256"] == sha
    assert "q3.csv" not in json.dumps(entry) and "q3.csv" not in json.dumps(item)
    ev = build_event({k: v for k, v in item.items() if not k.startswith("_")},
                     device_id="dev-1")
    assert ev is not None and set(ev) == set(EVENT_FIELDS)
    blob = json.dumps(ev)
    assert sha not in blob and "hmac-sha256" not in blob and sha[:16] not in blob


def test_check_file_end_to_end(tmp_path, capsys):
    from byoai.integrations.shield_check import main
    data = b"quarterly numbers\n"
    _shield_with_upload(tmp_path, data)
    good = tmp_path / "q3.csv"
    good.write_bytes(data)
    args = ["--ledger", str(tmp_path / "captures.jsonl"), "--key-dir", str(tmp_path / "keys")]
    assert main([str(good), *args]) == 0
    out = capsys.readouterr().out
    assert "hash: match" in out and "rules reproduce: yes" in out and "seal verifies: yes" in out
    # a different file: no record carries its hash
    other = tmp_path / "other.csv"
    other.write_bytes(b"different")
    assert main([str(other), *args]) == 1
    assert "hash: different" in capsys.readouterr().out
    # against a named row, a different file is reported different
    feed = sh.Feed(_cfg(tmp_path))
    feed.scan_initial()
    row_id = feed.items[0]["id"]
    assert main([str(other), "--row", row_id, *args]) == 1
    out = capsys.readouterr().out
    assert "hash: different" in out and "seal verifies: yes" in out
    assert main([str(good), "--row", row_id, *args]) == 0
    assert main([str(good), "--row", "nosuchrow", *args]) == 2


def test_check_file_refuses_rules_from_another_version(tmp_path, capsys, monkeypatch):
    import byoai.integrations.shield_check as chk
    data = b"quarterly numbers\n"
    _shield_with_upload(tmp_path, data)
    good = tmp_path / "q3.csv"
    good.write_bytes(data)
    args = ["--ledger", str(tmp_path / "captures.jsonl"), "--key-dir", str(tmp_path / "keys")]
    monkeypatch.setattr(chk, "RULES_VERSION", "0" * 12)
    assert chk.main([str(good), *args]) == 1
    out = capsys.readouterr().out
    assert "cannot check" in out and "hash: match" in out


def test_check_file_reports_an_edited_seal_log(tmp_path, capsys):
    from byoai.integrations.shield_check import main
    data = b"quarterly numbers\n"
    _shield_with_upload(tmp_path, data)
    good = tmp_path / "q3.csv"
    good.write_bytes(data)
    args = ["--ledger", str(tmp_path / "captures.jsonl"), "--key-dir", str(tmp_path / "keys")]
    log = tmp_path / "sealchain.log.jsonl"
    log.write_text(log.read_text().replace(hashlib.sha256(data).hexdigest(), "0" * 64))
    assert main([str(good), *args]) == 1
    out = capsys.readouterr().out
    assert "seal verifies: no" in out and "hash: match" not in out


# ---- review round 2: repros turned into tests ------------------------------

import re as _re  # noqa: E402
import time as _time  # noqa: E402

_VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "uploads" / "key_redaction_vectors.json"


def _expand(s: str) -> str:
    return _re.sub(r"\{A(\d+)\}", lambda m: "A" * int(m.group(1)), s)


def test_private_key_redaction_vectors():
    for v in json.loads(_VECTORS.read_text())["vectors"]:
        assert sh.redact_with_rules(_expand(v["in"]))[0] == _expand(v["out"]), v["in"][:60]


def test_private_key_rule_is_header_only_and_linear():
    unit = "-----BEGIN PRIVATE KEY-----"
    text = unit * ((5 * 1024 * 1024 - 64) // len(unit))
    t = _time.time()
    rule = dict(sh.SECRET_RULES)["private_key_block"]
    assert sh._first_match("private_key_block", rule, text) is not None
    assert _time.time() - t < 0.5
    t = _time.time()
    out = sh._redact_private_keys(text, lambda m: "[K]", rule)
    assert _time.time() - t < 0.5 and out == "[K]" * text.count(unit)
    spaced = ("-----BEGIN " + "A " * 3000 + "X") * 800
    t = _time.time()
    sh._redact_private_keys(spaced, lambda m: "[K]", rule)
    assert _time.time() - t < 0.5
    ends = ("-----BEGIN PRIVATE KEY-----" + "-----END X-----") * 100_000  # END candidates that are not END lines
    t = _time.time()
    sh._redact_private_keys(ends, lambda m: "[K]", rule)
    assert _time.time() - t < 0.5


AWS_LINE = b"aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"


@pytest.mark.parametrize("mime", ["application/json", "", "application/octet-stream",
                                  "application/x-x509-ca-cert", "application/x-yaml", "text/plain"])
def test_raw_upload_of_text_is_read_whatever_it_is_called(mime, tmp_path):
    facts = sh.inspect_file(None, mime, AWS_LINE, "chatgpt", {}, key_path=tmp_path / "k", raw_upload=True)
    assert facts["scanned"] is True and facts["flags"] == ["secret:aws_access_key"]
    assert facts["action"] == "block"


def test_raw_upload_that_is_binary_is_not_read_and_named_files_follow_their_type(tmp_path):
    kw = {"key_path": tmp_path / "k"}
    assert sh.inspect_file(None, "", b"\xff\xfe\x00\x01" * 3 + b"\x80\x81", "chatgpt", {}, raw_upload=True, **kw)["scanned"] is True  # UTF-16 mark
    assert sh.inspect_file(None, "", b"\x89PNG\r\n\x1a\n\xff\xd8\xff", "chatgpt", {}, raw_upload=True, **kw)["scanned"] is False
    assert sh.inspect_file(None, "", AWS_LINE, "chatgpt", {}, **kw)["scanned"] is False  # not a raw upload
    assert sh.inspect_file("x.png", "image/png", AWS_LINE, "claude", {}, raw_upload=True, **kw)["scanned"] is False  # has a name
    for mime in ("application/json", "application/ld+json", "application/x-yaml", "text/yaml",
                 "application/x-x509-ca-cert", "application/x-pem-file", "application/x-sh"):
        assert sh.is_text_like(None, mime), mime
    assert not sh.is_text_like(None, "image/png")


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
def test_utf16_without_a_mark_is_decoded(codec, tmp_path):
    data = "key=AKIAIOSFODNN7EXAMPLE\n".encode(codec)
    facts = sh.inspect_file("k.txt", "text/plain", data, "claude", {}, key_path=tmp_path / "k")
    assert facts["flags"] == ["secret:aws_access_key"]


def test_a_page_cannot_say_text_was_not_read(tmp_path, monkeypatch):
    real = sh._fingerprint_key
    monkeypatch.setattr(sh, "_fingerprint_key", lambda p=None: real(tmp_path / "fp.key"))
    row = {"kind": "browser.chat.attachment", "app": "claude", "mime": "text/plain", "bytes": 41,
           "scanned": False, "verdict": "allowed"}
    assert sh.clean_browser_row(row)["scanned"] is True
    assert sh.clean_browser_row({**row, "mime": "image/png"})["scanned"] is False
    assert sh.clean_browser_row({**row, "bytes": 6 * 1024 * 1024})["scanned"] is False


def test_check_file_refuses_an_unscanned_record_of_a_readable_file(tmp_path, monkeypatch):
    import io

    from byoai.integrations.shield_check import check_file
    real = sh._fingerprint_key
    monkeypatch.setattr(sh, "_fingerprint_key", lambda p=None: real(tmp_path / "fp.key"))
    row = {"kind": "browser.chat.attachment", "app": "claude", "name": "creds.txt", "mime": "text/plain",
           "bytes": len(AWS_LINE), "sha256": hashlib.sha256(AWS_LINE).hexdigest(), "scanned": False,
           "flags": [], "rules_version": sh.RULES_VERSION, "verdict": "allowed",
           "sent_at": "2026-09-29T10:00:00Z"}
    led = tmp_path / "captures.jsonl"
    led.write_text(json.dumps({**sh.clean_browser_row(row), "source": "browser",
                               "wall_clock": "2026-09-29 10:00:00"}) + "\n")
    cfg = sh.ShieldConfig(ledger=led, seal_path=tmp_path / "sealchain.json", key_dir=tmp_path / "keys",
                          policy_path=tmp_path / "policy.json")
    feed = sh.Feed(cfg)
    feed.scan_initial()
    f = tmp_path / "creds.txt"
    f.write_bytes(AWS_LINE)
    out = io.StringIO()
    assert check_file(f, row=None, ledger=led, key_dir=tmp_path / "keys", out=out) == 1
    assert "hash: match" in out.getvalue() and "rules reproduce: no" in out.getvalue()


def test_check_file_reads_a_nameless_upload_like_the_upload_did(tmp_path, monkeypatch):
    real = sh._fingerprint_key
    monkeypatch.setattr(sh, "_fingerprint_key", lambda p=None: real(tmp_path / "fp.key"))
    facts = sh.inspect_file(None, "application/octet-stream", AWS_LINE, "chatgpt", {}, raw_upload=True)
    assert facts["scanned"] and facts["flags"] == ["secret:aws_access_key"]


def _put(px, body, ctype, path="/files/f/raw", host="files.oaiusercontent.com", method="PUT"):
    flow = _Flow(host, path, "")
    flow.request.method = method
    flow.request.headers = {"content-type": ctype}
    flow.request.get_content = lambda strict=False: body
    px.request(flow)
    return flow


@pytest.mark.parametrize("ctype", ["application/json", "application/octet-stream", "text/plain", ""])
def test_proxy_reads_a_raw_presigned_put_whatever_its_type(px, ctype):
    px.policy_path.write_text(json.dumps({"policy_version": 2, "apps": {"chatgpt": True}}))
    px._policy = None
    flow = _put(px, AWS_LINE, ctype)
    assert flow.response is not None
    (row,) = _rows(px)
    assert row["scanned"] is True and row["flags"] == ["secret:aws_access_key"]


def test_proxy_reads_lf_only_multipart_and_keeps_exact_bytes(px):
    body = (b'--B\nContent-Disposition: form-data; name="file"; filename="k.txt"\n'
            b"Content-Type: text/plain\n\n" + AWS_LINE + b"\n--B--\n")
    flow = _upload(px, body)
    assert flow.response.status_code == 403
    (row,) = _rows(px)
    assert row["sha256"] == hashlib.sha256(AWS_LINE).hexdigest() and row["scanned"] is True


def test_proxy_lists_32_file_parts_and_hashes_the_rest_as_one_blob(px):
    def part(i):
        return (f'--B\r\nContent-Disposition: form-data; name="f{i}"; filename="a{i}.bin"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode() + bytes([i]) + b"\r\n"
    body = b"".join(part(i) for i in range(50)) + b"--B--\r\n"
    _upload(px, body)
    rows = _rows(px)
    assert len(rows) == 33
    tail = rows[-1]
    assert tail["flags"] == ["flag:too_many_files"] and tail["scanned"] is False
    assert tail["sha256"] == hashlib.sha256(bytes(range(32, 50))).hexdigest()
    many = b"".join(part(i % 250) for i in range(20000)) + b"--B--\r\n"
    t = _time.time()
    px.ledger.unlink()
    _upload(px, many)
    assert len(_rows(px)) == 33 and _time.time() - t < 5


# ---- review round 3 ---------------------------------------------------------

def _enable_all(px):
    px.policy_path.write_text(json.dumps({"policy_version": 2, "apps": {"chatgpt": True, "claude": True}}))
    px._policy = None


def test_multipart_with_no_file_part_is_not_an_upload(px):
    body = (b'--B\r\nContent-Disposition: form-data; name="a"\r\n\r\nhello\r\n--B--\r\n')
    px.policy_path.write_text(json.dumps({"policy_version": 2, "files": {"claude": "block"}}))
    px._policy = None
    flow = _upload(px, body)
    assert flow.response is None
    assert not px.ledger.exists()


def test_upload_path_with_no_content_type_is_an_upload_and_json_metadata_is_not(px):
    flow = _upload(px, AWS.encode(), ctype="", path="/api/o/upload")
    assert flow.response.status_code == 403
    px.ledger.unlink()
    flow = _upload(px, b'{"file_name": "a.txt"}', ctype="", path="/api/o/upload")
    assert flow.response is None and not px.ledger.exists()
    flow = _upload(px, b"[1, 2]", ctype="application/json", path="/api/o/upload")
    assert flow.response is None
    flow = _upload(px, b"{not json " + AWS.encode(), ctype="application/json", path="/api/o/upload")
    assert flow.response is not None  # JSON by label only: hashed and read as a file


@pytest.mark.parametrize("method,path", [("PUT", "/anything/at/all"), ("POST", "/other/x"), ("PUT", "/files/f/raw/extra")])
def test_any_body_to_the_chatgpt_storage_host_is_an_upload(px, method, path):
    _enable_all(px)
    flow = _put(px, AWS_LINE, "application/octet-stream", path=path, method=method)
    assert flow.response is not None
    (row,) = _rows(px)
    assert row["scanned"] is True


def test_parts_beyond_32_are_still_read_only_rows_are_capped(px):
    def part(i, data):
        return (f'--B\r\nContent-Disposition: form-data; name="f{i}"; filename="a{i}.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n").encode() + data + b"\r\n"
    body = b"".join(part(i, b"hello") for i in range(40)) + part(40, AWS_LINE) + b"--B--\r\n"
    flow = _upload(px, body)
    assert flow.response.status_code == 403  # the 41st part holds a key
    rows = _rows(px)
    assert len(rows) == 33 and all(r["verdict"] == "blocked" for r in rows)
    tail = rows[-1]
    assert tail["flags"] == ["secret:aws_access_key", "flag:too_many_files"]
    assert tail["bytes"] == 8 * 5 + len(AWS_LINE)
    assert tail["sha256"] == hashlib.sha256(b"hello" * 8 + AWS_LINE).hexdigest()
    px.ledger.unlink()
    px.policy_path.write_text(json.dumps({"policy_version": 2, "files": {"claude": "block"}}))
    px._policy = None
    assert _upload(px, b"".join(part(i, b"x") for i in range(40)) + b"--B--\r\n").response is not None


def test_check_file_decides_by_content_not_by_the_local_name(tmp_path, monkeypatch):
    import io

    from byoai.integrations.shield_check import check_file
    real = sh._fingerprint_key
    monkeypatch.setattr(sh, "_fingerprint_key", lambda p=None: real(tmp_path / "fp.key"))
    data = b"quarterly numbers\n"
    facts = sh.inspect_file("q3.csv", "application/octet-stream", data, "claude", {})
    row = {"kind": "browser.chat.attachment", "app": "claude", "name": "q3.csv", "mime": "application/octet-stream",
           "bytes": len(data), "sha256": facts["sha256"], "scanned": True, "flags": [],
           "rules_version": sh.RULES_VERSION, "verdict": "allowed", "sent_at": "2026-09-29T10:00:00Z"}
    led = tmp_path / "captures.jsonl"
    led.write_text(json.dumps({**sh.clean_browser_row(row), "source": "browser",
                               "wall_clock": "2026-09-29 10:00:00"}) + "\n")
    sh.Feed(sh.ShieldConfig(ledger=led, seal_path=tmp_path / "sealchain.json", key_dir=tmp_path / "keys",
                            policy_path=tmp_path / "policy.json")).scan_initial()
    for name in ("renamed.bin", "q3.csv"):
        f = tmp_path / name
        f.write_bytes(data)
        out = io.StringIO()
        assert check_file(f, row=None, ledger=led, key_dir=tmp_path / "keys", out=out) == 0, out.getvalue()
        assert "rules reproduce: yes" in out.getvalue()
    binary = tmp_path / "x.bin"
    binary.write_bytes(b"\x89PNG\xff\xd8\xff\xfe")
    out = io.StringIO()
    check_file(binary, row=None, ledger=led, key_dir=tmp_path / "keys", out=out)
    assert "hash: different" in out.getvalue()


def test_unreadable_and_many_files_flags_survive_cleaning(tmp_path, monkeypatch):
    real = sh._fingerprint_key
    monkeypatch.setattr(sh, "_fingerprint_key", lambda p=None: real(tmp_path / "fp.key"))
    row = {"kind": "browser.chat.attachment", "app": "claude", "mime": "text/plain", "bytes": 3,
           "scanned": False, "flags": ["flag:unreadable", "flag:bogus"], "verdict": "allowed"}
    assert sh.clean_browser_row(row)["flags"] == ["flag:unreadable"]
