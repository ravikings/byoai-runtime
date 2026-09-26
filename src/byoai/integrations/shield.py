"""BYOAI capture shield — behavioral capture, verdicts, and tamper-evident
sealing as a first-class integration.

Schema-contract is intentionally shared with the captures demo
(``captures.jsonl``): feed rows are ``kind``-tagged records produced by the
MCP connector gateway and the desktop capture proxy. This module keeps the
behavioral rules in one place so the console (``web/``) and the demo UI read
the same analyzer.

    python -m byoai.integrations.shield          # serves UI + API on :17831

Components:

* flag rules (PII / HIGH / AGENT) — same regex rules as the demo
* local PII redaction — matched spans are replaced with compartment labels;
  only their sha256 survives inside the seal
* sealed interactions — RFC 6962 Merkle tree + per-device Ed25519 signed
  checkpoints (same primitives as :mod:`byoai.recorder.merkle` /
  :mod:`byoai.recorder.keys`), receipts that verify offline in the browser

The UI is the ``/shield`` route of the console app in ``web/``. This server
serves its build (``byoai/console_static/``) and answers the API both at
``/api/*`` and at ``/shield-api/*``, the prefix the app calls through the Vite
dev proxy, so one build works in dev and standalone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from byoai.recorder.keys import atomic_write_bytes, load_or_create_device_key
from byoai.recorder.merkle import MerkleTree, checkpoint_leaf_hash

# Shield's own port. Not 8300 (Consul), 8800 (the MCP demo gateway), 8080
# (the proxy) or any other number a dev stack likes; the browser extension
# ships the same default (browser_extension/background.js). Override with
# BYOAI_SHIELD_PORT and enter the same port in the extension popup.
import os as _os
import sys as _sys
DEFAULT_PORT = int(_os.environ.get("BYOAI_SHIELD_PORT", "17831"))

# What /api/identity signs (prefix + caller's nonce); the extension checks the
# same bytes. Changing it breaks pairing with installed extensions.
IDENTITY_PREFIX = b"byoai-shield-identity:"

MAX_INTERACTIONS = 400


# ------------------------------------------------- private files, portable
#
# Shield's files hold a usage record, and the seal chain is the evidence. They
# are private to the user by default (0600 on POSIX; on Windows the user-profile
# ACL already limits them to the user, SYSTEM and administrators), written
# atomically so a crash or a reader never sees half a file, and locked with the
# platform's own primitive so this runs on Windows and Linux as well as macOS.

import contextlib as _contextlib


def atomic_write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-directory temp file: fsync, then
    replace. Readers see the old file or the new one, never a torn one."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")  # 0600
    try:
        with _os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            _os.fsync(fh.fileno())
        _os.replace(tmp, path)
    except BaseException:
        with _contextlib.suppress(OSError):
            _os.unlink(tmp)
        raise


def harden_files(*paths: Path | None) -> None:
    """Tighten files Shield owns to owner-only. Best effort; POSIX only, since
    chmod means nothing on Windows."""
    if _os.name != "posix":
        return
    for path in paths:
        if path is not None and path.exists():
            with _contextlib.suppress(OSError):
                path.chmod(0o600)
    # Shield's own data folder is owner-only too. A folder the user chose (a
    # repo checkout, say) is left as they made it.
    with _contextlib.suppress(OSError):
        if DATA_DIR.is_dir():
            DATA_DIR.chmod(0o700)


@_contextlib.contextmanager
def file_lock(fh):
    """Exclusive lock on an open file, restored to its position on release."""
    if _os.name == "nt":
        import msvcrt
        pos = fh.tell()
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # retries ~10 s
                break
            except OSError:
                continue
        fh.seek(pos)
        try:
            yield
        finally:
            pos = fh.tell()
            fh.seek(0)
            with _contextlib.suppress(OSError):
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            fh.seek(pos)
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

PII_RULES = [
    ("emails",        re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)),
    ("cards",         re.compile(r"\b4[0-9]{12}(?:[0-9]{3})?\b|\b(5[1-5][0-9]{14})\b")),
    ("wallets",       re.compile(r"\b0x[a-fA-F0-9]{40}\b|\bbc1[a-z0-9]{25,}\b")),
    ("phone_numbers", re.compile(r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")),
    ("ssn_like",      re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("api_keys",      re.compile(r"\bsk-[a-zA-Z0-9]{16,}\b|\bBearer\s+[A-Za-z0-9._-]{20,}\b")),
    ("password_said", re.compile(r"\b(password|passphrase|credential)\b", re.I)),
]
HIGH_RULES = [
    ("executable_masquerade", re.compile(r"\b[\w.-]+\.(exe|scr|bat|cmd|dmg|msi|sh)\b", re.I)),
    ("credential_block", re.compile(r"(?i)\b(password|secret|api[_ ]?key)\s*[:=]")),
]
AGENT_RULES = [
    ("tool_intent", re.compile(r"\b(execute|execute_stream|tool_use|deploy|refund|wire ?transfer|send (the |my )?money|draft_email)\b", re.I)),
]
REPLY_RULES = [
    ("reply_pii_echo", re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|\b4[0-9]{12}(?:[0-9]{3})?\b")),
    ("reply_leak", re.compile(r"(?i)(your api key|secret[^\n]{0,24}=|\bsk-[a-zA-Z0-9]{16,})")),
    ("reply_toxic", re.compile(r"(?i)\b(stupid|idiot|hate|hope you die)\b")),
]

RULE_LABEL = {
    "emails": "email address", "cards": "card number", "wallets": "wallet address",
    "phone_numbers": "phone number", "ssn_like": "SSN-like number",
    "api_keys": "API key", "password_said": "credential mention",
    "executable_masquerade": "dangerous file", "credential_block": "credential text",
    "tool_intent": "agent action intent", "connector_tool_call": "agent tool call",
    "reply_pii_echo": "PII echoed in reply", "reply_leak": "secret echoed in reply",
    "reply_toxic": "harsh language in reply",
}
RULE_REDACT = {"emails": "[redacted-email]", "cards": "[redacted-card]",
               "wallets": "[redacted-wallet]", "phone_numbers": "[redacted-number]",
               "ssn_like": "[redacted-ssn]", "api_keys": "[redacted-token]"}
# password_said is a flag, not a secret: rewriting the word "password" out of
# "how do I reset my password" would break the question and protect nothing.

APP_LABEL = {"claude": "Claude", "chatgpt": "ChatGPT", "gemini": "Gemini",
             "copilot": "Copilot"}
# Apps whose chat traffic the capture proxy can read and redact. Gemini's web
# client sends form-encoded batch RPC and Copilot talks over a websocket, so
# neither can be governed yet; their toggles stay off and say why.
COVERED_APPS = ("claude", "chatgpt")

DATA_DIR = Path.home() / ".byoai" / "shield"
FINGERPRINT_KEY = DATA_DIR / "fingerprint.key"

# Ledger fields that carry message text. Privacy-first rows never write them;
# the scrub pass removes them from rows written before it.
TEXT_FIELDS = ("prompt", "preview", "input_preview", "delta_preview",
               "output_preview")
RETENTION_CHOICES = (7, 30, 90, 365)


@dataclass
class ShieldConfig:
    ledger: Path
    seal_path: Path
    key_dir: Path
    policy_path: Path | None = None
    sig_every: int = 8
    max_interactions: int = 400


def flags_for(text: str) -> list[tuple[str, str, str]]:
    flags: list[tuple[str, str, str]] = []
    for tier, rules in (("high", HIGH_RULES), ("pii", PII_RULES),
                        ("agent", AGENT_RULES), ("reply", REPLY_RULES)):
        for rule, pattern in rules:
            m = pattern.search(text)
            if m:
                flags.append((reply_tier(rule) if tier == "reply" else tier,
                              rule, m.group(0)[:60]))
    return flags


def reply_tier(rule: str) -> str:
    """A reply that echoes personal data or a secret is a privacy hit; harsh
    language is a conduct flag, not personal data."""
    return "conduct" if rule == "reply_toxic" else "pii"


def redact(text: str) -> str:
    for tier, p in PII_RULES:
        token = RULE_REDACT.get(tier)
        if token:
            text = p.sub(token, text)
    return text


def redact_with_rules(text: str) -> tuple[str, list[str]]:
    """Like :func:`redact`, also naming which rules rewrote something."""
    hit: list[str] = []
    for rule, p in PII_RULES:
        token = RULE_REDACT.get(rule)
        if token and p.search(text):
            text = p.sub(token, text)
            hit.append(rule)
    return text, hit


# ------------------------------------------------------------ privacy-first


def _fingerprint_key(path: Path = FINGERPRINT_KEY) -> bytes:
    """Per-device secret for message fingerprints, created on first use
    (mode 0600). Keyed so a fingerprint of a short message can't be reversed
    by hashing guesses."""
    import os
    import secrets
    try:
        return path.read_bytes()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # lost a creation race to the other process
            return path.read_bytes()
        key = secrets.token_bytes(32)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key


def fingerprint(text: str, key_path: Path = FINGERPRINT_KEY) -> str:
    import hmac
    mac = hmac.new(_fingerprint_key(key_path), text.encode("utf-8"),
                   hashlib.sha256)
    return "hmac-sha256:" + mac.hexdigest()


def inspect_text(text: str, policy: dict, *, rules: str = "request",
                 key_path: Path = FINGERPRINT_KEY) -> dict:
    """What a ledger row may hold about a message: rule hits, length and a
    keyed fingerprint. The redacted preview only when the admin opted in."""
    if rules == "reply":
        found = [(reply_tier(r), r) for r, p in REPLY_RULES if p.search(text)]
    else:
        found = [(t, r) for t, r, _ in flags_for(text)
                 if not r.startswith("reply_")]
    out = {
        "flags": [f"{t}:{r}" for t, r in found],
        "chars": len(text),
        "text_hmac": fingerprint(text, key_path) if text else None,
    }
    if policy.get("keep_text"):
        out["preview"] = redact(text)[:200]
    return out


def _content_text(content) -> str:
    """Text of one message's content in any shape we read: a string, a list
    of text blocks (Anthropic / OpenAI), or ChatGPT web's ``{"parts": [...]}``."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict) and isinstance(content.get("parts"), list):
        return "\n".join(x for x in content["parts"] if isinstance(x, str))
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content
                         if isinstance(b, dict)
                         and b.get("type") in ("text", "input_text"))
    return ""


