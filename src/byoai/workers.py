"""Background worker / queue-consumer transport.

A :class:`RuntimeWorker` consumes jobs from a queue, executes them through the
runtime (same payload dialect as HTTP/WS — see ``byoai.transport``), and pushes
results back. Concurrency is semaphore-bounded; shutdown is graceful (stops
popping, drains in-flight jobs).

BYOI: bring your own queue. :class:`RedisStreamQueue` rides an existing Redis
with consumer groups (at-least-once, ``XACK`` on completion) under the
isolated ``byoai:`` namespace; :class:`MemoryJobQueue` serves dev/tests.

    queue = RedisStreamQueue(url="redis://redis.internal:6379")
    worker = RuntimeWorker(runtime, queue, concurrency=32)
    await worker.run()          # until worker.stop() or SIGTERM handling calls it
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from . import _json as json
from .errors import ByoAIError
from .runtime import Runtime
from .transport import execute_payload


@dataclass
class Job:
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # transport-specific delivery tag (e.g. Redis stream entry id) for acking
    delivery_tag: Any = None


@runtime_checkable
class JobQueue(Protocol):
    async def publish(self, job: Job) -> str: ...

    async def pop(self, timeout: float = 1.0) -> Job | None:
        """Next job, or None if none arrived within ``timeout`` seconds."""
        ...

    async def ack(self, job: Job) -> None: ...

    async def push_result(self, job: Job, result: dict[str, Any]) -> None: ...

    async def read_result(self, job_id: str) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...


class MemoryJobQueue:
    """In-process queue for dev/tests. Same contract as RedisStreamQueue.

    ``maxsize`` (default 0 = unbounded, matching ``asyncio.Queue``) bounds
    memory when publishers can outrun a slow worker fleet; ``publish()``
    then backpressures by awaiting free space instead of growing forever.
    """

    def __init__(self, *, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=maxsize)
        self._results: dict[str, dict[str, Any]] = {}
        self.acked: list[str] = []

    async def publish(self, job: Job) -> str:
        await self._queue.put(job)
        return job.id

    async def pop(self, timeout: float = 1.0) -> Job | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def ack(self, job: Job) -> None:
        self.acked.append(job.id)

    async def push_result(self, job: Job, result: dict[str, Any]) -> None:
        self._results[job.id] = result

    async def read_result(self, job_id: str) -> dict[str, Any] | None:
        return self._results.get(job_id)

    async def close(self) -> None:
        pass


class RedisStreamQueue:
    """Jobs on a Redis Stream with a consumer group; results as ``byoai:``-
    namespaced keys with TTL. At-least-once delivery: entries are XACKed only
    after the result is stored.

    Requires the ``redis`` extra: ``pip install byoai-runtime[redis]``.
    """

    def __init__(
        self,
        *,
        url: str = "redis://localhost:6379",
        stream: str = "byoai:jobs",
        group: str = "byoai-workers",
        consumer: str | None = None,
        result_prefix: str = "byoai:result:",
        result_ttl: int = 3600,
        prefetch: int = 16,
        client: Any | None = None,
        mode: str = "standalone",
        sentinels: list | None = None,
        service_name: str | None = None,
        maxlen: int | None = None,
        approximate_trim: bool = True,
        start_id: str = "0",
        **client_kwargs: Any,
    ) -> None:
        """``maxlen`` caps the jobs stream (unbounded by default) so an idle or
        crashed worker fleet doesn't let publishers grow it forever.
        ``start_id`` is the consumer group's initial read position — ``"0"``
        (default) replays the whole existing stream for a fresh group;
        ``"$"`` starts from only new entries, for attaching a new worker
        fleet to a pre-existing, already-large stream without a backlog
        replay. ``**client_kwargs`` are forwarded to the redis-py client."""
        if client is None:
            from .cache.redis import make_redis_client

            client = make_redis_client(
                url=url, mode=mode, sentinels=sentinels, service_name=service_name,
                **client_kwargs,
            )
        # Explicitly typed: see the matching comment in cache/redis.py.
        self._client: Any = client
        self.stream = stream
        self.group = group
        self.consumer = consumer or f"worker-{uuid.uuid4().hex[:8]}"
        self.result_prefix = result_prefix
        self.result_ttl = result_ttl
        self.maxlen = maxlen
        self.approximate_trim = approximate_trim
        self.start_id = start_id
        # Batch up to `prefetch` entries per XREADGROUP round-trip; pop()
        # serves from the local buffer so throughput isn't capped at one
        # network round-trip per job.
        self.prefetch = max(1, prefetch)
        self._buffer: list[Job] = []
        self._group_ready = False

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        try:
            await self._client.xgroup_create(
                self.stream, self.group, id=self.start_id, mkstream=True
            )
        except Exception as exc:  # BUSYGROUP = already exists
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def publish(self, job: Job) -> str:
        await self._ensure_group()
        await self._client.xadd(
            self.stream,
            {"id": job.id, "payload": json.dumps(job.payload)},
            maxlen=self.maxlen,
            approximate=self.approximate_trim,
        )
        return job.id

    async def pop(self, timeout: float = 1.0) -> Job | None:
        if self._buffer:
            return self._buffer.pop(0)
        await self._ensure_group()
        entries = await self._client.xreadgroup(
            self.group,
            self.consumer,
            {self.stream: ">"},
            count=self.prefetch,
            block=int(timeout * 1000),
        )
        if not entries:
            return None
        _, messages = entries[0]
        for entry_id, fields in messages:
            self._buffer.append(
                Job(
                    id=fields.get("id", entry_id),
                    payload=json.loads(fields["payload"]),
                    delivery_tag=entry_id,
                )
            )
        return self._buffer.pop(0) if self._buffer else None

    async def ack(self, job: Job) -> None:
        if job.delivery_tag is not None:
            await self._client.xack(self.stream, self.group, job.delivery_tag)

    async def push_result(self, job: Job, result: dict[str, Any]) -> None:
        await self._client.set(
            f"{self.result_prefix}{job.id}", json.dumps(result), ex=self.result_ttl
        )

    async def read_result(self, job_id: str) -> dict[str, Any] | None:
        raw = await self._client.get(f"{self.result_prefix}{job_id}")
        return json.loads(raw) if raw is not None else None

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except AttributeError:  # older redis-py
            await self._client.close()


class RuntimeWorker:
    """Consume jobs and execute them through the runtime, ``concurrency`` at a
    time. Failed jobs get an ``{"error": ...}`` result and are still acked
    (dead-lettering/retry policy belongs to the queue, not the worker)."""

    def __init__(
        self,
        runtime: Runtime,
        queue: JobQueue,
        *,
        concurrency: int = 10,
        shutdown_timeout: float | None = None,
    ) -> None:
        self.runtime = runtime
        self.queue = queue
        self.concurrency = concurrency
        # Caps how long stop()/run() waits for in-flight jobs to finish
        # draining; None (default) waits indefinitely. A stuck job otherwise
        # blocks graceful shutdown forever.
        self.shutdown_timeout = shutdown_timeout
        self._stopping = asyncio.Event()
        self._in_flight: set[asyncio.Task] = set()
        self.processed = 0
        self.failed = 0

    async def _drain(self) -> None:
        if not self._in_flight:
            return
        gather = asyncio.gather(*self._in_flight, return_exceptions=True)
        if self.shutdown_timeout is None:
            await gather
            return
        try:
            await asyncio.wait_for(asyncio.shield(gather), timeout=self.shutdown_timeout)
        except asyncio.TimeoutError:
            pass  # remaining tasks keep running in the background; not awaited further

    async def run(self, *, until_idle: bool = False, poll_timeout: float = 0.5) -> None:
        """Consume jobs; drains in-flight work on exit.

        Default mode runs until :meth:`stop` is called. With ``until_idle=True``
        it returns once the queue stays empty and nothing is in flight
        (batch/test runs).
        """
        semaphore = asyncio.Semaphore(self.concurrency)
        # At full concurrency, semaphore.acquire() alone can block past a
        # stop() call until some in-flight job happens to finish — race it
        # against the stop signal so shutdown_timeout can actually take
        # effect instead of waiting on an already-slow/stuck job. One
        # long-lived stop_task is reused across iterations (it only resolves
        # once, when stop() fires) rather than spun up fresh per job.
        stop_task = asyncio.ensure_future(self._stopping.wait())
        try:
            while not self._stopping.is_set():
                acquire_task = asyncio.ensure_future(semaphore.acquire())
                await asyncio.wait(
                    {acquire_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task.done():
                    if acquire_task.done():
                        semaphore.release()  # acquired right as we were stopping; give it back
                    else:
                        acquire_task.cancel()
                    break
                job = await self.queue.pop(timeout=poll_timeout)
                if job is None:
                    semaphore.release()
                    if until_idle:
                        if not self._in_flight:
                            return
                        await self._drain()
                    continue
                task = asyncio.create_task(self._process(job, semaphore))
                self._in_flight.add(task)
                task.add_done_callback(self._in_flight.discard)
        finally:
            stop_task.cancel()
        await self._drain()

    async def _process(self, job: Job, semaphore: asyncio.Semaphore) -> None:
        try:
            try:
                result = await execute_payload(self.runtime, job.payload)
                self.processed += 1
            except ByoAIError as exc:
                result = {"error": str(exc), "error_type": type(exc).__name__}
                self.failed += 1
            except Exception as exc:  # noqa: BLE001 - a bad job must not kill the worker
                result = {"error": str(exc), "error_type": type(exc).__name__}
                self.failed += 1
            await self.queue.push_result(job, result)
            await self.queue.ack(job)
        finally:
            semaphore.release()

    def stop(self) -> None:
        self._stopping.set()

    async def run_until_idle(self) -> None:
        """Convenience for batch/test runs: consume until the queue stays empty."""
        await self.run(until_idle=True, poll_timeout=0.1)
