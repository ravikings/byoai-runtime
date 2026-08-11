"""Tests for the offline ledger verifier.

Ledger fixtures are always built with raw ``sqlite3`` against the schema in
``internal_doc/recorder_contract.md`` — the verifier is never handed a
database written by the code it is meant to police.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from byoai.recorder.canonical import canonicalize, sha256_hex
from byoai.recorder.cli import format_report, main
from byoai.recorder.verify import GENESIS_PREV_HASH, VerifyError, verify_ledger

DEVICE_ID = "dev_TESTDEVICE"
SESSION_ID = "ses_01TEST"

EVENT_COLUMNS = [
    "schema_version",
    "event_id",
    "device_id",
    "session_id",
    "seq",
    "kind",
    "ts_device",
    "ts_monotonic_ns",
    "tool_use_id",
    "tool_name",
    "payload",
    "payload_hash",
    "model",
    "provider",
]

_DDL = """
CREATE TABLE agent_events (
    seq INTEGER PRIMARY KEY,
    schema_version TEXT NOT NULL,
    event_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts_device TEXT NOT NULL,
    ts_monotonic_ns INTEGER NOT NULL,
    tool_use_id TEXT,
    tool_name TEXT,
    payload TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    model TEXT,
    provider TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL
);
CREATE TABLE checkpoints (
    seq_end INTEGER PRIMARY KEY,
    device_id TEXT NOT NULL,
    seq_start INTEGER NOT NULL,
    chain_head TEXT NOT NULL,
    ts_device TEXT NOT NULL,
    sig TEXT NOT NULL
);
"""


def _event(
    seq: int,
    kind: str,
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    payload: dict | None = None,
) -> dict:
    payload = payload or {"n": seq}
    return {
        "schema_version": "1",
        "event_id": f"evt_{seq:04d}",
        "device_id": DEVICE_ID,
        "session_id": SESSION_ID,
        "seq": seq,
        "kind": kind,
        "ts_device": f"2026-08-04T14:0{seq % 10}:00.000000Z",
        "ts_monotonic_ns": 1_000_000 * seq,
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "payload": payload,
        "payload_hash": sha256_hex(canonicalize(payload)),
        "model": "claude-opus-5",
        "provider": "anthropic",
    }


def _event_digest(event: dict) -> str:
    return sha256_hex(canonicalize(event))


def _entry_hash(prev_hash: str, seq: int, digest: str) -> str:
    return sha256_hex(canonicalize({"prev_hash": prev_hash, "seq": seq, "event_digest": digest}))


def _default_events() -> list[dict]:
    return [
        _event(1, "session_start"),
        _event(2, "tool_use", tool_use_id="toolu_A", tool_name="Bash"),
        _event(3, "tool_result", tool_use_id="toolu_A", tool_name="Bash"),
        _event(4, "message"),
        _event(5, "tool_use", tool_use_id="toolu_B", tool_name="Read"),
        _event(6, "tool_result", tool_use_id="toolu_B", tool_name="Read"),
    ]


def _make_key() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    return priv, pub_b64


def _sign(priv: Ed25519PrivateKey, data: bytes) -> str:
    return "ed25519:" + base64.b64encode(priv.sign(data)).decode()


def build_ledger(
    path: Path,
    events: list[dict] | None = None,
    *,
    priv: Ed25519PrivateKey | None = None,
    checkpoint: bool = True,
    forge_signature: bool = False,
) -> list[dict]:
    """Write a well-formed, correctly chained ledger with raw sqlite3."""
    events = events if events is not None else _default_events()
    conn = sqlite3.connect(path)
    conn.executescript(_DDL)

    prev = GENESIS_PREV_HASH
    rows = []
    for event in events:
        entry_hash = _entry_hash(prev, event["seq"], _event_digest(event))
        row = dict(event)
        row["payload"] = json.dumps(event["payload"])
        row["prev_hash"] = prev
        row["entry_hash"] = entry_hash
        rows.append(row)
        prev = entry_hash

    cols = [*EVENT_COLUMNS, "prev_hash", "entry_hash"]
    conn.executemany(
        f"INSERT INTO agent_events ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [tuple(r[c] for c in cols) for r in rows],
    )

    if checkpoint and priv is not None and events:
        cp = {
            "device_id": DEVICE_ID,
            "seq_start": events[0]["seq"],
            "seq_end": events[-1]["seq"],
            "chain_head": prev,
            "ts_device": "2026-08-04T14:09:00.000000Z",
        }
        if forge_signature:
            other, _ = _make_key()
            sig = _sign(other, canonicalize(cp))
        else:
            sig = _sign(priv, canonicalize(cp))
        conn.execute(
            "INSERT INTO checkpoints "
            "(seq_end, device_id, seq_start, chain_head, ts_device, sig) "
            "VALUES (?,?,?,?,?,?)",
            (
                cp["seq_end"],
                cp["device_id"],
                cp["seq_start"],
                cp["chain_head"],
                cp["ts_device"],
                sig,
            ),
        )
    conn.commit()
    conn.close()
    return rows


@pytest.fixture()
def keypair() -> tuple[Ed25519PrivateKey, str]:
    return _make_key()


# --------------------------------------------------------------------------


def test_clean_ledger_verifies(tmp_path, keypair):
    priv, pub = keypair
    db = tmp_path / "clean.db"
    build_ledger(db, priv=priv)

    report = verify_ledger(db, public_key_b64=pub)

    assert report.ok is True
    assert report.entries_checked == 6
    assert report.broken_links == []
    assert report.bad_signatures == []
    assert report.gaps == []
    assert report.unpaired_tool_uses == []
    assert report.orphan_tool_results == []
    assert report.checkpoints_checked == 1
    assert report.signatures_verified is True
    assert report.seq_start == 1 and report.seq_end == 6
    assert report.device_ids == [DEVICE_ID]

    text = format_report(report)
    assert "record complete and unaltered" in text
    assert DEVICE_ID in text and SESSION_ID in text
    assert "seq 1–6" in text
    assert "6 events" in text
    assert "UTC" in text


def test_verifier_does_not_trust_stored_chain_head(tmp_path, keypair):
    """Every entry hash is re-derived; a rewritten chain head cannot rescue it."""
    priv, pub = keypair
    db = tmp_path / "l.db"
    build_ledger(db, priv=priv)

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE agent_events SET payload = ? WHERE seq = 4",
        (json.dumps({"n": "rewritten"}),),
    )
    conn.commit()
    conn.close()

    assert verify_ledger(db, public_key_b64=pub).ok is False


def test_tampered_payload_fails_at_exactly_that_seq(tmp_path, keypair):
    priv, pub = keypair
    db = tmp_path / "tampered.db"
    build_ledger(db, priv=priv)

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE agent_events SET payload = ? WHERE seq = 3",
        (json.dumps({"n": 3, "exfiltrated": True}),),
    )
    conn.commit()
    conn.close()

    report = verify_ledger(db, public_key_b64=pub)

    assert report.ok is False
    assert report.broken_links == [3]  # no cascade onto 4, 5, 6
    assert report.gaps == []
    assert report.bad_signatures == []
    assert report.entries_checked == 6
    assert "entry seq 3 does not match its own hash chain" in format_report(report)


def test_deleted_middle_row_is_a_gap_and_a_broken_link(tmp_path, keypair):
    priv, pub = keypair
    db = tmp_path / "deleted.db"
    build_ledger(db, priv=priv)

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM agent_events WHERE seq = 4")
    conn.commit()
    conn.close()

    report = verify_ledger(db, public_key_b64=pub)

    assert report.ok is False
    assert report.gaps == [(4, 4)]
    assert report.broken_links == [5]
    assert report.entries_checked == 5

    text = format_report(report)
    assert "record incomplete: seq 4–4 missing (1 event)" in text
    assert "CANNOT be relied upon" in text


def test_deleted_range_reports_the_whole_gap(tmp_path, keypair):
    priv, pub = keypair
    db = tmp_path / "range.db"
    build_ledger(db, priv=priv)

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM agent_events WHERE seq IN (3, 4)")
    conn.commit()
    conn.close()

    report = verify_ledger(db, public_key_b64=pub)

    assert report.gaps == [(3, 4)]
    assert "seq 3–4 missing (2 events)" in format_report(report)


def test_forged_checkpoint_signature_fails(tmp_path, keypair):
    priv, pub = keypair
    db = tmp_path / "forged.db"
    build_ledger(db, priv=priv, forge_signature=True)

    report = verify_ledger(db, public_key_b64=pub)

    assert report.ok is False
    assert report.bad_signatures == [6]
    assert report.broken_links == []  # the chain itself is fine
    assert "signature does not verify" in " ".join(report.notes)
    assert "failed verification" in format_report(report)


def test_checkpoint_over_a_rewritten_chain_head_fails(tmp_path, keypair):
    """A validly signed checkpoint that no longer matches the entries fails."""
    priv, pub = keypair
    db = tmp_path / "head.db"
    build_ledger(db, priv=priv)

    conn = sqlite3.connect(db)
    conn.execute("UPDATE checkpoints SET chain_head = ?", ("sha256:" + "ff" * 32,))
    conn.commit()
    conn.close()

    report = verify_ledger(db, public_key_b64=pub)

    assert report.ok is False
    assert report.bad_signatures == [6]
    assert any("chain head does not match" in n for n in report.notes)


def test_without_pubkey_signatures_are_not_claimed_verified(tmp_path, keypair):
    priv, _ = keypair
    db = tmp_path / "nokey.db"
    build_ledger(db, priv=priv)

    report = verify_ledger(db)

    assert report.ok is True
    assert report.signatures_verified is False
    assert any("NOT signature-checked" in n for n in report.notes)


def test_unpaired_tool_use_is_a_finding(tmp_path, keypair):
    priv, pub = keypair
    events = [
        _event(1, "session_start"),
        _event(2, "tool_use", tool_use_id="toolu_A", tool_name="Bash"),
        _event(3, "tool_use", tool_use_id="toolu_B", tool_name="Write"),
        _event(4, "tool_result", tool_use_id="toolu_A", tool_name="Bash"),
    ]
    db = tmp_path / "unpaired.db"
    build_ledger(db, events, priv=priv)

    report = verify_ledger(db, public_key_b64=pub)

    assert report.unpaired_tool_uses == ["toolu_B"]
    assert report.orphan_tool_results == []
    # The chain is intact — an unfinished tool call is reported, not a forgery.
    assert report.ok is True

    text = format_report(report)
    assert "tool call `toolu_B` has no recorded result" in text
    assert "outcome was never returned" in text
    assert main([str(db), "--pubkey", pub]) == 0


def test_orphan_tool_result_is_a_stronger_finding(tmp_path, keypair):
    priv, pub = keypair
    events = [
        _event(1, "session_start"),
        _event(2, "tool_result", tool_use_id="toolu_GHOST", tool_name="Bash"),
    ]
    db = tmp_path / "orphan.db"
    build_ledger(db, events, priv=priv)

    report = verify_ledger(db, public_key_b64=pub)

    assert report.orphan_tool_results == ["toolu_GHOST"]
    assert report.unpaired_tool_uses == []
    assert report.ok is False

    text = format_report(report)
    assert "tool result `toolu_GHOST` has no preceding tool_use" in text
    assert "never requested" in text


def test_tool_result_before_its_tool_use_is_an_orphan(tmp_path, keypair):
    priv, pub = keypair
    events = [
        _event(1, "tool_result", tool_use_id="toolu_X", tool_name="Bash"),
        _event(2, "tool_use", tool_use_id="toolu_X", tool_name="Bash"),
    ]
    db = tmp_path / "outoforder.db"
    build_ledger(db, events, priv=priv)

    report = verify_ledger(db, public_key_b64=pub)

    assert report.orphan_tool_results == ["toolu_X"]
    assert report.ok is False


def test_empty_ledger_is_vacuously_clean(tmp_path):
    db = tmp_path / "empty.db"
    build_ledger(db, [], checkpoint=False)

    report = verify_ledger(db)

    assert report.ok is True
    assert report.entries_checked == 0
    assert report.seq_start is None
    assert "0 events" in format_report(report)


def test_missing_ledger_file_raises(tmp_path):
    with pytest.raises(VerifyError):
        verify_ledger(tmp_path / "nope.db")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_exit_codes_and_json(tmp_path, keypair, capsys):
    priv, pub = keypair
    clean = tmp_path / "clean.db"
    build_ledger(clean, priv=priv)

    assert main([str(clean), "--pubkey", pub]) == 0
    assert "record complete and unaltered" in capsys.readouterr().out

    assert main([str(clean), "--pubkey", pub, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["entries_checked"] == 6
    assert payload["gaps"] == []
    for key in (
        "broken_links",
        "bad_signatures",
        "unpaired_tool_uses",
        "orphan_tool_results",
    ):
        assert payload[key] == []

    broken = tmp_path / "broken.db"
    build_ledger(broken, priv=priv)
    conn = sqlite3.connect(broken)
    conn.execute("DELETE FROM agent_events WHERE seq = 4")
    conn.commit()
    conn.close()

    assert main([str(broken), "--pubkey", pub]) == 1
    assert "CANNOT be relied upon" in capsys.readouterr().out

    assert main([str(broken), "--pubkey", pub, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["gaps"] == [[4, 4]]
    assert payload["broken_links"] == [5]


def test_cli_reports_unreadable_ledger(tmp_path, capsys):
    assert main([str(tmp_path / "missing.db")]) == 2
    assert "coriqo-verify:" in capsys.readouterr().err


def test_verifier_makes_no_network_calls(tmp_path, keypair, monkeypatch):
    import socket

    priv, pub = keypair
    db = tmp_path / "offline.db"
    build_ledger(db, priv=priv)

    def _boom(*args, **kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError("verifier attempted network I/O")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    assert verify_ledger(db, public_key_b64=pub).ok is True