def message_text(body: dict) -> str:
    """The human-typed part of a chat request body: claude.ai web ``prompt``,
    the last turn of a Messages / Chat Completions / ChatGPT web ``messages``
    list, or a Responses API ``input``."""
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return prompt
    messages = body.get("messages")
    if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
        return _content_text(messages[-1].get("content"))
    inp = body.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list) and inp and isinstance(inp[-1], dict):
        return _content_text(inp[-1].get("content"))
    return ""


def redact_json_body(raw: str) -> tuple[str, list[str]]:
    """Redact personal details inside the message fields of a JSON chat body.

    Only message text is rewritten: ``prompt``, ``input`` and the content of
    each ``messages[]`` / ``input[]`` turn (strings, text blocks, ChatGPT web
    ``parts``). Running the
    rules over the raw wire body would also hit conversation ids, timestamps
    and model names that happen to look like phone numbers. A body that isn't
    JSON is returned unchanged with no rules."""
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return raw, []
    if not isinstance(body, dict):
        return raw, []
    hit: set[str] = set()

    def fix(s: str) -> str:
        out, rules = redact_with_rules(s)
        hit.update(rules)
        return out

    def fix_content(content):
        if isinstance(content, str):
            return fix(content)
        if isinstance(content, dict) and isinstance(content.get("parts"), list):
            content["parts"] = [fix(x) if isinstance(x, str) else x
                                for x in content["parts"]]
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    block["text"] = fix(block["text"])
        return content

    for key in ("prompt", "input"):
        if isinstance(body.get(key), str):
            body[key] = fix(body[key])
    turns = [*(body.get("messages") or []),
             *(body["input"] if isinstance(body.get("input"), list) else [])]
    for msg in turns:
        if isinstance(msg, dict) and "content" in msg:
            msg["content"] = fix_content(msg["content"])
    if not hit:
        return raw, []
    return json.dumps(body, ensure_ascii=False), sorted(hit)


