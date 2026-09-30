"""Shield's rules as a plain function, for chat apps that are not the browser.

``check(body)`` runs the same rules, actions and file logic as the Shield
extension and desktop proxy over a chat request body and says what to do:
``allow``, ``warn``, ``redact`` or ``block``. It does no I/O and imports no web
framework; the gateway in ``byoai-cache`` and :mod:`byoai.shield_guard.fastapi`
are thin callers.

A decision never carries matched text: only rule names, labels, lengths and
keyed fingerprints. In a server there is nobody to ask, so a ``warn`` hit blocks
unless the caller passes ``on_warn="redact"`` (and even then a hit that has no
placeholder blocks).
"""

from __future__ import annotations

import base64
import json
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from byoai.integrations import shield as _shield

__all__ = ["Decision", "RawResult", "check", "check_raw", "decode_body", "blocked_payload", "load_policy_from", "BLOCKED_TYPE"]

BLOCKED_TYPE = "coriqo_shield_blocked"
_ORDER = ("allow", "warn", "redact", "block")


@dataclass
class Decision:
    action: str  # allow | warn | redact | block
    rules: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    body: Any = None  # the body to forward (redacted when action == "redact")
    files: list[dict] = field(default_factory=list)
    chars: int = 0
    text_hmac: str | None = None
    rules_version: str = _shield.RULES_VERSION

    def to_dict(self) -> dict:
        return {"action": self.action, "rules": self.rules, "labels": self.labels,
                "body": self.body, "files": self.files, "chars": self.chars,
                "text_hmac": self.text_hmac, "rules_version": self.rules_version}


def load_policy_from(path: str | Path | None) -> dict:
    """Shield's policy file (same format the extension and desktop app use);
    a missing or unreadable file gives the privacy-first defaults."""
    return _shield.read_policy(Path(path).expanduser()) if path else _shield.default_policy()


def _labels(rules: list[str]) -> list[str]:
    return list(dict.fromkeys(_shield.RULE_LABEL.get(r, r) for r in rules))


def _file_input(f: Any) -> tuple[str | None, str | None, bytes]:
    if isinstance(f, dict):
        return f.get("name"), f.get("mime"), bytes(f.get("data") or b"")
    name, mime, data = f
    return name, mime, bytes(data)


def _text_of(body: Any) -> str:
    return _shield.all_message_text(body) if isinstance(body, (dict, list)) else str(body)


def _rules_left(text: str, policy: dict) -> list[str]:
    """Rules in ``text`` whose action still is redact or stricter."""
    act, rules = _shield.evaluate(text, policy)
    return rules if act != "log" else []


_B64_KEYS = ("data", "file_data")
_B64 = re.compile(r"[A-Za-z0-9+/\-_\s]+={0,2}")
MAX_DECODED = 5 * 1024 * 1024


def _leaves(node, skipped=False, key=""):
    """Every string in a JSON value as ``(text, skipped, key)``: keys too, and
    including the id-like and base64 fields Shield's own walk leaves out
    (``skipped`` marks those)."""
    if isinstance(node, str):
        yield node, skipped, key
    elif isinstance(node, list):
        for x in node:
            yield from _leaves(x, skipped, key)
    elif isinstance(node, dict):
        binary = node.get("type") == "base64" or "mimeType" in node or "mime_type" in node
        for k, v in node.items():
            yield str(k), True, "key"
            skip = skipped or _shield._idish(k) or (binary and str(k).lower() == "data")
            yield from _leaves(v, skip, str(k).lower())


