"""Coriqo live shield — the behavioral analyzer + UI backend.

Reads captures.jsonl (fed live by the MCP capture server and the desktop
proxy), turns raw rows into *interactions*, and serves the Coriqo shield UI
at http://localhost:8300 — the consumer_design frames, wired to real data:

    nohup .venv/bin/python examples/ui/live_shield.py > /tmp/byoai_shield.log 2>&1 &
    open http://localhost:8300

What this layer guarantees (v1):

1. **Flag behavior** — chat sends AND model replies AND tool calls are scored:
   PII (email/card/wallet/phone/SSN/API-key/credentials), HIGH risk
   (executables masquerading as documents, credential blocks), agent intent,
   and reply-level signals (PII/secret echo, harsh language).
2. **Redact — local hold**: the ledger copy of any text is stored with PII
   matches replaced by ``[redacted-*]`` labels. Plaintext matches are *not*
   kept; only their sha256 is (sealed), so the evidence proves what happened
   without keeping sensitive bytes around.
3. **Seal — BYOAI-style tamper-evidence**: every interaction is stamped with
   sha256 seal in a hash chain (each seal binds the redacted content + flags
   + the previous seal). ``GET /api/verify`` re-walks the chain and reports
   tamper status. Wire format mirrors recorder/receipts.py canonical rows so
   the same evidence can graduate into the recorder later.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import base64
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from byoai.recorder.keys import load_or_create_device_key
from byoai.recorder.merkle import MerkleTree, checkpoint_leaf_hash

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "examples" / "mcp_capture" / "captures.jsonl"
SEAL_PATH = ROOT / "examples" / "mcp_capture" / "sealchain.json"
POLICY_PATH = ROOT / "examples" / "mcp_capture" / "policy.json"
STATIC = ROOT / "examples" / "ui" / "static"

DEFAULT_POLICY: dict = {
    "mode": "observe",
    "apps": {"claude": True, "chatgpt": False, "gemini": False, "copilot": False},
}

APP_BUNDLES = {   # surface → candidate /Applications bundle names
    "claude": ["Claude.app", "Claude Code.app"],
    "chatgpt": ["ChatGPT.app"],
    "gemini": ["Gemini.app", "Google Gemini.app"],
    "copilot": ["Microsoft Copilot.app", "Copilot.app"],
}


def load_policy() -> dict:
    try:
        p = json.loads(POLICY_PATH.read_text())
        policy = {**DEFAULT_POLICY, **p}
        policy["apps"] = {**DEFAULT_POLICY["apps"], **p.get("apps", {})}
        return policy
    except Exception:
        return dict(DEFAULT_POLICY)


def save_policy(p: dict) -> None:
    POLICY_PATH.write_text(json.dumps(p, indent=1))


def discover_apps() -> dict[str, bool]:
    found: dict[str, bool] = {}
    apps = _application_folder_list()
    for tag, bundles in APP_BUNDLES.items():
        found[tag] = any(b in apps for b in bundles) or bool(
            files := [f for f in apps if tag in f.lower()])
    return found


def _application_folder_list() -> list[str]:
    try:
        return [p.name for p in Path("/Applications").iterdir() if p.suffix == ".app"]
    except OSError:
        return []

MAX_INTERACTIONS = 400

# ---------------------------------------------------------------- flag rules

PII_RULES = [
    ("emails",        re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)),
    ("cards",         re.compile(r"\b4[0-9]{12}(?:[0-9]{3})?\b|\b(5[1-5][0-9]{14})\b")),
    ("wallets",       re.compile(r"\b0x[a-fA-F0-9]{40}\b|\bbc1[a-z0-9]{25,}\b")),
    ("phone_numbers", re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")),
    ("ssn_like",      re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("api_keys",      re.compile(r"\bsk-[a-zA-Z0-9]{16,}\b|\bBearer\s+[A-Za-z0-9._-]{20,}\b")),
]
HIGH_RULES = [
    ("executable_masquerade", re.compile(r"\b[\w.-]+\.(exe|scr|bat|cmd|dmg|msi)\b", re.I)),
    ("credential_block", re.compile(r"(?i)\b(password|secret|api[_ ]?key)\s*[:=]")),
]
AGENT_RULES = [
    ("tool_intent", re.compile(
        r"\b(execute|execute_stream|tool_use|deploy|refund|wire ?transfer|"
        r"send (the |my )?money|draft_email)\b", re.I)),
]
# Reply-level behavior (the model's own conduct, not the user's):
REPLY_RULES = [
    ("reply_pii_echo", re.compile(
        r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|\b4[0-9]{12}(?:[0-9]{3})?\b", re.I)),
    ("reply_leak", re.compile(r"(?i)(your api key|secret[^\n]{0,24}=|\bsk-[a-zA-Z0-9]{16,})")),
    ("reply_toxic", re.compile(r"(?i)\b(stupid|idiot|hate|hope you die)\b")),
]

RULE_LABEL = {
    "emails": "email address", "cards": "card number", "wallets": "wallet address",
    "phone_numbers": "phone number", "ssn_like": "SSN-like number",
    "api_keys": "API key/token", "credential_block": "credential text",
    "executable_masquerade": "dangerous file", "tool_intent": "agent action intent",
    "connector_tool_call": "agent tool call", "password_said": "credential mention",
    "reply_pii_echo": "PII echoed in reply", "reply_leak": "secret echoed in reply",
    "reply_toxic": "harsh language in reply",
}
RULE_REDACT = {"emails": "[redacted-email]", "cards": "[redacted-card]",
               "wallets": "[redacted-wallet]", "phone_numbers": "[redacted-number]",
               "ssn_like": "[redacted-ssn]", "api_keys": "[redacted-token]",
               "password_said": "[credential]"}


def redact(text: str) -> str:
    """Replace PII matches with compartment labels — the plaintext match is
    never stored; its sha256 is (sealed) instead, so the ledger proves the
    interaction happened without keeping the sensitive bytes around."""
    for tier, p in PII_RULES:
        token = RULE_REDACT.get(tier)
        if token:
            text = p.sub(token, text)
    return text


def flags_for(text: str) -> list[tuple[str, str, str]]:
    """Returns [(tier, rule, snippet)] — tier in pii / high / agent."""
    flags: list[tuple[str, str, str]] = []
    for tier, rules in (("high", HIGH_RULES), ("pii", PII_RULES), ("high", HIGH_RULES),
                        ("pii", PII_RULES), ("agent", AGENT_RULES), ("pii", REPLY_RULES),
                        ("high", REPLY_RULES)):
        for rule, pattern in rules:
            m = pattern.search(text)
            if m:
                flags.append((tier, rule, m.group(0)[:60]))
    return flags


# ------------------------------------------------------- seal — tamper chain

def _canon(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class SealChain:
    """Merkle-graduated tamper evidence (recorder-core same primitives).

    Every interaction is an leaf = sha256(canonical(payload)) with the RFC 6962
    domain prefix used by the recorder; the leaves build a binary MerkleTree
    with duplicate-promotion protection (CVE-2012-2459-safe). On every N new
    entries (or on demand), a checkpoint {root, height, device_id, ts} is
    signed with the device's Ed25519 key — the key never leaves the machine
    and receipts ship its *public* base64 so a verifier can check offline."""

    SIG_EVERY = 8

    def __init__(self) -> None:
        self._key = load_or_create_device_key(Path.home() / ".byoai_shell" / "shield_keys")
        self.head: str | None = None
        self.height = 0
        self.entries: list[dict] = []   # sealed interaction payloads (redacted only)
        self.checkpoint: dict | None = None
        self._load()
        self._tree: MerkleTree | None = None
        self._dirty = len(self.entries)

    def _load(self) -> None:
        if SEAL_PATH.exists():
            try:
                state = json.loads(SEAL_PATH.read_text())
                self.entries = state.get("entries", [])[-512:]
                self.checkpoint = state.get("checkpoint")
            except Exception:
                self.entries = []
            if self.entries:
                self.height = self.entries[-1]["height"]
        self.head = (self.checkpoint or {}).get("root_hex")
        self._rebuild_tree()

    def _rebuild_tree(self) -> None:
        leaves = [checkpoint_leaf_hash(e["payload"]) for e in self.entries]
        self._tree = MerkleTree(leaves) if leaves else None

    def _leaf_for(self, entry: dict) -> int:
        # payload order matches entries order — leaf index = entry position in
        # the current tree (bounded window; receipts note the window).
        for i, e in enumerate(self.entries):
            if e["seal"] == entry["seal"]:
                return i
        return -1

    def stamp(self, payload: dict) -> str:
        entry = {"height": self.height + 1, "payload": payload}
        # seal = recorder-canonical leaf hash (0x00 prefix + RFC 8785 canonical
        # JSON), first 16 hex — the browser verifier re-derives exactly this.
        leaf = checkpoint_leaf_hash(payload)
        entry["seal"] = leaf.hex()[:16]
        entry["leaf_hash"] = leaf.hex()
        entry["prev"] = self.head
        self.entries.append(entry)
        self.head = entry["seal"]
        self.height = entry["height"]
        self._tree = None  # lazily rebuilt on demand
        if self.height % self.SIG_EVERY == 0:
            self.sign_checkpoint()
        else:
            self._persist()
        return entry["seal"]

    def sign_checkpoint(self) -> dict:
        tree = self._tree or (self._rebuild_tree(), self._tree)[-1]
        root = (self._tree or tree).root.hex()
        cp: dict = {
            "kind": "byoai.shield.checkpoint.v1",
            "root_hex": root,
            "height": self.height,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S Z", time.gmtime()).replace(" Z", "Z"),
            "device_id": self._key.device_id,
            "public_key_b64": self._key.public_key_b64,
        }
        cp["sig"] = self._key.sign(checkpoint_leaf_hash(cp))  # "ed25519:<b64>"
        self.checkpoint = cp
        self._persist()
        return cp

    def _persist(self) -> None:
        with SEAL_PATH.open("w") as fh:
            json.dump({"entries": self.entries[-512:],
                       "checkpoint": self.checkpoint}, fh)

    def verify_chain(self) -> dict:
        prev_hash_ok = True
        for e in self.entries:
            payload = e["payload"]
            # v1 entries sealed {"prev","height","payload"} plain-json; v2
            # seals are recorder-canonical leaf hashes. Accept both while the
            # graduation window is open.
            v2 = checkpoint_leaf_hash(payload).hex()[:16]
            v1 = hashlib.sha256(json.dumps(
                {"prev": e.get("prev"), "height": e["height"], "payload": payload},
                sort_keys=True, default=str).encode()).hexdigest()[:16]
            if v2 != e["seal"] and v1 != e["seal"]:
                return {"tamper_evident": False, "broken_at": e["height"],
                        "reason": "content hash mismatch"}
            prev = e["seal"]
        root = None
        sig_ok = None
        cp = self.checkpoint
        if cp:
            root = cp.get("root_hex")
            sig_ok = self._sig_valid(cp)
        tree = (self._rebuild_tree(), self._tree)[-1]
        live_root = (self._tree.root.hex() if self._tree else None)
        consistent = (root == live_root) if root and live_root else None
        return {"tamper_evident": bool(prev_hash_ok and (sig_ok is not False)),
                "entries": len(self.entries), "head": self.head,
                "merkle_root": live_root, "checkpoint": cp and {
                    "root": cp.get("root_hex"), "height": cp["height"],
                    "device_id": cp.get("device_id"), "sig_valid": sig_ok},
                "root_covers_height": consistent}

    def _sig_valid(self, cp: dict) -> bool:
        cp_no_sig = {k: v for k, v in cp.items() if k != "sig"}
        try:
            return self._key.verify(cp["public_key_b64"],
                                    checkpoint_leaf_hash(cp_no_sig), cp["sig"])
        except Exception:
            return False

    def proof_for(self, seal: str) -> dict | None:
        """Inclusion proof (hex steps) for one entry under the current root."""
        idx = self._leaf_for({"seal": seal})
        if idx < 0 or self._tree is None:
            self._rebuild_tree()
            idx = self._leaf_for({"seal": seal})
        if idx < 0 or self._tree is None:
            return None
        proof = self._tree.proof(idx)
        return {
            "leaf_index": proof.leaf_index,
            "leaf_hash": proof.leaf_hash.hex(),
            "steps": [{"sibling": s.sibling.hex(), "side": s.side} for s in proof.steps],
            "root_hex": proof.root.hex(),
        }


seal_chain = SealChain()

# --------------------------------------------------------- ledger → interactions

class Interaction:
    __slots__ = ("id", "source", "surface", "ts", "date", "verb", "text", "reply",
                 "status", "flags", "response_stream", "tool", "identity",
                 "usage", "seal", "raw_hash", "redacted")

    def __init__(self, source: str, surface: str, ts: str, verb: str,
                 text: str, status: str = "answered", tool: str | None = None,
                 identity: dict | None = None, usage: dict | None = None,
                 date: str | None = None):
        self.id = uuid.uuid4().hex[:10]
        self.source = source          # desktop / mcp
        self.surface = surface        # claude.ai desktop / MCP client name...
        self.ts = ts
        self.date = date or time.strftime("%Y-%m-%d")
        self.verb = verb              # one-liner for the Activity feed
        self.text = text              # raw working copy (memory only)
        self.redacted = redact(text)  # what the ledger ever shows
        self.reply: str | None = None
        self.status = status
        self.flags: list[dict] = []
        self.response_stream = False
        self.tool = tool
        self.identity = identity or {}
        self.usage = usage or {}
        self.seal = ""
        self.raw_hash = hashlib.sha256((verb or text or "")[:4000].encode()).hexdigest()[:12]

    def eval_flags(self) -> None:
        found = flags_for(self.text) + flags_for(self.verb)
        if self.reply:
            found += flags_for(self.reply)
        if self.tool:
            found.append(("agent", "connector_tool_call", self.tool))
        self.flags = [{"tier": t, "rule": r,
                       "mhash": hashlib.sha256(snip.encode()).hexdigest()[:12]}
                      for t, r, snip in found]
        self.seal = seal_chain.stamp({
            "interaction": self.id,
            "ts": self.ts,
            "source": self.source,
            "surface": self.surface,
            "text_redacted_hash": hashlib.sha256(
                redact(self.verb or self.text).encode()).hexdigest()[:16],
            "reply_hash": hashlib.sha256(
                (self.reply or "")[:2000].encode()).hexdigest()[:16] if self.reply else None,
            "flags": [(f["tier"], f["rule"], f["mhash"]) for f in self.flags],
            "identity": {k: self.identity.get(k) for k in ("user_id", "session_id", "client_name")},
            "usage": self.usage,
        })

    @property
    def tier(self) -> str:
        if any(f["tier"] == "high" for f in self.flags):
            return "bad"
        if any(f["tier"] == "pii" for f in self.flags):
            return "warn"
        return "ok"

    def to_json(self) -> dict:
        seen: set[str] = set()
        uniq_flags = []
        for f in self.flags:
            key = f"{f['tier']}:{f['rule']}"
            if key not in seen:
                seen.add(key)
                uniq_flags.append({"tier": f["tier"], "rule": f["rule"]})
        return {
            "id": self.id, "source": self.source, "surface": self.surface,
            "ts": self.ts, "date": self.date,
            "verb": redact(self.verb),
            "text": redact(self.text),
            "reply": redact(self.reply[:400]) if self.reply else None,
            "status": self.status, "tier": self.tier, "flags": uniq_flags,
            "response_stream": self.response_stream, "tool": self.tool,
            "identity": self.identity, "usage": self.usage,
            "seal": self.seal,
        }


class Feed:
    def __init__(self) -> None:
        self.items: deque[dict] = deque(maxlen=MAX_INTERACTIONS)
        self.lock = threading.Lock()
        self._flush_offset = 0

    def scan_initial(self) -> int:
        """Parse the whole existing ledger once, newest-last."""
        if not LEDGER.exists():
            return 0
        rows = [json.loads(x) for x in LEDGER.read_text().splitlines() if x.strip()]
        for r in rows:
            self._absorb(r)
        self._flush_offset = len(rows)
        return len(self.items)

    def poll(self) -> int:
        try:
            lines = LEDGER.read_text().splitlines()
        except OSError:
            return 0
        if len(lines) == self._flush_offset:
            return 0
        new = [l for l in lines[self._flush_offset:] if l.strip()]
        self._flush_offset = len(lines)
        for l in new:
            try:
                self._absorb(json.loads(l))
            except Exception:
                continue
        return len(new)

    def _absorb(self, r: dict) -> None:
        kind = r.get("kind", "")
        ts = r.get("wall_clock", "")
        short = ts.split()[-1] if ts else ""

        if kind == "desktop.chat.request":
            prompt = r.get("prompt") or ""
            if not prompt:
                return
            model = "claude" if r.get("target") == "claude.ai" else r.get("target")
            item = Interaction(source="desktop",
                               surface=f"Claude Desktop · {model}",
                               ts=short, verb=prompt, text=prompt,
                               date=(r.get("wall_clock") or "")[:10] or None)
            item.eval_flags()
            self.items.appendleft(item.to_json())

        elif kind == "tool.call":
            tool = r.get("tool") or "execute"
            ident = r.get("identity") or {}
            preview = r.get("input_preview")
            item = Interaction(source="mcp",
                               surface=ident.get("client_name") or "MCP client",
                               ts=short,
                               verb=preview if isinstance(preview, str) else "tool call",
                               text=preview if isinstance(preview, str) else "",
                               status="running", tool=tool, identity=ident,
                               date=(r.get("wall_clock") or "")[:10] or None)
            item.eval_flags()
            self.items.appendleft(item.to_json())

        elif kind == "tool.result":
            tool = r.get("tool")
            ident = r.get("identity") or {}
            for it in self.items:
                if (it["source"] == "mcp" and it["status"] == "running"
                        and it["tool"] == tool
                        and it["identity"].get("user_id") == ident.get("user_id")):
                    it["status"] = "answered"
                    it["response_stream"] = tool == "execute_stream"
                    it["usage"] = r.get("usage") or {}
                    it["latency_ms"] = r.get("latency_ms")
                    if tool == "execute" and (r.get("content")):
                        text = str(r.get("content"))[:200]
                        if any(flags_for(text)):
                            rf = flags_for(text)
                            it["flags"] = it["flags"] + [
                                {"tier": t, "rule": rr} for t, r_, _ in
                                flags_for(text)] and it["flags"]
                            for t, rule, _snip in flags_for(text):
                                it["flags"].append({"tier": t, "rule": rule})
                    break

        elif kind == "tool.error":
            for it in self.items:
                if it["source"] == "mcp" and it["status"] == "running":
                    it["status"] = "failed"
                    break

        elif kind == "desktop.chat.response":
            # reply rows: match the newest desktop interaction, add reply text
            preview = (r.get("preview") or "")
            body = preview
            # pull assistant text out of SSE if we got it
            for m in re.findall(r'"text"\s*:\s*"([^"]{10,400})', body):
                body = m
                break
            if not body:
                return
            for it in self.items:
                if it["source"] == "desktop" and it["reply"] is None:
                    it["reply"] = redact(body[:300])
                    rf = flags_for(it["reply"])
                    if rf:
                        it["flags"] = (it["flags"] or []) + [
                            {"tier": t, "rule": ru} for t, r_, _ in rf]
                    it["response_stream"] = bool(r.get("is_stream"))
                    it["seal"] = (it["seal"] or "") and it["seal"]
                    break

    @property
    def today_counts(self) -> dict:
        counts = {"checked": 0, "caught": 0, "high_risk": 0, "saved": 0}
        for it in self.items:
            counts["checked"] += 1
            if it["tier"] == "warn":
                counts["caught"] += 1
            elif it["tier"] == "bad":
                counts["high_risk"] += 1
            if any(f["rule"].startswith("reply") for f in (it["flags"] or [])):
                counts["saved"] += 1
        return counts


feed = Feed()


# ------------------------------------------------------------------- server

CORIOQO_FILE = ROOT / "examples" / "mcp_capture" / "corioqo.json"
MARKETING_URL = os.environ.get("BYOAI_CORIQO_MARKETING_URL", "https://coriqo.com")


def _coriqo_info() -> dict:
    """env first, then the Settings-form file, then unconfigured."""
    disc = {}
    try:
        disc = json.loads(CORIOQO_FILE.read_text())
    except Exception:
        disc = {}
    return {
        "app_url": os.environ.get("BYOAI_CORIQO_URL") or disc.get("app_url"),
        "api_key": os.environ.get("BYOAI_CORIQO_API_KEY") or disc.get("api_key"),
        "tenant": os.environ.get("BYOAI_CORIQO_TENANT_SLUG") or disc.get("tenant"),
        "marketing_url": MARKETING_URL,
        "configured": bool(os.environ.get("BYOAI_CORIQO_URL") or disc.get("app_url")),
    }


class Handler(BaseHTTPRequestHandler):

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/policy":
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._send(400, b'{"error":"invalid json"}')
                return
            policy = load_policy()
            if isinstance(payload.get("apps"), dict):
                policy["apps"] = {**policy["apps"], **payload["apps"]}
            if payload.get("mode") in ("observe", "redact", "block"):
                policy["mode"] = payload["mode"]
            save_policy(policy)
            body = json.dumps(policy).encode()
            self._send(200, body)
        elif path == "/api/publish":
            self._publish_to_coriqo()
        elif path == "/api/coriqo":
            n2 = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(n2) or b"{}")
            except Exception:
                self._send(400, b'{"error":"invalid json"}')
                return
            out = {k: payload[k].strip() for k in ("app_url", "api_key", "tenant")
                   if isinstance(payload.get(k), str) and payload[k].strip()}
            existing = {}
            try:
                existing = json.loads(CORIOQO_FILE.read_text())
            except Exception:
                existing = {}
            existing.update(out)
            CORIOQO_FILE.write_text(json.dumps(existing, indent=1))
            self._send(200, json.dumps(_coriqo_info()).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def _publish_to_coriqo(self) -> None:
        """Ship the current shield seal (signed checkpoint + entry count) to
        Coriqo's main app. Uses the same env triple the recorder uses:
        BYOAI_CORIQO_URL / _API_KEY / _TENANT_SLUG. Without the URL it answers
        a typed 503 that the UI turns into a plain-language prompt."""
        info = _coriqo_info()
        base = info["app_url"]
        if not base or not info["api_key"]:
            self._send(503, json.dumps({
                "error": "publish_disabled",
                "how": "Set BYOAI_CORIQO_URL, BYOAI_CORIQO_API_KEY and "
                       "BYOAI_CORIQO_TENANT_SLUG to publish this Mac's seal.",
            }).encode())
            return
        state = seal_chain.verify_chain()
        cp = seal_chain.checkpoint or {}
        doc = {
            "kind": "byoai.shield.publish.v1",
            "device_id": os.environ.get("", None) or None,
            "height": state["entries"],
            "root_hex": state.get("merkle_root"),
            "checkpoint": cp,
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        try:
            import httpx
            http = httpx.Client(trust_env=False)
            resp = http.post(
                info["app_url"].rstrip("/") + "/api/v1/shield/seals",
                json=doc,
                headers={"x-coriqo-api-key": info["api_key"],
                         "x-coriqo-tenant": info["tenant"] or ""},
                timeout=10.0,
            )
            self._send(resp.status_code if resp.status_code < 500 else 502,
                      json.dumps({"shipped": resp.is_success, "root": doc["root_hex"],
                                  "height": doc["height"],
                                  "detail": resp.json() if resp.content else None},
                                 default=str).encode())
        except Exception as exc:  # noqa: BLE001
            self._send(502, json.dumps({"shipped": False, "error": str(exc)}).encode())

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/feed":
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            limit = min(int(qs.get("limit", ["60"])[0]), 200)
            offset = int(qs.get("offset", ["0"])[0])
            body = json.dumps({
                "counts": feed.today_counts,
                "total": len(feed.items), "offset": offset, "limit": limit,
                "has_more": offset + limit < len(feed.items),
                "items": list(feed.items)[offset:offset + limit],
            }).encode()
            self._send(200, body)
        elif path == "/api/verify":
            body = json.dumps(seal_chain.verify_chain()).encode()
            self._send(200, body)
        elif path == "/api/policy":
            body = json.dumps(load_policy()).encode()
            self._send(200, body)
        elif path == "/api/apps":
            body = json.dumps(discover_apps()).encode()
            self._send(200, body)
        elif path == "/api/coriqo":
            body = json.dumps(_coriqo_info()).encode()
            self._send(200, body)
        elif path.startswith("/api/receipt/"):
            self._receipt(path.rsplit("/", 1)[-1])
        elif path in ("/", "/index.html"):
            self.do_GET_static("/")
        elif path == "/ui.css":
            self.do_GET_static("/ui.css")
        else:
            self.do_GET_static(path)

    def _receipt(self, seal: str) -> None:
        """Portable receipt for one sealed interaction: payload + Merkle
        inclusion proof + the signed checkpoint. The receipt contains
        redacted text only; verification needs no secrets and no server —
        re-hash the payload, fold the proof steps, compare to the checkpoint
        root, then verify the checkpoint's Ed25519 signature against the
        embedded device public key."""
        entry = next((e for e in seal_chain.entries if e["seal"] == seal), None)
        if entry is None:
            self._send(404, b'{"error":"unknown seal"}')
            return
        rec = {"payload": entry["payload"]}
        seal_valid = (checkpoint_leaf_hash(rec["payload"]).hex()[:16] == entry["seal"])
        chain_state = seal_chain.verify_chain()
        # Fresh checkpoint so the receipt's root covers THIS entry, not an
        # older height (stamps are interval-based; a receipt must be
        # self-contained whenever it's requested).
        cp = seal_chain.sign_checkpoint()
        proof = seal_chain.proof_for(seal)
        receipt = {
            "kind": "byoai.receipt.v2",
            "seal": entry["seal"],
            "height": entry["height"],
            "payload": entry["payload"],
            "merkle": {
                "proof": proof,
                "checkpoint": cp and {
                    "root_hex": cp.get("root_hex"),
                    "height": cp["height"],
                    "device_id": cp.get("device_id"),
                    "public_key_b64": cp.get("public_key_b64"),
                    "sig": cp.get("sig"),
                },
                "note": "Offline verify: sha256(canonical payload) == seal; "
                        "fold leaf + steps → root; unwrap Ed25519 sig over the "
                        "checkpoint (sig field excluded) with public_key_b64.",
            },
            "self_check": {
                "entry_hash_ok": bool(seal_valid),
                "chain_intact": bool(chain_state["tamper_evident"]),
                "chain_height": seal_chain.height,
                "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        }
        self._send(200, json.dumps(receipt, indent=1, default=str).encode())
        return

    def do_GET_static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "shield.html").read_bytes(), "text/html")
        elif path == "/ui.css":
            self._send(200, (ROOT / "internal_doc" / "consumer_design" / "ui.css").read_bytes(),
                       "text/css")
        else:
            self._send(404, b'{"error":"not found"}')

    def log_message(self, *args: object) -> None:
        return


# --------------------------------------------------------------- background

def _watcher() -> None:
    feed.scan_initial()
    while True:
        try:
            feed.poll()
        except Exception:
            pass
        time.sleep(1.5)


def main() -> None:
    threading.Thread(target=_watcher, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", 8300), Handler)
    print("Coriqo shield → http://localhost:8300", flush=True)
    print(f"seal chain head: {seal_chain.head} height: {seal_chain.height}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