def append_row(ledger: Path, rec: dict) -> None:
    """Append one ledger row under an exclusive lock, so a scrub rewriting the
    file can't drop a row the proxy writes at the same moment."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    fd = _os.open(ledger, _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT, 0o600)
    with _os.fdopen(fd, "a") as fh:
        with file_lock(fh):
            fh.write(json.dumps(rec, default=str, sort_keys=True) + "\n")


class SealChainError(RuntimeError):
    pass


class SealChain:
    """RFC 6962-graded tamper evidence on recorder-core primitives.

    leaf = sha256(0x00 || canonical(payload)) per entry (via
    :data:`byoai.recorder.merkle.checkpoint_leaf_hash`), a binary MerkleTree
    over all leaves, and a checkpoint {root, height, device_id} Ed25519-signed
    with a locally-held DeviceKey (same trust root as recorder/receipts.py).

    Storage is an append-only log, one entry per line (``log_path``), plus a
    small state file (``cfg.seal_path``: latest checkpoint, incidents). An
    append is O(1), nothing is ever trimmed, and every entry back to the first
    is covered by the signed checkpoint. Only the newest ``WINDOW`` payloads
    are kept in memory; every leaf hash is, which is what the tree needs.
    Sealing is idempotent: a payload already in the chain is not added again,
    so replaying the ledger after a restart adds nothing.
    """

    WINDOW = 512

    def __init__(self, cfg: ShieldConfig) -> None:
        import threading
        self.cfg = cfg
        # The feed's watcher stamps entries while the Coriqo publisher and
        # receipt requests sign checkpoints, each on its own thread. One lock
        # keeps the entries, the checkpoint and the file in step.
        self._lock = threading.RLock()
        self._key = load_or_create_device_key(cfg.key_dir)
        self.entries: list[dict] = []        # newest WINDOW entries, with payloads
        self.checkpoint: dict | None = None
        self._leaves: list[bytes] = []       # every leaf, in order
        self._leaf_set: set[bytes] = set()
        self._tree: MerkleTree | None = None
        self._log_sig: tuple = (0, 0)
        # Things an examiner must be able to see, kept in the state file (a
        # reset that left no trace would be the easiest way to hide edits).
        self.incidents: list[dict] = []
        #: Stable random id of this local record, made once with the seal file
        #: and kept across restarts and window trims. A new id means the record
        #: was wiped or recreated; Coriqo compares it between sends.
        self.record_id: str = ""
        self._load()

    @property
    def log_path(self) -> Path:
        return self.cfg.seal_path.with_name(self.cfg.seal_path.stem + ".log.jsonl")

    @property
    def height(self) -> int:
        return len(self._leaves)

    @property
    def total(self) -> int:
        """Sealed entries ever. Only goes up, so a chain that comes back
        shorter than a witness remembers (the extension keeps one) has been
        rolled back or wiped."""
        return len(self._leaves)

    # -- persistence ------------------------------------------------------
    def _note(self, what: str, detail: str) -> None:
        self.incidents.append({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "what": what, "detail": detail})

    def _quarantine(self, path: Path, tag: str) -> Path:
        moved = path.with_name(f"{path.name}.{tag}-{int(time.time())}")
        _os.replace(path, moved)
        return moved

    def _scan_log(self):
        """Yield ``(entry, problem)`` for each log line, in order. A problem
        ends the scan: nothing after a bad line can be trusted. A torn final
        line (a crash mid-write) is dropped without a problem."""
        try:
            raw = self.log_path.read_bytes()
        except FileNotFoundError:
            return
        lines = raw.split(b"\n")
        torn = lines.pop() if lines and lines[-1] else b""   # no trailing newline
        del torn
        for i, line in enumerate(lines):
            if not line:
                continue
            try:
                e = json.loads(line)
                ok = (e["height"] == i + 1
                      and self._leaf(e["payload"]).hex() == e["leaf_hash"]
                      and e["leaf_hash"][:16] == e["seal"])
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                yield None, f"entry {i + 1} does not match its own hash"
                return
            yield e, None

    def _load(self) -> None:
        state: dict = {}
        if self.cfg.seal_path.exists():
            try:
                state = json.loads(self.cfg.seal_path.read_text())
            except Exception as exc:  # noqa: BLE001
                moved = self._quarantine(self.cfg.seal_path, "unreadable")
                self._note("chain_unreadable",
                           f"{type(exc).__name__}; the old file was kept as {moved.name}")
        self.incidents = list(state.get("incidents", [])) + self.incidents
        if "entries" in state:                # format 1: one JSON file, last 512
            self._migrate_v1(state)
            state = {"incidents": self.incidents, "record_id": state.get("record_id"),
                     "checkpoint": None}
        self.checkpoint = state.get("checkpoint")
        stored_id = state.get("record_id")
        self.record_id = stored_id if isinstance(stored_id, str) and stored_id else uuid.uuid4().hex
        good: list[dict] = []
        for entry, problem in self._scan_log():
            if problem:
                moved = self._quarantine(self.log_path, "broken")
                self._note("chain_broken", f"{problem}; the log was kept as {moved.name} "
                           f"and its {len(good)} intact entries carried over")
                self._rewrite_log(good)
                break
            good.append(entry)
        else:
            self._trim_torn_tail()
        self._leaves = [bytes.fromhex(e["leaf_hash"]) for e in good]
        self._leaf_set = set(self._leaves)
        self.entries = good[-self.WINDOW:]
        self._tree = None
        self._log_sig = _stat_sig(self.log_path)
        if self.checkpoint and self._checkpoint_matches() is False:
            self._note("checkpoint_mismatch",
                       "the signed checkpoint no longer matches the entries it covered "
                       "(entries were edited or removed while Shield was off)")
        self._persist()

    def _migrate_v1(self, state: dict) -> None:
        entries = state.get("entries") or []
        lines = []
        for i, e in enumerate(entries, 1):
            leaf = self._leaf(e["payload"])
            lines.append(json.dumps({"height": i, "payload": e["payload"],
                                     "seal": leaf.hex()[:16], "leaf_hash": leaf.hex()},
                                    sort_keys=True))
        if lines:
            atomic_write_private(self.log_path, "\n".join(lines) + "\n")

    def _rewrite_log(self, entries: list[dict]) -> None:
        atomic_write_private(self.log_path, "".join(
            json.dumps(e, sort_keys=True) + "\n" for e in entries))

    def _trim_torn_tail(self) -> None:
        try:
            raw = self.log_path.read_bytes()
        except FileNotFoundError:
            return
        if raw and not raw.endswith(b"\n"):
            keep = raw[: raw.rfind(b"\n") + 1]
            atomic_write_private(self.log_path, keep.decode("utf-8", "replace"))

    def _persist(self) -> None:
        atomic_write_private(self.cfg.seal_path, json.dumps({
            "format": 2, "record_id": self.record_id, "checkpoint": self.checkpoint,
            "total": self.total, "incidents": self.incidents,
        }, indent=1))

    def _append_line(self, entry: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        fd = _os.open(self.log_path, _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT, 0o600)
        with _os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            fh.flush()
            _os.fsync(fh.fileno())
        self._log_sig = _stat_sig(self.log_path)

    # -- chain ------------------------------------------------------------
    def _leaf(self, payload: dict) -> bytes:
        return checkpoint_leaf_hash(payload)

    def _rebuild_tree(self) -> None:
        self._tree = MerkleTree(self._leaves) if self._leaves else None

    def _leaf_index(self, seal: str) -> int:
        try:
            prefix = bytes.fromhex(seal)
        except ValueError:
            return -1
        for i, leaf in enumerate(self._leaves):
            if leaf.startswith(prefix):
                return i
        return -1

    def stamp(self, payload: dict) -> str:
        with self._lock:
            return self._stamp(payload)

    def _stamp(self, payload: dict) -> str:
        leaf = self._leaf(payload)
        if leaf in self._leaf_set:            # already sealed: replay adds nothing
            return leaf.hex()[:16]
        entry: dict = {"height": len(self._leaves) + 1, "payload": payload,
                       "seal": leaf.hex()[:16], "leaf_hash": leaf.hex()}
        self._append_line(entry)
        self._leaves.append(leaf)
        self._leaf_set.add(leaf)
        self.entries.append(entry)
        del self.entries[:-self.WINDOW]
        self._tree = None  # lazily rebuilt on demand
        if self.height % self.cfg.sig_every == 0:
            self.sign_checkpoint()
        else:
            self._persist()
        return entry["seal"]

    def sign_checkpoint(self) -> dict:
        with self._lock:
            return self._sign_checkpoint()

    def _sign_checkpoint(self) -> dict:
        if self._tree is None:
            self._rebuild_tree()
        assert self._tree is not None, "empty seal chain — at least one entry required"
        cp = {
            "kind": "byoai.shield.checkpoint.v1",
            "root_hex": self._tree.root.hex(),
            "height": self.height,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "device_id": self._key.device_id,
            "public_key_b64": self._key.public_key_b64,
        }
        cp["sig"] = self._key.sign(checkpoint_leaf_hash(cp))
        self.checkpoint = cp
        self._persist()
        return cp

    def _checkpoint_matches(self) -> bool | None:
        """Does the signed checkpoint still describe the entries it covered?
        The seal is an unkeyed hash, so someone who edits an entry can
        recompute it; what they can't redo is the signature."""
        cp = self.checkpoint
        if not cp:
            return None
        n = int(cp.get("height", 0))
        if n > len(self._leaves):
            return False                       # entries were cut off the end
        if n == 0:
            return None
        return MerkleTree(self._leaves[:n]).root.hex() == cp.get("root_hex")

    def verify_chain(self) -> dict:
        with self._lock:
            return self._verify_chain()

    def _verify_chain(self) -> dict:
        self._rebuild_tree()
        # If the log changed by anything but our own appends, re-read it all.
        if _stat_sig(self.log_path) != self._log_sig:
            for _entry, problem in self._scan_log():
                if problem:
                    self._note("chain_broken", f"{problem} (found while running)")
                    self._persist()
                    break
            else:
                fresh = [e["leaf_hash"] for e, _ in self._scan_log()]
                if fresh != [leaf.hex() for leaf in self._leaves]:
                    self._note("chain_changed", "the log was changed while Shield was running")
                    self._persist()
            self._log_sig = _stat_sig(self.log_path)
        for e in self.entries:
            if self._leaf(e["payload"]).hex()[:16] != e["seal"]:
                return {"tamper_evident": False, "broken_at": e["height"],
                        "reason": "content hash mismatch"}
        cp = self.checkpoint
        sig_ok = self._sig_valid(cp) if cp else None
        matches = self._checkpoint_matches()
        return {
            "tamper_evident": (cp is None or bool(sig_ok)) and matches is not False
                              and not self.incidents,
            "checkpoint_matches_entries": matches,
            "incidents": self.incidents,
            "sealed_total": self.total,
            "entries": self.height,
            "merkle_root": self._tree.root.hex() if self._tree else None,
            "checkpoint": None if not cp else {
                "root": cp.get("root_hex"), "height": cp["height"],
                "device_id": cp.get("device_id"), "sig_valid": sig_ok,
            },
        }

    def _sig_valid(self, cp: dict) -> bool:
        return self._key.verify(cp["public_key_b64"],
                                   checkpoint_leaf_hash(
                                       {k: v for k, v in cp.items() if k != "sig"}),
                                   cp.get("sig", ""))

    def _find_entry(self, seal: str) -> dict | None:
        found = next((e for e in self.entries if e["seal"] == seal), None)
        if found is not None:
            return found
        for e, _problem in self._scan_log():   # older than the in-memory window
            if e and e["seal"] == seal:
                return e
        return None

    def count_with_text(self) -> int:
        """Entries whose payload carries a redacted preview (keep_text was on)."""
        return sum(1 for e, _ in self._scan_log()
                   if e and (e.get("payload") or {}).get("text_redacted"))

    def proof_for(self, seal: str) -> dict | None:
        idx = self._leaf_index(seal)
        if idx < 0:
            return None
        if self._tree is None:
            self._rebuild_tree()
        if self._tree is None:
            return None
        p = self._tree.proof(idx)
        return {
            "leaf_index": p.leaf_index,
            "leaf_hash": p.leaf_hash.hex(),
            "steps": [{"sibling": s.sibling.hex(), "side": s.side}
                      for s in p.steps],
            "root_hex": p.root.hex(),
        }

    def receipt(self, seal: str) -> dict | None:
        """Portable receipt — verifies fully offline given the checkpoint."""
        entry = self._find_entry(seal)
        if entry is None:
            return None
        cp = self.sign_checkpoint()  # fresh, so the root covers this entry
        return {
            "kind": "byoai.receipt.v2",
            "seal": entry["seal"], "height": entry["height"],
            "payload": entry["payload"],
            "merkle": {
                "proof": self.proof_for(seal),
                "checkpoint": {
                    "root_hex": cp["root_hex"], "height": cp["height"],
                    "device_id": cp["device_id"],
                    "public_key_b64": cp["public_key_b64"], "sig": cp["sig"],
                },
                "note": "Offline verify: sha256(0x00||canonical(payload)) == "
                        "seal; fold leaf + steps → root; unwrap Ed25519 sig over "
                        "the checkpoint (sig excluded) with public_key_b64.",
            },
            "self_check": {
                "entry_hash_ok": self._leaf(entry["payload"]).hex()[:16] == entry["seal"],
                "chain_intact": self.verify_chain()["tamper_evident"],
                "chain_height": self.height,
                "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        }

    def root_hex(self) -> str | None:
        with self._lock:
            return self._root_hex()

    def _root_hex(self) -> str | None:
        self._rebuild_tree()
        return self._tree.root.hex() if self._tree else None


def receipt_kind(cfg: ShieldConfig) -> str:  # tiny helper for console wiring
    return "byoai.receipt.v2"


# Policy files written before privacy-first carry no version. The old UI saved
# its own default ("observe") into them, so a stored "observe" there is not a
# choice anyone made: those files are read as privacy-first.
POLICY_VERSION = 2


def default_policy() -> dict:
    """Privacy-first defaults: redact before sending, keep no message text,
    30 days of history, notice on."""
    return {
        "policy_version": POLICY_VERSION,
        "mode": "redact",
        "apps": {"claude": True, "chatgpt": False, "gemini": False,
                 "copilot": False},
        "keep_text": False,
        "retention_days": 30,
        "notice": True,
    }


def merge_policy(disc: dict) -> dict:
    p = {**default_policy(), **disc}
    p["apps"] = {**default_policy()["apps"], **(disc.get("apps") or {})}
    return p


def load_policy(cfg: ShieldConfig) -> dict:
    return read_policy(cfg.policy_path)


def upgrade_policy(disc: dict) -> dict:
    """Bring a pre-privacy-first file up to the privacy-first defaults: redact
    (block stays block, it is stricter) and no stored text. App toggles and
    other choices are kept."""
    version = disc.get("policy_version")
    if isinstance(version, int) and version >= POLICY_VERSION:
        return disc  # current, or written by a newer Shield: leave it alone
    out = dict(disc)
    if out.get("mode") != "block":
        out["mode"] = "redact"
    out["keep_text"] = False
    out["policy_version"] = POLICY_VERSION
    return out


def read_policy(path: Path | None) -> dict:
    if path and path.exists():
        try:
            return merge_policy(upgrade_policy(json.loads(path.read_text())))
        except Exception:
            return default_policy()
    return default_policy()


def is_less_private(policy: dict) -> bool:
    """Record-only mode or stored previews: weaker than the default."""
    return policy.get("mode") == "observe" or bool(policy.get("keep_text"))


# The capture proxy writes this next to the ledger while it runs (pid and
# listen port) and removes it on a clean stop. Shield reads it to know whether
# anything is checking traffic right now.
PROXY_ALIVE_FILE = "shield_proxy.alive"


def proxy_alive_path(ledger: Path) -> Path:
    return Path(ledger).parent / PROXY_ALIVE_FILE


def _pid_running(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _port_open(port: int) -> bool:
    """Something is listening on 127.0.0.1:port."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def read_proxy_alive(ledger: Path) -> dict | None:
    """The running proxy's marker, or None when no proxy is running (no file,
    unreadable, its process is gone after a crash, or nothing listens on its
    port). The port check matters: after a crash macOS can hand the old pid to
    another process, and a pid check alone would then claim protection."""
    try:
        data = json.loads(proxy_alive_path(ledger).read_text())
        pid = int(data.get("pid"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if pid <= 0 or not _pid_running(pid):
        return None
    port = data.get("port")
    if isinstance(port, int) and not _port_open(port):
        return None
    return data


def macos_https_proxy() -> tuple[bool, int | None] | None:
    """(enabled, port) of this Mac's system HTTPS proxy from ``scutil
    --proxy``, or None when that can't be read (not a Mac, scutil missing)."""
    import subprocess
    import sys
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                             text=True, timeout=3, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fields = dict(m.groups() for m in re.finditer(r"(\w+)\s*:\s*(\S+)", out))
    port = fields.get("HTTPSPort")
    return (fields.get("HTTPSEnable") == "1",
            int(port) if port and port.isdigit() else None)


def protection_state(cfg: ShieldConfig, *, system_proxy=macos_https_proxy) -> dict:
    """Is Shield checking traffic right now? Read from what is running, not
    from what was configured.

    Protecting means all of: the capture proxy is running; this Mac's system
    HTTPS proxy points at it (when that can be read); the policy redacts or
    blocks (record-only watches but checks nothing); and at least one app it
    can read is switched on. The browser extension does not count: it records
    that a chat happened, it never reads or changes what is sent.

    ``reasons`` are short machine codes for Coriqo, empty when protecting."""
    policy = load_policy(cfg)
    reasons: list[str] = []
    alive = read_proxy_alive(cfg.ledger)
    if alive is None:
        reasons.append("capture_stopped")
    else:
        sysproxy = system_proxy()
        if sysproxy is not None:
            enabled, port = sysproxy
            want = alive.get("port")
            if not enabled or (want and port and int(want) != port):
                reasons.append("proxy_off")
    if policy.get("mode") == "observe":
        reasons.append("policy_monitor_only")
    if not any(policy["apps"].get(app) for app in COVERED_APPS):
        reasons.append("no_apps_enabled")
    return {"protecting": not reasons, "reasons": reasons,
            "mode": policy.get("mode")}


def apply_policy_update(policy: dict, payload: dict) -> dict:
    """Validated merge of a Settings write. Raises ValueError naming the first
    bad field; nothing is saved in that case.

    A change that makes Shield less private than it is now (record-only mode,
    or keeping previews) must carry ``"acknowledge": "less_private"``: the
    screen asks first, and a script can't weaken it by accident."""
    out = merge_policy(policy)
    if "mode" in payload:
        if payload["mode"] not in ("observe", "redact", "block"):
            raise ValueError("mode must be observe, redact or block")
        out["mode"] = payload["mode"]
    if "apps" in payload:
        if not isinstance(payload["apps"], dict) or not all(
                isinstance(v, bool) for v in payload["apps"].values()):
            raise ValueError("apps must map app names to true/false")
        for app, on in payload["apps"].items():
            if on and app not in COVERED_APPS:
                raise ValueError(f"Shield can't read {APP_LABEL.get(app, app)} "
                                 "messages yet, so it can't be turned on")
        out["apps"] = {**out["apps"], **payload["apps"]}
    for key in ("keep_text", "notice"):
        if key in payload:
            if not isinstance(payload[key], bool):
                raise ValueError(f"{key} must be true or false")
            out[key] = payload[key]
    if "retention_days" in payload:
        if payload["retention_days"] not in RETENTION_CHOICES:
            raise ValueError("retention_days must be one of "
                             + ", ".join(map(str, RETENTION_CHOICES)))
        out["retention_days"] = payload["retention_days"]
    weakened = ((out["mode"] == "observe" and policy.get("mode") != "observe")
                or (out["keep_text"] and not policy.get("keep_text")))
    if weakened and payload.get("acknowledge") != "less_private":
        raise ValueError("This makes Shield less private than it is now; "
                         "confirm it to save (acknowledge: less_private)")
    return out


def _row_date(row: dict) -> str:
    return (row.get("wall_clock") or "")[:10]


def _cutoff(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))


def _read_rows(ledger: Path) -> list[dict]:
    try:
        lines = ledger.read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def privacy_report(cfg: ShieldConfig, seals: "SealChain | None" = None) -> dict:
    """Facts for the "What Shield keeps" panel, read from disk, not assumed."""
    policy = load_policy(cfg)
    rows = _read_rows(cfg.ledger)
    cutoff = _cutoff(policy["retention_days"])
    dates = [d for d in (_row_date(r) for r in rows) if d]
    sealed_with_text = 0
    if seals is not None:
        sealed_with_text = seals.count_with_text()
    return {
        "ledger_rows": len(rows),
        "rows_with_text": sum(1 for r in rows
                              if any(r.get(f) for f in TEXT_FIELDS)),
        "oldest_row": min(dates) if dates else None,
        "rows_past_retention": sum(1 for d in dates if d < cutoff),
        "sealed_with_text": sealed_with_text,
        "keep_text": policy["keep_text"],
        "retention_days": policy["retention_days"],
        "mode": policy["mode"],
        "less_private": is_less_private(policy),
        # "Where your data lives": real paths, not a description of them.
        "ledger_path": str(cfg.ledger.resolve()),
        "seal_path": str(cfg.seal_path.resolve()),
        "device_id": seals._key.device_id if seals is not None else None,
    }


def scrub_ledger(cfg: ShieldConfig, key_path: Path = FINGERPRINT_KEY, *,
                 scrub_text: bool = True) -> dict:
    """Replace stored message text with fingerprint + length, and delete rows
    past retention (``scrub_text=False``: retention only, as at startup). Holds the ledger lock for the whole rewrite so the proxy
    can't append into a file that is about to be replaced."""
    import os
    policy = load_policy(cfg)
    cutoff = _cutoff(policy["retention_days"])
    scrubbed = deleted = 0
    if not cfg.ledger.exists():
        return {"scrubbed": 0, "deleted": 0, "ledger_rows": 0}
    with cfg.ledger.open("r+") as fh:
        with file_lock(fh):
            kept: list[str] = []
            for line in fh.read().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if _row_date(row) and _row_date(row) < cutoff:
                    deleted += 1
                    continue
                text = next((row[f] for f in TEXT_FIELDS
                             if isinstance(row.get(f), str) and row[f]), None)
                if text is not None and scrub_text:
                    if not row.get("flags"):
                        row["flags"] = [f"{t}:{r}" for t, r, _ in flags_for(text)]
                    row["text_hmac"] = fingerprint(text, key_path)
                    for f in TEXT_FIELDS:
                        row.pop(f, None)
                    row["scrubbed"] = True
                    scrubbed += 1
                kept.append(json.dumps(row, default=str, sort_keys=True))
            # Rewrite in place, not via a temp file + rename: an appender
            # waiting on this lock holds the same inode and, being O_APPEND,
            # writes after our new end. A rename would leave it writing into
            # the unlinked old file.
            fh.seek(0)
            fh.write("\n".join(kept) + ("\n" if kept else ""))
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())
    return {"scrubbed": scrubbed, "deleted": deleted, "ledger_rows": len(kept)}


