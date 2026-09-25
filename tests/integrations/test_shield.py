"""Promoted shield engine — rules, redaction, and the Merkle seal chain."""
from __future__ import annotations

import json

from byoai.integrations.shield import (
    ShieldConfig, Feed, flags_for, redact, default_policy, load_policy,
    SealChain, MAX_INTERACTIONS,
)


def make_cfg(tmp_path):
    ledger = tmp_path / "captures.jsonl"
    ledger.write_text("")
    return ShieldConfig(
        ledger=ledger,
        seal_path=tmp_path / "sealchain.json",
        key_dir=tmp_path / "keys",
        policy_path=tmp_path / "policy.json",
        sig_every=4, max_interactions=100,
    )


def test_pii_rules_and_redaction():
    text = "refund my order, email j.rivers@gmail.com card 4242424242424242"
    flags = flags_for(text)
    rules = {r for _, r, _ in flags}
    assert {"emails", "cards", "tool_intent"} <= rules
    out = redact(text)
    assert "j.rivers@" not in out and "4242424242424242" not in out
    assert "[redacted-email]" in out and "[redacted-card]" in out


def test_executable_masquerade_is_high_tier():
    assert any(tier == "high" for tier, _, _ in flags_for("please open invoice_2026.pdf.exe"))


def test_reply_pii_echo_flagged():
    flags = flags_for("your receipt, sent to j.rivers@gmail.com")
    assert any(r == "reply_pii_echo" for _, r, _ in flags)


def test_seal_chain_roundtrip_and_tamper(tmp_path):
    cfg = make_cfg(tmp_path)
    chain = SealChain(cfg)
    s1 = chain.stamp({"interaction": "a", "ts": "14:00", "text_redacted": "hi"})
    chain.stamp({"interaction": "b", "ts": "14:05", "text_redacted": "yo"})
    v = chain.verify_chain()
    assert v["tamper_evident"] is True and v["entries"] == 2

    rc = chain.receipt(s1)
    proof = rc["merkle"]["proof"]
    assert proof["root_hex"] == rc["merkle"]["checkpoint"]["root_hex"]
    assert rc["merkle"]["checkpoint"]["sig"].startswith("ed25519:")

    state = json.loads((tmp_path / "sealchain.json").read_text())
    state["entries"][0]["payload"]["ts"] = "EDITED"
    (tmp_path / "sealchain.json").write_text(json.dumps(state))
    reborn = SealChain(make_cfg(tmp_path))
    assert reborn.verify_chain()["tamper_evident"] is False


def test_feed_pagination_counts(tmp_path):
    cfg = make_cfg(tmp_path)
    rows = [{"wall_clock": "2026-09-24 10:0%d:00" % i,
             "kind": "desktop.chat.request", "target": "claude.ai",
             "prompt": f"checkpoint smoke {i} fill"} for i in range(6)]
    cfg.ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    feed = Feed(cfg)
    feed.scan_initial()
    assert feed.counts()["checked"] == 6
    assert feed.seals.height == 6


def test_policy_roundtrip(tmp_path):
    cfg = make_cfg(tmp_path)
    p = default_policy()
    p["mode"] = "redact"
    from byoai.integrations.shield import save_policy
    save_policy(cfg, p)
    assert load_policy(cfg)["mode"] == "redact"


# ------------------------------------------------------------- privacy-first

import os
import time as _time

import pytest

from byoai.integrations.shield import (
    apply_policy_update, fingerprint, inspect_text, privacy_report,
    redact_json_body, scrub_ledger,
)

SECRET = "email j.rivers@gmail.com about card 4242424242424242"


@pytest.fixture
def key_path(tmp_path, monkeypatch):
    path = tmp_path / "fp.key"
    monkeypatch.setattr("byoai.integrations.shield.FINGERPRINT_KEY", path)
    # default args were bound at import; route every call through tmp_path
    import byoai.integrations.shield as sh
    real = sh._fingerprint_key
    monkeypatch.setattr(sh, "_fingerprint_key", lambda p=path: real(path))
    return path