def _decoded(text: str, key: str) -> str | None:
    """Text inside a base64 string (a ``data`` / ``file_data`` value, or any
    ``data:...;base64,`` URL) when it decodes to valid UTF-8 up to 5 MB."""
    if "base64," in text[:200]:
        text = text.split("base64,", 1)[1]
    elif key not in _B64_KEYS:
        return None
    if len(text) < 16 or len(text) > MAX_DECODED * 4 // 3 + 8 or not _B64.fullmatch(text):
        return None
    t = "".join(text.split()).replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(t + "=" * (-len(t) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _sweep(body: Any, policy: dict) -> tuple[list[str], list[str]]:
    """(secret rules that still apply, pii rules in skipped fields).

    The main decision reads the message fields; this reads everything else too,
    so a key under ``id``, ``metadata.user_id``, a document ``data`` field or a
    base64 payload cannot carry one past it. A secret anywhere counts unless the
    policy sets that rule to ``log``; personal details in skipped fields are
    only reported (OpenAI's ``user`` often holds an email)."""
    secrets: list[str] = []
    pii: list[str] = []
    for text, skipped, key in _leaves(body):
        views = [(text, skipped)]
        inner = _decoded(text, key)
        if inner is not None:
            views.append((inner, True))
        for t, sk in views:
            for tier, rule, _ in _shield.flags_for(t, replies=False):
                if tier == "secret" and _shield.action_for(rule, policy) != "log":
                    secrets.append(rule)
                elif tier == "pii" and sk:
                    pii.append(rule)
    return list(dict.fromkeys(secrets)), list(dict.fromkeys(pii))


def check(body: dict | list | str, *, policy: dict | None = None,
          files: list | None = None, on_warn: str = "block",
          key_path: Path | None = None, app: str | None = None) -> Decision:
    """Decide what to do with a chat request.

    ``body`` is a parsed JSON body (every message-like string is read, not only
    the last turn; secrets are also looked for in every other string, key and
    base64 ``data`` field) or a JSON string; any other string is plain text.
    ``files`` is a list of ``(name, mime, bytes)`` or ``{"name","mime","data"}``.
    ``key_path`` picks the fingerprint key (default: Shield's own).
    ``app`` (a Shield app name such as ``"claude"``) applies that app's per-app
    ``files`` policy to ``files``; without it only the file content rules apply.

    A blocked decision carries no body (``body`` is ``None``).
    """
    policy = policy if policy is not None else _shield.default_policy()
    if on_warn not in ("block", "redact"):
        raise ValueError("on_warn must be 'block' or 'redact'")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, (dict, list)):
                body = parsed
        except ValueError:
            pass
    text = _text_of(body)
    key = key_path or _shield.FINGERPRINT_KEY
    act, rules = _shield.evaluate(text, policy)
    action = {"log": "allow"}.get(act, act)

    # Files: never rewritten, so a warn or block on one stops the request.
    facts = []
    file_stop = False
    for f in files or []:
        name, mime, data = _file_input(f)
        info = _shield.inspect_file(name, mime, data, app, policy, key_path=key)
        facts.append(info)
        if info["action"] in ("warn", "block"):
            file_stop = True
            rules = rules + [x.split(":", 1)[1] for x in info["flags"]]

    out_body = body
    if action == "warn":
        action = "redact" if on_warn == "redact" else "block"
    if action == "redact":
        if isinstance(body, (dict, list)):
            wrapped = {"b": body}
            raw, _hit = _shield.redact_json_body(json.dumps(wrapped, ensure_ascii=False), policy)
            out_body = json.loads(raw)["b"]
        else:
            out_body, _hit = _shield.redact_with_rules(text, policy)
        # Fail closed: anything a rewrite can't remove (no placeholder) stops the send.
        if _rules_left(_text_of(out_body), policy):
            action = "block"
    # What is left (or was never read by the main pass) must hold no secret.
    secrets, pii = _sweep(out_body, policy)
    if secrets:
        action = "block"
        rules = rules + secrets
    rules = rules + pii
    if file_stop:
        action = "block"
    if action == "block":
        out_body = None
    rules = list(dict.fromkeys(rules))
    return Decision(
        action=action, rules=rules, labels=_labels(rules), body=out_body, files=facts,
        chars=len(text), text_hmac=_shield.fingerprint(text, key) if text else None)


_UNREADABLE = "unreadable"
MAX_RAW = 32 * 1024 * 1024


def decode_body(raw: bytes, content_encoding: str | None) -> bytes:
    """Undo gzip/deflate (ValueError for anything else or a bad body)."""
    return _decode_encoding(raw, content_encoding or "")


def _decode_encoding(raw: bytes, enc: str) -> bytes:
    """Undo a Content-Encoding we can read (gzip, deflate); anything else is
    refused. Bounded, so a compressed bomb cannot exhaust memory."""
    encs = [e.strip() for e in enc.lower().split(",") if e.strip() and e.strip() != "identity"]
    for e in reversed(encs):
        if e not in ("gzip", "x-gzip", "deflate"):
            raise ValueError(f"unsupported content-encoding {e}")
        try:
            d = zlib.decompressobj(31 if "gzip" in e else zlib.MAX_WBITS)
            try:
                out = d.decompress(raw, MAX_RAW + 1)
            except zlib.error:
                if e != "deflate":
                    raise
                d = zlib.decompressobj(-zlib.MAX_WBITS)  # raw deflate
                out = d.decompress(raw, MAX_RAW + 1)
        except zlib.error as exc:
            raise ValueError("bad compressed body") from exc
        if len(out) > MAX_RAW or d.unconsumed_tail:
            raise ValueError("body too large")
        raw = out
    return raw


def _pairs(dropped: list):
    def hook(pairs):
        out: dict = {}
        for k, v in pairs:
            if k in out:
                dropped.append(out[k])  # a repeated key: the earlier value still gets read
            out[k] = v
        return out
    return hook


class RawResult(NamedTuple):
    decoded: bytes           # the body with any gzip/deflate undone (b"" if unreadable)
    decision: Decision
    rewritten: bytes | None  # JSON to send instead of the original, when redacted or re-keyed


def _text_secrets(raw: bytes, policy: dict, key: Path | None) -> Decision:
    """A body that is not JSON (multipart, binary, plain text): only the secret
    rules read it, as text, and it is never rewritten."""
    texts = [raw.decode("utf-8", "replace"), raw.decode("latin-1")]
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        texts.append(raw.decode("utf-16", "replace"))
    rules: list[str] = []
    pii: list[str] = []
    for t in texts:
        for tier, rule, _ in _shield.flags_for(t, replies=False):
            if tier == "secret" and _shield.action_for(rule, policy) != "log":
                rules.append(rule)
            elif tier == "pii":
                pii.append(rule)
    rules = list(dict.fromkeys(rules))
    every = list(dict.fromkeys(rules + pii))
    return Decision(
        action="block" if rules else "allow", rules=every, labels=_labels(every),
        body=None, chars=len(texts[0]),
        text_hmac=_shield.fingerprint(texts[0], key or _shield.FINGERPRINT_KEY) if raw else None)


def check_raw(raw: bytes, content_encoding: str | None = None, *,
              policy: dict | None = None, on_warn: str = "block",
              key_path: Path | None = None, app: str | None = None) -> RawResult:
    """``check`` for a request body as it came off the wire.

    Undoes gzip/deflate, reads JSON (every repeated key too) and returns a
    :class:`RawResult`. ``decoded`` is the readable body; ``rewritten`` is the
    JSON to send instead when the decision redacted something or a key was
    repeated (the caller then drops ``Content-Encoding`` and re-sizes). A body
    that cannot be decoded blocks. A body that is not JSON is scanned for
    secrets as text and never rewritten."""
    policy = policy if policy is not None else _shield.default_policy()

    def stop():
        return RawResult(b"", Decision(action="block", rules=[_UNREADABLE],
                                       labels=_labels([_UNREADABLE])), None)
    if len(raw) > MAX_RAW:
        return stop()
    try:
        raw = _decode_encoding(raw, content_encoding or "")
    except ValueError:
        return stop()
    dropped: list = []
    try:
        parsed = json.loads(raw, object_pairs_hook=_pairs(dropped))
    except (ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, (dict, list)):
        return RawResult(raw, _text_secrets(raw, policy, key_path), None)
    target = {"body": parsed, "repeated": dropped} if dropped else parsed
    d = check(target, policy=policy, on_warn=on_warn, key_path=key_path, app=app)
    if dropped and d.body is not None:
        d.body = d.body["body"]
    rewritten = None
    if d.body is not None and (d.action == "redact" or dropped):
        rewritten = json.dumps(d.body, ensure_ascii=False).encode()
    return RawResult(raw, d, rewritten)


def blocked_payload(decision: Decision, shape: str = "openai") -> dict:
    """The JSON error for a blocked request, in the provider's own shape.
    Names rule labels only, never the matched text."""
    what = ", ".join(decision.labels) or "sensitive data"
    message = f"Shield stopped this request: it contains {what}."
    if shape == "anthropic":
        return {"type": "error", "error": {
            "type": BLOCKED_TYPE, "message": message,
            "rules": decision.rules, "labels": decision.labels}}
    return {"error": {"message": message, "type": BLOCKED_TYPE, "code": "shield_blocked",
                      "rules": decision.rules, "labels": decision.labels}}
