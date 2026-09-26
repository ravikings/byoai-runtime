"""Coriqo Shield capture proxy — a mitmproxy addon, privacy-first.

It sits between an AI chat app and its API on this Mac and, per request:

* ``redact`` (default) — replaces personal details inside the message fields
  of the outgoing JSON before it leaves the machine;
* ``block`` — also stops high-risk sends (credentials, executables) locally;
* ``observe`` — records rule hits only, sends the message unchanged.

What it writes to the ledger is **what happened, not what was said**: the
app, the verdict, which rules fired, the message length and a keyed
fingerprint (:func:`byoai.integrations.shield.fingerprint`). A redacted
preview is written only when the admin turned on ``keep_text``. The same
applies to replies.

Rules, redaction and policy come from :mod:`byoai.integrations.shield`, so
the proxy and the shield UI can't disagree about what a rule is.

    mitmdump --listen-port 8080 -s examples/desktop_proxy_capture.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from byoai.integrations.shield import (
    COVERED_APPS,
    HIGH_RULES,
    append_row,
    inspect_text,
    message_text,
    proxy_alive_path,
    read_policy,
    redact_json_body,
)

_APP_TAG_FOR_HOST = (("claude.ai", "claude"), ("anthropic.com", "claude"),
                     ("chatgpt.com", "chatgpt"), ("openai.com", "chatgpt"),
                     ("gemini.google.com", "gemini"),
                     ("copilot.microsoft.com", "copilot"))

# Paths that ARE the product: the user's chat going out, the model's reply
# coming back, and MCP tool traffic on the chat wire.
INTERESTING = (
    re.compile(r"/chat_conversations/.+/completion"),   # claude.ai chat send
    re.compile(r"/v1/messages"),                        # API / agent wire format
    re.compile(r"/api/organizations/[^/]+/mcp"),        # MCP aggregation endpoints
    re.compile(r"/backend-api/(f/)?conversation$"),     # chatgpt.com chat send
    re.compile(r"/v1/chat/completions"),                # OpenAI Chat Completions
    re.compile(r"/v1/responses"),                       # OpenAI Responses
)
# Endpoints worth one body-free metadata row each.
MEANS = (
    re.compile(r"/system_prompts"),
    re.compile(r"/cowork_sysprompt_map"),
    re.compile(r"/desktop/features"),
    re.compile(r"/organizations/[^/]+/usage"),
)
# Hosts of the apps in COVERED_APPS. An app outside it is never inspected,
# whatever its toggle says: Shield can't read its messages yet.
TARGET_HOSTS = ("api.anthropic.com", "claude.ai", "anthropic.com",
                "chatgpt.com", "api.openai.com")


def app_for_host(host: str | None) -> str | None:
    if not host:
        return None
    for frag, tag in _APP_TAG_FOR_HOST:
        if frag in host:
            return tag
    return None


def _path(flow) -> str:
    parts = flow.request.path_components
    return "/" + "/".join(parts) if parts else (flow.request.path or "")


def _match(flow, patterns) -> bool:
    path = _path(flow)
    return any(p.search(path) for p in patterns)


def reply_text(raw: str) -> str:
    """The model's words out of a reply: joins SSE text chunks (Anthropic
    ``delta.text``, claude.ai ``completion``, OpenAI ``choices[].delta.content``
    and Responses ``response.output_text.delta``), or reads a plain JSON
    Messages / Chat Completions response. ChatGPT web streams whole-message
    snapshots; the last one wins."""
    parts: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        delta = ev.get("delta")
        choices = ev.get("choices")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            parts.append(delta["text"])
        elif isinstance(delta, str) and ev.get("type") == "response.output_text.delta":
            parts.append(delta)
        elif isinstance(ev.get("completion"), str):
            parts.append(ev["completion"])
        elif isinstance(choices, list) and choices and isinstance(choices[0], dict):
            piece = (choices[0].get("delta") or {}).get("content")
            if isinstance(piece, str):
                parts.append(piece)
        elif isinstance(ev.get("message"), dict):  # chatgpt.com snapshot
            snap = ev["message"].get("content")
            if isinstance(snap, dict) and isinstance(snap.get("parts"), list):
                parts = [x for x in snap["parts"] if isinstance(x, str)]
    if parts:
        return "".join(parts)
    try:
        body = json.loads(raw)
    except ValueError:
        return ""
    if isinstance(body, dict) and isinstance(body.get("content"), list):
        return "".join(str(b.get("text", "")) for b in body["content"]
                       if isinstance(b, dict))
    if isinstance(body, dict) and isinstance(body.get("choices"), list):
        return "".join(str((c.get("message") or {}).get("content") or "")
                       for c in body["choices"] if isinstance(c, dict))
    return ""


class ShieldProxy:
    """The addon. ``ledger`` and ``policy_path`` are where the shield UI reads
    and writes; policy is re-read at most every two seconds."""

    def __init__(self, ledger: Path, policy_path: Path) -> None:
        self.ledger = ledger
        self.policy_path = policy_path
        self._policy: dict | None = None
        self._policy_at = 0.0

    # -- plumbing ---------------------------------------------------------
    def policy(self) -> dict:
        if self._policy is None or time.time() - self._policy_at >= 2:
            self._policy = read_policy(self.policy_path)
            self._policy_at = time.time()
        return self._policy

    def _log(self, kind: str, **fields: object) -> None:
        rec = {"wall_clock": time.strftime("%Y-%m-%d %H:%M:%S"),
               "kind": "desktop." + kind, **fields}
        append_row(self.ledger, rec)
        # stderr gets the same row: it holds no message text either.
        print("[shield] " + json.dumps(rec, default=str)[:300],
              file=sys.stderr, flush=True)

    def _governed(self, flow) -> str | None:
        """The app tag when this flow is one Shield should look at, else None
        (not an AI host, or the admin turned that app off)."""
        host = flow.request.pretty_host or ""
        if not any(t in host for t in TARGET_HOSTS):
            return None
        app = app_for_host(host)
        if app not in COVERED_APPS or not self.policy()["apps"].get(app, False):
            return None
        return app

    # -- liveness ---------------------------------------------------------
    def running(self) -> None:
        """mitmproxy is up: leave a marker (pid, listen port) next to the
        ledger, so Shield can tell Coriqo whether traffic is being checked."""
        import os

        from byoai.recorder.keys import atomic_write_bytes
        port = None
        try:
            from mitmproxy import ctx
            port = int(ctx.options.listen_port or 8080)
        except Exception:  # noqa: BLE001 - outside mitmproxy, or no port option
            pass
        try:
            atomic_write_bytes(proxy_alive_path(self.ledger), json.dumps({
                "pid": os.getpid(), "port": port,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }).encode(), mode=0o644, prefix=".shield-proxy-")
        except OSError:
            pass  # never stop the proxy over its own marker

    def done(self) -> None:
        """Clean stop: remove our marker (only ours, not a newer proxy's)."""
        import os
        path = proxy_alive_path(self.ledger)
        try:
            if json.loads(path.read_text()).get("pid") == os.getpid():
                path.unlink()
        except (OSError, ValueError, AttributeError):
            pass

    # -- mitmproxy hooks --------------------------------------------------
    def request(self, flow) -> None:
        app = self._governed(flow)
        if app is None:
            return
        host = flow.request.pretty_host or ""
        if _match(flow, MEANS):
            self._log("meta", target=host, app=app, path=_path(flow)[:120])
            return
        if not _match(flow, INTERESTING):
            return
        policy = self.policy()
        mode = policy["mode"]
        raw = flow.request.get_text(strict=False) or ""
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        text = message_text(body) if isinstance(body, dict) else ""

        high = [rule for rule, p in HIGH_RULES if p.search(text)]
        if high and mode == "block":
            facts = inspect_text(text, policy)
            self._log("verdict.denied", target=host, app=app, verdict="blocked",
                      **facts)
            _refuse(flow, "Stopped on this Mac: " + ", ".join(high))
            return

        redactions: list[str] = []
        if mode in ("redact", "block"):
            new_raw, redactions = redact_json_body(raw)
            if redactions:
                flow.request.set_text(new_raw)
        # Rule hits come from the text as typed, so the record says what was
        # caught; the fingerprint is of that same text.
        facts = inspect_text(text, policy)
        verdict = f"redacted({len(redactions)})" if redactions else mode
        self._log("chat.request", target=host, app=app, verdict=verdict,
                  redactions=redactions, **facts)
        flow.metadata["shield_app"] = app

    def response(self, flow) -> None:
        app = flow.metadata.get("shield_app")
        if app is None or flow.response is None:
            return
        try:
            raw = flow.response.get_text(strict=False) or ""
        except ValueError:
            raw = ""
        text = reply_text(raw)
        facts = inspect_text(text, self.policy(), rules="reply")
        self._log("chat.response", target=flow.request.pretty_host or "",
                  app=app, status=flow.response.status_code, **facts)


def _refuse(flow, message: str) -> None:
    """Answer the app locally; the request never leaves the machine."""
    from mitmproxy import http

    flow.response = http.Response.make(
        403,
        json.dumps({"error": {"type": "coriqo_shield_blocked",
                              "message": message}}).encode(),
        {"Content-Type": "application/json", "x-coriqo-shield": "blocked"},
    )
