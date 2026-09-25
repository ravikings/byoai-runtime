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

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/enroll":
            return httpx.Response(201, json={
                "device_id": "dev-enrolled-1", "coriqo_base_url": "https://coriqo.test",
                "tenant_slug": "acme_bank", "label": "shield"})
        if request.url.path == "/v1/checkpoints/batch":
            if self.fail_with:
                headers = {"retry-after": self.retry_after} if self.retry_after else {}
                return httpx.Response(self.fail_with, json={"detail": "nope"}, headers=headers)
            body = json.loads(gzip.decompress(request.content))
            assert request.headers["x-coriqo-signature"]
            self.batches.append(body)
            return httpx.Response(200, json={"accepted": 1, "duplicates": 0, "rejected": []})
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
    assert set(batch) == {"device_id", "checkpoints"}   # no ledger rows, no text
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
    assert coriqo.batches[-1] == coriqo.batches[0] and again.state.last_seq == 1


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


def test_publishing_continues_after_a_restart_trims_the_chain(setup, tmp_path):
    """The seal chain reloads only its last 512 entries, so after a restart its
    count drops below what was last sent. Change is detected by root, so new
    activity still goes out; comparing counts would stall until the count
    climbed past the old value."""
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 523)
    pub.tick()
    reloaded = SealChain(cfg)                       # restart: window of 512
    assert reloaded.height == 512
    again = Publisher(reloaded, cfg.key_dir, tmp_path, now=clock, client_factory=coriqo.client)
    _grow(reloaded, 3)                              # 515 < 523 last sent
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
    assert coriqo.batches[1] == coriqo.batches[0]
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
    assert coriqo.batches[-1] == coriqo.batches[-2]


def test_nothing_is_sent_for_an_empty_record(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    clock.t += 24 * 3600
    assert pub.tick() is False and coriqo.batches == []
