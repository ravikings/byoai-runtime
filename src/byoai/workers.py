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
from .errors import ByoAIError, ConfigurationError
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
    """In-process queue for dev/tests. Same contract as RedisStreamQueue."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
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
    ) -> None:
        if client is None:
            try:
                import redis.asyncio as aioredis
            except ImportError as exc:  # pragma: no cover
                raise ConfigurationError(
                    "RedisStreamQueue requires redis: pip install 'byoai-runtime[redis]'"
                ) from exc
            client = aioredis.from_url(url, decode_responses=True)
        self._client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer or f"worker-{uuid.uuid4().hex[:8]}"
        self.result_prefix = result_prefix
        self.result_ttl = result_ttl
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
            await self._client.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP = already exists
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    async def publish(self, job: Job) -> str:
        await self._ensure_group()
        await self._client.xadd(
            self.stream, {"id": job.id, "payload": json.dumps(job.payload)}
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

    def __init__(self, runtime: Runtime, queue: JobQueue, *, concurrency: int = 10) -> None:
        self.runtime = runtime
        self.queue = queue
        self.concurrency = concurrency
        self._stopping = asyncio.Event()
        self._in_flight: set[asyncio.Task] = set()
        self.processed = 0
        self.failed = 0

    async def run(self, *, until_idle: bool = False, poll_timeout: float = 0.5) -> None:
        """Consume jobs; drains in-flight work on exit.

        Default mode runs until :meth:`stop` is called. With ``until_idle=True``
        it returns once the queue stays empty and nothing is in flight
        (batch/test runs).
        """
        semaphore = asyncio.Semaphore(self.concurrency)
        while not self._stopping.is_set():
            await semaphore.acquire()
            if self._stopping.is_set():
                semaphore.release()
                break
            job = await self.queue.pop(timeout=poll_timeout)
            if job is None:
                semaphore.release()
                if until_idle:
                    if not self._in_flight:
                        return
                    await asyncio.gather(*self._in_flight, return_exceptions=True)
                continue
            task = asyncio.create_task(self._process(job, semaphore))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)

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
