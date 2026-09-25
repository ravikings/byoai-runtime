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
    assert cp["seq_end"] == 3 and cp["chain_hash"] == cp["checkpoint"]["root_hex"]
    assert cp["signature"].startswith("ed25519:")
    assert set(batch) == {"device_id", "checkpoints"}   # no ledger rows, no text
    # same head again: nothing to send, however much time passes
    clock.t += 10 * 3600
    assert pub.tick() is False and len(coriqo.batches) == 1
    s = pub.status()
    assert s["connected"] and s["tenant"] == "acme_bank" and s["pending_entries"] == 0


def test_new_entries_wait_for_the_interval(setup):
    seals, coriqo, clock, pub, cfg = setup
    _enrol(cfg, coriqo)
    _grow(seals, 2)
    pub.tick()
    _grow(seals, 5)
    clock.t += 2 * 3600
    assert pub.tick() is False                      # grew, but only 2h later
    assert pub.status()["pending_entries"] == 5
    clock.t += 4 * 3600
    assert pub.tick() is True and len(coriqo.batches) == 2
    assert coriqo.batches[-1]["checkpoints"][0]["seq_end"] == 7


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
    assert again.state.last_height == 2
    clock.t += 7 * 3600
    assert again.tick() is False                    # restart doesn't resend the same head


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
