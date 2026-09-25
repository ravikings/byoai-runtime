"""Coriqo Shield capture proxy for the local demo — loads the packaged addon
(:mod:`byoai.integrations.shield_proxy`) against this repo's example ledger.

    mitmdump --listen-port 8080 -s examples/desktop_proxy_capture.py

Point the macOS system proxy at 127.0.0.1:8080 and chat in Claude Desktop.
Privacy-first: the ledger gets rule hits, verdicts, lengths and keyed
fingerprints, not message text (see the shield Settings to change that).
"""

from pathlib import Path

from byoai.integrations.shield_proxy import ShieldProxy

_CAPTURE = Path(__file__).resolve().parent / "mcp_capture"

addons = [ShieldProxy(ledger=_CAPTURE / "captures.jsonl",
                      policy_path=_CAPTURE / "policy.json")]