def save_policy(cfg: ShieldConfig, p: dict) -> None:
    if cfg.policy_path:
        atomic_write_private(cfg.policy_path, json.dumps(p, indent=1))


# --------------------------------------------------------------------- feed


def _flags_from_row(r: dict) -> list[tuple[str, str, str]]:
    out = []
    for f in r.get("flags") or []:
        tier, _, rule = str(f).partition(":")
        if rule:
            out.append((tier if tier in ("high", "pii", "agent", "conduct")
                        else "pii", rule, ""))
    return out


def _chars(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    return "1 character" if n == 1 else f"{n:,} characters"


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str) and v:
        return v.split(",")
    return []


def _tier(item: dict) -> str:
    if item.get("status") == "blocked" or any(
            f["tier"] == "high" for f in item["flags"]):
        return "bad"
    if any(f["tier"] in ("pii", "conduct") for f in item["flags"]):
        return "warn"
    return "ok"


class Interaction(dict):
    """One user-visible activity event, already flag-evaluated."""


def _stat_sig(path) -> tuple:
    if path is None:
        return (0, 0)
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


class Feed:
    """Turns ledger rows into shield interactions and re-flags replies."""

    def __init__(self, cfg: ShieldConfig) -> None:
        self.cfg = cfg
        self.seals = SealChain(cfg)
        self.items: deque = deque(maxlen=cfg.max_interactions)
        self._offset = 0
        self._policy = load_policy(cfg)
        self._scanned = False
        self._poll_lock = threading.Lock()
        self._seen_rows: dict[str, int] = {}
        self._current_id = ""
        # The browser extension never reads message text, so it can't supply
        # rule hits — it always ships length only. Recorded clean here; if the
        # request also crosses the desktop proxy for the same app, that side
        # (with the wire body) does the flagging.
        self.BROWSER_FLAGS: list[tuple[str, str, str]] = []

    def scan_initial(self) -> int:
        self._scanned = True
        self._policy = load_policy(self.cfg)
        try:
            lines = self.cfg.ledger.read_text().splitlines()
        except OSError:
            return 0
        for x in lines:
            try:
                self._absorb(json.loads(x))
            except ValueError:
                continue
        self._offset = len(lines)
        return len(self.items)

    def resync(self) -> None:
        """After a scrub rewrote the ledger, continue from its new end."""
        try:
            self._offset = len(self.cfg.ledger.read_text().splitlines())
        except OSError:
            self._offset = 0

    def poll(self) -> int:
        # A watcher thread can start before the first scan; reading from
        # offset 0 then would absorb (and seal) every row twice.
        with self._poll_lock:
            return self._poll_locked()

    def _poll_locked(self) -> int:
        if not self._scanned:
            return 0
        # Idle is the common case: two stat() calls, no read, no parse.
        sig = (_stat_sig(self.cfg.ledger), _stat_sig(self.cfg.policy_path))
        if sig == getattr(self, "_poll_sig", None):
            return 0
        self._poll_sig = sig
        self._policy = load_policy(self.cfg)
        try:
            lines = self.cfg.ledger.read_text().splitlines()
        except OSError:
            return 0
        if len(lines) < self._offset:  # rewritten under us (scrub/retention)
            self._offset = len(lines)
            return 0
        if len(lines) == self._offset:
            return 0
        new = [l for l in lines[self._offset:] if l.strip()]
        self._offset = len(lines)
        for l in new:
            try:
                self._absorb(json.loads(l))
            except Exception:
                continue
        return len(new)

    def _row_id_for(self, r: dict) -> str:
        """The same ledger row gets the same interaction id on every restart,
        so sealing it again is a no-op. Text and scrub markers are left out of
        the key: a scrub rewrites them, and must not make the row look new."""
        skip = set(TEXT_FIELDS) | {"text_hmac", "scrubbed", "flags"}
        key = json.dumps({k: v for k, v in r.items() if k not in skip},
                         sort_keys=True, default=str)
        n = self._seen_rows[key] = self._seen_rows.get(key, 0) + 1
        return hashlib.sha256(f"{key}#{n}".encode()).hexdigest()[:10]

    def _absorb(self, r: dict) -> None:
        self._current_id = self._row_id_for(r)
        kind = r.get("kind", "")
        wall = r.get("wall_clock", "")
        ts = wall.split()[-1] if wall else ""
        date = wall[:10] or time.strftime("%Y-%m-%d")
        keep_text = bool(self._policy.get("keep_text"))
        if kind in ("desktop.chat.request", "desktop.verdict.denied"):
            app = r.get("app") or "claude"
            label = APP_LABEL.get(app, app)
            legacy = r.get("prompt") if isinstance(r.get("prompt"), str) else None
            if legacy is not None and not legacy:
                return  # pre-PF row with nothing typed (a retry, a ping)
            flags_box = (_flags_from_row(r) if legacy is None
                         else flags_for(legacy))
            # Pre-PF rows logged prompt_chars as the whole wire body and kept a
            # 200-char cut of the prompt, so neither is the message length.
            chars = r.get("chars") if legacy is None else None
            shown = r.get("preview") if keep_text else None
            if shown is None and keep_text and legacy:
                shown = redact(legacy)[:200]
            blocked = kind == "desktop.verdict.denied"
            verb = shown or (f"Message to {label} stopped on this Mac"
                             if blocked else
                             f"Message to {label} · {_chars(chars)}"
                             if chars is not None else
                             f"Message to {label} · sent before privacy settings")
            self._add(source="desktop", surface=f"{label} · desktop", ts=ts,
                      verb=verb, date=date, flags_box=flags_box,
                      status="blocked" if blocked else "answered",
                      verdict=r.get("verdict"), text_hmac=r.get("text_hmac"),
                      chars=chars, redactions=r.get("redactions"))
        elif kind == "desktop.chat.response":
            self._attach_reply(r)
        elif kind in ("browser.chat.request", "browser.chat.status"):
            app = r.get("app") or "claude"
            label = APP_LABEL.get(app, app)
            wire = r.get("wire") if isinstance(r.get("wire"), str) else None
            ts_raw = r.get("sent_at") or ""
            # Extension rows carry their own ISO timestamp; fall back to now.
            try:
                when = ts_raw.replace("Z", "+00:00")
                import datetime as _dt
                parsed = _dt.datetime.fromisoformat(when)
                ts = parsed.strftime("%H:%M:%S.%f")
                ts = ts[:ts.index('.')] if '.' in ts else ts
                date = parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass
            status = ("answered" if r.get("ok") else "failed"
                      if kind == "browser.chat.status" and "ok" in r
                      else "answered")
            # Browser sends stop here only for a fetch that failed; statuses
            # attach to the latest browser request from the same app.
            if kind == "browser.chat.status":
                for it in reversed(list(self.items)):
                    if it["source"] == "browser" and it["surface"].startswith(label):
                        if status == "failed":
                            it["status"] = "failed"
                            it["tier"] = _tier(it)
                        break
                return
            first = self._is_first_browser_send(label)
            verb = (f"First message to {label} in this browser"
                    if first else f"Message to {label}")
            self._add(source="browser", surface=f"{label} · browser", ts=ts,
                      verb=verb, date=date, flags_box=self.BROWSER_FLAGS,
                      status=status, text_hmac=r.get("text_hmac"),
                      chars=r.get("chars"))
        elif kind == "tool.call":
            ident = r.get("identity") or {}
            tool = r.get("tool") or "execute"
            legacy = r.get("input_preview")
            legacy = legacy if isinstance(legacy, str) else None
            flags_box = (_flags_from_row(r) if legacy is None
                         else flags_for(legacy))
            chars = r.get("chars") or r.get("input_chars") or 0
            shown = r.get("preview") if keep_text else None
            if shown is None and keep_text and legacy:
                shown = redact(legacy)
            self._add(source="mcp",
                      surface=ident.get("client_name") or "MCP client", ts=ts,
                      verb=shown or f"{tool} call · {_chars(chars)}",
                      date=date, status="running", tool=tool, identity=ident,
                      flags_box=flags_box, text_hmac=r.get("text_hmac"),
                      chars=chars)
            self.items[0]["call_id"] = r.get("call_id")
        elif kind in ("tool.result", "tool.error"):
            # Seal on completion, so the seal binds the outcome too (and a
            # finished row always carries a seal).
            self.close_seal(r.get("tool"), r.get("identity") or {},
                            r.get("usage") or {}, r.get("latency_ms"),
                            call_id=r.get("call_id"),
                            status="failed" if kind == "tool.error" else "answered")

    def _is_first_browser_send(self, label: str) -> bool:
        return not any(
            it["source"] == "browser" and it["surface"].startswith(label)
            for it in self.items)

    def _add(self, *, source, surface, ts, verb, date, status="answered",
             tool=None, identity=None, flags_box, verdict=None, text_hmac=None,
             chars=None, redactions=None):
        item = {
            "id": self._current_id, "source": source, "surface": surface,
            "ts": ts, "date": date, "verb": redact(verb), "status": status,
            "tool": tool, "identity": identity or {},
            "flags": [{"tier": t, "rule": ru} for t, ru, _ in flags_box],
            "seal": "", "usage": {}, "latency_ms": None,
            "verdict": verdict, "chars": chars,
            "redactions": _as_list(redactions), "reply": None,
        }
        item["tier"] = _tier(item)
        # The seal binds what happened, not what was said: the message
        # fingerprint and length stand in for its text unless the admin
        # opted into previews (then the redacted preview is what's shown).
        item["_seal_payload"] = {
            "interaction": item["id"], "date": date, "ts": ts,
            "surface": surface, "status": status, "verdict": verdict,
            "flags": [f["tier"] + ":" + f["rule"] for f in item["flags"]],
            "text_hmac": text_hmac, "chars": chars,
            "identity": item["identity"], "usage": {},
        }
        if self._policy.get("keep_text"):
            item["_seal_payload"]["text_redacted"] = item["verb"][:400]
        item["seal"] = (self.seals.stamp(item["_seal_payload"])
                        if item["status"] != "running" else "")
        # tool.call rows are resealed when their result lands (Feed.close_seal)
        item["_pending"] = item["status"] == "running"
        self.items.appendleft(item)

    def close_seal(self, tool: str | None, ident: dict, usage: dict,
                   latency_ms, *, call_id: str | None = None,
                   status: str = "answered") -> None:
        """Reseal the matching running tool interaction once its result lands,
        so the seal binds the full interaction outcome, not the request only.

        Matched by ``call_id`` when the gateway sent one. Rows without it fall
        back to the *oldest* running call for the same tool and user; items
        are newest-first, and picking the newest would hand call 1's result to
        call 2 whenever two calls overlap."""
        running = [it for it in self.items
                   if it["source"] == "mcp" and it.get("status") == "running"]
        if call_id:
            match = next((it for it in running if it.get("call_id") == call_id),
                         None)
        else:
            match = next((it for it in reversed(running)
                          if it["tool"] == tool
                          and (it["identity"] or {}).get("user_id")
                          == ident.get("user_id")), None)
        if match is None:
            return
        match["status"] = status
        match["response_stream"] = tool == "execute_stream"
        match["usage"] = usage or {}
        match["latency_ms"] = latency_ms
        match["tier"] = _tier(match)
        payload = {**match["_seal_payload"], "status": status,
                   "usage": match["usage"]}
        match["seal"] = self.seals.stamp(payload)
        match["_pending"] = False

    def _attach_reply(self, r: dict) -> None:
        """Reply-side rule hits land on the latest desktop message from the
        same app; the reply's text is never shown or sealed."""
        app = r.get("app") or "claude"
        flags = _flags_from_row(r) if r.get("flags") is not None else [
            (reply_tier(ru), ru, "") for ru, p in REPLY_RULES
            if isinstance(r.get("preview"), str) and p.search(r["preview"])]
        for it in self.items:
            if it["source"] != "desktop" or it["reply"] is not None:
                continue
            if not it["surface"].startswith(APP_LABEL.get(app, app)):
                continue
            it["reply"] = f"Reply · {_chars(r.get('chars'))}" if r.get("chars") \
                else "Reply received"
            if flags:
                seen = {f["rule"] for f in it["flags"]}
                it["flags"] += [{"tier": t, "rule": ru} for t, ru, _ in flags
                                if ru not in seen]
                it["tier"] = _tier(it)
                it["seal"] = self.seals.stamp({
                    **it["_seal_payload"],
                    "flags": [f["tier"] + ":" + f["rule"] for f in it["flags"]],
                    "reply_hmac": r.get("text_hmac"),
                })
            break

    def counts(self) -> dict:
        counts = {"checked": len(self.items), "caught": 0, "high_risk": 0}
        for it in self.items:
            if it["tier"] == "warn":
                counts["caught"] += 1
            elif it["tier"] == "bad":
                counts["high_risk"] += 1
        return counts


