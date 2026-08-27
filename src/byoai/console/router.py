"""``/v1/console/*`` — what the console reads.

Every response here is assembled from :class:`byoai.ingest.IngestStore`, which
holds only what devices actually shipped. That constraint produces the one
design decision worth reading before the code:

**Some fields the console asks for cannot be known on this side, and are
served as ``null`` rather than zero.**

``backlog_entries`` and ``oldest_unshipped_at`` describe what a device is still
holding and has not sent. The ingest side sees batches that arrived; it has no
visibility into a device's local queue, and a device that stops shipping looks
identical to one with nothing left to ship. Reporting ``0`` there would state
"nothing outstanding" on the strength of data nobody has — the precise failure
this product exists to prevent. They are ``null``, and the console renders
unknown.

The same applies to integrity: no verify walk runs here, so every device is
``unverified`` — not ``intact``. Absence of a failed check is not a pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query

from byoai.ingest import IngestStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _inclusion(devices: list[dict[str, Any]]) -> dict[str, int]:
    """Every aggregate carries its own denominator.

    A figure computed over the devices that reported, presented without the
    count of those that did not, is short by exactly the number the reader most
    needs.
    """
    reporting = [d for d in devices if d["last_batch_at"] is not None]
    return {"devices_included": len(reporting), "devices_enrolled": len(devices)}


def build_console_router(store: IngestStore) -> APIRouter:
    router = APIRouter(prefix="/v1/console", tags=["console"])

    @router.get("/fleet")
    def fleet(
        tenant: str = Query(...),
        from_: str | None = Query(None, alias="from"),
        to: str | None = Query(None),
    ) -> dict[str, Any]:
        # No 404 for an empty tenant. The sibling endpoints answer 200 with
        # empty lists for exactly this state, and a fleet with nothing enrolled
        # yet is a real, reportable condition — not a missing resource. Failing
        # only here made one panel error while the rest of the page loaded.
        devices = store.devices(tenant)

        reporting = [d for d in devices if d["last_batch_at"] is not None]
        never_seen = [
            d for d in devices if d["last_batch_at"] is None and d["last_contact_at"] is None
        ]
        silent = [
            d for d in devices if d["last_batch_at"] is None and d["last_contact_at"] is not None
        ]
        window_to = _parse(to) or _now()
        window_from = _parse(from_) or (window_to - timedelta(hours=24))

        return {
            "tenant": tenant,
            "window": {"from": _iso(window_from), "to": _iso(window_to)},
            "inclusion": _inclusion(devices),
            "coverage": {
                "reporting": len(reporting),
                "enrolled": len(devices),
                "silent": len(silent),
                "never_seen": len(never_seen),
            },
            # No verify walk runs on this side, so nothing here is intact —
            # it is unwalked. Reporting these as intact would turn "we have not
            # checked" into "we checked and it was fine".
            "integrity": {
                "intact": 0,
                "broken": 0,
                "unverified": len(reporting),
                "no_verdict": len(devices) - len(reporting),
            },
            "ingest": {
                "entries_received": sum(int(d["entries_received"] or 0) for d in devices),
                # Device-side facts. See the module docstring: null, not zero.
                "backlog_entries": None,
                "backlog_devices": None,
                "oldest_unshipped_at": None,
                "last_batch_at": max(
                    (d["last_batch_at"] for d in devices if d["last_batch_at"]), default=None
                ),
                "checkpoints_pending": None,
                "rate_series": store.entries_per_minute(tenant, window_from, window_to),
                "rate_flat_for_minutes": store.flat_for_minutes(tenant, window_to),
            },
            "denial": store.denial_summary(tenant, window_from, window_to),
            "open_findings": 0,
        }

    @router.get("/fleet/devices")
    def devices(tenant: str = Query(...)) -> dict[str, Any]:
        rows = store.devices(tenant)
        return {
            "inclusion": _inclusion(rows),
            "devices": [_device_wire(r) for r in rows],
            "next_cursor": None,
        }

    @router.get("/fleet/coverage")
    def coverage(tenant: str = Query(...)) -> dict[str, Any]:
        report = store.coverage(tenant)
        return {
            "tenant": report["tenant"],
            "as_of": report["as_of"],
            "enrolled": report["enrolled"],
            # Disjoint on the wire. In the store `contact_without_evidence` is
            # a subset of `never_seen` — both lack evidence — but a device that
            # is talking is a different finding from one that has never been
            # heard from, and listing it under both double-counts the fleet's
            # unaccounted-for devices on the screen that exists to count them.
            "never_seen": [
                _device_wire(d)
                for d in report["never_seen"]
                if d["last_contact_at"] is None
            ],
            "silent": [_device_wire(d) for d in report["contact_without_evidence"]],
            "unverified_ranges": _unverified_ranges(report),
            "checkpoint_gaps": {
                "sessions_without_checkpoint": len(report["devices_without_checkpoint"]),
                "checkpoints_never_countersigned": 0,
                "detail": [
                    {
                        "device_id": d["device_id"],
                        "what": "shipped entries but no checkpoint",
                        "quiet_for_s": 0.0,
                        "count": int(d["entries_received"] or 0),
                    }
                    for d in report["devices_without_checkpoint"]
                ],
            },
            # Mandate verdicts are sealed as events, but nothing on this side
            # decides which agents were ungoverned — that needs the mandate
            # snapshot the device evaluated against, which is not shipped.
            "ungoverned_agents": [],
            "blind_spot": {
                "basis": "device_enrolments",
                "statement": report["blind_spot"]["statement"],
                "defensible_claim": (
                    f"Complete across {report['enrolled']} enrolled devices, with "
                    f"{len(report['never_seen'])} unaccounted for."
                ),
            },
        }

    @router.get("/fleet/findings")
    def findings(tenant: str = Query(...)) -> dict[str, Any]:
        rows = store.devices(tenant)
        # Findings come from verify walks. None run here yet, so the honest
        # answer is an empty list with a total of zero — not a fabricated one.
        return {"inclusion": _inclusion(rows), "findings": [], "total": 0}

    return router


def _device_wire(r: dict[str, Any]) -> dict[str, Any]:
    """One enrolment row in the shape the console's schema expects."""
    last_batch = r["last_batch_at"]
    contact = r["last_contact_at"]
    if last_batch is not None:
        liveness = "reporting"
    elif contact is not None:
        liveness = "silent"
    else:
        liveness = "never_seen"
    quiet_for = None
    parsed = _parse(last_batch or contact)
    if parsed is not None:
        quiet_for = (_now() - parsed).total_seconds()
    interval = r["expected_interval_s"]
    return {
        "device_id": r["device_id"],
        "host": r.get("host") or r["device_id"],
        "agent_ids": [],
        "liveness": liveness,
        "enrolled_at": r["enrolled_at"],
        "last_batch_at": last_batch,
        "last_seq_received": r["last_seq_received"],
        "expected_interval_s": interval,
        "quiet_for_s": quiet_for,
        # Suppressed, not guessed: without an observed cadence there is no
        # multiple to report, and an unknown cadence must never render as an
        # on-time one.
        "overdue_multiple": (
            (quiet_for / interval) if (interval and quiet_for is not None) else None
        ),
        "ship_lag_s": None,
        "key_state": "unchecked" if r["revoked_at"] is None else "rotation_failed",
        "integrity": "unverified",
        "batches_received": int(r["batches_received"] or 0),
    }


def _unverified_ranges(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything received is unverified until a verify walk says otherwise."""
    out: list[dict[str, Any]] = []
    for d in report["reporting"]:
        last_seq = d["last_seq_received"]
        if last_seq is None:
            continue
        out.append(
            {
                "device_id": d["device_id"],
                "seq_start": 0,
                "seq_end": int(last_seq),
                "seqs": int(d["entries_received"] or 0),
                "accepted_from": d["first_batch_at"],
                "accepted_to": d["last_batch_at"],
                "last_verify_walk": None,
                "unverified_for_s": 0.0,
            }
        )
    return out