def test_defaults_are_privacy_first():
    """Pinned: a Mac that never opens Settings redacts, keeps no text, keeps
    30 days and shows the notice."""
    p = default_policy()
    assert p["mode"] == "redact"
    assert p["keep_text"] is False
    assert p["retention_days"] == 30
    assert p["notice"] is True


def test_pre_privacy_first_file_is_read_as_privacy_first(tmp_path):
    """Pinned: a policy.json from before privacy-first (no version) holding the
    old UI's saved default is read as redact with no stored text; the app
    toggles in it are kept."""
    cfg = make_cfg(tmp_path)
    cfg.policy_path.write_text(json.dumps(
        {"mode": "observe", "keep_text": True, "apps": {"chatgpt": True}}))
    p = load_policy(cfg)
    assert p["mode"] == "redact" and p["keep_text"] is False
    assert p["apps"]["chatgpt"] is True


def test_block_is_kept_when_upgrading(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.policy_path.write_text(json.dumps({"mode": "block"}))
    assert load_policy(cfg)["mode"] == "block"


def test_a_newer_policy_file_is_not_downgraded(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.policy_path.write_text(json.dumps({"policy_version": 3, "mode": "observe"}))
    assert load_policy(cfg)["mode"] == "observe"


def test_a_confirmed_choice_after_the_upgrade_is_kept(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.policy_path.write_text(json.dumps({"policy_version": 2, "mode": "observe"}))
    assert load_policy(cfg)["mode"] == "observe"


def test_weakening_needs_acknowledgement():
    p = default_policy()
    for change in ({"mode": "observe"}, {"keep_text": True}):
        with pytest.raises(ValueError, match="less private"):
            apply_policy_update(p, change)
        out = apply_policy_update(p, {**change, "acknowledge": "less_private"})
        assert "acknowledge" not in out
    # tightening, or re-saving an already-weaker setting, needs nothing
    observe = apply_policy_update(p, {"mode": "observe", "acknowledge": "less_private"})
    assert apply_policy_update(observe, {"mode": "redact"})["mode"] == "redact"
    assert apply_policy_update(observe, {"retention_days": 7})["mode"] == "observe"


def test_inspect_text_keeps_no_text_by_default(key_path):
    facts = inspect_text(SECRET, default_policy(), key_path=key_path)
    blob = json.dumps(facts)
    assert "j.rivers" not in blob and "4242" not in blob
    assert "preview" not in facts
    assert facts["chars"] == len(SECRET)
    assert {"pii:emails", "pii:cards"} <= set(facts["flags"])
    assert facts["text_hmac"].startswith("hmac-sha256:")


def test_inspect_text_preview_is_redacted_when_opted_in(key_path):
    facts = inspect_text(SECRET, {**default_policy(), "keep_text": True},
                         key_path=key_path)
    assert "[redacted-email]" in facts["preview"]
    assert "j.rivers" not in facts["preview"]


def test_fingerprint_is_keyed_stable_and_private(tmp_path):
    k1, k2 = tmp_path / "a.key", tmp_path / "b.key"
    assert fingerprint("123-45-6789", k1) == fingerprint("123-45-6789", k1)
    assert fingerprint("123-45-6789", k1) != fingerprint("123-45-6789", k2)
    assert os.stat(k1).st_mode & 0o777 == 0o600


def test_redact_json_body_only_touches_message_fields():
    body = {"conversation_uuid": "555-123-4567-ab", "timezone": "America/Chicago",
            "prompt": "call me on 555-123-4567 or mail a@b.co"}
    out, rules = redact_json_body(json.dumps(body))
    parsed = json.loads(out)
    assert parsed["conversation_uuid"] == "555-123-4567-ab"
    assert parsed["prompt"] == "call me on [redacted-number] or mail [redacted-email]"
    assert rules == ["emails", "phone_numbers"]

    api = {"model": "claude-x", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "ssn 123-45-6789"}]}]}
    out, rules = redact_json_body(json.dumps(api))
    assert "123-45-6789" not in out and rules == ["ssn_like"]


def test_redact_json_body_leaves_clean_and_non_json_bodies_alone():
    raw = json.dumps({"prompt": "reset my password please"})
    assert redact_json_body(raw) == (raw, [])     # the word is a flag, not PII
    assert redact_json_body("not json") == ("not json", [])


