"""Shield → Coriqo publishing: rare, self-healing, and never needs a person
once enrolled. Coriqo is a mock transport here; the wire format is the one the
recorder's /v1/checkpoints/batch already uses."""
from __future__ import annotations

import gzip
import json

import httpx
import pytest

from byoai.integrations.shield import SealChain, ShieldConfig
from byoai.integrations.shield_publish import BACKOFF_CAP_S, Publisher
from byoai.recorder.enroll import enroll


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeCoriqo:
    """Answers /v1/enroll and /v1/checkpoints/batch; can be told to fail."""

    def __init__(self):
        self.batches: list[dict] = []
        self.fail_with: int | None = None
        self.retry_after: str | None = None
        #: Set to make /v1/enroll pin a policy key straight away, like an
        #: enrolment response that carries EnrolByTokenOut.policy_key.
        self.enrol_policy_key: dict | None = None
        #: What POST /v1/shield/policy answers next.
        self.policy_changed = False
        self.policy_envelope: dict | None = None
        self.policy_have_version: int | None = None
        self.policy_fail = False
        self.policy_requests: list[dict] = []
        #: GET /api/v1/checkpoints/public-keys — the already-enrolled path.
        self.public_keys: list[dict] = []
        self.public_keys_requests = 0
        self.public_keys_down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/enroll":
            body = {"device_id": "dev-enrolled-1", "coriqo_base_url": "https://coriqo.test",
                    "tenant_slug": "acme_bank", "label": "shield"}
            if self.enrol_policy_key:
                body["policy_key"] = self.enrol_policy_key
            return httpx.Response(201, json=body)
        if request.url.path == "/v1/checkpoints/batch":
            if self.fail_with:
                headers = {"retry-after": self.retry_after} if self.retry_after else {}
                return httpx.Response(self.fail_with, json={"detail": "nope"}, headers=headers)
            body = json.loads(gzip.decompress(request.content))
            assert request.headers["x-coriqo-signature"]
            self.batches.append(body)
            return httpx.Response(200, json={"accepted": 1, "duplicates": 0, "rejected": []})
        if request.url.path == "/v1/shield/policy":
            if self.policy_fail:
                return httpx.Response(500, json={"detail": "nope"})
            self.policy_requests.append(json.loads(gzip.decompress(request.content)))
            if self.policy_changed:
                return httpx.Response(200, json={"changed": True, "envelope": self.policy_envelope})
            return httpx.Response(200, json={"changed": False, "version": self.policy_have_version})
        if request.url.path == "/api/v1/checkpoints/public-keys":
            self.public_keys_requests += 1
            if self.public_keys_down:
                return httpx.Response(503)
            return httpx.Response(200, json={"keys": self.public_keys})
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def setup(tmp_path):
    cfg = ShieldConfig(ledger=tmp_path / "c.jsonl", seal_path=tmp_path / "s.json",
                       key_dir=tmp_path / "keys", sig_every=4)
    seals = SealChain(cfg)
    coriqo = FakeCoriqo()
    clock = Clock()
    pub = Publisher(seals, cfg.key_dir, tmp_path, every_hours=6, now=clock,
                    client_factory=coriqo.client)
    return seals, coriqo, clock, pub, cfg


def _enrol(cfg, coriqo):
    enroll(coriqo_base_url="https://coriqo.test", token="cik_live_x",
           key_dir=cfg.key_dir, http_client=coriqo.client())


def _grow(seals, n=1):
    for i in range(n):
        seals.stamp({"interaction": f"i{seals.height}", "flags": [], "chars": 3})


def test_nothing_happens_until_the_mac_is_enrolled(setup):
    seals, coriqo, clock, pub, cfg = setup
    _grow(seals, 3)
    assert pub.tick() is False and coriqo.batches == []
    assert pub.status()["connected"] is False


