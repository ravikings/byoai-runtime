from __future__ import annotations

import asyncio

from tests.conftest import FakeProvider

from byoai import Runtime
from byoai.workers import Job, MemoryJobQueue, RuntimeWorker


class SlowProvider(FakeProvider):
    async def complete(self, messages, **options):
        await asyncio.sleep(0.05)
        return await super().complete(messages, **options)


async def test_worker_processes_jobs_and_pushes_results():
    queue = MemoryJobQueue()
    runtime = Runtime(providers=[FakeProvider()])
    ids = [await queue.publish(Job(payload={"input": f"q{i}"})) for i in range(10)]

    worker = RuntimeWorker(runtime, queue, concurrency=4)
    await worker.run_until_idle()

    assert worker.processed == 10
    assert worker.failed == 0
    assert sorted(queue.acked) == sorted(ids)
    for job_id in ids:
        result = await queue.read_result(job_id)
        assert result is not None
        assert result["content"] == "hello from fake"
        assert result["usage"]["input_tokens"] == 10


async def test_worker_bad_job_gets_error_result_not_crash():
    queue = MemoryJobQueue()
    runtime = Runtime(providers=[FakeProvider()])
    bad = await queue.publish(Job(payload={"pipeline": "no-input-key"}))
    good = await queue.publish(Job(payload={"input": "fine"}))

    worker = RuntimeWorker(runtime, queue, concurrency=2)
    await worker.run_until_idle()

    assert worker.processed == 1
    assert worker.failed == 1
    bad_result = await queue.read_result(bad)
    assert bad_result is not None
    assert "error" in bad_result and bad_result["error_type"] == "ConfigurationError"
    good_result = await queue.read_result(good)
    assert good_result is not None
    assert good_result["content"] == "hello from fake"
    assert len(queue.acked) == 2  # failures are acked too; retry is queue policy


async def test_worker_runs_jobs_concurrently():
    queue = MemoryJobQueue()
    runtime = Runtime(providers=[SlowProvider()])
    for i in range(8):
        await queue.publish(Job(payload={"input": f"q{i}"}))

    worker = RuntimeWorker(runtime, queue, concurrency=8)
    start = asyncio.get_running_loop().time()
    await worker.run_until_idle()
    elapsed = asyncio.get_running_loop().time() - start

    assert worker.processed == 8
    # 8 x 50ms jobs at concurrency 8 should take ~1 batch, not 8 sequential
    assert elapsed < 0.3


async def test_worker_counts_and_logs_push_result_failures_instead_of_losing_them(caplog):
    # Regression: push_result()/ack() raising after execute_payload() already
    # succeeded used to propagate out of a task nobody awaits — the exception
    # (and the fact the job's result never made it to the queue) vanished
    # silently except for asyncio's generic "never retrieved" warning.
    class FlakyQueue(MemoryJobQueue):
        async def push_result(self, job, result):
            raise ConnectionError("redis blip")

    queue = FlakyQueue()
    runtime = Runtime(providers=[FakeProvider()])
    await queue.publish(Job(payload={"input": "hi"}))

    worker = RuntimeWorker(runtime, queue, concurrency=1)
    import logging

    with caplog.at_level(logging.ERROR, logger="byoai.workers"):
        await worker.run_until_idle()

    assert worker.processed == 1  # the runtime did answer
    assert worker.errors == 1  # but delivering the result failed, and is now visible
    assert "push_result/ack failed" in caplog.text


async def test_worker_graceful_stop_drains_in_flight():
    queue = MemoryJobQueue()
    runtime = Runtime(providers=[SlowProvider()])
    for i in range(3):
        await queue.publish(Job(payload={"input": f"q{i}"}))

    worker = RuntimeWorker(runtime, queue, concurrency=4)
    run_task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.02)  # let it pick jobs up
    worker.stop()
    await asyncio.wait_for(run_task, timeout=2)

    # everything picked up before stop() completed, none dropped mid-flight
    assert worker.processed == len(queue.acked)
    assert worker.processed >= 1
