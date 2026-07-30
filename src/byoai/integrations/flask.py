"""Flask integration — sync WSGI in front of the async ByoAI runtime.

Flask is WSGI/sync; :class:`~byoai.Runtime` (and its httpx-based providers)
is asyncio-native. This module bridges the two with a persistent background
event-loop thread owned by the attached app, so route handlers stay plain
``def`` — no ``flask[async]``/asgiref, no ``async def`` views, and (unlike a
fresh ``asyncio.run()`` per request) the provider's httpx connection pool
survives across requests instead of reconnecting every call.

Usage with an existing Flask app, built inside an application factory (see
the "Deploying with gunicorn" note below for why the factory matters here)::

    from flask import Flask, request, jsonify
    from byoai import Runtime
    from byoai.integrations.flask import attach, execute, stream_response

    def create_app():
        app = Flask(__name__)
        attach(app, Runtime(llm={"provider": "anthropic", "model": "claude-sonnet-5"}))

        @app.post("/ask")
        def ask():
            result = execute(request.get_json()["query"])
            return jsonify({"content": result.content, "cached": result.cached})

        @app.post("/ask/stream")
        def ask_stream():
            return stream_response(request.get_json()["query"])

        return app

Deploying with gunicorn: build the ``Runtime``/call ``attach()`` inside the
app factory, never at module import time, and never under ``gunicorn
--preload`` — preload forks the master process *after* this module's
background thread has started, and ``fork()`` doesn't carry other threads
into the child. Use ``gthread`` workers; ``gevent``/``eventlet`` are not
validated against this bridge.

Requires the ``flask`` extra: ``pip install byoai-runtime[flask]``.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
import time
from collections.abc import AsyncGenerator, Iterator
from concurrent.futures import CancelledError as ConcurrentCancelledError
from concurrent.futures import Future
from concurrent.futures import wait as wait_futures
from typing import Any, TypeVar

from .. import _json as json

try:
    from flask import Flask, Response, current_app, stream_with_context
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "byoai.integrations.flask requires Flask: pip install 'byoai-runtime[flask]'"
    ) from exc

from ..errors import ByoAIError, ConfigurationError
from ..runtime import Runtime
from ..transport import chunk_to_dict, has_content
from ..types import ExecutionResult

logger = logging.getLogger(__name__)

_STATE_ATTR = "byoai"

# "Effectively unbounded" for an ordinary (non-shutdown) stream cleanup —
# far beyond any realistic teardown, but finite: a fully unbounded wait
# left a narrow TOCTOU window (close() stops the loop between
# _run_ignoring_closed's liveness check and its scheduling call) able to
# hang a request thread forever instead of just very rarely, very slowly.
_UNBOUNDED_CLEANUP_TIMEOUT = 300.0

T = TypeVar("T")


class _FutureCancelledError(ConfigurationError):
    """``run()`` relabeling a cancelled future's ``ConcurrentCancelledError``
    as "bridge is closed" — a distinct type (not just plain
    ``ConfigurationError``) so ``run_stream()`` can tell this apart from a
    coroutine that was never scheduled at all (the bridge was already
    closed *before* this call started) without relying on
    ``exc.__cause__`` being set exactly right, which a future edit to
    ``run()`` could silently change out from under it.
    """


class _StreamHandle:
    """One run_stream() session's identity, for its whole lifetime — a
    plain object (not the agen itself) so it can be tracked in a set/used as
    a dict key regardless of whether the async generator type is hashable.
    ``done`` (guarded by the bridge's lock) makes finalizing a stream
    idempotent: whichever of run_stream()'s own finally block or close()'s
    shutdown sweep gets there first performs the actual ``agen.aclose()``;
    the other is a no-op, so a generator's frame is never entered by two
    tasks at once (which raises ``RuntimeError: already running``).
    """

    __slots__ = ("agen", "done")

    def __init__(self, agen: AsyncGenerator[Any, None]) -> None:
        self.agen = agen
        self.done = False


class _FlaskBridge:
    """Owns one persistent asyncio event loop, in a background thread, for
    the app's lifetime. Every ``Runtime`` call is dispatched onto it via
    ``run_coroutine_threadsafe`` so the provider's httpx client binds to a
    single loop instead of a fresh one per request (which would break
    connection pooling — httpx binds its transport lazily on first use)."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._loop = asyncio.new_event_loop()
        self._closed = False
        # Set alongside _closed, under the same lock — the timeout close()
        # was actually called with, so a request thread racing shutdown
        # (run_stream()'s finally, below) can bound its own cleanup to
        # close()'s real budget instead of a value picked independently of
        # whatever the caller asked for.
        self._close_timeout = 5.0
        # Every in-flight run()/run_stream() call's future, so close() can
        # unblock request threads waiting on one instead of leaving them
        # hung forever once the loop stops processing callbacks. One lock
        # guards _closed/_pending/_active_streams together: run()'s "check
        # closed, then register" and close()'s "mark closed, then snapshot"
        # must be atomic with respect to each other, or a future/stream can
        # be registered in the TOCTOU window right after close() already
        # took its snapshot — reintroducing the exact hang this exists to
        # prevent, since by then the loop's thread may have already stopped.
        self._pending: set[Future] = set()
        # Every run_stream() session currently open, for its whole lifetime
        # — not just while it happens to have a future in _pending. A
        # generator idle between chunks (the common case: waiting on the
        # WSGI layer to pull the next one) has no live future at all, so
        # _pending alone can't tell close() it still needs finalizing.
        self._active_streams: set[_StreamHandle] = set()
        # Which _StreamHandle (if any) a pending future in _pending belongs
        # to — lets close() synchronously exclude a handle from its idle
        # sweep the *instant* it cancels that handle's future, rather than
        # waiting for run_stream()'s own (asynchronous, racy — see close())
        # cleanup to get around to discarding it from _active_streams.
        self._pending_owner: dict[Future, _StreamHandle] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run_forever, name="byoai-flask-bridge", daemon=True
        )
        self._thread.start()

    def _run_forever(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _track_pending(self, future: Future, *, stream_handle: _StreamHandle | None = None) -> None:
        """Register ``future`` as in-flight. Caller must already hold
        ``self._lock`` — this is a plain mutation, not its own critical
        section, so it can be folded into whichever atomic block the caller
        needs (``run()`` registers in the same lock acquisition as its
        closed-check and scheduling; see its docstring for why that
        matters). Paired with ``_untrack_pending()``.
        """
        self._pending.add(future)
        # stream_handle: run_stream() calling on behalf of a tracked
        # session — see close()'s use of _pending_owner for why this
        # matters (letting close() exclude this handle from its idle
        # sweep synchronously, the instant it cancels this future).
        if stream_handle is not None:
            self._pending_owner[future] = stream_handle

    def _untrack_pending(self, future: Future) -> None:
        with self._lock:
            self._pending.discard(future)
            self._pending_owner.pop(future, None)

    def run(self, coro: Any, *, stream_handle: _StreamHandle | None = None) -> Any:
        with self._lock:
            if self._closed:
                # coro was never scheduled — close() it explicitly rather
                # than dropping the object on the floor, which would surface
                # as a "coroutine was never awaited" warning at GC time.
                coro.close()
                raise ConfigurationError("byoai Flask bridge is closed")
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            self._track_pending(future, stream_handle=stream_handle)
        try:
            return future.result()
        except ConcurrentCancelledError as exc:
            # Only relabel as "bridge closed" if that's actually why this was
            # cancelled — an unrelated cancellation (e.g. an internal timeout
            # inside the awaited coroutine) must not be misreported as
            # shutdown when the bridge is still open.
            if self._closed:
                raise _FutureCancelledError("byoai Flask bridge is closed") from exc
            raise
        finally:
            self._untrack_pending(future)

    def _run_ignoring_closed(self, coro: Any, *, timeout: float) -> Any:
        """Like ``run()``, but doesn't refuse once ``_closed`` is already
        True — for cleanup calls (``agen.aclose()``, ``runtime.aclose()``)
        that must still reach the loop during the window between ``_closed``
        flipping True and the loop actually stopping.

        If the loop's thread has *already* exited (``close()`` finished
        some time ago and a new call reaches this afterward), nothing will
        ever drive a newly-scheduled callback — schedule anyway and this
        blocks for the full ``timeout`` for nothing, so that case is
        detected up front and fails fast instead.

        A large ``timeout`` (``_UNBOUNDED_CLEANUP_TIMEOUT``) lets an
        ordinary (non-shutdown) cleanup run essentially to completion —
        the loop stays alive indefinitely then, so there's no reason to
        cut off a legitimately slow teardown, just to guard against a
        narrow TOCTOU (the liveness check above and this scheduling call
        aren't atomic with a concurrent close() stopping the loop in
        between) that would otherwise hang genuinely forever instead of
        very rarely, very slowly. Callers racing shutdown pass a real
        bound so a call that loses that race (the loop stops before this
        resolves) fails fast instead of hanging the same way an unbounded
        ``run()`` would.

        Registered in ``_pending`` like ``run()``'s futures: ``close()``
        cancelling a ``run()``/``run_stream()`` future is exactly what wakes
        the caller to *make* one of these cleanup calls, so ``close()`` must
        be able to see it and wait for it before stopping the loop out from
        under it.
        """
        if not self._loop.is_running():
            coro.close()
            raise ConfigurationError("byoai Flask bridge is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        with self._lock:
            self._track_pending(future)
        try:
            return future.result(timeout=timeout)
        except Exception:
            future.cancel()
            raise
        finally:
            self._untrack_pending(future)

    def _finalize_stream(self, handle: _StreamHandle, *, timeout: float) -> None:
        """Close ``handle``'s generator exactly once, however this is
        reached: natural exhaustion, an early client disconnect, a
        cancelled in-flight future waking run_stream()'s own finally block,
        or close()'s shutdown sweep finalizing a stream that was idle
        between chunks (see run_stream()'s docstring). The ``done`` guard
        (checked and set atomically under the lock) makes whichever caller
        gets here first the one that actually runs ``aclose()`` — the other
        is a no-op, so the generator's frame is never entered concurrently
        by two different tasks.
        """
        with self._lock:
            if handle.done:
                return
            handle.done = True
            self._active_streams.discard(handle)
        try:
            self._run_ignoring_closed(handle.agen.aclose(), timeout=timeout)
        except Exception:
            logger.warning("byoai Flask bridge: failed to close stream generator", exc_info=True)

    def run_stream(self, agen: AsyncGenerator[T, None]) -> Iterator[T]:
        """Pull one item at a time, blocking the calling (Flask request)
        thread until each is ready — one round trip to the bridge loop per
        chunk. Always closes ``agen`` on exit, including an early client
        disconnect (Werkzeug's ``stream_with_context`` throws ``GeneratorExit``
        in here) — otherwise the abandoned provider stream/connection is
        never finalized on the bridge's loop. Also finalized by close()
        directly if this session is idle between chunks (no pending future
        of its own) when shutdown happens — see ``close()``.
        """
        handle = _StreamHandle(agen)
        with self._lock:
            self._active_streams.add(handle)
        try:
            while True:
                try:
                    yield self.run(agen.__anext__(), stream_handle=handle)
                except StopAsyncIteration:
                    return
        except (ConfigurationError, ConcurrentCancelledError) as exc:
            # run() raised because the future backing this agen.__anext__()
            # call was itself cancelled (ConcurrentCancelledError directly,
            # or _FutureCancelledError relabeling one — a dedicated type,
            # not plain ConfigurationError, so this check doesn't depend on
            # exc.__cause__ being set exactly right; plain ConfigurationError
            # is raised, with no such relabeling, when the bridge was already
            # closed *before* this call even started and nothing was ever
            # scheduled). A real in-flight task being cancelled already runs
            # the generator's own cleanup as it unwinds through
            # __anext__()'s frame — calling aclose() again in the finally
            # below would race a brand-new task against one that may still
            # be mid-unwind on the very same generator (RuntimeError:
            # "already running"). Mark it done now so _finalize_stream's
            # guard skips that redundant, unsafe call; the "never scheduled"
            # case is untouched (no task was ever driving the generator's
            # frame) and still needs it, same as an idle-between-chunks
            # stream.
            if isinstance(exc, (ConcurrentCancelledError, _FutureCancelledError)):
                with self._lock:
                    handle.done = True
                    self._active_streams.discard(handle)
            raise
        finally:
            # Not self.run(): if we got here because close() cancelled the
            # in-flight future above, _closed is already True and run()
            # would refuse to even schedule this cleanup — leaking the
            # generator (and the provider stream/connection behind it)
            # instead of finalizing it, the exact thing this exists to
            # prevent. Bounded only when racing shutdown (_closed already
            # True, close() may stop the loop soon) — an ordinary exit
            # (natural exhaustion, early client disconnect) lets a
            # legitimately slow provider teardown finish instead of cutting
            # it off with a fire-and-forget cancel after an arbitrary cap.
            # Read under the lock like every other access to _closed in
            # this class: an unlocked read here could see a stale False
            # right as a concurrent close() flips it, picking the
            # essentially-unbounded fallback instead of the shutdown-safe
            # cap for a stream that's actually racing shutdown. That cap is
            # close()'s own requested timeout, not a value picked
            # independently of it — close(timeout=0.5) (e.g. a tight
            # preStop budget) must not leave a request thread's own cleanup
            # free to block up to some unrelated fixed duration, well past
            # what close()'s caller actually asked for.
            with self._lock:
                closed = self._closed
                close_timeout = self._close_timeout
            self._finalize_stream(
                handle, timeout=close_timeout if closed else _UNBOUNDED_CLEANUP_TIMEOUT
            )

    def close(self, timeout: float = 5.0) -> None:
        start = time.monotonic()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._close_timeout = timeout
            pending = list(self._pending)
        # Regression: an in-flight run()/run_stream() call on another
        # (Flask request) thread blocks on future.result() with no
        # timeout; once the loop below stops processing new callbacks,
        # that future never resolves and the request thread hangs
        # forever. cancel() unblocks it immediately — run_coroutine_
        # threadsafe's returned future stays in concurrent.futures'
        # PENDING state throughout (the done-callback wiring sets its
        # result directly rather than via the normal running
        # transition), so cancel() succeeds synchronously regardless of
        # whether the underlying coroutine is mid-await.
        #
        # cancel() first, check its own return value, *then* exclude the
        # owning handle — never the other way around. cancel() is a no-op
        # (returns False) if the future already completed normally (the
        # provider yielded its next chunk right as shutdown began); marking
        # the handle done unconditionally here, before confirming the
        # cancellation actually took, would make _finalize_stream silently
        # no-op for a stream that in fact needs — and never gets — a real
        # aclose(), leaking it. Only a future we *actually* interrupted
        # means "close() prevented run_stream() from renewing this future,
        # so nothing is left to drive the generator's frame" and is safe to
        # exclude synchronously here (see the class docstring) rather than
        # waiting for run_stream()'s own (separate-thread, and thus racy —
        # see the sweep below) cleanup to eventually discard it itself.
        with self._lock:
            for future in pending:
                if future.cancel():
                    handle = self._pending_owner.get(future)
                    if handle is not None:
                        handle.done = True
                        self._active_streams.discard(handle)
        # Streams get at most half the budget — never zero, never more than
        # half — so runtime.aclose() below is guaranteed a real window
        # regardless of how long stream cleanup takes. A single deadline
        # every phase shares (streams get whatever's left, however little)
        # sounds fairer but isn't: a starved runtime.aclose() doesn't just
        # get skipped cleanly, _run_ignoring_closed's timeout firing calls
        # future.cancel(), which propagates a *real* cancellation into the
        # still-running coroutine and can abort the provider's httpx client
        # mid-teardown — a worse state than either "closed" or "never
        # touched".
        stream_deadline = start + timeout / 2
        # Wait for every future just cancelled to actually settle before
        # touching _active_streams. Once _closed is True (set above, under
        # the lock), run() can never register a *new* future — every call
        # sees _closed and refuses immediately — and run_stream()'s own
        # finally no longer schedules a cleanup future for a cancelled
        # session either (see the except clause above), so `pending` is a
        # fixed set for the rest of this method: concurrent.futures.wait()
        # can block on it directly instead of polling. This does correctly
        # wait for the *real* task to finish, not just the wrapping future's
        # own cancelled-state flip: cancel() above flips this future to
        # CANCELLED synchronously, but wait()'s internal "is it done"
        # check only treats CANCELLED_AND_NOTIFIED (not plain CANCELLED) as
        # done — and a run_coroutine_threadsafe future only reaches
        # CANCELLED_AND_NOTIFIED via _chain_future's completion callback,
        # which fires once the real asyncio Task (and everything it was
        # awaiting, including a generator's own cleanup unwinding through a
        # cancelled await) has actually finished. (Verified empirically —
        # this same-looking reasoning misled an earlier review pass into
        # treating this as a race and "fixing" it with a redundant
        # threading.Event-based signal; it wasn't needed.) Combined with the
        # proactive discard above, any handle still in _active_streams once
        # every future here is done is *provably* idle (nothing has ever
        # driven its generator's frame during this wait, and nothing new
        # can), so finalizing it directly cannot race a task that's still
        # inside it.
        if pending:
            wait_futures(pending, timeout=max(0.0, stream_deadline - time.monotonic()))
        # A cancelled future above is what wakes a run_stream() caller into
        # finalizing its own stream — but a stream idle between chunks (the
        # common case: waiting on the WSGI layer to pull the next item, e.g.
        # writing to a slow client's socket) never had a future to cancel in
        # the first place, so _pending draining says nothing about it.
        with self._lock:
            idle = list(self._active_streams)
        self._sweep_idle_streams(idle, timeout=max(0.0, stream_deadline - time.monotonic()))
        # Re-check _pending once more before moving on: a stream that was
        # idle (and thus absent from both the `pending` snapshot above and
        # this method's own idle-stream handling of it) at the instant
        # close() began can still race into becoming active in the narrow
        # window between that snapshot and the _active_streams snapshot
        # just above — e.g. the WSGI layer finally calls next(gen) for the
        # first time in a while, right as close() runs. run() then sees
        # _closed already True and refuses immediately (no future, so it's
        # invisible to the `pending` snapshot above) — but run_stream()'s
        # own finally block still calls _finalize_stream(), which (unlike
        # run()) doesn't check _closed and so schedules a *new* future via
        # _run_ignoring_closed(), registered in _pending. Whichever thread's
        # _finalize_stream() call wins that handle's `done` guard is the one
        # that actually runs its aclose() — but if it isn't this method's
        # own sweep just above, that future is a new arrival close() hasn't
        # waited for yet. Not waiting for it here would let close() stop
        # the loop and join the thread while that cleanup is still being
        # scheduled/run on it, leaking the stream's connection instead of
        # finalizing it — the exact class of bug the `pending` wait above
        # exists to prevent, just for a stream that wasn't visible yet at
        # that snapshot.
        with self._lock:
            still_pending = list(self._pending)
        if still_pending:
            wait_futures(still_pending, timeout=max(0.0, stream_deadline - time.monotonic()))
        # Whatever's left of the *full* timeout, not a fixed half: if
        # stream cleanup finished well under its allotted half (the common
        # case — most shutdowns have no active streams at all), runtime.
        # aclose() gets that unused time back instead of being capped to
        # half regardless, which would prematurely cancel a runtime.aclose()
        # that legitimately needs more than half but less than the full
        # requested timeout. Still never less than half — the floor that
        # guarantees it, even if stream cleanup used its entire allotment.
        runtime_timeout = max(timeout / 2, timeout - (time.monotonic() - start))
        # A timed-out runtime.aclose() here is cancelled below but not
        # waited on further: giving it a real settle window (see the
        # stream-cancellation wait above) would need to borrow time from
        # *beyond* the caller's requested `timeout` — by construction,
        # runtime_timeout is already "whatever's left of the full budget",
        # so elapsed time is already at ~timeout the instant this fires,
        # leaving nothing left to wait with. Between overshooting the
        # requested timeout (risking the SIGKILL grace period this class
        # exists to respect) and a cancelled-but-unconfirmed provider
        # teardown, the hard bound on close()'s own total duration wins —
        # same tradeoff already accepted for a mid-teardown abort being
        # "worse than either closed or never touched" in the first place.
        try:
            self._run_ignoring_closed(self.runtime.aclose(), timeout=runtime_timeout)
        except Exception:
            logger.warning("byoai Flask bridge: runtime close failed", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        # Whatever's left of the overall requested timeout — not its own
        # fresh budget stacked on top, which would let close()'s total
        # worst case reach ~1.5x timeout despite the splitting above being
        # specifically about keeping the total near ~timeout. This should
        # be near-instant in practice regardless: nothing should still be
        # running on the loop by this point. Accepted consequence: if the
        # phases above already consumed the *entire* budget (streams and/or
        # runtime.aclose() both timing out), this can receive at or near a
        # 0s timeout, and close() can then return before self._thread has
        # actually finished exiting run_forever() — a caller that needs to
        # observe the thread as fully stopped (not just close() having
        # returned) would need to join() it again themselves. Preferred
        # over giving this its own fresh budget: that reintroduces the
        # ~1.5x-timeout blowup this line exists to prevent, for a case
        # (both earlier phases already timing out) that's already a
        # degraded shutdown regardless.
        self._thread.join(timeout=max(0.0, timeout - (time.monotonic() - start)))

    def _sweep_idle_streams(self, handles: list[_StreamHandle], *, timeout: float) -> None:
        """Finalize every handle in ``handles`` concurrently — used only by
        ``close()``'s shutdown sweep. Unlike ``_finalize_stream()`` (one
        handle at a time, used by ``run_stream()``'s own cleanup, where
        that's correctness-critical — see its docstring), every handle
        reaching here is already proven idle, so scheduling every
        ``aclose()`` up front and waiting on them together bounds total
        sweep time by the single slowest one instead of summing every
        handle's own wait — sequential waits would split a shrinking
        budget unevenly across streams (e.g. 4 streams each needing ~1.2s
        could serialize to ~4.8s and starve whichever's last, when all four
        could finish in ~1.2s scheduled together).
        """
        futures: list[Future] = []
        for handle in handles:
            with self._lock:
                if handle.done:
                    continue
                handle.done = True
                self._active_streams.discard(handle)
            coro = handle.agen.aclose()
            if not self._loop.is_running():
                coro.close()
                continue
            futures.append(asyncio.run_coroutine_threadsafe(coro, self._loop))
        if not futures:
            return
        _done, not_done = wait_futures(futures, timeout=timeout)
        for future in not_done:
            future.cancel()
        if not_done:
            logger.warning(
                "byoai Flask bridge: %d stream(s) failed to close before the shutdown timeout",
                len(not_done),
            )


def attach(app: Flask, runtime: Runtime) -> Runtime:
    """Bind ``runtime`` to ``app`` via ``app.extensions`` (Flask's own
    extension-state slot) and start its background bridge thread.

    Idempotent: calling this again on an already-attached app (a duplicate
    call, or Werkzeug's debug-mode reloader re-executing module code) returns
    the existing runtime instead of starting a second bridge thread — the
    ``runtime`` passed on that second call is unused and gets closed
    immediately (its provider(s) already opened real connections in
    ``__init__``, so leaving it open would leak them for the rest of the
    process). Registers ``atexit`` cleanup — WSGI has no lifespan-shutdown
    protocol like ASGI's, so this is the best available hook; it won't fire
    on SIGKILL, but does on gunicorn's graceful SIGTERM path.
    """
    existing: _FlaskBridge | None = app.extensions.get(_STATE_ATTR)
    if existing is not None:
        if runtime is not existing.runtime:
            try:
                existing.run(runtime.aclose())
            except Exception:
                logger.warning(
                    "byoai Flask bridge: failed to close a redundant Runtime "
                    "passed to a duplicate attach() call",
                    exc_info=True,
                )
        return existing.runtime
    bridge = _FlaskBridge(runtime)
    app.extensions[_STATE_ATTR] = bridge
    atexit.register(bridge.close)
    return runtime


def _bridge(app: Flask | None) -> _FlaskBridge:
    target = app if app is not None else current_app
    bridge = target.extensions.get(_STATE_ATTR)
    if bridge is None:
        raise ConfigurationError(
            "no ByoAI runtime attached to this app — call "
            "byoai.integrations.flask.attach(app, runtime) in your app factory"
        )
    return bridge


def get_runtime(app: Flask | None = None) -> Runtime:
    """The attached ``Runtime`` — defaults to ``flask.current_app``; pass
    ``app=`` explicitly when calling outside a request/app context (e.g. a
    background job wrapped in ``with app.app_context(): ...``)."""
    return _bridge(app).runtime


def execute(input: Any, *, app: Flask | None = None, **kwargs: Any) -> ExecutionResult:
    """Sync wrapper for ``runtime.execute()`` — blocks the calling (Flask
    request) thread until the result is ready."""
    bridge = _bridge(app)
    try:
        return bridge.run(bridge.runtime.execute(input, **kwargs))
    except ConcurrentCancelledError as exc:
        # run() re-raises this as-is (not wrapped in a ByoAIError) when a
        # future gets cancelled for a reason unrelated to the bridge
        # closing — the same race stream_response() already handles for
        # the streaming path (see its event_source()). A typical Flask view
        # built on this helper only expects `except ByoAIError:`, so a raw
        # concurrent.futures.CancelledError escaping here surfaces as an
        # unhandled 500 instead of a normal, catchable runtime error.
        # str(CancelledError()) is always "" (it never carries a message),
        # so fall back to a description instead of an empty one.
        raise ByoAIError(str(exc) or "request cancelled") from exc


def stream_response(
    input: Any,
    *,
    app: Flask | None = None,
    media_type: str = "text/event-stream",
    headers: dict[str, str] | None = None,
    **execute_kwargs: Any,
) -> Response:
    """SSE ``Response`` streaming ``runtime.stream()`` chunks — same frame
    shape as the FastAPI/Robyn integrations (via ``transport.chunk_to_dict``).
    ``headers`` defaults to disabling proxy buffering; pass ``{}`` to omit.
    """
    bridge = _bridge(app)
    effective_headers = (
        {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if headers is None
        else headers
    )

    def event_source() -> Iterator[str]:
        agen = bridge.runtime.stream(input, **execute_kwargs)
        try:
            for chunk in bridge.run_stream(agen):
                if has_content(chunk):
                    yield f"data: {json.dumps(chunk_to_dict(chunk))}\n\n"
        except (ByoAIError, ConcurrentCancelledError) as exc:
            # Headers are already on the wire; emit the same terminal error
            # event as the other transports rather than tearing the
            # connection down. ConcurrentCancelledError: run() re-raises
            # this as-is (not wrapped in a ByoAIError) when a future gets
            # cancelled for a reason unrelated to the bridge closing — a
            # ByoAIError-only catch here let it escape unhandled instead of
            # ending the stream cleanly. str(CancelledError()) is always ""
            # (it never carries a message), so that case specifically falls
            # back to a description rather than an empty, undiagnosable
            # error — but only that case: a genuine ByoAIError that happens
            # to carry no message (e.g. a bring-your-own-function provider
            # raising ByoAIError() bare) must not also get mislabeled as a
            # cancellation that never happened, misleading any client-side
            # handling that branches on that specific message.
            if isinstance(exc, ConcurrentCancelledError):
                message = str(exc) or "request cancelled"
            else:
                message = str(exc) or type(exc).__name__
            yield f"data: {json.dumps({'error': message, 'done': True})}\n\n"

    # stream_with_context is required: Flask tears down the request/app
    # context as soon as the view returns unless the generator is wrapped.
    return Response(
        stream_with_context(event_source()), mimetype=media_type, headers=effective_headers
    )