def test_policy_update_validates_every_field():
    p = default_policy()
    assert apply_policy_update(p, {"retention_days": 90})["retention_days"] == 90
    for bad in ({"mode": "spy"}, {"retention_days": 45}, {"keep_text": "yes"},
                {"notice": 1}, {"apps": {"claude": "on"}}):
        with pytest.raises(ValueError):
            apply_policy_update(p, bad)


def test_scrub_removes_text_and_applies_retention(tmp_path, key_path):
    cfg = make_cfg(tmp_path)
    old = _time.strftime("%Y-%m-%d %H:%M:%S",
                         _time.localtime(_time.time() - 40 * 86400))
    now = _time.strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {"wall_clock": old, "kind": "desktop.chat.request", "prompt": "old one"},
        {"wall_clock": now, "kind": "desktop.chat.request", "prompt": SECRET},
        {"wall_clock": now, "kind": "tool.call", "tool": "execute",
         "input_preview": "refund j.rivers@gmail.com"},
        {"wall_clock": now, "kind": "desktop.meta", "target": "claude.ai"},
    ]
    cfg.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))

    before = privacy_report(cfg)
    assert before["rows_with_text"] == 3 and before["rows_past_retention"] == 1

    result = scrub_ledger(cfg, key_path)
    assert result == {"scrubbed": 2, "deleted": 1, "ledger_rows": 3}
    text = cfg.ledger.read_text()
    assert "j.rivers" not in text and "4242" not in text and "old one" not in text

    after = privacy_report(cfg)
    assert after["rows_with_text"] == 0 and after["rows_past_retention"] == 0
    scrubbed = json.loads(text.splitlines()[0])
    assert "chars" not in scrubbed   # a 200-char cut is not the message length
    assert "pii:emails" in scrubbed["flags"]


def test_feed_shows_and_seals_no_text_by_default(tmp_path, key_path):
    cfg = make_cfg(tmp_path)
    facts = inspect_text(SECRET, default_policy(), key_path=key_path)
    rows = [
        {"wall_clock": "2026-09-25 09:00:00", "kind": "desktop.chat.request",
         "app": "claude", "verdict": "redacted(2)", **facts},
        # a pre-PF row still holding text on disk
        {"wall_clock": "2026-09-25 09:01:00", "kind": "desktop.chat.request",
         "target": "claude.ai", "prompt": SECRET},
        {"wall_clock": "2026-09-25 09:02:00", "kind": "desktop.verdict.denied",
         "app": "claude", "verdict": "blocked",
         **inspect_text("password: hunter2", default_policy(), key_path=key_path)},
    ]
    cfg.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
    feed = Feed(cfg)
    feed.scan_initial()
    items = list(feed.items)
    blocked, legacy, fresh = items
    assert fresh["verb"] == f"Message to Claude · {len(SECRET)} characters"
    assert legacy["verb"] == "Message to Claude · sent before privacy settings"
    assert blocked["status"] == "blocked" and blocked["tier"] == "bad"
    assert "stopped" in blocked["verb"]
    sealed = (tmp_path / "sealchain.json").read_text()
    assert "j.rivers" not in sealed and "hunter2" not in sealed
    assert "text_redacted" not in sealed
    assert feed.seals.verify_chain()["tamper_evident"] is True


