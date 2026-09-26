"""``/v1/console/fleet*`` — the fleet console, answered by one Mac.

The console SPA is one build served by three hosts: the caching proxy
(:mod:`byoai.agent_context_cache`), the self-hosted Shield server
(:mod:`byoai.shield_server`, where the fleet router lives behind the admin
token), and this host — the local Shield on :17831. On the first two, "the
fleet" is whatever devices happened to enroll there. On this host the fleet
is exactly one device: the Mac whose Ed25519 key seals the ledger this server
reads. That is not a degraded fleet view; it is the only honest one — a
single device cannot know about the others, and a console that rendered
"unknown" forever while holding a locally verifiable chain would be hiding
the one real thing it has.

So every figure here comes from data this process can actually see, and the
rule the server-side router follows (:mod:`byoai.console.router`: unknown is
null, never zero) is followed harder here, because the sample size is one and
a guess is easier to spot:

* **Integrity is a real walk, run on every read.**
  :meth:`byoai.integrations.shield.SealChain.verify_chain` re-hashes the
  in-window payloads, re-checks the signed checkpoint against the tree, and
  reports recorded incidents. This is the only host where "intact" is earned
  rather than deferred — there is no verify job to wait for, because the walk
  *is* the read path.
* **Coverage, liveness and the rate series come from the seal log** — when
  entries were sealed, not when some server happened to receive them.
* **Anything that lives on a remote server is null, never zero.** The
  receiving side's batch count is not knowable from here. The backlog *is*
  (entries sealed past the last published checkpoint height), so it is
  reported when connected and null when not.
* **Denials are local verdicts** — interactions the flag rules stopped —
  counted over the ledger window the feed holds. When the loaded rows do not
  reach back across the previous window, ``previous_per_1k`` is null: "no
  comparison" is a fact, "0.0" is not.

Wire shapes mirror :mod:`byoai.console.router` field for field, because the
same zod contract in ``web/src/api/schemas.ts`` validates both hosts. Read-
only like the rest of the local API; the same-host Origin/Host guard in
:func:`byoai.integrations.shield.request_problem` already applies.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: Named windows the SPA's scope can carry; anything else is an explicit
#: from/to pair. Mirrors WINDOW_SECONDS in web/src/app/scope.ts.
_WINDOW_SECONDS = {"1h": 3600, "24h": 86_400, "7d": 604_800, "30d": 2_592_000}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_bound(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_time(payload: dict) -> datetime | None:
    """A sealed payload's stamp, resolved to the clock that actually wrote it.

    The payload's ``date`` + ``ts`` are NOT one clock, and that split is the
    whole hazard of this function:

    * desktop and MCP rows carry the server's own wall-clock text from
      ``strftime`` — naive LOCAL;
    * browser-extension rows reformat the relay's ``sent_at``, which is an
      ISO UTC instant — the text reads like local but stands for UTC, and
      reading it as local puts every seal five hours in the future.

    The surface names the source, so the source sets the interpretation; a
    stamp still landing well ahead of now then retries under the other
    reading (a relay whose UTC drifted, a row that aged out of the "· browser"
    marker), and a stamp wrong under both readings is dropped rather than
    guessed — the entry counts toward totals but shapes no timeline.
    """
    date = payload.get("date")
    ts = payload.get("ts")
    if not isinstance(date, str) or not isinstance(ts, str):
        return None
    try:
        naive = datetime.strptime(f"{date} {ts}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    surface = payload.get("surface")
    as_utc = isinstance(surface, str) and surface.endswith("· browser")
    primary = naive.replace(tzinfo=timezone.utc) if as_utc else naive.astimezone()
    now = datetime.now(timezone.utc)
    if (primary - now).total_seconds() <= 300:
        return primary
    alternate = naive.astimezone() if as_utc else naive.replace(tzinfo=timezone.utc)
    if (alternate - now).total_seconds() <= 300:
        return alternate
    return None


def _file_birth(path: Path) -> datetime:
    """Creation time of a file that normal operation never replaces — the
    chain log for a never-sealed record. ``st_birthtime`` is macOS/BSD;
    ``st_ctime`` is the closest elsewhere."""
    try:
        st = path.stat()
        raw = getattr(st, "st_birthtime", None) or st.st_ctime
        return datetime.fromtimestamp(float(raw), timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


class ShieldConsoleAPI:
    """Read-only fleet answers for the local host, with one cache.

    The scan of the seal log is keyed on the log's (mtime_ns, size) — the
    same idle-skip the feed watcher uses — so the console's 30 s poll costs
    two ``stat()`` calls on a quiet machine and one sequential read when the
    chain actually grew.
    """

    def __init__(self, cfg, feed, publisher) -> None:  # noqa: ANN001 - duck-typed by design
        self.cfg = cfg
        self.feed = feed
        self.publisher = publisher
        self._rows: list[tuple[int, datetime | None]] = []
        self._scan_sig: tuple = (0, 0)

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------

    def handle(self, path: str, query: dict[str, list[str]]) -> tuple[int, bytes]:
        """Answer ``GET /v1/console/...``; 404 JSON for paths there is not."""
        route = path[len("/v1/console"):] if path.startswith("/v1/console") else path
        tenant = (query.get("tenant") or [""])[0]
        if not tenant:
            return self._json(400, {"error": "tenant is required"})
        if route == "/fleet":
            return self._json(200, self.fleet_summary(tenant, query))
        if route == "/fleet/devices":
            return self._json(200, self.devices(tenant, query))
        if route == "/fleet/findings":
            return self._json(200, self.findings(tenant))
        if route == "/fleet/coverage":
            return self._json(200, self.coverage(tenant, query))
        if route == "/verdicts":
            return self._json(200, self.verdicts(tenant, query))
        return self._json(404, {"error": "not found"})

    @staticmethod
    def _json(code: int, body: dict) -> tuple[int, bytes]:
        return code, json.dumps(body).encode()

    # ------------------------------------------------------------------
    # local facts
    # ------------------------------------------------------------------

    def _scan(self) -> list[tuple[int, datetime | None]]:
        """``(height, sealed-at)`` for every entry in the chain log, oldest
        first. Heights are LOCAL chain heights (1..len(leaves)) — the same
        space the published checkpoint heights live in, which is what makes
        the ship backlog a plain subtraction."""
        seals = self.feed.seals
        try:
            st = seals.log_path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = (0, 0)
        if sig != self._scan_sig:
            rows: list[tuple[int, datetime | None]] = []
            for entry, _problem in seals._scan_log():
                if entry is None:
                    break  # a problem ends the scan; incidents record the why
                rows.append((int(entry["height"]), _event_time(entry.get("payload") or {})))
            self._rows = rows
            self._scan_sig = sig
        return self._rows

    def _device_state(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """Everything the four endpoints share, computed once per request."""
        seals = self.feed.seals
        rows = self._scan()
        now = datetime.now(timezone.utc)
        chain = seals.verify_chain()  # the walk IS the read; fresh by construction
        total = int(chain.get("sealed_total") or 0)

        last_dt: datetime | None = None
        # Payload stamps can arrive out of order (a tool result reseals the
        # older call it closes), so the newest event is the max over the
        # recent tail, not simply the last row's stamp.
        for _h, dt in reversed(rows[-20:]):
            if dt is not None and (last_dt is None or dt > last_dt):
                last_dt = dt

        frm, to = self._window(query)
        # A live window's `to` is floored to the minute by the SPA (stable
        # cache keys), which would drop everything sealed in the minute still
        # running — and on a single-device fleet that minute IS the fleet's
        # heartbeat. A window ending at or past now therefore reads to now;
        # an explicitly archived window (older `to`) stays exactly as wide as
        # it was named, because a permalink must resolve to the same slice
        # today as tomorrow.
        edge = now if (to - now).total_seconds() >= -120 else to
        quiet_for_s = (to - last_dt).total_seconds() if last_dt else None
        span_s = (to - frm).total_seconds()
        if total == 0:
            liveness = "never_seen"
        elif last_dt is None or quiet_for_s is None or quiet_for_s > span_s:
            # Has entries but their stamps cannot be read, or they all predate
            # the window: silence with no measurement is still silence.
            liveness = "silent"
        else:
            liveness = "reporting"
        broken = not chain.get("tamper_evident")
        verdict = ("broken" if broken else "intact") if total else "unverified"

        try:
            pub = self.publisher.status()
        except Exception:  # noqa: BLE001 - a missing publisher must not blank the page
            pub = {"connected": False}

        return {
            "now": now, "frm": frm, "to": to, "edge": edge,
            "rows": rows, "chain": chain, "total": total,
            "last_dt": last_dt, "quiet_for_s": quiet_for_s, "liveness": liveness,
            "verdict": verdict, "device_id": seals._key.device_id,
            "host": socket.gethostname(),
            # The moment this Mac sealed its first entry. The state file's
            # birth is rewritten on every atomic persist, so it is no start
            # at all; the first stamp in the chain is the only timestamp
            # local record actually has — and a chain that has never sealed
            # falls back to the log file's birth (which normal operation
            # never replaces).
            "enrolled_at": (next((dt for _h, dt in rows if dt is not None), None)
                            or _file_birth(seals.log_path)),
            "connected": bool(pub.get("connected")), "pub": pub,
        }

    @staticmethod
    def _window(query: dict[str, list[str]]) -> tuple[datetime, datetime]:
        to = _parse_bound((query.get("to") or [""])[0]) or datetime.now(timezone.utc)
        frm = _parse_bound((query.get("from") or [""])[0])
        if frm is None:
            seconds = _WINDOW_SECONDS.get((query.get("window") or [""])[0])
            frm = to - timedelta(seconds=seconds if seconds is not None else 86_400)
        return frm, to

    # -- ship-line facts (knowable only when connected) -----------------

    def _unshipped(self, dev: dict) -> tuple[int | None, str | None, int | None]:
        """``(backlog_entries, oldest_unshipped_at, checkpoints_pending)``.

        The publisher records the checkpoint height it last shipped, in the
        same local height space as the log rows, so entries sealed past that
        height are — by definition, not by estimate — unsent. Unconnected,
        there is no ship line at all, and null is what the contract reserves
        for facts this host cannot see.
        """
        if not dev["connected"]:
            return None, None, None
        last_height = int(dev["pub"].get("last_height") or 0)
        height = int(dev["chain"].get("entries") or 0)
        backlog = max(0, height - last_height)
        oldest = None
        for row_height, dt in dev["rows"]:
            if row_height > last_height and dt is not None:
                oldest = _iso(dt)
                break
        cp = self.feed.seals.checkpoint or {}
        pending = 1 if (int(cp.get("height") or 0) > last_height) else 0
        return backlog, oldest, pending

    # -- rate series -----------------------------------------------------

    def _rate_series(self, dev: dict) -> list[int]:
        frm, to, edge = dev["frm"], dev["to"], dev["edge"]
        minutes = max(1, int((to - frm).total_seconds() // 60))
        buckets = [0] * minutes
        for _height, dt in dev["rows"]:
            if dt is None:
                continue
            when = dt.astimezone(timezone.utc)
            if when < frm or when > edge:
                continue
            idx = int((when - frm).total_seconds() // 60)
            # An entry landing exactly at `to` folds into the last bucket —
            # same boundary rule as the server-side store's per-minute series.
            buckets[min(idx, minutes - 1)] += 1
        return buckets

    # -- denial ----------------------------------------------------------

    def _denial(self, dev: dict) -> dict:
        """Verdicts over the interactions the feed holds, in window.

        Locally "tool_use" is any sealed interaction — a chat send, a tool
        call — because the flag rules decide at exactly that granularity and
        the seal is the unit of evidence. An unsealed (running) call has no
        verdict yet, so it is in neither the count nor the denominator.
        """
        frm, to, edge = dev["frm"], dev["to"], dev["edge"]
        denied = flagged = total = 0
        refused: dict[tuple[str, str], int] = {}
        oldest_seen: datetime | None = None
        for it in list(self.feed.items):
            dt = _event_time({"date": it.get("date"), "ts": it.get("ts"),
                              "surface": it.get("surface")})
            if dt is not None:
                when = dt.astimezone(timezone.utc)
                if oldest_seen is None or when < oldest_seen:
                    oldest_seen = when
            else:
                continue
            if it.get("status") == "running" or when < frm or when > edge:
                continue
            total += 1
            if it.get("status") == "blocked":
                denied += 1
                flags = it.get("flags") or []
                reason = (f"{flags[0]['tier'].upper()}:{flags[0]['rule']}"
                          if flags else "policy verdict")
                tool = it.get("tool") or it.get("surface") or "message"
                refused[(tool, reason)] = refused.get((tool, reason), 0) + 1
            elif it.get("tier") in ("warn", "bad"):
                flagged += 1
        # The previous window is a comparison only when the loaded rows
        # actually span it; feed.items holds the last 400 interactions, and an
        # item-less previous window could equally mean "nothing happened" or
        # "the rows aged out". Null says we do not know, which is what is true.
        prev: float | None = None
        span = to - frm
        if oldest_seen is not None and oldest_seen <= frm + timedelta(seconds=60):
            prev_from, prev_to = frm - span, frm
            p_denied = p_total = 0
            for it in list(self.feed.items):
                dt = _event_time({"date": it.get("date"), "ts": it.get("ts"),
                                  "surface": it.get("surface")})
                if dt is None or it.get("status") == "running":
                    continue
                when = dt.astimezone(timezone.utc)
                if when < prev_from or when >= prev_to:
                    continue
                p_total += 1
                if it.get("status") == "blocked":
                    p_denied += 1
            if p_total:
                prev = p_denied / p_total * 1000.0
        top = [
            {"tool": t, "reason": r, "count": c}
            for (t, r), c in sorted(refused.items(), key=lambda kv: -kv[1])[:5]
        ]
        return {
            "denied_per_1k": (denied / total * 1000.0) if total else 0.0,
            "previous_per_1k": prev,
            "denied": denied,
            "flagged": flagged,
            "tool_use_total": total,
            "top_refused": top,
        }

    # -- findings ---------------------------------------------------------

    #: The seal chain's own incident vocabulary, mapped onto the console's
    #: fixed finding kinds. Each mapping names the same fact a server-side
    #: verify walk would: the chain log is content-hash-linked, so a line
    #: that fails its own hash IS a broken link, and a checkpoint that no
    #: longer matches the tree IS a signature that stopped being valid.
    _INCIDENT_KIND = {
        "chain_broken": ("broken_links", "bad"),
        "chain_unreadable": ("broken_links", "bad"),
        "chain_changed": ("broken_links", "bad"),
        "checkpoint_mismatch": ("bad_signatures", "bad"),
    }

    def _findings(self, dev: dict) -> list[dict]:
        out: list[dict] = []
        device_id = dev["device_id"]
        for i, inc in enumerate(reversed(dev["chain"].get("incidents") or [])):
            kind, sev = self._INCIDENT_KIND.get(
                inc.get("what", ""), ("broken_links", "warn"))
            out.append({
                "id": f"loc-{device_id[:8]}-{i}",
                "kind": kind,
                "severity": sev,
                "what": f"{inc.get('what', 'chain incident')} at "
                        f"{inc.get('at', '?')}: {inc.get('detail', '')}",
                "device_id": device_id,
                "ref": None,
            })
        cp = dev["chain"].get("checkpoint")
        if cp and cp.get("sig_valid") is False:
            out.append({
                "id": f"loc-{device_id[:8]}-sig",
                "kind": "bad_signatures",
                "severity": "bad",
                "what": "the newest signed checkpoint does not verify against this "
                        "device's key — the seal is not the device's own",
                "device_id": device_id,
                "ref": None,
            })
        return out

    # ------------------------------------------------------------------
    # the four endpoints
    # ------------------------------------------------------------------

    def fleet_summary(self, tenant: str, query: dict[str, list[str]]) -> dict:
        dev = self._device_state(query)
        frm, to = dev["frm"], dev["to"]
        reporting = dev["liveness"] == "reporting"
        backlog, oldest, pending = self._unshipped(dev)
        quiet = dev["quiet_for_s"]
        flat = (quiet / 60.0) if (quiet is not None and quiet >= 60) else None
        findings = self._findings(dev)
        return {
            "tenant": tenant,
            "window": {"from": _iso(frm), "to": _iso(to)},
            "inclusion": {"devices_included": 1 if reporting else 0,
                          "devices_enrolled": 1},
            "coverage": {
                "reporting": 1 if reporting else 0,
                "enrolled": 1,
                "silent": 1 if dev["liveness"] == "silent" else 0,
                "never_seen": 1 if dev["liveness"] == "never_seen" else 0,
            },
            "integrity": {
                # Every entry on disk has been walked by this read — there is
                # no unverified remainder on this host. A device that has
                # never produced an entry has no verdict at all, which is the
                # fourth state, not "broken" and not "intact".
                "intact": 1 if dev["verdict"] == "intact" else 0,
                "broken": 1 if dev["verdict"] == "broken" else 0,
                "unverified": 0,
                "no_verdict": 1 if dev["verdict"] == "unverified" else 0,
            },
            "ingest": {
                "entries_received": sum(
                    1 for _h, dt in dev["rows"]
                    if dt is not None and frm <= dt.astimezone(timezone.utc) <= dev["edge"]
                ),
                "backlog_entries": backlog,
                "backlog_devices": (1 if (backlog or 0) > 0 else 0)
                                   if backlog is not None else None,
                "oldest_unshipped_at": oldest,
                "last_batch_at": _iso(dev["last_dt"]) if dev["last_dt"] else None,
                "checkpoints_pending": pending,
                "rate_series": self._rate_series(dev),
                "rate_flat_for_minutes": flat,
            },
            "denial": self._denial(dev),
            "open_findings": len(findings),
            # Optional extension of the shared contract (see schemas.ts): the
            # one host where integrity is produced locally may say so, with
            # the root a reader can take to `byoai shield verify`.
            "local": {
                "device_id": dev["device_id"],
                "host": dev["host"],
                "merkle_root": dev["chain"].get("merkle_root"),
                "sealed_total": dev["total"],
                "chain_verified_at": _iso(dev["now"]),
                "incidents": len(dev["chain"].get("incidents") or []),
                "connected": dev["connected"],
                "coriqo_tenant": dev["pub"].get("tenant"),
            },
        }

    def devices(self, tenant: str, query: dict[str, list[str]]) -> dict:  # noqa: ARG002
        dev = self._device_state(query)
        agents: set[str] = set()
        for it in list(self.feed.items):
            src = it.get("source")
            if src == "mcp":
                name = (it.get("identity") or {}).get("client_name")
                if name:
                    agents.add(f"mcp:{name}")
            elif src in ("desktop", "browser"):
                # Surfaces read "Claude · desktop" / "ChatGPT · browser" —
                # the agent id is the observed app, lowercased and
                # slugified, never an invented registry name.
                label = str(it.get("surface") or "").split(" · ")[0]
                label = label.strip().lower().replace(" ", "-")
                if label:
                    agents.add(f"{src}:{label}")
        return {
            "inclusion": {
                "devices_included": 1 if dev["liveness"] == "reporting" else 0,
                "devices_enrolled": 1,
            },
            "devices": [self._device_row(dev, agents)],
            "next_cursor": None,
        }

    def _device_row(self, dev: dict, agents: set[str]) -> dict:
        cp = dev["chain"].get("checkpoint") or {}
        return {
            "device_id": dev["device_id"],
            "host": dev["host"],
            "agent_ids": sorted(agents),
            "liveness": dev["liveness"],
            "enrolled_at": _iso(dev["enrolled_at"]),
            "last_batch_at": _iso(dev["last_dt"]) if dev["last_dt"] else None,
            "last_seq_received": dev["total"] if dev["total"] else None,
            # Observed, not declared: local seals are event-driven (a burst
            # of traffic, then quiet), not a ship timer, so there is no
            # honest median inter-batch cadence to claim — and an unknown
            # cadence must never render as an on-time one.
            "expected_interval_s": None,
            "quiet_for_s": dev["quiet_for_s"],
            "overdue_multiple": None,
            "ship_lag_s": None,
            # `verified` means exactly what it says here: this request checked
            # the checkpoint signature against the key held on this machine.
            "key_state": "verified" if cp.get("sig_valid") is True else "unchecked",
            "integrity": dev["verdict"],
            # How many batches a remote server received is that server's
            # fact; this Mac cannot observe it. Null, not zero.
            "batches_received": None,
        }

    def findings(self, tenant: str) -> dict:  # noqa: ARG002
        dev = self._device_state({})
        rows = self._findings(dev)
        return {
            "inclusion": {"devices_included": 1, "devices_enrolled": 1},
            "findings": rows,
            "total": len(rows),
        }

    def coverage(self, tenant: str, query: dict[str, list[str]]) -> dict:
        dev = self._device_state(query)
        row = self._device_row(dev, set())
        backlog, _oldest, pending = self._unshipped(dev)
        detail: list[dict] = []
        if pending:
            cp = self.feed.seals.checkpoint or {}
            signed = _parse_bound(cp.get("ts"))
            detail.append({
                "device_id": dev["device_id"],
                "what": "signed checkpoints this Mac has not shipped yet",
                "quiet_for_s": max(0.0, (dev["now"] - signed).total_seconds())
                              if signed else 0.0,
                "count": int(backlog or 0),
            })
        unconnected_note = (
            " Nothing has left this Mac: Shield is not connected to a fleet "
            "server, so no receiving-side countersignature exists to wait for."
            if not dev["connected"] else ""
        )
        return {
            "tenant": tenant,
            "as_of": _iso(dev["now"]),
            "enrolled": 1,
            "never_seen": [row] if dev["liveness"] == "never_seen" else [],
            "silent": [row] if dev["liveness"] == "silent" else [],
            # The read path walks every entry before answering, so on this
            # host "stored but never verified" cannot happen. This emptiness
            # is earned — which is exactly why the walk runs per request.
            "unverified_ranges": [],
            "checkpoint_gaps": {
                "sessions_without_checkpoint": 1 if pending else 0,
                # Countersignature by a fleet server is that server's record,
                # and this host reports only what it can see.
                "checkpoints_never_countersigned": 0,
                "detail": detail,
            },
            # Local flag rules decide on every captured interaction before it
            # is sealed; an interaction with no verdict is unsealed and so
            # absent from the record entirely. There is no "ran unevaluated"
            # on this host — that is a fleet-server observation, not this one.
            "ungoverned_agents": [],
            "blind_spot": {
                "basis": "device_enrolments",
                "statement": (
                    "This console is served by one Mac's Shield. The fleet it "
                    "can see is this device: every other machine you run "
                    "produces no entry here — not in any count, and not in "
                    f"the denominator.{unconnected_note}"
                ),
                "defensible_claim": (
                    f"Complete across 1 enrolled device: its {dev['total']} "
                    "sealed entries were each re-hashed and its checkpoint "
                    "signature re-verified while this report was built."
                ),
            },
        }

    # ------------------------------------------------------------------
    # mandate — the verdict stream
    # ------------------------------------------------------------------

    def verdicts(self, tenant: str, query: dict[str, list[str]]) -> dict:
        """The fleet verdict stream, local edition (spec §6.5).

        The server-side stream reads sealed ``mandate_verdict`` events; on
        this host the rules decide at capture time, inline, and what each
        sealed interaction carries IS its verdict: blocked is `denied`, a
        rule hit that still passed is `flagged`, everything else was
        `allowed`. Enforcement comes from the live policy the same request
        path reads (managed-locked included), so the posture banner cannot
        disagree with what the proxy actually did.

        The rows cover what the feed holds in memory — the most recent 400
        interactions — which is the stream's honest reach; every older
        verdict is still sealed in the ledger, just not listed here, and the
        page says so in those terms rather than letting "showing N" imply a
        complete history.
        """
        from byoai.integrations.shield import load_policy  # lazy: same module owns the feed

        dev = self._device_state(query)
        frm, to, edge = dev["frm"], dev["to"], dev["edge"]
        policy = load_policy(self.cfg)
        mode = policy.get("mode")
        enforcement = "observe" if mode == "observe" else "enforce"

        # Payload stamps are not seqs; a verdict is addressable by the chain
        # height its seal took, which the in-memory window carries.
        heights: dict[str, int] = {}
        for e in self.feed.seals.entries:
            iid = (e.get("payload") or {}).get("interaction")
            if isinstance(iid, str) and iid:
                heights[iid] = int(e["height"])

        rows: list[dict] = []
        allowed = flagged = denied = 0
        for it in list(self.feed.items):          # newest first
            if it.get("status") == "running":
                continue
            dt = _event_time({"date": it.get("date"), "ts": it.get("ts"),
                              "surface": it.get("surface")})
            if dt is None:
                continue
            when = dt.astimezone(timezone.utc)
            if when < frm or when > edge:
                continue
            flags = it.get("flags") or []
            if it.get("status") == "blocked":
                verdict = "denied"
                denied += 1
                reason = (f"{flags[0]['tier'].upper()}:{flags[0]['rule']}" if flags
                          else "policy verdict")
            elif it.get("tier") in ("warn", "bad"):
                verdict = "flagged"
                flagged += 1
                reason = f"{flags[0]['tier'].upper()}:{flags[0]['rule']}" if flags else None
            else:
                verdict = "allowed"
                allowed += 1
                reason = None
            ident = it.get("identity") or {}
            rows.append({
                "device_id": dev["device_id"],
                "seq": heights.get(it.get("id") or ""),
                "ts": _iso(when),
                "surface": it.get("surface") or "",
                "tool": it.get("tool"),
                "agent_id": ident.get("client_name"),
                "verdict": verdict,
                "reason": reason,
                "chars": it.get("chars"),
            })
            if len(rows) >= 200:
                break

        # Denial latches: an agent retrying past a first denial is a
        # different fact from a one-off. Grouped by surface+tool; the count
        # is attempts AFTER the first denial, so a lone denial is not a
        # latch. Newest-first rows mean the last row seen is the first in
        # time.
        by_key: dict[tuple[str, str | None], dict] = {}
        for row in rows:
            if row["verdict"] != "denied":
                continue
            key = (row["surface"], row["tool"])
            g = by_key.get(key)
            if g is None:
                by_key[key] = {"device_id": row["device_id"], "surface": row["surface"],
                               "tool": row["tool"], "attempts": 0,
                               "first_seq": row["seq"]}
            else:
                g["attempts"] += 1
                g["first_seq"] = row["seq"] if row["seq"] is not None else g["first_seq"]
        latches = sorted((g for g in by_key.values() if g["attempts"] > 0),
                         key=lambda g: -g["attempts"])[:10]

        return {
            "tenant": tenant,
            "window": {"from": _iso(frm), "to": _iso(to)},
            "inclusion": {"devices_included": 1 if dev["liveness"] == "reporting" else 0,
                          "devices_enrolled": 1},
            "rollup": {"allowed": allowed, "flagged": flagged, "denied": denied},
            "enforcement": enforcement,
            # Observe mode's entire number: actions that WOULD have been
            # stopped. Meaningless (null, not 0) under an enforcing mode,
            # where flagged actions did pass with their flags on record.
            "observe_flagged": flagged if enforcement == "observe" else None,
            "latches": latches,
            "verdicts": rows,
            "next_cursor": None,
        }
