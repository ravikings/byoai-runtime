"""Rule table v2: the precision gate, actions, numbered placeholders."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from byoai.integrations.shield import (
    RULE_ACTIONS,
    action_for,
    apply_policy_update,
    clean_actions,
    default_policy,
    evaluate,
    flags_for,
    load_policy,
    read_policy,
    redact_json_body,
    redact_with_rules,
)

CORPUS = Path(__file__).resolve().parents[1] / "shield_rules_corpus"


def _lines(name):
    # Secret-shaped fixtures carry a {!} marker so the repo holds no scannable tokens.
    text = (CORPUS / name).read_text().replace("{!}", "")
    return [ln for ln in text.splitlines() if ln.strip()]


def test_corpus_is_big_enough():
    assert len(_lines("benign.txt")) >= 200 and len(_lines("positive.txt")) >= 100


def test_benign_corpus_has_no_block_or_warn_hits():
    bad = [(ln, evaluate(ln, default_policy())) for ln in _lines("benign.txt")
           if evaluate(ln, default_policy())[0] in ("block", "warn")]
    assert not bad, bad[:5]


def test_positive_corpus_recall_is_at_least_95_percent():
    lines = _lines("positive.txt")
    missed = [ln for ln in lines
              if not [1 for t, r, _ in flags_for(ln) if t in ("secret", "pii")]]
    assert 1 - len(missed) / len(lines) >= 0.95, missed


def test_every_secret_rule_is_exercised_by_the_positive_corpus():
    from byoai.integrations.shield import SECRET_RULES
    seen = {r for ln in _lines("positive.txt") for _, r, _ in flags_for(ln)}
    assert {r for r, _ in SECRET_RULES} <= seen


def test_luhn_and_iban_validators_gate_the_match():
    assert [r for _, r, _ in flags_for("card 4111 1111 1111 1111")] == ["cards"]
    assert not flags_for("card 4111 1111 1111 1112")
    assert any(r == "iban" for _, r, _ in flags_for("IBAN GB82 WEST 1234 5698 7654 32"))
    assert not any(r == "iban" for _, r, _ in flags_for("GB82 WEST 1234 5698 7654 33"))


def test_secrets_block_pii_redacts_flags_log_by_default():
    p = default_policy()
    assert evaluate("key AKIAIOSFODNN7EXAMPLE", p)[0] == "block"
    assert evaluate("mail a@b.co", p)[0] == "redact"
    assert evaluate("why does deploy.sh fail", p)[0] == "log"
    # credential text warns instead of blocking (rule override)
    assert action_for("credential_assign", p) == RULE_ACTIONS["credential_assign"] == "warn"
    assert evaluate("password: hunter42x", p)[0] == "warn"


def test_strictest_action_wins_and_observe_logs_everything():
    p = default_policy()
    assert evaluate("mail a@b.co and AKIAIOSFODNN7EXAMPLE", p)[0] == "block"
    assert evaluate("mail a@b.co and AKIAIOSFODNN7EXAMPLE",
                    {**p, "mode": "observe"})[0] == "log"
    p["actions"] = {"secret": "warn", "pii": "block", "flag": "log"}
    assert evaluate("mail a@b.co and AKIAIOSFODNN7EXAMPLE", p)[0] == "block"


def test_actions_policy_is_validated_with_defaults():
    assert clean_actions({"secret": "nonsense", "pii": "warn"}) == {
        "secret": "block", "pii": "warn", "flag": "log"}
    assert clean_actions(None) == default_policy()["actions"]
    out = apply_policy_update(default_policy(), {"actions": {"pii": "warn"}})
    assert out["actions"] == {"secret": "block", "pii": "warn", "flag": "log"}
    with pytest.raises(ValueError):
        apply_policy_update(default_policy(), {"actions": {"pii": "explode"}})
    with pytest.raises(ValueError):
        apply_policy_update(default_policy(), {"actions": {"nope": "log"}})
    # weaker than before needs the less-private acknowledgement
    with pytest.raises(ValueError, match="less private"):
        apply_policy_update(default_policy(), {"actions": {"secret": "log"}})


def test_a_bad_stored_actions_value_falls_back(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"policy_version": 2, "actions": {"secret": 5, "pii": "log"}}))
    assert read_policy(path)["actions"] == {"secret": "block", "pii": "log", "flag": "log"}


def test_policy_the_server_hands_out_carries_actions(tmp_path):
    from byoai.integrations.shield import ShieldConfig
    cfg = ShieldConfig(ledger=tmp_path / "l.jsonl", seal_path=tmp_path / "s.json",
                       key_dir=tmp_path / "k", policy_path=tmp_path / "p.json",
                       managed_policy_path=tmp_path / "m.json")
    assert load_policy(cfg)["actions"] == default_policy()["actions"]


def test_placeholders_are_numbered_per_distinct_value():
    out, rules = redact_with_rules("a@x.co, b@x.co, a@x.co; card 4111111111111111 and 5500005555555559")
    assert out == "[EMAIL_1], [EMAIL_2], [EMAIL_1]; card [CARD_1] and [CARD_2]"
    assert rules == ["emails", "cards"]


def test_numbering_spans_the_whole_body_and_each_body_starts_over():
    body = {"messages": [{"role": "user", "content": "a@x.co"},
                         {"role": "user", "content": [{"type": "text", "text": "b@x.co a@x.co"}]}]}
    out, _ = redact_json_body(json.dumps(body))
    msgs = json.loads(out)["messages"]
    assert msgs[0]["content"] == "[EMAIL_1]"
    assert msgs[1]["content"][0]["text"] == "[EMAIL_2] [EMAIL_1]"
    out2, _ = redact_json_body(json.dumps({"prompt": "b@x.co"}))
    assert json.loads(out2)["prompt"] == "[EMAIL_1]"  # nothing carried over


def test_redaction_respects_a_log_action():
    p = {**default_policy(), "actions": {"secret": "block", "pii": "log", "flag": "log"}}
    out, rules = redact_json_body(json.dumps({"prompt": "a@x.co"}), p)
    assert rules == [] and json.loads(out)["prompt"] == "a@x.co"


def test_gemini_contents_are_read_and_redacted():
    from byoai.integrations.shield import message_text
    body = {"contents": [{"role": "user", "parts": [{"text": "hi a@x.co"}, {"inlineData": {"data": "zzz"}}]}]}
    assert message_text(body) == "hi a@x.co"
    out, rules = redact_json_body(json.dumps(body))
    assert json.loads(out)["contents"][0]["parts"][0]["text"] == "hi [EMAIL_1]"
    assert json.loads(out)["contents"][0]["parts"][1] == {"inlineData": {"data": "zzz"}}
    assert rules == ["emails"]


def test_browser_rows_accept_attachment_health_and_warned_verdicts():
    from byoai.integrations.shield import clean_browser_row
    att = clean_browser_row({"kind": "browser.chat.attachment", "app": "claude",
                             "mime": "application/pdf", "bytes": 1234, "name": "secret.pdf"})
    assert att["mime"] == "application/pdf" and att["bytes"] == 1234 and "name" not in att
    health = clean_browser_row({"kind": "browser.health.unmatched", "app": "chatgpt",
                                "path": "/backend-api/new/conversation?token=abc" + "x" * 80})
    assert health["path"] == "/backend-api/new/conversation" and "token" not in json.dumps(health)
    row = clean_browser_row({"kind": "browser.chat.request", "verdict": "warned→sent",
                             "flags": ["secret:credential_assign", "flag:tool_intent"]})
    assert row["verdict"] == "warned→sent" and len(row["flags"]) == 2
    assert "verdict" not in clean_browser_row({"kind": "browser.chat.request",
                                               "verdict": "warned→leaked"})


def test_the_generated_js_rules_pass_the_same_corpus_in_node():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    done = subprocess.run([node, str(CORPUS / "check.mjs")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr + done.stdout
