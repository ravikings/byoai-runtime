from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import Future
from typing import cast

import pytest

flask = pytest.importorskip("flask")

from flask import Flask, jsonify, request  # noqa: E402
from tests.conftest import FakeProvider  # noqa: E402

from byoai import Runtime  # noqa: E402
from byoai.errors import ConfigurationError  # noqa: E402
from byoai.integrations.flask import (  # noqa: E402
    _FlaskBridge,
    _StreamHandle,
    attach,
    execute,
    get_runtime,
    stream_response,
)


@pytest.fixture
def flask_apps():
    """Tracks every Flask app attach()ed during a test so its bridge (and the
    background event-loop thread it owns) gets closed on teardown — otherwise
    each test leaks a live thread for the rest of the pytest process."""
    apps: list[Flask] = []
    yield apps
    for app in apps:
        bridge = app.extensions.get("byoai")
        if bridge is not None:
            bridge.close()


def make_app(track: list[Flask], *, providers=None) -> Flask:
    app = Flask(__name__)
    attach(app, Runtime(providers=providers or [FakeProvider()]))
    track.append(app)

    @app.post("/ask")
    def ask():
        result = execute(request.get_json()["query"])
        return jsonify({"content": result.content, "cached": result.cached})

    @app.post("/ask/stream")
    def ask_stream():
        return stream_response(request.get_json()["query"])

    return app


def test_ask_roundtrip(flask_apps):
    app = make_app(flask_apps)
    client = app.test_client()
    response = client.post("/ask", json={"query": "hi"})
    assert response.status_code == 200
    assert response.get_json() == {"content": "hello from fake", "cached": False}


def test_sse_stream(flask_apps):
    app = make_app(flask_apps)
    client = app.test_client()
    response = client.post("/ask/stream", json={"query": "hi"})
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")
    body = response.get_data(as_text=True)
    lines = [line for line in body.split("\n\n") if line.startswith("data:")]
    events = [json.loads(line[len("data:") :]) for line in lines]
    assert events  # at least one frame
    text = "".join(e.get("delta", "") for e in events)
    assert text.strip() == "hello from fake"
    assert events[-1]["done"] is True


def test_sse_stream_carries_tool_call_chunks(flask_apps):
    # Regression: stream_response()'s SSE filter (`if chunk.done or
    # chunk.delta:`) silently dropped every tool_call chunk, since one has
    # delta="" (falsy) and done=False — a forced tool_choice call streamed
    # over this Flask route got zero tool_call frames.
    from tests.conftest import ToolCallingProvider

    app = make_app(flask_apps, providers=[ToolCallingProvider()])
    client = app.test_client()
    response = client.post("/ask/stream", json={"query": "hi"})
    body = response.get_data(as_text=True)
    lines = [line for line in body.split("\n\n") if line.startswith("data:")]
    events = [json.loads(line[len("data:") :]) for line in lines]
    tool_events = [e for e in events if "tool_call" in e]
    assert tool_events == [
        {"delta": "", "tool_call": {"index": 0, "id": "toolu_1", "name": "answer"}},
        {"delta": "", "tool_call": {"index": 0, "partial_json": '{"a": 1}'}},
    ]


def test_attach_twice_on_same_app_returns_same_runtime_and_no_second_bridge(flask_apps):
    app = Flask(__name__)
    flask_apps.append(app)
    runtime = Runtime(providers=[FakeProvider()])
    returned_first = attach(app, runtime)
    bridge_first = app.extensions["byoai"]

    other_runtime = Runtime(providers=[FakeProvider(reply="should never be used")])
    returned_second = attach(app, other_runtime)
    bridge_second = app.extensions["byoai"]

    assert returned_first is runtime
    assert returned_second is runtime  # not other_runtime — idempotent
    assert bridge_first is bridge_second  # no second bridge/thread created
    assert get_runtime(app) is runtime


def test_get_runtime_without_attach_raises_configuration_error():

    app = Flask(__name__)
    with pytest.raises(ConfigurationError):
        get_runtime(app)