# --------------------------------------------------------------------- server


# ------------------------------------------------------------ request guard
#
# The shield listens on localhost, but every web page the user opens can send
# requests to localhost too. Without these checks a page could POST
# {"mode": "observe", "acknowledge": "less_private"} and quietly weaken
# Shield, rewrite the Coriqo connection, or write fake rows into the sealed
# ledger through /api/browser. A page can't forge Origin or Host, and a
# cross-site JSON POST needs a CORS preflight this server never approves.

LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")
EXTENSION_SCHEMES = ("chrome-extension://", "moz-extension://")
BROWSER_KINDS = ("browser.chat.request", "browser.chat.status")

# Hosts the browser extension watches. The relay covers the whole page even
# when the send itself bypassed fetch, so a bare host must classify like the
# tagged capture does.
_APP_TAG_FOR_HOST = {
    "claude.ai": "claude",
    "chatgpt.com": "chatgpt",
    "chat.openai.com": "chatgpt",
    "gemini.google.com": "gemini",
    "copilot.microsoft.com": "copilot",
}


def _host_name(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[: value.find("]") + 1]
    return value.split(":", 1)[0]


def request_problem(method: str, path: str, headers) -> str | None:
    """Why a request must be refused, or None. ``headers`` is anything with
    ``.get`` (an HTTP message or a dict)."""
    import os
    from urllib.parse import urlsplit
    if _host_name(headers.get("Host", "")) not in LOCAL_HOSTS:
        return "Shield only answers requests addressed to localhost."
    if method != "POST":
        return None
    ctype = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return "Send JSON with Content-Type: application/json."
    origin = (headers.get("Origin") or "").strip()
    if path == "/api/browser":
        if not origin.startswith(EXTENSION_SCHEMES):
            return "Only the Shield browser extension can send browser rows."
        pinned = [x.strip() for x in os.environ.get(
            "BYOAI_SHIELD_EXTENSION_IDS", "").split(",") if x.strip()]
        if pinned and origin.split("://", 1)[1].rstrip("/") not in pinned:
            return "This browser extension isn't the one Shield was set up to trust."
        return None
    # Settings writes come from Shield's own page (or a local dev server
    # proxying to it), never from another site. "Localhost" alone is not the
    # rule: anything else on this Mac (a dev server, a daemon) is also a
    # localhost origin, and letting localhost:* write policy hands every
    # local process the keys. The Origin must name the same host:port this
    # request was addressed to — Shield's own page in production, the Vite
    # dev server proxying in dev.
    if origin:
        if urlsplit(origin).netloc.lower() != (headers.get("Host") or "").strip().lower():
            return "Settings can only be changed from Shield's own page."
    return None


def clean_browser_row(row: object) -> dict | None:
    """Keep only the fields a browser row is allowed to carry. Anything else,
    message text included, is dropped before it can reach the ledger."""
    if not isinstance(row, dict) or row.get("kind") not in BROWSER_KINDS:
        return None
    out: dict = {"kind": row["kind"],
                 "wall_clock": time.strftime("%Y-%m-%d %H:%M:%S")}
    app = row.get("app")
    # The relay publishes the host (`location.host`) because only it knows it
    # reliably; the fetch-patch path sends the app tag directly. Map known
    # hosts to apps so every path lands under the right surface.
    app = _APP_TAG_FOR_HOST.get(app) or app
    if app in APP_LABEL:
        out["app"] = app
    chars = row.get("chars")
    if isinstance(chars, int) and not isinstance(chars, bool) and 0 <= chars < 10_000_000:
        out["chars"] = chars
    for key, limit in (("wire", 60), ("sent_at", 40)):
        if isinstance(row.get(key), str):
            out[key] = row[key][:limit]
    # Dedupe key for the extension's retry batches. It's an opaque id —
    # random at the source, carries nothing about the user.
    if isinstance(row.get("row_id"), str) and 8 <= len(row["row_id"]) <= 64:
        out["row_id"] = row["row_id"]
    if isinstance(row.get("ok"), bool):
        out["ok"] = row["ok"]
    if isinstance(row.get("status"), int) and not isinstance(row.get("status"), bool):
        out["status"] = row["status"]
    return out


def serve(cfg: ShieldConfig, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          feed: Feed | None = None) -> None:
    """Run the capture shield (API + static UI) until interrupted."""
    feed = feed or Feed(cfg)
    if cfg.policy_path and cfg.policy_path.exists():
        try:
            disc = json.loads(cfg.policy_path.read_text())
        except ValueError:
            disc = None
        if isinstance(disc, dict) and upgrade_policy(disc) is not disc:
            save_policy(cfg, merge_policy(upgrade_policy(disc)))
            print("policy: upgraded to privacy-first defaults (redact, no stored "
                  "text); app choices kept", flush=True)
    from byoai.integrations.shield_publish import Publisher
    publisher = Publisher(feed.seals, cfg.key_dir, cfg.key_dir.parent,
                          protection=lambda: protection_state(cfg))
    publisher.start()  # background; sends only when enrolled and due
    pruned = scrub_ledger(cfg, scrub_text=False)["deleted"]
    if pruned:
        print(f"retention: deleted {pruned} ledger rows past "
              f"{load_policy(cfg)['retention_days']} days", flush=True)
    feed.scan_initial()

    # Dedupe across the extension's batch retries; shared by all requests.
    browser_dedupe = {"ids": set(), "order": []}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json",
                  cache="no-store"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _apps(self) -> dict:
            """Nontechnical-first app discovery: scan /Applications per
            request — cheap, no daemon, no root. Policy ON starts from the
            user's saved form; discovery never widens it silently."""
            import os
            try:
                apps = [q for q in os.listdir("/Applications") if q.endswith(".app")]
            except OSError:
                apps = []
            bundles = {
                "claude": ("Claude.app", "Claude Code.app"),
                "chatgpt": ("ChatGPT.app",),
                "gemini": ("Gemini.app", "Google Gemini.app"),
                "copilot": ("Microsoft Copilot.app", "Copilot.app"),
            }
            return {tag: any(b in apps for b in v) for tag, v in bundles.items()}

        def _coriqo(self) -> dict:
            """Connection and publishing status for Settings, plus the
            marketing link. Nothing secret is stored or returned: the Mac is
            identified by its own key after a one-time enrolment."""
            import os
            return {**publisher.status(),
                    "marketing_url": os.environ.get("BYOAI_CORIQO_MARKETING_URL")
                    or "https://coriqo.com"}

        def _api(self):
            from urllib.parse import urlparse, parse_qs
            path = self.path.split("?")[0]
            if path == "/api/feed":
                qs = parse_qs(urlparse(self.path).query)
                limit = min(int(qs.get("limit", ["60"])[0]), 200)
                offset = int(qs.get("offset", ["0"])[0])
                page = [{k: v for k, v in it.items() if not k.startswith("_")}
                        for it in list(feed.items)[offset:offset + limit]]
                return {"counts": feed.counts(),
                        "total": len(feed.items), "offset": offset,
                        "limit": limit, "has_more": offset + limit < len(feed.items),
                        "items": page}
            if path == "/api/verify":
                return feed.seals.verify_chain()
            if path == "/api/identity":
                # Proof that this is the Shield holding this device's key, not
                # another process that got to the port first. The caller picks
                # the nonce, so a recorded answer can't be replayed.
                nonce = parse_qs(urlparse(self.path).query).get("nonce", [""])[0]
                if not re.fullmatch(r"[0-9a-f]{16,64}", nonce):
                    return None
                key = feed.seals._key
                return {"device_id": key.device_id, "public_key": key.public_key_b64,
                        "sig": key.sign(IDENTITY_PREFIX + nonce.encode())}
            if path == "/api/policy":
                return {**load_policy(cfg), "covered_apps": list(COVERED_APPS)}
            if path == "/api/privacy":
                return privacy_report(cfg, feed.seals)
            return None

        def _unprefix(self) -> None:
            """The app calls ``/shield-api/*`` (Vite rewrites it in dev);
            standalone, this server takes the same prefix."""
            if self.path.startswith("/shield-api/"):
                self.path = "/api/" + self.path[len("/shield-api/"):]

        def _spa(self, path: str) -> None:
            """Serve the built console app: a real file under console_static,
            else index.html so client routes like /shield/timeline load."""
            import mimetypes

            from byoai.agent_context_cache import console
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/shield")
                self.end_headers()
                return
            if not console.assets_available():
                self._send(503, console.MISSING_BUILD_MESSAGE.encode(),
                           "text/plain; charset=utf-8")
                return
            # The build's asset URLs sit under the console base, /console/.
            rel = path[len("/console"):] if path.startswith("/console/") else path
            asset = console.resolve_asset(rel)
            if asset is not None:
                ctype = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                self._send(200, asset.read_bytes(), ctype,
                           console.cache_headers(rel.lstrip("/"))["Cache-Control"])
                return
            # A missing file is a 404, never the app page: HTML served as a
            # stale script fails in the browser with no sign what went missing.
            last = path.rsplit("/", 1)[-1]
            if path.startswith("/console/assets/") or "." in last:
                self._send(404, b'{"error":"not found"}')
                return
            # Client routes: /shield itself, and old /console/<tenant>/shield
            # links, which the app redirects to /shield.
            if path.startswith(("/shield", "/console/")):
                self._send(200, console.INDEX_FILE.read_bytes(),
                           "text/html; charset=utf-8", "no-cache")
                return
            self._send(404, b'{"error":"not found"}')

        def _refused(self, method: str) -> bool:
            problem = request_problem(method, self.path.split("?")[0], self.headers)
            if problem is None:
                return False
            self._send(403 if "Content-Type" not in problem else 415,
                       json.dumps({"error": problem}).encode())
            return True

        def do_GET(self):  # noqa: N802
            self._unprefix()
            if self._refused("GET"):
                return
            path = self.path.split("?")[0]
            api = self._api()
            if path == "/api/apps":
                self._send(200, json.dumps(self._apps()).encode())
                return
            if path == "/api/coriqo":
                self._send(200, json.dumps(self._coriqo()).encode())
                return
            if path in ("/api/feed", "/api/verify", "/api/policy",
                        "/api/privacy") or (path == "/api/identity" and api):
                self._send(200, json.dumps(api).encode())
                return
            if path.startswith("/api/receipt/"):
                rc = feed.seals.receipt(path.rsplit("/", 1)[-1])
                self._send(200 if rc else 404,
                           (json.dumps(rc, indent=1, default=str) if rc
                            else b'{"error":"unknown seal"}'))
                return
            if path.startswith("/api/"):
                self._send(404, b'{"error":"not found"}')
                return
            self._spa(path)

        def do_POST(self):  # noqa: N802
            self._unprefix()
            if self._refused("POST"):
                return
            if self.path.split("?")[0] == "/api/browser":
                self._browser_ingest()
                return
            if self.path.split("?")[0] == "/api/coriqo/enrol":
                self._enrol()
                return
            if self.path.split("?")[0] == "/api/publish":
                self._publish_now()
                return
            if self.path.split("?")[0] == "/api/privacy/scrub":
                result = scrub_ledger(cfg)
                feed.resync()
                self._send(200, json.dumps(
                    {**result, **privacy_report(cfg, feed.seals)}).encode())
                return
            if self.path.split("?")[0] != "/api/policy":
                self._send(404, b'{"error":"not found"}')
                return
            n = int(self.headers.get("Content-Length", 0))
            policy = load_policy(cfg)
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._send(400, b'{"error":"invalid json"}')
                return
            if not isinstance(payload, dict):
                self._send(400, b'{"error":"expected a JSON object"}')
                return
            try:
                policy = apply_policy_update(policy, payload)
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode())
                return
            save_policy(cfg, policy)
            self._send(200, json.dumps(
                {**policy, "covered_apps": list(COVERED_APPS)}).encode())

        def _browser_ingest(self) -> None:
            """Rows from the browser extension: same ledger, same seal chain,
            same privacy-first stance as the desktop proxy. The extension was
            told the endpoint at install; an unconfigured one is not an error
            here — it is recorded, subject to the same policy as everything
            else."""
            n = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._send(400, b'{"error":"invalid json"}')
                return
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                self._send(400, b'{"error":"expected {rows: [...]}"}')
                return
            policy = load_policy(cfg)
            accepted = 0
            for row in rows[:200]:
                clean = clean_browser_row(row)
                if clean is None:
                    continue
                # The extension retries its batch after network trouble; a
                # row sealed twice reads as two chats. row_id (a uuid from
                # the extension) is the dedupe key, kept for the last 1000 —
                # far beyond any retry window the extension has.
                rid = clean.get("row_id")
                if rid:
                    if rid in browser_dedupe["ids"]:
                        continue
                    browser_dedupe["ids"].add(rid)
                    browser_dedupe["order"].append(rid)
                    if len(browser_dedupe["order"]) > 1000:
                        for old in browser_dedupe["order"][:-500]:
                            browser_dedupe["ids"].discard(old)
                        del browser_dedupe["order"][:-500]
                if not policy["apps"].get(clean.get("app") or "claude"):
                    continue
                append_row(cfg.ledger, clean)
                accepted += 1
            # Seal now, not on the watcher's next tick: the gap between "a row
            # is on disk" and "a row is sealed" is where an edit costs nothing.
            # poll() is locked, so this can't race the watcher into sealing a
            # row twice.
            if accepted:
                feed.poll()
            self._send(200, json.dumps(
                {"accepted": accepted, "sealed_total": feed.seals.total}).encode())

        def _enrol(self) -> None:
            """One-time setup: exchange a Coriqo enrolment token for this
            Mac's device identity. The token is used once and not kept."""
            from byoai.recorder.enroll import EnrollmentError
            n = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._send(400, b'{"error":"invalid json"}')
                return
            try:
                status = publisher.enrol(str(payload.get("base_url") or ""),
                                         str(payload.get("token") or ""))
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode())
                return
            except EnrollmentError as exc:
                msg = str(exc)
                if "already enrolled" in msg or "HTTP 409" in msg:
                    hint = ("This Mac is already connected to Coriqo with its current key, "
                            "so it doesn't need a new token. The token hasn't been used.")
                elif "rejected" in msg:
                    hint = ("Coriqo didn't accept that token. It may be used, expired "
                            "or revoked; ask your Coriqo admin for a new one.")
                else:
                    hint = "Couldn't reach Coriqo at that address. Check it and try again."
                self._send(400, json.dumps({"error": hint}).encode())
                return
            self._send(200, json.dumps(status).encode())

        def _publish_now(self) -> None:
            """"Send now": publish the current seal immediately. Failures are
            recorded in the status (and retried on their own), not raised."""
            try:
                status = publisher.send()
            except ValueError as exc:
                self._send(409, json.dumps({"error": str(exc)}).encode())
                return
            except Exception:  # noqa: BLE001 - always answer, never drop the request
                self._send(500, json.dumps({
                    "error": "Couldn't send right now. Shield will try again on its own."
                }).encode())
                return
            self._send(200, json.dumps(status).encode())

        def log_message(self, *a):  # per-request rlog silenced
            return

    harden_files(cfg.ledger, cfg.seal_path, cfg.policy_path)
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        # Never slide to another port: the extension and bookmarks look for
        # Shield at one address, and a silent move would hide it from them.
        raise SystemExit(
            f"Shield cannot listen on {host}:{port} ({exc.strerror}). Something "
            f"else is using that port. Set BYOAI_SHIELD_PORT (or --port) to a "
            f"free one and enter the same address in the extension popup.") from exc
    print(f"Coriqo Shield → http://{host}:{port}/shield", flush=True)
    print(f"seal chain: {feed.seals.height} entries, root "
          f"{(feed.seals.root_hex() or '')[:10]}", flush=True)
    print(f"device: {feed.seals._key.device_id}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def default_paths(ledger: str | Path) -> ShieldConfig:
    ledger = Path(ledger)
    return ShieldConfig(
        ledger=ledger,
        seal_path=ledger.parent / "sealchain.json",
        key_dir=DATA_DIR / "keys",
        policy_path=ledger.parent / "policy.json",
        sig_every=1, max_interactions=MAX_INTERACTIONS,
    )


def main() -> None:  # console-script entrypoint: byoai-shield
    import argparse

    ap = argparse.ArgumentParser(prog="byoai-shield", description=__doc__)
    ap.add_argument("ledger", nargs="?",
                    default=str(DATA_DIR / "captures.jsonl"),
                    help="path to captures.jsonl (default: ~/.byoai/shield/captures.jsonl)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--daemonize-watch", action="store_true", default=True)
    ap.add_argument("--install-login-item", action="store_true",
                    help="start Shield when you log in (macOS), then exit")
    ap.add_argument("--remove-login-item", action="store_true",
                    help="stop starting Shield at login (macOS), then exit")
    args = ap.parse_args()
    if _sys.stdout is None or _sys.stderr is None:
        # Started without a console (Windows pythonw, a service): print() would
        # raise on a missing stdout, so send output to the log instead.
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = open(DATA_DIR / "shield.log", "a", buffering=1)  # noqa: SIM115
        _sys.stdout = _sys.stdout or log
        _sys.stderr = _sys.stderr or log
    if args.install_login_item or args.remove_login_item:
        from byoai.integrations import shield_login
        raise SystemExit(shield_login.run(
            install=args.install_login_item, ledger=args.ledger, port=args.port))
    cfg = default_paths(args.ledger)
    feed = Feed(cfg)

    def _watcher():
        while True:
            try:
                feed.poll()
            except Exception:
                pass
            time.sleep(1.5)

    import threading
    threading.Thread(target=_watcher, daemon=True).start()
    serve(cfg, host=args.host, port=args.port, feed=feed)


if __name__ == "__main__":
    main()