def test_sends_only_the_signed_seal_once_per_head(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 3)
    assert pub.tick() is True
    (batch,) = coriqo.batches
    (cp,) = batch["checkpoints"]
    assert cp["seq_start"] == cp["seq_end"] == 1 and cp["entries"] == 3
    assert cp["chain_hash"] == cp["checkpoint"]["root_hex"]
    assert cp["session_id"] == "coriqo-shield"
    # no ledger rows, no text: the seal, when it left, and the protection state
    assert set(batch) == {"device_id", "checkpoints", "sent_at", "shield"}
    # same head again: nothing new to send before the heartbeat is due
    clock.t += 5 * 3600
    assert pub.tick() is False and len(coriqo.batches) == 1
    s = pub.status()
    assert s["connected"] and s["tenant"] == "acme_bank" and s["has_new"] is False


def test_new_entries_wait_for_the_interval(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    pub.tick()
    _grow(seals, 5)
    clock.t += 2 * 3600
    assert pub.tick() is False                      # grew, but only 2h later
    assert pub.status()["has_new"] is True
    clock.t += 4 * 3600
    assert pub.tick() is True and len(coriqo.batches) == 2
    last = coriqo.batches[-1]["checkpoints"][0]
    assert last["seq_end"] == 2 and last["entries"] == 7


def test_failures_back_off_and_recover_on_their_own(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    coriqo.fail_with = 503
    pub.tick()
    s = pub.state
    assert s.failures == 1 and 45 <= s.next_attempt_at - clock.t <= 75
    assert pub.tick() is False                      # still backing off
    clock.t = s.next_attempt_at + 1
    pub.tick()
    assert pub.state.failures == 2                  # doubled
    for _ in range(20):                             # never beyond the cap
        clock.t = pub.state.next_attempt_at + 1
        pub.tick()
    assert pub.state.next_attempt_at - clock.t <= BACKOFF_CAP_S * 1.2 + 1
    assert pub.status()["needs_attention"] is False
    coriqo.fail_with = None
    clock.t = pub.state.next_attempt_at + 1
    pub.tick()
    assert pub.state.failures == 0 and pub.state.last_error is None
    assert len(coriqo.batches) == 1


def test_retry_after_is_honoured(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals)
    coriqo.fail_with, coriqo.retry_after = 429, "3600"
    pub.tick()
    assert pub.state.next_attempt_at - clock.t >= 3600


def test_a_revoked_mac_is_the_one_state_that_asks_for_a_person(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals)
    coriqo.fail_with = 401
    pub.tick()
    s = pub.status()
    assert s["needs_attention"] is True and "enrolment token" in s["last_error"]


def test_state_survives_a_restart(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    pub.tick()
    again = Publisher(seals, cfg.key_dir, tmp_path, now=clock, client_factory=coriqo.client)
    assert again.state.last_height == 2 and again.state.last_seq == 1
    assert again.tick() is False                    # restart doesn't resend straight away
    clock.t += 7 * 3600
    assert again.tick() is True                     # the heartbeat: same entry, same number
    assert coriqo.batches[-1]["checkpoints"] == coriqo.batches[0]["checkpoints"]
    assert again.state.last_seq == 1


def test_a_broken_transport_never_escapes_tick(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals)

    def boom():
        raise RuntimeError("socket exploded")
    pub._client_factory = boom
    assert pub.tick() is True                       # attempted, didn't raise
    assert pub.state.failures == 1


def test_enrol_validates_input(setup):
    seals, coriqo, clock, pub, cfg = setup
    with pytest.raises(ValueError):
        pub.enrol("coriqo.test", "tok")
    with pytest.raises(ValueError):                 # a token never crosses a network in clear
        pub.enrol("http://coriqo.example.com", "tok")
    with pytest.raises(ValueError):
        pub.enrol("https://coriqo.test", "  ")


def test_sealing_and_signing_from_two_threads_keeps_the_chain_intact(setup):
    import threading
    seals, coriqo, clock, pub, cfg = setup

    def stamper():
        for i in range(150):
            seals.stamp({"interaction": f"t{i}", "flags": [], "chars": i})

    def signer():
        for _ in range(150):
            if seals.height:
                seals.sign_checkpoint()

    threads = [threading.Thread(target=stamper), threading.Thread(target=signer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seals.height == 150
    assert seals.verify_chain()["tamper_evident"] is True
    assert SealChain(cfg).verify_chain()["tamper_evident"] is True   # the file too



# -------------------------------------------- what Coriqo's verifier needs


def test_the_entry_verifies_the_way_coriqo_checks_it(setup):
    """Coriqo (ledger_checkpoint_verify.signing_bytes) verifies Ed25519 over
    canonical(entry minus sig/signature) with the device's enrolled key."""
    from byoai.recorder.canonical import canonicalize
    from byoai.recorder.keys import DeviceKey
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    pub.tick()
    (entry,) = coriqo.batches[0]["checkpoints"]
    body = {k: v for k, v in entry.items() if k not in ("sig", "signature")}
    assert DeviceKey.verify(seals._key.public_key_b64, canonicalize(body), entry["sig"])


def test_send_numbers_never_repeat_with_a_different_head(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    heads = {}
    for _ in range(4):
        _grow(seals)
        clock.t += 7 * 3600
        pub.tick()
    for b in coriqo.batches:
        cp = b["checkpoints"][0]
        assert heads.setdefault(cp["seq_end"], cp["chain_hash"]) == cp["chain_hash"]
    assert sorted(heads) == [1, 2, 3, 4]


def test_a_failed_send_is_retried_unchanged(setup):
    """The outbox: after a failure the same signed entry goes again, even if
    new entries were sealed meanwhile, so one number never names two heads."""
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    coriqo.fail_with = 503
    pub.tick()
    waiting = dict(pub.state.outbox)
    _grow(seals, 3)                                 # chain moves on meanwhile
    coriqo.fail_with = None
    clock.t = pub.state.next_attempt_at + 1
    pub.tick()
    (sent,) = coriqo.batches
    assert sent["checkpoints"][0] == waiting
    assert pub.state.outbox is None and pub.status()["has_new"] is True


def test_publishing_continues_after_a_restart(setup, tmp_path):
    """The chain now reloads in full (no 512-entry window), so the count after
    a restart is the count that was sent. Change is still detected by root, so
    new activity goes out, and an unchanged chain sends only a heartbeat."""
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 523)
    pub.tick()
    reloaded = SealChain(cfg)                       # restart: everything is kept
    assert reloaded.height == 523
    again = Publisher(reloaded, cfg.key_dir, tmp_path, now=clock, client_factory=coriqo.client)
    _grow(reloaded, 3)
    clock.t += 7 * 3600
    assert again.tick() is True and len(coriqo.batches) == 2


def test_no_proxy_is_used(setup, monkeypatch):
    seals, coriqo, clock, pub, cfg = setup
    fresh = Publisher(seals, cfg.key_dir, cfg.key_dir.parent)
    with fresh._client_factory() as client:
        assert client._trust_env is False


def test_an_idle_mac_sends_a_heartbeat_that_is_the_same_entry(setup):
    """Nothing new for hours: Shield resends the accepted seal byte for byte,
    so Coriqo hears from the Mac without storing a second entry."""
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 3)
    pub.tick()
    clock.t += 6 * 3600
    assert pub.tick() is True and len(coriqo.batches) == 2
    assert coriqo.batches[1]["checkpoints"] == coriqo.batches[0]["checkpoints"]
    assert pub.state.last_seq == 1 and pub.state.last_sent_at == clock.t
    # and not again until the next interval
    clock.t += 3600
    assert pub.tick() is False and len(coriqo.batches) == 2


def test_the_heartbeat_stays_between_one_and_six_hours(setup, tmp_path):
    seals, coriqo, clock, _pub, cfg = setup
    fast = Publisher(seals, cfg.key_dir, tmp_path, every_hours=0, now=clock,
                     client_factory=coriqo.client)
    slow = Publisher(seals, cfg.key_dir, tmp_path, every_hours=48, now=clock,
                     client_factory=coriqo.client)
    assert fast.heartbeat_s == 3600 and slow.heartbeat_s == 6 * 3600


def test_a_mac_upgraded_from_before_heartbeats_numbers_its_seal_once_more(setup, tmp_path):
    """State written before heartbeats has no last_entry. The unchanged seal
    goes out once under a new number; from then on the heartbeat repeats it."""
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    pub.tick()
    pub.state.last_entry = None
    pub._save()
    clock.t += 6 * 3600
    assert pub.tick() is True
    assert coriqo.batches[-1]["checkpoints"][0]["seq_end"] == 2
    clock.t += 6 * 3600
    assert pub.tick() is True
    assert coriqo.batches[-1]["checkpoints"] == coriqo.batches[-2]["checkpoints"]


def test_nothing_is_sent_for_an_empty_record(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    clock.t += 24 * 3600
    assert pub.tick() is False and coriqo.batches == []


# ------------------------------------ freshness, protection, record identity


def test_every_request_says_when_it_left_and_whether_shield_protects(setup, tmp_path):
    seals, coriqo, clock, _pub, cfg = setup
    state = {"protecting": True, "reasons": [], "mode": "redact"}
    pub = Publisher(seals, cfg.key_dir, tmp_path, now=clock,
                    client_factory=coriqo.client, protection=lambda: dict(state))
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    pub.tick()
    first = coriqo.batches[0]
    assert first["sent_at"] == "1970-01-12T13:46:40Z"   # the injected clock, RFC3339 UTC
    assert first["shield"] == {"protecting": True, "reasons": [], "mode": "redact",
                                "policy_version": None}
    state.update(protecting=False, reasons=["capture_stopped"])
    clock.t += 6 * 3600
    assert pub.tick() is True                       # heartbeat
    second = coriqo.batches[1]
    assert second["shield"] == {"protecting": False, "reasons": ["capture_stopped"],
                                "mode": "redact", "policy_version": None}
    assert second["sent_at"] != first["sent_at"]
    # the entry is byte for byte the one Coriqo already holds
    assert json.dumps(second["checkpoints"], sort_keys=True) == \
        json.dumps(first["checkpoints"], sort_keys=True)


def test_a_body_is_signed_with_its_top_level_fields(setup):
    """sent_at and shield sit inside what the Mac signs, so neither can be
    swapped on the way."""
    from byoai.recorder.canonical import canonicalize
    from byoai.recorder.keys import DeviceKey
    seals, coriqo, clock, pub, cfg = setup
    seen = {}

    def handler(request):
        if request.url.path == "/v1/checkpoints/batch":
            seen["raw"] = gzip.decompress(request.content)
            seen["headers"] = dict(request.headers)
        return coriqo.handler(request)
    pub._client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    _enrol(cfg, coriqo)
    _grow(seals)
    pub.tick()
    body = json.loads(seen["raw"])
    assert "sent_at" in body and "shield" in body
    assert seen["raw"] == canonicalize(body)        # the signed bytes are the whole body
    assert DeviceKey.verify(seals._key.public_key_b64, seen["raw"],
                            seen["headers"]["x-coriqo-signature"])


def test_without_a_protection_check_shield_never_claims_protection(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals)
    pub.tick()
    assert coriqo.batches[0]["shield"] == {"protecting": False,
                                          "reasons": ["state_unknown"], "mode": None,
                                          "policy_version": None}


def test_a_failing_protection_check_does_not_stop_the_send(setup, tmp_path):
    seals, coriqo, clock, _pub, cfg = setup

    def broken():
        raise RuntimeError("scutil exploded")
    pub = Publisher(seals, cfg.key_dir, tmp_path, now=clock,
                    client_factory=coriqo.client, protection=broken)
    _enrol(cfg, coriqo)
    _grow(seals)
    assert pub.tick() is True and len(coriqo.batches) == 1
    assert coriqo.batches[0]["shield"]["protecting"] is False


def test_record_id_is_stable_across_restart(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 520)
    rid = seals.record_id
    assert rid
    pub.tick()
    assert coriqo.batches[0]["checkpoints"][0]["record_id"] == rid
    reloaded = SealChain(cfg)                       # restart
    assert reloaded.height == 520 and reloaded.record_id == rid
    again = Publisher(reloaded, cfg.key_dir, tmp_path, now=clock, client_factory=coriqo.client)
    _grow(reloaded)
    clock.t += 7 * 3600
    again.tick()
    assert coriqo.batches[-1]["checkpoints"][0]["record_id"] == rid


def test_record_id_changes_when_the_record_is_wiped(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    _grow(seals)
    rid = seals.record_id
    cfg.seal_path.unlink()                          # the local record is deleted
    fresh = SealChain(cfg)
    assert fresh.record_id and fresh.record_id != rid
    _grow(fresh)
    assert SealChain(cfg).record_id == fresh.record_id   # and the new one is kept


def test_a_seal_file_from_before_record_ids_gets_one_and_keeps_it(setup):
    seals, coriqo, clock, pub, cfg = setup
    _grow(seals, 2)
    old = json.loads(cfg.seal_path.read_text())
    del old["record_id"]
    cfg.seal_path.write_text(json.dumps(old))
    first = SealChain(cfg)
    assert first.record_id and SealChain(cfg).record_id == first.record_id


def test_prev_chain_hash_links_each_new_entry_to_the_last_accepted(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    pub.tick()
    first = coriqo.batches[0]["checkpoints"][0]
    assert first["prev_chain_hash"] is None         # first entry after enrolment
    _grow(seals)
    clock.t += 7 * 3600
    pub.tick()
    second = coriqo.batches[1]["checkpoints"][0]
    assert second["prev_chain_hash"] == first["chain_hash"]
    assert second["chain_hash"] != first["chain_hash"]


def test_prev_chain_hash_skips_a_send_coriqo_never_accepted(setup):
    """A failed send is retried unchanged, so the next new entry still points
    at the last one Coriqo actually stored."""
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals)
    pub.tick()
    accepted = coriqo.batches[0]["checkpoints"][0]["chain_hash"]
    _grow(seals)
    clock.t += 7 * 3600
    coriqo.fail_with = 503
    pub.tick()
    assert pub.state.outbox["prev_chain_hash"] == accepted


def test_reenrolling_starts_the_link_again(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals)
    pub.tick()
    pub.enrol("https://coriqo.test", "cik_live_y")
    assert coriqo.batches[-1]["checkpoints"][0]["prev_chain_hash"] is None
    pub.tick()
    assert coriqo.batches[-1]["checkpoints"][0]["prev_chain_hash"] is None


def test_a_clock_refusal_says_fix_the_clock_and_keeps_retrying(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals)

    def stale(request):
        if request.url.path == "/v1/checkpoints/batch":
            return httpx.Response(401, json={"detail": "Request is stale or dated in "
                                             "the future; check this Mac's clock."})
        return coriqo.handler(request)
    pub._client_factory = lambda: httpx.Client(transport=httpx.MockTransport(stale))
    pub.tick()
    s = pub.status()
    assert s["last_error"] == ("This Mac's clock is wrong. Fix the date and time, "
                               "and Shield will send again on its own.")
    assert s["needs_attention"] is False and "enrolment token" not in s["last_error"]
    assert pub.state.failures == 1 and pub.state.next_attempt_at > clock.t
    # backs off like any transient failure, then recovers on its own
    pub.tick()
    assert pub.state.failures == 1                  # still waiting
    clock.t = pub.state.next_attempt_at + 1
    pub._client_factory = coriqo.client
    pub.tick()
    assert pub.state.failures == 0 and pub.status()["last_error"] is None
    assert len(coriqo.batches) == 1


def test_a_401_without_the_clock_is_still_a_refusal(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals)
    coriqo.fail_with = 401
    pub.tick()
    assert pub.status()["needs_attention"] is True


# ------------------------------------------- what "protecting" means on a Mac


@pytest.fixture(autouse=True)
def _proxy_port_answers(monkeypatch):
    """Protection tests fake the proxy's process; its port is faked as open
    too, except where a test says otherwise."""
    import byoai.integrations.shield as shield
    monkeypatch.setattr(shield, "_port_open", lambda port: True)


def test_a_reused_pid_with_nothing_on_the_port_does_not_count(tmp_path, monkeypatch):
    import byoai.integrations.shield as shield
    from byoai.integrations.shield import protection_state
    cfg = _protection_cfg(tmp_path)
    _proxy_running(cfg)
    monkeypatch.setattr(shield, "_port_open", lambda port: False)
    assert protection_state(cfg, system_proxy=lambda: None)["reasons"] == ["capture_stopped"]


def _protection_cfg(tmp_path, policy=None):
    cfg = ShieldConfig(ledger=tmp_path / "c.jsonl", seal_path=tmp_path / "s.json",
                       key_dir=tmp_path / "keys", policy_path=tmp_path / "policy.json")
    if policy is not None:
        (tmp_path / "policy.json").write_text(json.dumps({"policy_version": 2, **policy}))
    return cfg


def _proxy_running(cfg, port=8080):
    import os

    from byoai.integrations.shield import proxy_alive_path
    proxy_alive_path(cfg.ledger).write_text(json.dumps({"pid": os.getpid(), "port": port}))


def test_protecting_when_the_proxy_runs_and_redacts(tmp_path):
    from byoai.integrations.shield import protection_state
    cfg = _protection_cfg(tmp_path)
    _proxy_running(cfg)
    got = protection_state(cfg, system_proxy=lambda: (True, 8080))
    assert got == {"protecting": True, "reasons": [], "mode": "redact"}


def test_not_protecting_names_each_reason(tmp_path):
    from byoai.integrations.shield import protection_state
    cfg = _protection_cfg(tmp_path, {"mode": "observe",
                                     "apps": {"claude": False, "chatgpt": False}})
    got = protection_state(cfg, system_proxy=lambda: (True, 8080))
    assert got["protecting"] is False and got["mode"] == "observe"
    assert got["reasons"] == ["capture_stopped", "policy_monitor_only", "no_apps_enabled"]


def test_system_proxy_off_or_elsewhere_is_not_protecting(tmp_path):
    from byoai.integrations.shield import protection_state
    cfg = _protection_cfg(tmp_path)
    _proxy_running(cfg, port=8080)
    assert protection_state(cfg, system_proxy=lambda: (False, None))["reasons"] == ["proxy_off"]
    assert protection_state(cfg, system_proxy=lambda: (True, 9999))["reasons"] == ["proxy_off"]
    # not a Mac, or scutil unreadable: no claim either way about the system proxy
    assert protection_state(cfg, system_proxy=lambda: None)["protecting"] is True


def test_a_crashed_proxy_leaves_a_marker_that_does_not_count(tmp_path):
    from byoai.integrations.shield import protection_state, proxy_alive_path
    cfg = _protection_cfg(tmp_path)
    proxy_alive_path(cfg.ledger).write_text(json.dumps({"pid": 2 ** 22 + 12345, "port": 8080}))
    assert protection_state(cfg, system_proxy=lambda: None)["reasons"] == ["capture_stopped"]


def test_the_proxy_marks_itself_running_and_clears_on_stop(tmp_path):
    from byoai.integrations.shield import proxy_alive_path, read_proxy_alive
    from byoai.integrations.shield_proxy import ShieldProxy
    ledger = tmp_path / "c.jsonl"
    proxy = ShieldProxy(ledger=ledger, policy_path=tmp_path / "policy.json")
    proxy.running()
    assert read_proxy_alive(ledger) is not None
    proxy.done()
    assert not proxy_alive_path(ledger).exists() and read_proxy_alive(ledger) is None


# --------------------------------------------------- managed policy polling
#
# Phase 1 of the Shield MSP plan: poll POST /v1/shield/policy, signed exactly
# like the checkpoint post above; pin the signing key at enrolment or (an
# already-enrolled device) by fetching the public-keys list once; verify and
# apply via byoai.integrations.shield.apply_managed_envelope.

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from byoai.integrations.shield import load_managed_policy
from byoai.recorder.canonical import canonicalize

# Coriqo signs with its own server key, not a device key: lowercase hex of the
# raw 32-byte public key and the raw 64-byte signature, no "ed25519:" prefix.
# These fixtures build server-shaped envelopes/responses, not DeviceKey's.


def _server_key():
    return Ed25519PrivateKey.generate()


def _pub_hex(key) -> str:
    return key.public_key().public_bytes_raw().hex()


def _policy_envelope(key, **doc_overrides):
    doc = {
        "policy_id": "pol_1", "version": 1, "tenant_slug": "acme_bank",
        "device_id": None, "issued_at": "2026-09-26T10:00:00Z",
        "key_id": "coriqo-v1", "managed_by": "Acme IT",
        "policy": {"mode": "block", "apps": {"claude": True}, "keep_text": False,
                   "retention_days": 7, "notice": True},
        "locked": ["mode"],
    }
    doc.update(doc_overrides)
    return {"document": doc, "signature": key.sign(canonicalize(doc)).hex()}


def test_poll_policy_with_a_key_pinned_at_enrolment_applies_a_good_signature(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    coriqo.enrol_policy_key = {"key_id": "coriqo-v1", "public_key": _pub_hex(key)}
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)
    pub.poll_policy(force=True)
    stored = load_managed_policy(pub.managed_policy_path)
    assert stored["envelope"]["document"]["version"] == 1
    assert pub.policy_status()["error"] is None
    (req,) = coriqo.policy_requests
    assert "sent_at" in req and req["have_version"] is None


def test_poll_policy_pins_the_key_by_fetching_public_keys_once(setup, tmp_path):
    """An already-enrolled device (no policy_key from /v1/enroll) fetches
    GET .../public-keys once and pins whichever entry matches the envelope's
    key_id."""
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)                              # no policy_key pinned
    coriqo.public_keys = [{"key_id": "coriqo-v1", "public_key": _pub_hex(key)}]
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 1
    assert coriqo.public_keys_requests == 1
    # a second poll with another changed envelope reuses the pinned key
    coriqo.policy_envelope = _policy_envelope(key, version=2)
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 2
    assert coriqo.public_keys_requests == 1          # not fetched again


def test_poll_policy_pins_a_pem_key_from_the_real_public_keys_shape(setup, tmp_path):
    """Coriqo's GET .../public-keys sends ``public_key_pem`` (an SPKI PEM),
    not ``public_key`` — Shield loads it and pins the raw hex form."""
    from cryptography.hazmat.primitives import serialization
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    coriqo.public_keys = [{"key_id": "coriqo-v1", "public_key_pem": pem, "revoked": False,
                          "active": True}]
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 1
    assert pub.state.policy_public_key == _pub_hex(key)


def test_a_base64_key_pinned_at_enrolment_is_normalised_to_hex(setup, tmp_path):
    import base64
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    raw = key.public_key().public_bytes_raw()
    coriqo.enrol_policy_key = {"key_id": "coriqo-v1",
                               "public_key": base64.b64encode(raw).decode()}
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 1
    assert pub.policy_status()["error"] is None


def test_a_key_list_outage_is_not_reported_as_an_unknown_key_and_retries_soon(setup, tmp_path):
    from byoai.integrations.shield_publish import POLICY_POLL_S
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    coriqo.public_keys = [{"key_id": "coriqo-v1", "public_key": _pub_hex(key)}]
    coriqo.public_keys_down = True
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path) is None
    assert "signing key" in pub.policy_status()["error"]
    assert "unknown" not in pub.policy_status()["error"]
    clock.t += 61                                    # due again in a minute, not ten
    assert pub.policy_due() and 61 < POLICY_POLL_S
    coriqo.public_keys_down = False
    pub.poll_policy()
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 1


def test_a_revoked_key_is_refused_even_if_the_id_matches(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    from cryptography.hazmat.primitives import serialization
    pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    coriqo.public_keys = [{"key_id": "coriqo-v1", "public_key_pem": pem, "revoked": True}]
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path) is None
    assert "unknown key" in pub.policy_status()["error"]


def test_an_unknown_key_id_is_refused_and_fetched_only_once(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    other = _server_key()
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    coriqo.public_keys = [{"key_id": "some-other-key", "public_key": _pub_hex(other)}]
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)   # signed with a key Coriqo never lists
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path) is None
    assert "unknown key" in pub.policy_status()["error"]
    assert coriqo.public_keys_requests == 1
    pub.poll_policy(force=True)                      # doesn't refetch every poll
    assert coriqo.public_keys_requests == 1


def test_a_bad_signature_wrong_tenant_and_rollback_are_refused_and_keep_the_policy(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    coriqo.enrol_policy_key = {"key_id": "coriqo-v1", "public_key": _pub_hex(key)}
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key, version=5)
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 5

    bad = _policy_envelope(key, version=6)
    bad["signature"] = bad["signature"][:-4] + "abcd"
    coriqo.policy_envelope = bad
    pub.poll_policy(force=True)
    assert "signature" in pub.policy_status()["error"]
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 5

    coriqo.policy_envelope = _policy_envelope(key, version=6, tenant_slug="someone_else")
    pub.poll_policy(force=True)
    assert "tenant" in pub.policy_status()["error"]
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 5

    coriqo.policy_envelope = _policy_envelope(key, version=4)   # older than what's stored
    pub.poll_policy(force=True)
    assert "rollback" in pub.policy_status()["error"]
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 5


def test_unmanage_clears_the_locked_keys(setup, tmp_path):
    from byoai.integrations.shield import load_policy, merge_policy, save_policy
    seals, coriqo, clock, pub, cfg = setup
    cfg.managed_policy_path = tmp_path / "managed.json"
    pub.managed_policy_path = cfg.managed_policy_path
    key = _server_key()
    coriqo.enrol_policy_key = {"key_id": "coriqo-v1", "public_key": _pub_hex(key)}
    _enrol(cfg, coriqo)
    save_policy(cfg, merge_policy({"mode": "redact"}))
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)
    pub.poll_policy(force=True)
    assert load_policy(cfg)["mode"] == "block"
    coriqo.policy_envelope = _policy_envelope(key, version=2, policy=None, locked=[])
    pub.poll_policy(force=True)
    assert load_policy(cfg)["mode"] == "redact"


def test_a_poll_failure_keeps_the_current_policy_and_never_raises(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    coriqo.enrol_policy_key = {"key_id": "coriqo-v1", "public_key": _pub_hex(key)}
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    coriqo.policy_changed = True
    coriqo.policy_envelope = _policy_envelope(key)
    pub.poll_policy(force=True)
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 1
    coriqo.policy_fail = True
    coriqo.policy_envelope = _policy_envelope(key, version=2)
    pub.poll_policy(force=True)                      # never raises
    assert load_managed_policy(pub.managed_policy_path)["envelope"]["document"]["version"] == 1
    assert pub.policy_status()["error"] is not None
    _grow(seals)
    assert pub.tick() is True                        # capture/publishing keeps going


def test_poll_policy_only_runs_every_ten_minutes_and_on_send_now(setup, tmp_path):
    seals, coriqo, clock, pub, cfg = setup
    key = _server_key()
    coriqo.enrol_policy_key = {"key_id": "coriqo-v1", "public_key": _pub_hex(key)}
    pub.managed_policy_path = tmp_path / "managed.json"
    _enrol(cfg, coriqo)
    pub.tick()                                       # "at start"
    assert len(coriqo.policy_requests) == 1
    pub.tick()                                       # not due yet
    assert len(coriqo.policy_requests) == 1
    clock.t += 601
    pub.tick()
    assert len(coriqo.policy_requests) == 2
    _grow(seals)
    pub.publish_now()                                # "Send now" forces a poll
    assert len(coriqo.policy_requests) == 3
