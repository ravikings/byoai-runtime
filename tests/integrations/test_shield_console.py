"""The fleet console served by ONE Mac — /v1/console/fleet* on local Shield.

The contract these endpoints answer is the same zod schema the console SPA
validates (web/src/api/schemas.ts); the honesty rules they must keep are in
``byoai.integrations.shield_console``'s docstring. Each test here pins one of
those rules: what is knowable locally is reported, what is not, is null.
"""
from __future__ import annotations

import json
import time as _time
from datetime import datetime, timedelta, timezone

from byoai.integrations.shield import Feed, ShieldConfig
from byoai.integrations.shield_console import ShieldConsoleAPI


class StubPublisher:
    """Publisher.status() stand-in; the real one only speaks when enrolled."""

    def __init__(self, **overrides):
        self._status = {"connected": False, "tenant": None, "last_height": 0,
                        "last_sent_at": None, **overrides}

    def status(self):
        return dict(self._status)


def make_cfg(tmp_path):
    ledger = tmp_path / "captures.jsonl"
    ledger.write_text("")
    return ShieldConfig(
        ledger=ledger,
        seal_path=tmp_path / "sealchain.json",
        key_dir=tmp_path / "keys",
        policy_path=tmp_path / "policy.json",
        managed_policy_path=tmp_path / "managed_policy.json",
        sig_every=4, max_interactions=100,
    )


