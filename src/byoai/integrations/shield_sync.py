"""Shield Sync: what a device may tell Coriqo about its activity.

Shield's promise is "what happened, never what was said". This module is the
one place that decides what leaves the device, and it does it by *allowlist*:
an event is built field by field from a feed item, and nothing is copied
wholesale. A field Shield adds to its rows tomorrow does not sync until someone
adds it here, in review.

Levels (the ``sync`` key of the signed policy):

``seal``    root, count and signature only (the checkpoint publisher; no
            events are built at this level)
``daily``   per-day totals per app, aggregated here so the server never holds
            per-message rows
``events``  one row per message: app, time, length, verdict, rule ids

Sending these to Coriqo is not wired up yet; today the level is only
disclosed to the person (see ``/api/policy`` -> ``sharing``).
"""
from __future__ import annotations

import datetime as _dt
import re

SYNC_LEVELS = ("seal", "daily", "events")
DEFAULT_SYNC_LEVEL = "seal"

#: Plain-words description of each level, shown to the person verbatim
#: (UI banner, extension popup). ``{by}`` is the organisation receiving it.
LEVEL_WORDS = {
    "seal": "{by} can check your record is intact. They see no activity.",
    "daily": ("{by} sees daily totals: how many messages per app, and how "
              "many were caught. Not individual messages."),
    "events": ("{by} sees each message's app, time, length and whether it "
               "was caught. Never what you wrote."),
}

#: Every key an event may carry. The server's closed schema is this list; a
#: test pins both to it.
EVENT_FIELDS = ("event_id", "occurred_at", "device_id", "person_id", "source",
                "app", "kind", "chars", "verdict", "flags", "redactions",
                "tool", "seal")

SOURCES = ("browser", "desktop", "mcp", "agent")

_NAME_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,40}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SEAL_RE = re.compile(r"^[0-9a-f]{8,64}$")
#: Rule and tier ids come from Shield's fixed rule set; anything else is
#: dropped rather than trusted.
_RULE_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_TIERS = ("high", "pii", "agent", "conduct")
_MAX_CHARS = 10_000_000


def sync_level(policy: dict) -> str:
    level = (policy or {}).get("sync", DEFAULT_SYNC_LEVEL)
    return level if level in SYNC_LEVELS else DEFAULT_SYNC_LEVEL


def level_words(level: str, managed_by: str) -> str:
    return LEVEL_WORDS[level if level in LEVEL_WORDS else DEFAULT_SYNC_LEVEL
                       ].format(by=managed_by or "Your administrator")


def _name(value: object) -> str | None:
    """A vendor-chosen name (MCP client, tool), never user content. Truncated
    first, then it must match; a name that doesn't is dropped, not cleaned."""
    if not isinstance(value, str):
        return None
    value = value.strip()[:40]
    return value if _NAME_RE.match(value) else None


def _app(item: dict) -> str:
    """The app id: a known app from the surface label, else the MCP client
    name from the handshake."""
    from byoai.integrations.shield import APP_LABEL
    surface = item.get("surface")
    head = surface.split(" · ")[0] if isinstance(surface, str) else ""
    for app, label in APP_LABEL.items():
        if head == label:
            return app
    return _name(head) or "other"


def _verdict(item: dict) -> str:
    raw = item.get("verdict")
    raw = raw if isinstance(raw, str) else ""
    if item.get("status") == "blocked" or raw in ("blocked", "block"):
        return "blocked"
    if raw.startswith("redacted") or item.get("redactions"):
        return "redacted"
    if raw == "observe" or item.get("source") == "browser":
        return "observed"
    return "allowed"


def _flags(item: dict) -> list[dict]:
    """Tier and rule id only. The third element of ``flags_for`` (the matched
    text) never exists on a feed item; even so, only these two keys are read."""
    out: list[dict] = []
    for f in item.get("flags") or []:
        if not isinstance(f, dict):
            continue
        tier, rule = f.get("tier"), f.get("rule")
        if tier in _TIERS and isinstance(rule, str) and _RULE_RE.match(rule):
            entry = {"tier": tier, "rule": rule}
            if entry not in out:
                out.append(entry)
    return out


def _occurred_at(item: dict) -> str | None:
    """UTC ISO time from the item's date and time of day. Browser rows carry
    the extension's own UTC timestamp; every other source stamps this Mac's
    local wall clock. (A local time inside a DST fold resolves to the first
    occurrence, at worst an hour off.)"""
    date, ts = item.get("date"), item.get("ts")
    if not (isinstance(date, str) and isinstance(ts, str)):
        return None
    try:
        local = _dt.datetime.strptime(f"{date} {ts[:8]}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if item.get("source") == "browser":
        utc = local.replace(tzinfo=_dt.timezone.utc)
    else:
        utc = local.astimezone(_dt.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_event(item: dict, *, device_id: str,
                person_id: str | None = None) -> dict | None:
    """One sync event from one feed item, or ``None`` when the item can't be
    synced yet (still running, unsealed, or missing what the schema needs).

    Built from scratch: no key of ``item`` is copied except through the
    conversions below."""
    if not isinstance(item, dict) or not _ID_RE.match(str(device_id or "")):
        return None
    event_id, seal = item.get("id"), item.get("seal")
    if not (isinstance(event_id, str) and _ID_RE.match(event_id)):
        return None
    if not (isinstance(seal, str) and _SEAL_RE.match(seal)):
        return None  # unsealed (a tool call still running): sent once sealed
    occurred_at = _occurred_at(item)
    source = item.get("source")
    if occurred_at is None or source not in SOURCES:
        return None
    chars = item.get("chars")
    if not (isinstance(chars, int) and not isinstance(chars, bool)
            and 0 <= chars < _MAX_CHARS):
        chars = None
    is_tool = source == "mcp"
    redactions = item.get("redactions")
    return {
        "event_id": event_id,
        "occurred_at": occurred_at,
        "device_id": device_id,
        "person_id": person_id if isinstance(person_id, str)
        and _ID_RE.match(person_id) else None,
        "source": source,
        "app": _app(item),
        "kind": "tool_call" if is_tool else "message",
        "chars": chars,
        "verdict": _verdict(item),
        "flags": _flags(item),
        "redactions": len(redactions) if isinstance(redactions, list) else 0,
        "tool": _name(item.get("tool")) if is_tool else None,
        "seal": seal,
    }


def aggregate_daily(events: list[dict]) -> list[dict]:
    """Per (day, app) totals from already-built events, days on this Mac's
    local calendar. This runs on the device: at ``daily`` level the
    per-message rows are never sent."""
    days: dict[tuple[str, str], dict] = {}
    for ev in events:
        when = _dt.datetime.strptime(ev["occurred_at"], "%Y-%m-%dT%H:%M:%SZ")
        local_day = when.replace(tzinfo=_dt.timezone.utc).astimezone().strftime("%Y-%m-%d")
        key = (local_day, ev["app"])
        row = days.setdefault(key, {"date": key[0], "app": key[1],
                                    "messages": 0, "flags": {},
                                    "redacted": 0, "blocked": 0})
        row["messages"] += 1
        for f in ev["flags"]:
            name = f"{f['tier']}:{f['rule']}"
            row["flags"][name] = row["flags"].get(name, 0) + 1
        if ev["verdict"] == "redacted":
            row["redacted"] += 1
        elif ev["verdict"] == "blocked":
            row["blocked"] += 1
    return [days[k] for k in sorted(days)]