class _ClosableFakeProvider(FakeProvider):
    """FakeProvider.close() in conftest.py is a no-op `pass`, so it can't
    prove a Runtime was actually closed — this local subclass adds an
    observable flag."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _SlowClosingProvider(FakeProvider):
    """A provider whose close() takes a configurable, real amount of time
    — used by the close()-budget tests below to distinguish "waited the
    full delay" from "returned early/was cancelled" via the `closed` flag
    plus wall-clock timing in the test itself."""

    def __init__(self, delay: float, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay
        self.closed = False

    async def close(self) -> None:
        await asyncio.sleep(self.delay)
        self.closed = True


def test_attach_twice_closes_the_redundant_second_runtime(flask_apps):
    # Regression: a second attach() call on an already-attached app returned
    # the existing runtime (correct — asserted above) but silently leaked the
    # redundant second Runtime, which already opened a real client/connection
    # in __init__. attach() now closes it instead.
    app = Flask(__name__)
    flask_apps.append(app)
    runtime = Runtime(providers=[FakeProvider()])
    attach(app, runtime)

    redundant_provider = _ClosableFakeProvider(reply="should never be used")
    other_runtime = Runtime(providers=[redundant_provider])
    returned = attach(app, other_runtime)

    assert returned is runtime
    assert redundant_provider.closed is True


def test_run_stream_closes_underlying_async_generator_on_early_exit(flask_apps):
    # Regression: _FlaskBridge.run_stream used to leave the async generator
    # (and the provider stream/connection behind it) unclosed on any exit
    # other than natural exhaustion — e.g. an early client disconnect, where
    # Werkzeug's stream_with_context throws GeneratorExit into the sync
    # generator run_stream() returns. It now wraps the loop in try/finally
    # and always calls agen.aclose().
    from collections.abc import Generator

    closed = {"v": False}

    async def wrapped(inner):
        try:
            async for item in inner:
                yield item
        finally:
            closed["v"] = True

    app = Flask(__name__)
    flask_apps.append(app)
    runtime = attach(app, Runtime(providers=[FakeProvider()]))
    bridge = app.extensions["byoai"]
    assert isinstance(bridge, _FlaskBridge)

    agen = wrapped(runtime.stream("hi"))
    gen = cast(Generator, bridge.run_stream(agen))
    next(gen)  # pull exactly one chunk, leaving the stream mid-flight
    gen.close()  # simulates stream_with_context tearing down early

    assert closed["v"] is True


def test_close_finalizes_stream_idle_between_chunks(flask_apps):
    # Regression: close() only detected in-flight streams via _pending,
    # which only has a future while run_stream() is *actively* awaiting
    # agen.__anext__() — a stream idle between chunks (e.g. the WSGI layer
    # hasn't asked for the next one yet, the common case for most of a real
    # SSE stream's lifetime) had no future to cancel, so close() stopped the
    # loop believing shutdown was clean. The generator's own cleanup then
    # never ran, and a later next(gen) call would stall for the full
    # cleanup timeout against a loop nothing was pumping anymore.

    closed = {"v": False}

    async def slow_stream():
        try:
            yield "first"
            yield "second"  # pragma: no cover - never reached
        finally:
            closed["v"] = True

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    gen = bridge.run_stream(slow_stream())
    assert next(gen) == "first"
    # gen is now idle between chunks: no future in _pending for it.

    start = time.monotonic()
    bridge.close(timeout=2.0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"close() should finalize an idle stream promptly, took {elapsed:.2f}s"
    assert closed["v"] is True, "close() never finalized the idle-between-chunks generator"


def test_close_waits_for_a_stream_that_races_from_idle_to_closing_during_shutdown(flask_apps):
    # Regression: close() snapshots `pending` once at the very start, then
    # separately snapshots `_active_streams` for its idle-stream sweep. A
    # stream that was idle (no future, so absent from that first snapshot)
    # at the instant close() began could still race into actively closing
    # itself in the window between the two snapshots — e.g. an early
    # client disconnect or the WSGI layer finally calling next(gen) again,
    # right as close() runs. Whichever thread drives that lands in
    # run_stream()'s own finally block, which calls _finalize_stream() —
    # unlike run(), it doesn't check _closed, so it schedules a *new*
    # future for agen.aclose() via _run_ignoring_closed(), invisible to
    # close()'s original `pending` snapshot. close() used to stop the loop
    # and join the thread without ever waiting for that new future,
    # leaking the stream's connection instead of finalizing it.
    import threading

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()

    async def slow_cleanup_stream():
        try:
            yield "first"
        finally:
            cleanup_started.set()
            await asyncio.sleep(0.2)  # real cleanup work that takes real time
            cleanup_finished.set()

    from collections.abc import Generator

    gen = cast(Generator, bridge.run_stream(slow_cleanup_stream()))
    assert next(gen) == "first"
    # gen is now idle between chunks: no future in _pending, still in
    # _active_streams — exactly the state close()'s original `pending`
    # snapshot can't see.

    original_sweep = bridge._sweep_idle_streams

    def racing_sweep(handles, *, timeout):
        # Simulate another thread racing this stream from idle to closing
        # itself in the window between close()'s `pending` snapshot (long
        # since taken by the time this sweep call happens) and its
        # `_pending` re-check right after this method returns.
        racer = threading.Thread(target=gen.close, daemon=True)
        racer.start()
        assert cleanup_started.wait(timeout=2.0), (
            "racing gen.close() never reached the generator's own cleanup"
        )
        return original_sweep(handles, timeout=timeout)

    bridge._sweep_idle_streams = racing_sweep

    # stream phase gets half the 1.0s budget (0.5s), comfortably covering
    # the racing stream's own 0.2s cleanup sleep.
    bridge.close(timeout=1.0)

    assert cleanup_finished.is_set(), (
        "close() returned before a stream that raced from idle to closing during "
        "shutdown had actually finished its own cleanup"
    )


def test_close_respects_overall_timeout_even_with_slow_stream_cleanup(flask_apps):
    # Regression: close() gave wait_futures(), the stream sweep,
    # runtime.aclose(), and thread.join() each their own full separate
    # `timeout` budget, stacking into a worst case of ~3-4x the caller's
    # requested timeout instead of ~1x — a process supervisor's SIGKILL
    # grace period is typically sized for the requested timeout, so close()
    # could get killed mid-shutdown before runtime.aclose() ever ran,
    # leaking the provider's connections instead of the graceful close()
    # exists to guarantee. close() now splits its budget instead: streams
    # get the first half, runtime.aclose() a guaranteed second half — see
    # test_close_gives_runtime_aclose_a_guaranteed_share_not_just_leftovers
    # for why a single *shared* deadline (streams get whatever's left,
    # however little) isn't right either. Both the stream *and* the
    # provider's own close() need real time here — otherwise nothing
    # distinguishes "bounded total" from "each phase separately budgeted,"
    # since an instant runtime.aclose() finishes immediately either way.

    async def slow_stream():
        try:
            yield "first"
        finally:
            await asyncio.sleep(0.3)

    app = Flask(__name__)
    flask_apps.append(app)
    # both this provider's close() and the stream outlast `timeout` below
    attach(app, Runtime(providers=[_SlowClosingProvider(delay=0.3)]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    gen = bridge.run_stream(slow_stream())
    assert next(gen) == "first"

    start = time.monotonic()
    # Split budget: sweep gets ~half, times out; runtime.aclose() gets its
    # own guaranteed other half, also times out — total ~1x timeout. Each
    # phase with its own *full* budget instead: sweep consumes ~timeout,
    # then runtime.aclose() burns a *fresh* full timeout failing too — ~2x.
    bridge.close(timeout=0.1)
    elapsed = time.monotonic() - start

    assert elapsed < 0.17, f"close() should stay near its requested timeout, took {elapsed:.2f}s"


def test_close_gives_runtime_aclose_a_guaranteed_share_not_just_leftovers(flask_apps):
    # Regression: sharing one deadline across every phase (the fix for the
    # 3-4x blowup above) reintroduced the *other* problem it replaced —
    # runtime.aclose() could still get ~0s if stream cleanup consumed the
    # whole deadline first. Worse than just skipping cleanly: a timeout
    # firing in _run_ignoring_closed calls future.cancel(), which
    # propagates a *real* cancellation into the still-running
    # runtime.aclose() coroutine, aborting the provider's httpx client
    # mid-teardown — a worse state than either "closed" or "never
    # touched". close() now splits the budget instead of sharing one
    # deadline, so runtime.aclose() always gets a real, non-zero window
    # regardless of how long stream cleanup took.

    async def slow_stream():
        try:
            yield "first"
        finally:
            await asyncio.sleep(1.0)  # far exceeds its own half of the budget

    provider = _SlowClosingProvider(delay=0.05)  # comfortably within its guaranteed half
    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[provider]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    gen = bridge.run_stream(slow_stream())
    assert next(gen) == "first"

    bridge.close(timeout=0.2)  # stream cleanup alone would exhaust this if shared

    assert provider.closed is True, (
        "runtime.aclose() was starved (or cancelled mid-teardown) by stream-cleanup time"
    )


def test_close_gives_runtime_aclose_unused_stream_budget_back(flask_apps):
    # Regression: close() capped runtime.aclose() to a *fixed* half of the
    # requested timeout regardless of how quickly stream cleanup actually
    # finished — so with no active streams at all (the common case; that
    # phase finishes essentially instantly), a runtime.aclose() that
    # legitimately needs more than a fixed half but less than the full
    # requested timeout still got prematurely cancelled, for no reason
    # (nothing else was competing for that unused time).

    # delay is more than half of timeout below, less than all of it
    provider = _SlowClosingProvider(delay=0.2)
    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[provider]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    bridge.close(timeout=0.3)  # no streams at all — that phase finishes instantly

    assert provider.closed is True, (
        "runtime.aclose() was capped to a fixed half of the budget instead of "
        "getting the unused time back from the (instant, streamless) first phase"
    )


def test_close_sweeps_multiple_idle_streams_concurrently_not_serially(flask_apps):
    # Regression: close()'s idle-stream sweep finalized one handle at a
    # time, blocking on each before starting the next — N streams each
    # needing ~0.2s to close serialized into ~N*0.2s total instead of the
    # ~0.2s they could all take if scheduled together on the same loop, and
    # split a shrinking shared budget unevenly (whichever stream went last
    # got whatever scraps were left).

    async def slow_stream():
        try:
            yield "first"
        finally:
            await asyncio.sleep(0.2)

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    gens = [bridge.run_stream(slow_stream()) for _ in range(4)]
    for gen in gens:
        assert next(gen) == "first"
    # All 4 streams are now idle between chunks — no future in _pending for
    # any of them, so close() must find them via the _active_streams sweep.

    start = time.monotonic()
    bridge.close(timeout=2.0)
    elapsed = time.monotonic() - start

    assert elapsed < 0.6, (
        f"sweeping 4 streams that each take ~0.2s should take ~0.2s total "
        f"(concurrent), not ~0.8s (serial); took {elapsed:.2f}s"
    )


def test_run_stream_after_close_fails_fast_not_after_full_timeout(flask_apps):
    # Regression: run_stream() registered/finalized streams with no
    # up-front _closed check. Called after close() had already fully
    # finished (loop stopped, thread joined), self.run(agen.__anext__())
    # still correctly raised ConfigurationError immediately, but the
    # finally-block cleanup then tried to schedule agen.aclose() via
    # run_coroutine_threadsafe onto that dead loop anyway — nothing was
    # left to ever run it, so future.result(timeout=5.0) blocked the
    # calling (Flask worker) thread for the full 5 seconds before giving
    # up, instead of failing fast like the immediate error it wraps.

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])
    bridge.close(timeout=2.0)

    async def agen():
        yield "unreachable"  # pragma: no cover

    start = time.monotonic()
    with pytest.raises(ConfigurationError):
        next(bridge.run_stream(agen()))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"post-close run_stream() should fail fast, took {elapsed:.2f}s"


def test_run_stream_shutdown_cleanup_uses_close_actual_timeout_not_a_fixed_value(flask_apps):
    # Regression: run_stream()'s shutdown-racing cleanup capped itself to a
    # hardcoded 5.0s regardless of what timeout close() was actually called
    # with. close(timeout=0.3) (e.g. a tight preStop grace period) still let
    # a request thread's own cleanup block up to 5s — 10x past what the
    # caller asked for — risking a SIGKILL grace period sized for the
    # requested timeout expiring mid-cleanup instead of the graceful close
    # this exists to guarantee.
    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    captured: dict = {}
    original_finalize = bridge._finalize_stream

    def spy(handle, *, timeout):
        captured["timeout"] = timeout
        return original_finalize(handle, timeout=timeout)

    bridge._finalize_stream = spy
    bridge.close(timeout=0.3)

    async def agen():
        yield "unreachable"  # pragma: no cover

    with pytest.raises(ConfigurationError):
        next(bridge.run_stream(agen()))

    assert captured.get("timeout") == 0.3, (
        "run_stream()'s shutdown-racing cleanup should use close()'s actual "
        f"requested timeout, not a fixed value; got {captured.get('timeout')}"
    )


def test_run_stream_normal_cleanup_is_not_bounded_by_a_fixed_timeout(flask_apps):
    # Regression: run_stream()'s finally-block cleanup was capped at a
    # hardcoded 5s and abandoned via a fire-and-forget cancel() on timeout,
    # even for an ordinary (non-shutdown) exit — a legitimately slow
    # provider stream teardown (slow TLS/socket close, a final flush) could
    # get cut off mid-cleanup instead of being allowed to finish, same as
    # it always could before this class of fix existed. Asserts the actual
    # timeout value passed down rather than a real sleep exceeding some
    # cap — any positive number "shorter than the sleep" would demonstrate
    # the bug just as well as 5.0, so the meaningful thing to check is the
    # value itself, not a race against wall-clock time. The non-shutdown
    # path uses a large-but-finite fallback (not literally unbounded, to
    # close a narrow TOCTOU that could otherwise hang genuinely forever) —
    # what matters here is that it's nowhere near the shutdown-racing 5s
    # cap, not the exact number.
    from collections.abc import Generator

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    captured: dict = {}
    original_run_ignoring_closed = bridge._run_ignoring_closed

    def spy(coro, *, timeout):
        captured["timeout"] = timeout
        return original_run_ignoring_closed(coro, timeout=timeout)

    bridge._run_ignoring_closed = spy

    async def agen():
        yield "first"

    gen = cast(Generator, bridge.run_stream(agen()))
    assert next(gen) == "first"
    gen.close()  # ordinary early-exit, not a bridge shutdown — _closed stays False

    assert captured["timeout"] > 60.0, (
        "ordinary (non-shutdown) cleanup must not be bounded by a short fixed timeout"
    )


def test_stream_response_gives_diagnosable_message_for_unrelated_cancellation(flask_apps):
    # Regression: stream_response()'s SSE loop was widened to also catch
    # ConcurrentCancelledError (so it doesn't escape the ByoAIError-only
    # guard unhandled — a separate earlier fix), but str(CancelledError())
    # is always "" — the client got an uninformative `{"error": "",
    # "done": true}` frame instead of a diagnosable message.
    import threading

    entered = threading.Event()

    from byoai.types import StreamChunk

    class BlockingProvider(FakeProvider):
        async def stream(self, messages, **options):
            entered.set()
            await asyncio.Event().wait()
            yield StreamChunk(done=True)  # pragma: no cover - unreachable

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[BlockingProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    @app.post("/ask/stream")
    def ask_stream():
        return stream_response(request.get_json()["query"])

    client = app.test_client()
    result: dict = {}

    def do_request():
        response = client.post("/ask/stream", json={"query": "hi"})
        result["body"] = response.get_data(as_text=True)

    requester = threading.Thread(target=do_request, daemon=True)
    requester.start()
    assert entered.wait(timeout=2.0), "request thread never reached the blocking stream call"

    with bridge._lock:
        pending = list(bridge._pending)
    assert len(pending) == 1
    pending[0].cancel()  # cancel directly — NOT via close(); bridge stays open

    requester.join(timeout=3.0)
    assert not requester.is_alive()

    body = result["body"]
    lines = [line for line in body.split("\n\n") if line.startswith("data:")]
    events = [json.loads(line[len("data:") :]) for line in lines]
    error_events = [e for e in events if "error" in e]
    assert error_events, "expected a terminal error frame"
    assert error_events[-1]["error"] != "", "error message must not be empty"


def test_stream_response_does_not_mislabel_an_empty_byoai_error_as_cancelled(flask_apps):
    # Regression: the fallback added above for ConcurrentCancelledError's
    # always-empty str() applied to *any* exception this except clause
    # caught, not just cancellations — a genuine ByoAIError that happens
    # to carry no message (e.g. a bring-your-own-function provider raising
    # ByoAIError() bare) was mislabeled "request cancelled" even though
    # nothing was ever cancelled, misleading any client-side handling that
    # branches on that specific message.
    from byoai.errors import ByoAIError

    class BareErrorProvider(FakeProvider):
        async def stream(self, messages, **options):
            raise ByoAIError()
            yield  # pragma: no cover - unreachable, makes this an async generator

    app = make_app(flask_apps, providers=[BareErrorProvider()])
    client = app.test_client()
    response = client.post("/ask/stream", json={"query": "hi"})
    body = response.get_data(as_text=True)
    lines = [line for line in body.split("\n\n") if line.startswith("data:")]
    events = [json.loads(line[len("data:") :]) for line in lines]
    error_events = [e for e in events if "error" in e]

    assert error_events, "expected a terminal error frame"
    assert error_events[-1]["error"] != "request cancelled", (
        "a genuine (non-cancellation) ByoAIError with no message must not be "
        "mislabeled as a cancellation"
    )
    assert error_events[-1]["error"] == "ByoAIError"


def test_run_stream_does_not_reclose_a_generator_whose_future_was_cancelled(flask_apps):
    # Regression: close() cancelling an in-flight run_stream() future woke
    # the caller into run_stream()'s finally block, which scheduled a brand
    # new agen.aclose() task — while the *original*, just-cancelled
    # __anext__() task was still separately unwinding through the very same
    # generator's frame on the loop (a task's cancellation propagates
    # through and finalizes an async generator on its own, the same way an
    # explicit aclose() would — the generator ends up closed either way,
    # which is why an end-to-end "did cleanup run" assertion can't catch
    # this reliably). Only one task can drive a generator's frame at a
    # time, so a second, redundant aclose() attempt risked "RuntimeError:
    # aclose(): asynchronous generator is already running" — a narrow
    # interleaving window, not deterministic from end state, so this
    # verifies directly (via a spy) that the redundant attempt is never
    # made at all rather than racing to observe its failure.
    import threading

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    entered = threading.Event()
    aclose_attempts = {"n": 0}
    original_run_ignoring_closed = bridge._run_ignoring_closed

    def spy(coro, *, timeout):
        # Exclude close()'s own runtime.aclose() call — only interested in
        # attempts to aclose() *this test's* generator.
        if "Runtime.aclose" not in getattr(coro, "__qualname__", ""):
            aclose_attempts["n"] += 1
        return original_run_ignoring_closed(coro, timeout=timeout)

    bridge._run_ignoring_closed = spy

    async def agen():
        yield "first"
        entered.set()
        await asyncio.Event().wait()  # blocks forever — cancellable mid-await

    gen = bridge.run_stream(agen())
    assert next(gen) == "first"

    outcome: dict = {}

    def pull_next():
        try:
            next(gen)
        except Exception as exc:  # noqa: BLE001 - any error beats a hang
            outcome["exc"] = exc

    puller = threading.Thread(target=pull_next, daemon=True)
    puller.start()
    assert entered.wait(timeout=2.0), "pull_next thread never reached the blocking await"

    bridge.close(timeout=2.0)
    puller.join(timeout=3.0)

    assert not puller.is_alive()
    assert aclose_attempts["n"] == 0, (
        "run_stream() tried to aclose() a generator whose cancellation was already unwinding it"
    )


def test_close_excludes_cancelled_streams_from_its_sweep_via_pending_owner(flask_apps):
    # Regression: close() relied on run_stream()'s own (separate-thread)
    # cleanup having already discarded a cancelled stream's handle from
    # _active_streams by the time close() got around to reading it for the
    # idle-stream sweep — but cancelling a run_coroutine_threadsafe future
    # only *requests* the underlying task's cancellation asynchronously (it
    # doesn't run inline), so that discard could lag behind close()'s read,
    # letting the sweep schedule a second, racing aclose() on a generator
    # whose cancellation was still unwinding. The test above establishes
    # the *observable* guarantee (aclose() attempted exactly once) but
    # can't force the actual race deterministically — real thread timing
    # on this hardware doesn't reliably reproduce it either way. This
    # tests the actual mechanism directly instead: simulate the exact
    # mid-race state (a stream handle with a registered-but-unresolved
    # pending future, as run() would have mid-await, before anything has
    # processed its eventual cancellation) in a single thread, and verify
    # close() excludes it via _pending_owner in the same step that cancels
    # the future — not by relying on another thread to have done it first.

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    async def agen():
        yield "first"  # pragma: no cover - never actually driven

    handle = _StreamHandle(agen())
    fake_future: Future = Future()  # stands in for a real run_coroutine_
    # threadsafe future mid-await; never scheduled on the loop, so nothing
    # but close() itself ever touches it — isolates the exact mechanism.
    with bridge._lock:
        bridge._active_streams.add(handle)
        bridge._pending.add(fake_future)
        bridge._pending_owner[fake_future] = handle

    # _sweep_idle_streams also marks a handle done as it processes it, so
    # the end state (handle.done, absence from _active_streams) holds
    # either way — the meaningful thing to check is whether the handle
    # ever reached the sweep at all, i.e. was ever treated as idle instead
    # of being excluded up front for the reason it actually left
    # _active_streams here: a cancelled future, not idleness.
    sweep_calls: list[list] = []
    original_sweep = bridge._sweep_idle_streams

    def spy_sweep(handles, *, timeout):
        sweep_calls.append(list(handles))
        return original_sweep(handles, timeout=timeout)

    bridge._sweep_idle_streams = spy_sweep

    bridge.close(timeout=0.5)

    assert handle.done is True
    assert handle not in bridge._active_streams
    assert all(handle not in call for call in sweep_calls), (
        "close() let a cancelled stream's handle reach the idle-stream sweep"
    )


def test_close_does_not_skip_finalizing_a_future_that_completed_normally(flask_apps):
    # Regression: close() marked a stream handle's `done = True` *before*
    # confirming its future was actually cancelled. future.cancel() is a
    # no-op (returns False) if the underlying task already completed
    # normally — e.g. the provider yielded its next chunk right as
    # shutdown began. In that case the handle was incorrectly marked
    # "already finalized" even though nothing had actually closed its
    # generator, so it was silently excluded from the idle-stream sweep
    # too — leaking the generator and the provider connection behind it,
    # the exact class of bug this whole mechanism exists to prevent.

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    async def agen():
        yield "first"  # pragma: no cover - never actually driven

    handle = _StreamHandle(agen())
    already_done_future: Future = Future()
    already_done_future.set_result("chunk")  # completed normally: cancel() is a no-op
    with bridge._lock:
        bridge._active_streams.add(handle)
        bridge._pending.add(already_done_future)
        bridge._pending_owner[already_done_future] = handle

    sweep_calls: list[list] = []
    original_sweep = bridge._sweep_idle_streams

    def spy_sweep(handles, *, timeout):
        sweep_calls.append(list(handles))
        return original_sweep(handles, timeout=timeout)

    bridge._sweep_idle_streams = spy_sweep

    bridge.close(timeout=0.5)

    # A future that couldn't actually be cancelled must not have been
    # treated as "excluded due to cancellation" — it should still have
    # reached the idle sweep (where it's safely finalized: nothing was
    # ever really driving this never-started generator's frame) instead of
    # being silently skipped.
    assert any(handle in call for call in sweep_calls), (
        "a handle whose future completed normally (not cancelled) was incorrectly "
        "excluded from the sweep instead of being properly finalized through it"
    )
    assert handle.done is True


def test_close_unblocks_in_flight_run_stream_instead_of_hanging(flask_apps):
    # Regression: run()/run_stream()'s future.result() has no timeout, and
    # close() used to just stop the loop without cancelling any in-flight
    # future — a request thread blocked mid-stream (e.g. awaiting the next
    # provider chunk) at the moment of a graceful shutdown hung forever
    # instead of erroring out, since a stopped loop never resolves it.
    import threading

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    closed = {"v": False}
    entered = threading.Event()  # set from the bridge's loop thread, right
    # before the blocking await — a deterministic signal that pull_next()'s
    # future is actually registered in _pending, instead of guessing with a
    # fixed sleep() that could be too short under a loaded CI runner.

    async def never_resolves():
        try:
            yield "first"
            entered.set()
            await asyncio.Event().wait()  # blocks forever — simulates a stuck provider call
            yield "unreachable"  # pragma: no cover
        finally:
            closed["v"] = True

    gen = bridge.run_stream(never_resolves())
    assert next(gen) == "first"

    outcome: dict = {}

    def pull_next():
        try:
            next(gen)
        except StopIteration:
            outcome["stopped"] = True
        except Exception as exc:  # noqa: BLE001 - any error beats a hang
            outcome["exc"] = exc

    # daemon=True: if this regresses and the fix's cancellation doesn't
    # unblock it, this thread must not hang the whole test process — the
    # assertion below is what should fail, not the test runner itself.
    puller = threading.Thread(target=pull_next, daemon=True)
    puller.start()
    assert entered.wait(timeout=2.0), "pull_next thread never reached the blocking await"

    bridge.close(timeout=2.0)
    puller.join(timeout=3.0)

    assert not puller.is_alive(), "close() left the in-flight request thread hanging"
    assert "exc" in outcome or outcome.get("stopped")
    # Regression: run_stream()'s finally-block cleanup used to route through
    # run(), which refuses once _closed is True — so the abandoned
    # generator's aclose() was created but never awaited, leaking the
    # provider stream/connection behind it despite this method's own
    # docstring guarantee.
    assert closed["v"] is True, "run_stream() never finalized the abandoned generator"


def test_close_waits_for_a_cancelled_stream_to_actually_finish_cleanup(flask_apps):
    # Guards a subtle invariant close()'s cancel-then-wait step depends on:
    # concurrent.futures.wait() on a just-cancelled run_coroutine_threadsafe
    # future genuinely does wait for the underlying task (and the generator
    # it's driving) to actually finish running — not just for the wrapping
    # future's own state to flip to CANCELLED. That flip happens
    # synchronously, in close()'s own thread, the instant future.cancel()
    # is called — well before the real asyncio Task cancellation that call
    # merely *schedules* has even been delivered on the loop. It would be a
    # real bug if wait() returned at that point: close() could then stop
    # the loop and join the thread while the generator's own cleanup (its
    # `except CancelledError` handling, below) was still genuinely running,
    # abandoning it mid-flight — a connection leak on graceful shutdown.
    # wait() is safe here for a easy-to-miss reason (an earlier pass at
    # this test's design assumed the opposite and "fixed" it with a
    # redundant threading.Event-based signal before this was caught):
    # wait()'s internal "is it done" check only treats
    # CANCELLED_AND_NOTIFIED, not plain CANCELLED, as done, and a
    # run_coroutine_threadsafe future only reaches CANCELLED_AND_NOTIFIED
    # once the real task has actually finished. See close()'s own comment
    # on this. This test exists to catch a future change that breaks that
    # invariant (e.g. swapping wait() for a plain .done()/.cancelled()
    # poll, which resurfaces the bug this reasoning correctly ruled out).
    import threading

    cleanup_finished = threading.Event()
    entered = threading.Event()

    async def slow_cleanup_stream():
        try:
            yield "first"
            entered.set()
            await asyncio.Event().wait()  # blocks forever — cancelled by close()
            yield "unreachable"  # pragma: no cover
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # real cleanup work that takes real time
            cleanup_finished.set()
            raise

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    gen = bridge.run_stream(slow_cleanup_stream())
    assert next(gen) == "first"

    outcome: dict = {}

    def pull_next():
        try:
            next(gen)
        except BaseException as exc:  # noqa: BLE001 - any error beats a hang
            outcome["exc"] = exc

    puller = threading.Thread(target=pull_next, daemon=True)
    puller.start()
    assert entered.wait(timeout=2.0), "pull_next thread never reached the blocking await"

    # stream phase gets half the budget (0.5s), comfortably covering the
    # generator's own 0.2s cleanup sleep.
    bridge.close(timeout=1.0)
    puller.join(timeout=2.0)

    assert cleanup_finished.is_set(), (
        "close() returned before the cancelled stream's own cleanup had actually finished running"
    )


def test_run_does_not_mislabel_unrelated_cancellation_as_bridge_closed(flask_apps):
    # Regression: run() used to translate *any*
    # concurrent.futures.CancelledError into ConfigurationError("byoai Flask
    # bridge is closed"), even when the cancellation had nothing to do with
    # the bridge shutting down — misleading callers into believing the
    # process was shutting down when it wasn't.
    import threading
    from concurrent.futures import CancelledError as ConcurrentCancelledError

    app = Flask(__name__)
    flask_apps.append(app)
    attach(app, Runtime(providers=[FakeProvider()]))
    bridge = cast(_FlaskBridge, app.extensions["byoai"])

    # Set from the bridge's loop thread once the coroutine actually starts
    # running — by then run() has already added its future to _pending
    # (that happens synchronously before the loop gets a chance to run the
    # coroutine at all), so this is a deterministic signal instead of a
    # fixed sleep() that could be too short under a loaded CI runner.
    entered = threading.Event()

    async def blocks_forever():
        entered.set()
        await asyncio.Event().wait()

    outcome: dict = {}

    def call_run():
        try:
            bridge.run(blocks_forever())
        except Exception as exc:  # noqa: BLE001 - capturing whatever run() raises
            outcome["exc"] = exc

    caller = threading.Thread(target=call_run, daemon=True)
    caller.start()
    assert entered.wait(timeout=2.0), "call_run thread never reached the blocking await"

    with bridge._lock:
        pending = list(bridge._pending)
    assert len(pending) == 1
    pending[0].cancel()  # cancel directly — NOT via close()
    caller.join(timeout=3.0)

    assert bridge._closed is False  # the bridge itself was never closed
    assert not caller.is_alive()
    assert isinstance(outcome.get("exc"), ConcurrentCancelledError)
    assert not isinstance(outcome.get("exc"), ConfigurationError)