def rows(n=8, *, denied_at=None, age_minutes=5):
    """Rows stamped `age_minutes` ago (default: five, so every stamp is in the
    past and inside the console's default 24 h window, the way live traffic
    arriving before the request is)."""
    base = datetime.now() - timedelta(minutes=age_minutes)
    out = []
    for i in range(n):
        when = (base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
        kind = "desktop.chat.request"
        row = {"wall_clock": when, "kind": kind, "app": "claude", "chars": 20 + i}
        if denied_at is not None and i == denied_at:
            row = {"wall_clock": when, "kind": "desktop.verdict.denied",
                   "app": "claude", "verdict": "blocked", "chars": 12}
        out.append(row)
    return out


def make_api(cfg, publisher=None, seed=None):
    cfg.ledger.write_text("".join(json.dumps(r) + "\n" for r in (seed or [])))
    feed = Feed(cfg)
    feed.scan_initial()
    return ShieldConsoleAPI(cfg, feed, publisher or StubPublisher())


def get(api, route, **query):
    qs = {k: [str(v)] for k, v in query.items()}
    qs.setdefault("tenant", ["acme-prod"])
    code, body = api.handle("/v1/console" + route, qs)
    return code, json.loads(body)


# ------------------------------------------------------------- the empty case


def test_never_seen_device_is_the_fourth_state_not_broken(tmp_path):
    api = make_api(make_cfg(tmp_path))
    _, summary = get(api, "/fleet")
    assert summary["coverage"] == {"reporting": 0, "enrolled": 1,
                                   "silent": 0, "never_seen": 1}
    # Zero entries means no verdict exists — not "broken", and above all not
    # the green tick an empty screen would otherwise imply.
    assert summary["integrity"] == {"intact": 0, "broken": 0,
                                    "unverified": 0, "no_verdict": 1}
    assert summary["ingest"]["last_batch_at"] is None
    assert summary["open_findings"] == 0
    assert summary["local"]["sealed_total"] == 0

    _, devices = get(api, "/fleet/devices")
    assert devices["devices"][0]["liveness"] == "never_seen"
    assert devices["devices"][0]["integrity"] == "unverified"

    _, cov = get(api, "/fleet/coverage")
    assert [d["device_id"] for d in cov["never_seen"]] == \
        [devices["devices"][0]["device_id"]]
    assert cov["silent"] == []
    assert "one Mac's Shield" in cov["blind_spot"]["statement"]


def test_not_connected_means_the_ship_line_is_unknown(tmp_path):
    # Backlog, ship lag and countersignature are receiving-side facts. With
    # no fleet server configured, "0 unsynced" would claim a health nobody
    # measured — null is the contract's answer.
    api = make_api(make_cfg(tmp_path), seed=rows(6))
    _, summary = get(api, "/fleet")
    assert summary["ingest"]["backlog_entries"] is None
    assert summary["ingest"]["oldest_unshipped_at"] is None
    assert summary["ingest"]["checkpoints_pending"] is None
    _, cov = get(api, "/fleet/coverage")
    assert "not connected" in cov["blind_spot"]["statement"]
    _, devices = get(api, "/fleet/devices")
    assert devices["devices"][0]["batches_received"] is None


# ------------------------------------------------------- a live, intact chain


def test_intact_chain_reports_real_coverage_and_rates(tmp_path):
    api = make_api(make_cfg(tmp_path), seed=rows(8, denied_at=3))
    _, summary = get(api, "/fleet")
    assert summary["coverage"] == {"reporting": 1, "enrolled": 1,
                                   "silent": 0, "never_seen": 0}
    # Eight entries sealed, every one re-hashed by this very request, the
    # checkpoint (sig_every=4) signed and signature-valid: intact is EARNED
    # here, not deferred to a verify job.
    assert summary["integrity"]["intact"] == 1
    assert summary["integrity"]["broken"] == 0
    assert summary["ingest"]["entries_received"] == 8
    assert summary["ingest"]["last_batch_at"].endswith("Z")
    series = summary["ingest"]["rate_series"]
    assert len(series) == 1440 and sum(series) == 8
    assert summary["local"]["merkle_root"] and summary["local"]["incidents"] == 0
    assert summary["open_findings"] == 0

    _, devices = get(api, "/fleet/devices")
    dev = devices["devices"][0]
    assert dev["integrity"] == "intact"
    assert dev["key_state"] == "verified"
    assert dev["last_seq_received"] == 8
    assert "desktop:claude" in dev["agent_ids"]

    _, cov = get(api, "/fleet/coverage")
    # The walk is the read, so on this host nothing sits "stored but never
    # verified" — the one empty list on the coverage screen that is true.
    assert cov["unverified_ranges"] == []
    assert cov["never_seen"] == [] and cov["silent"] == []


def test_browser_rows_are_stamped_in_utc_and_land_in_their_real_minute(tmp_path):
    """Desktop rows carry the server's local wall clock; browser rows reformatted
    the relay's UTC `sent_at` into the same two fields. Reading both as local
    — or both as UTC — shifts one source by the machine's offset: browser seals
    would land hours in the future and drop out of every window, which on a
    browser-only Mac is the whole fleet."""
    now = datetime.now(timezone.utc)
    sent = (now - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows_ = [{"kind": "browser.chat.request", "app": "claude", "chars": 40,
              "sent_at": sent,
              "wall_clock": (now - timedelta(minutes=4)).astimezone()
                            .strftime("%Y-%m-%d %H:%M:%S")}]
    api = make_api(make_cfg(tmp_path), seed=rows_)
    _, summary = get(api, "/fleet")
    assert summary["coverage"]["reporting"] == 1
    assert summary["ingest"]["entries_received"] == 1
    # The seal's effective instant is the relay's UTC stamp, not that stamp
    # re-shifted by the local offset: within a minute of the send, never in
    # the future.
    batch = datetime.fromisoformat(summary["ingest"]["last_batch_at"].replace("Z", "+00:00"))
    sent_dt = datetime.fromisoformat(sent.replace("Z", "+00:00"))
    # The seal's effective instant is the relay's UTC stamp itself — neither
    # re-shifted by the local offset nor dropped as untrustworthy.
    assert abs((batch - sent_dt).total_seconds()) < 2
    assert batch <= datetime.now(timezone.utc)


def test_enrolled_at_is_the_first_seal_not_a_file_write(tmp_path):
    api = make_api(make_cfg(tmp_path), seed=rows(3, age_minutes=20))
    _, devices = get(api, "/fleet/devices")
    dev = devices["devices"][0]
    assert dev["enrolled_at"] <= dev["last_batch_at"]
    first = datetime.fromisoformat(dev["enrolled_at"].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - first).total_seconds() - 20 * 60) < 120


def test_local_verdicts_feed_the_denial_panel(tmp_path):
    api = make_api(make_cfg(tmp_path), seed=rows(8, denied_at=3))
    _, summary = get(api, "/fleet")
    denial = summary["denial"]
    assert denial["denied"] == 1
    assert denial["tool_use_total"] == 8
    assert round(denial["denied_per_1k"], 1) == round(1 / 8 * 1000, 1)
    # Nothing was loaded from before the window, so there is no comparison —
    # and the panel must not be handed a 0.0 to mistake for "unchanged".
    assert denial["previous_per_1k"] is None
    assert denial["top_refused"][0]["count"] == 1


def test_window_bounds_are_respected(tmp_path):
    api = make_api(make_cfg(tmp_path), seed=rows(6, age_minutes=90))
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    to = now.isoformat().replace("+00:00", "Z")
    _, summary = get(api, "/fleet", **{"from": frm, to: to})
    assert summary["ingest"]["entries_received"] == 0   # all rows predate the window
    assert summary["coverage"]["reporting"] == 0
    assert summary["coverage"]["silent"] == 1
    assert summary["ingest"]["rate_flat_for_minutes"] >= 60


def test_a_floor_to_the_minute_live_window_still_sees_this_minute(tmp_path):
    """The SPA floors `to` to the minute for stable cache keys. Seals that
    land in the still-running minute are the fleet's live heartbeat — a live
    window must read to now, while an archived `to` stays exactly as wide as
    it was named."""
    api = make_api(make_cfg(tmp_path), seed=rows(3, age_minutes=0))  # stamps: ~now
    now = datetime.now(timezone.utc)
    floored = now.replace(second=0, microsecond=0)  # exactly what the SPA floor does
    iso = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731
    _, summary = get(api, "/fleet", **{"from": iso(floored - timedelta(hours=24)),
                                       "to": iso(floored)})
    assert summary["ingest"]["entries_received"] >= 1  # current-minute seals counted
    archived = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    archived_to = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    _, past = get(api, "/fleet", **{"from": archived, "to": archived_to})
    assert past["ingest"]["entries_received"] == 0     # the named slice stays closed


# ------------------------------------------------- what a broken chain looks like


def test_tampered_entry_surfaces_as_a_finding_not_a_pass(tmp_path):
    cfg = make_cfg(tmp_path)
    api = make_api(cfg, seed=rows(6))
    log = cfg.seal_path.with_name("sealchain.log.jsonl")
    lines = log.read_text().splitlines()
    victim = json.loads(lines[2])
    victim["payload"]["chars"] = 9999            # edit, seal left untouched
    lines[2] = json.dumps(victim)
    log.write_text("\n".join(lines) + "\n")

    _, summary = get(api, "/fleet")
    assert summary["integrity"]["broken"] == 1
    assert summary["open_findings"] >= 1
    _, findings = get(api, "/fleet/findings")
    assert findings["total"] == len(findings["findings"]) >= 1
    kinds = {f["kind"] for f in findings["findings"]}
    assert "broken_links" in kinds
    assert all(f["severity"] == "bad" for f in findings["findings"])
    assert all(f["device_id"] == summary["local"]["device_id"]
               for f in findings["findings"])


def test_connected_ship_state_is_reported_not_guessed(tmp_path):
    # last_height=2 of 4 sealed entries: the two newest are unsent by
    # definition, and the signed checkpoint (height 4) is pending.
    pub = StubPublisher(connected=True, tenant="acme", last_height=2,
                        last_sent_at="2026-09-26T00:00:00Z")
    api = make_api(make_cfg(tmp_path), publisher=pub, seed=rows(4))
    _, summary = get(api, "/fleet")
    assert summary["ingest"]["backlog_entries"] == 2
    assert summary["ingest"]["backlog_devices"] == 1
    assert summary["ingest"]["checkpoints_pending"] == 1
    assert summary["ingest"]["oldest_unshipped_at"].endswith("Z")
    _, cov = get(api, "/fleet/coverage")
    assert cov["checkpoint_gaps"]["sessions_without_checkpoint"] == 1
    assert cov["checkpoint_gaps"]["detail"][0]["count"] == 2


# ------------------------------------------------------------ wire-shape guards


def test_404_and_missing_tenant(tmp_path):
    api = make_api(make_cfg(tmp_path))
    code, body = api.handle("/v1/console/fleet/nonesuch", {"tenant": ["t"]})
    assert code == 404 and json.loads(body)["error"] == "not found"
    assert api.handle("/v1/console/fleet", {})[0] == 400


def test_summary_carries_the_shapes_the_spa_parses(tmp_path):
    """One belt-and-braces pass over field types — the zod contract catches
    drift at runtime; this catches it before a release ships it."""
    api = make_api(make_cfg(tmp_path), seed=rows(5))
    _, s = get(api, "/fleet")
    assert isinstance(s["window"]["from"], str)
    assert set(s["inclusion"]) == {"devices_included", "devices_enrolled"}
    assert set(s["coverage"]) == {"reporting", "enrolled", "silent", "never_seen"}
    assert set(s["integrity"]) == {"intact", "broken", "unverified", "no_verdict"}
    assert all(isinstance(v, int) for v in s["ingest"]["rate_series"])
    assert set(s["denial"]) == {"denied_per_1k", "previous_per_1k", "denied",
                                "flagged", "tool_use_total", "top_refused"}
    assert all(set(r) == {"tool", "reason", "count"} for r in s["denial"]["top_refused"])
    _, d = get(api, "/fleet/devices")
    row = d["devices"][0]
    assert {"device_id", "host", "agent_ids", "liveness", "enrolled_at",
            "last_batch_at", "last_seq_received", "expected_interval_s",
            "quiet_for_s", "overdue_multiple", "ship_lag_s", "key_state",
            "integrity", "batches_received"} <= set(row)
    _, c = get(api, "/fleet/coverage")
    assert c["blind_spot"]["basis"] == "device_enrolments"
    assert set(c["checkpoint_gaps"]) == {"sessions_without_checkpoint",
                                         "checkpoints_never_countersigned",
                                         "detail"}
    assert c["ungoverned_agents"] == []


# ------------------------------------------------------------ served by the app


def _serve(cfg):
    import socket
    import threading
    import urllib.request

    from byoai.integrations.shield import serve
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    threading.Thread(target=serve, args=(cfg, "127.0.0.1", port), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(base + "/api/policy", timeout=0.2)
            return base
        except Exception:
            _time.sleep(0.05)
    raise RuntimeError("shield server did not start")


def test_the_real_server_answers_the_fleet_routes(tmp_path):
    import urllib.request
    cfg = make_cfg(tmp_path)
    cfg.ledger.write_text("".join(json.dumps(r) + "\n" for r in rows(4)))
    base = _serve(cfg)
    with urllib.request.urlopen(base + "/v1/console/fleet?tenant=acme-prod", timeout=5) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert body["integrity"]["intact"] == 1
    assert body["coverage"]["reporting"] == 1
    # A request addressed off localhost must not answer, same as every other
    # endpoint: the Host guard runs before the fleet router.
    import urllib.error
    req = urllib.request.Request(base + "/v1/console/fleet?tenant=x")
    req.add_header("Host", "example.com")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("a remote Host must not read the local fleet")
    except urllib.error.HTTPError as exc:
        assert exc.code == 403


# ------------------------------------------------------- the verdict stream


def _seed_verdict_rows(base):
    """7 plain sends, 1 denied, 1 flagged (legacy row with PII in its prompt)."""
    from datetime import timedelta as _td
    out = []
    for i in range(7):
        out.append({"wall_clock": (base + _td(seconds=i)).strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "desktop.chat.request", "app": "claude", "chars": 20 + i})
    out.append({"wall_clock": (base + _td(seconds=7)).strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "desktop.verdict.denied", "app": "claude", "verdict": "blocked",
                "chars": 12})
    out.append({"wall_clock": (base + _td(seconds=8)).strftime("%Y-%m-%d %H:%M:%S"),
                "kind": "desktop.chat.request", "target": "claude.ai",
                "prompt": "forward this to j.rivers@gmail.com please"})
    return out


def test_verdicts_stream_counts_local_decisions(tmp_path):
    from datetime import timedelta as _td
    base = datetime.now() - _td(minutes=6)
    api = make_api(make_cfg(tmp_path), seed=_seed_verdict_rows(base))
    code, stream = get(api, "/verdicts")
    assert code == 200
    assert stream["rollup"] == {"allowed": 7, "flagged": 1, "denied": 1}
    assert stream["enforcement"] == "enforce"
    # The would-have-been-stopped counter is an observe-mode question; under
    # enforcement it is null, not a 0 claiming the check ran and found none.
    assert stream["observe_flagged"] is None
    assert stream["latches"] == []
    assert len(stream["verdicts"]) == 9
    row = next(v for v in stream["verdicts"] if v["verdict"] == "denied")
    assert row["device_id"] == stream["verdicts"][0]["device_id"]
    assert row["seq"] is not None and row["seq"] >= 1
    assert row["reason"] is not None


def test_verdicts_latch_counts_repeats_after_the_first_denial(tmp_path):
    from datetime import timedelta as _td
    base = datetime.now() - _td(minutes=6)
    rows_ = _seed_verdict_rows(base)
    rows_.append({"wall_clock": (base + _td(seconds=9)).strftime("%Y-%m-%d %H:%M:%S"),
                  "kind": "desktop.verdict.denied", "app": "claude",
                  "verdict": "blocked", "chars": 30})
    api = make_api(make_cfg(tmp_path), seed=rows_)
    _, stream = get(api, "/verdicts")
    assert stream["rollup"]["denied"] == 2
    assert len(stream["latches"]) == 1
    latch = stream["latches"][0]
    assert latch["attempts"] == 1  # one repeat after the first denial
    assert latch["surface"].endswith("desktop")


def test_verdicts_observe_mode_reports_the_would_have_been_stopped_count(tmp_path):
    from datetime import timedelta as _td

    from byoai.integrations.shield import default_policy, save_policy
    cfg = make_cfg(tmp_path)
    pol = default_policy()
    pol["mode"] = "observe"
    save_policy(cfg, pol)
    base = datetime.now() - _td(minutes=6)
    api = make_api(cfg, seed=_seed_verdict_rows(base))
    _, stream = get(api, "/verdicts")
    assert stream["enforcement"] == "observe"
    assert stream["observe_flagged"] == stream["rollup"]["flagged"] == 1