def test_poll_before_first_scan_does_not_double_absorb(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.ledger.write_text(json.dumps({"wall_clock": "2026-09-25 09:00:00",
                                      "kind": "desktop.chat.request",
                                      "app": "claude", "chars": 5}) + "\n")
    feed = Feed(cfg)
    assert feed.poll() == 0          # watcher got there first
    feed.scan_initial()
    feed.poll()
    assert feed.counts()["checked"] == 1 and feed.seals.height == 1


# -------------------------------------------------------- review follow-ups


def _tool_rows(*rows):
    return "".join(json.dumps({"wall_clock": "2026-09-25 09:00:00", **r}) + "\n"
                   for r in rows)


def test_overlapping_tool_calls_seal_their_own_results(tmp_path):
    cfg = make_cfg(tmp_path)
    who = {"user_id": "u1"}
    cfg.ledger.write_text(_tool_rows(
        {"kind": "tool.call", "tool": "execute", "identity": who, "call_id": "A", "chars": 3},
        {"kind": "tool.call", "tool": "execute", "identity": who, "call_id": "B", "chars": 4},
        {"kind": "tool.result", "tool": "execute", "identity": who, "call_id": "A",
         "latency_ms": 11},
        {"kind": "tool.error", "tool": "execute", "identity": who, "call_id": "B"},
    ))
    feed = Feed(cfg)
    feed.scan_initial()
    by_call = {it["call_id"]: it for it in feed.items}
    assert by_call["A"]["status"] == "answered" and by_call["A"]["latency_ms"] == 11
    assert by_call["B"]["status"] == "failed" and by_call["B"]["seal"]


def test_rows_without_call_id_close_the_oldest_call(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.ledger.write_text(_tool_rows(
        {"kind": "tool.call", "tool": "execute", "chars": 1},
        {"kind": "tool.call", "tool": "execute", "chars": 2},
        {"kind": "tool.result", "tool": "execute", "latency_ms": 5},
    ))
    feed = Feed(cfg)
    feed.scan_initial()
    newest, oldest = list(feed.items)
    assert oldest["status"] == "answered" and newest["status"] == "running"


def test_harsh_reply_is_a_conduct_flag_not_pii():
    assert ("conduct", "reply_toxic") in [(t, r) for t, r, _ in flags_for("you idiot")]


def test_uncovered_apps_cannot_be_turned_on():
    with pytest.raises(ValueError, match="Gemini"):
        apply_policy_update(default_policy(), {"apps": {"gemini": True}})
    assert apply_policy_update(default_policy(),
                               {"apps": {"chatgpt": True}})["apps"]["chatgpt"]


def test_chatgpt_and_openai_bodies_are_redacted():
    web = {"action": "next", "messages": [{"id": "m1", "author": {"role": "user"},
            "content": {"content_type": "text", "parts": ["mail a@b.co"]}}]}
    out, rules = redact_json_body(json.dumps(web))
    assert json.loads(out)["messages"][0]["content"]["parts"] == ["mail [redacted-email]"]
    assert rules == ["emails"]
    responses = {"model": "gpt-x", "input": "ssn 123-45-6789"}
    out, _ = redact_json_body(json.dumps(responses))
    assert json.loads(out)["input"] == "ssn [redacted-ssn]"


# ------------------------------------------------------ one UI, served here


def _serve(cfg):
    import socket
    import threading
    from byoai.integrations.shield import serve
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    threading.Thread(target=serve, args=(cfg, "127.0.0.1", port), daemon=True).start()
    import urllib.request
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/policy", timeout=0.2)
            break
        except Exception:
            _time.sleep(0.05)
    return base


def _get(url):
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        r = opener.open(url, timeout=2)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_server_serves_the_app_and_both_api_prefixes(tmp_path, monkeypatch):
    from byoai.agent_context_cache import console
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<div id=root>app</div>")
    (static / "assets" / "a.js").write_text("console.log(1)")
    monkeypatch.setattr(console, "STATIC_DIR", static)
    monkeypatch.setattr(console, "INDEX_FILE", static / "index.html")
    base = _serve(make_cfg(tmp_path))

    code, headers, _ = _get(base + "/")
    assert code == 302 and headers["Location"] == "/shield"
    assert _get(base + "/shield")[2] == b"<div id=root>app</div>"
    assert _get(base + "/shield/anything")[0] == 200            # client route
    assert _get(base + "/console/acme/shield")[0] == 200         # old link, redirected in-app
    code, headers, body = _get(base + "/console/assets/a.js")
    assert code == 200 and "javascript" in headers["Content-Type"]
    assert _get(base + "/console/assets/gone-abc123.js")[0] == 404  # not the app page
    assert json.loads(_get(base + "/shield-api/policy")[2])["mode"] == "redact"
    assert json.loads(_get(base + "/api/policy")[2])["mode"] == "redact"
    assert _get(base + "/api/nope")[0] == 404
    assert _get(base + "/../../etc/passwd")[0] == 404


def test_server_says_how_to_build_when_the_app_is_missing(tmp_path, monkeypatch):
    from byoai.agent_context_cache import console
    monkeypatch.setattr(console, "STATIC_DIR", tmp_path / "none")
    monkeypatch.setattr(console, "INDEX_FILE", tmp_path / "none" / "index.html")
    base = _serve(make_cfg(tmp_path))
    code, _, body = _get(base + "/shield")
    assert code == 503 and b"npm" in body


# ------------------------------------------- other sites can't drive Shield

from byoai.integrations.shield import clean_browser_row, request_problem


def _post(url, body, headers):
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=2)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_a_web_page_cannot_weaken_shield(tmp_path):
    base = _serve(make_cfg(tmp_path))
    weaken = {"mode": "observe", "acknowledge": "less_private"}
    # a form/text POST from any site: no JSON content type
    assert _post(base + "/api/policy", weaken, {"Content-Type": "text/plain"})[0] == 415
    # JSON from another site (would need a preflight; Origin gives it away)
    code, _ = _post(base + "/api/policy", weaken, {
        "Content-Type": "application/json", "Origin": "https://evil.example"})
    assert code == 403
    # DNS rebinding: the page's own hostname pointed at 127.0.0.1
    code, _ = _post(base + "/api/policy", weaken, {
        "Content-Type": "application/json", "Host": "evil.example:8300"})
    assert code == 403
    assert json.loads(_get(base + "/api/policy")[2])["mode"] == "redact"
    # Shield's own page still can
    code, body = _post(base + "/api/policy", weaken, {
        "Content-Type": "application/json", "Origin": base})
    assert code == 200 and body["mode"] == "observe"


def test_only_the_extension_can_send_browser_rows(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    base = _serve(cfg)
    rows = {"rows": [{"kind": "browser.chat.request", "app": "claude", "chars": 12,
                      "prompt": "email j.rivers@gmail.com", "wire": "/api/x"}]}
    json_h = {"Content-Type": "application/json"}
    assert _post(base + "/api/browser", rows, {**json_h, "Origin": "https://claude.ai"})[0] == 403
    assert _post(base + "/api/browser", rows, json_h)[0] == 403
    code, body = _post(base + "/api/browser", rows,
                       {**json_h, "Origin": "chrome-extension://abcdef"})
    assert code == 200 and body["accepted"] == 1
    stored = cfg.ledger.read_text()
    assert "j.rivers" not in stored and '"chars": 12' in stored
    # pinned to one extension id
    monkeypatch.setenv("BYOAI_SHIELD_EXTENSION_IDS", "trusted-id")
    assert _post(base + "/api/browser", rows,
                 {**json_h, "Origin": "chrome-extension://abcdef"})[0] == 403
    assert _post(base + "/api/browser", rows,
                 {**json_h, "Origin": "chrome-extension://trusted-id"})[0] == 200


def test_browser_rows_keep_only_known_fields():
    row = clean_browser_row({"kind": "browser.chat.request", "app": "claude",
                             "chars": 5, "prompt": "secret", "preview": "x",
                             "wire": "w" * 500, "ok": True})
    assert row is not None and set(row) == {"kind", "wall_clock", "app", "chars", "wire", "ok"}
    assert len(row["wire"]) == 60
    assert clean_browser_row({"kind": "desktop.chat.request"}) is None
    assert clean_browser_row({"kind": "browser.chat.request", "app": "evil",
                              "chars": True})["kind"] == "browser.chat.request"


def test_request_problem_allows_local_pages_and_get():
    ok_host = {"Host": "127.0.0.1:8300"}
    assert request_problem("GET", "/api/feed", ok_host) is None
    assert request_problem("GET", "/api/feed", {"Host": "attacker.test"}) is not None
    # Same-origin write: the origin must name the very host:port the request
    # was addressed to — Shield's own page — not merely "some localhost port".
    assert request_problem("POST", "/api/policy", {
        **ok_host, "Content-Type": "application/json; charset=utf-8",
        "Origin": "http://127.0.0.1:8300"}) is None
    # A different localhost port is another process, not Shield's page —
    # anything running there can't weaken Shield.
    assert request_problem("POST", "/api/policy", {
        **ok_host, "Content-Type": "application/json",
        "Origin": "http://localhost:9999"}) is not None
