# Background workers

`RedisStreamQueue` requires the `redis` extra: `pip install "byoai-runtime[redis]"`.
`MemoryJobQueue` (dev/tests) has no extra dependency.

`byoai.workers` runs executions off the request path — a `RuntimeWorker` consumes jobs from a
queue, executes them through the runtime (the same payload dialect as HTTP/WS), and pushes
results back. Concurrency is semaphore-bounded; shutdown is graceful (stops popping new jobs,
drains in-flight ones).

BYOI: bring your own queue. `RedisStreamQueue` rides an existing Redis with consumer groups
(at-least-once delivery, `XACK` on completion) under the isolated `byoai:` namespace — standalone,
cluster, or Sentinel, via the same `mode`/`sentinels`/`service_name` options as
[`RedisCache`](caching.md#redis). `MemoryJobQueue` serves dev/tests, with an optional `maxsize`
to backpressure publishers when a slow worker fleet falls behind.

```python
from byoai import Runtime
from byoai.workers import RedisStreamQueue, RuntimeWorker

runtime = Runtime(llm={"provider": "openai", "model": "gpt-4o"})
queue = RedisStreamQueue(url="redis://redis.internal:6379")
worker = RuntimeWorker(runtime, queue, concurrency=32)

await worker.run()  # runs until worker.stop() is called
```

Publish jobs from anywhere with access to the same queue:

```python
from byoai.workers import Job

job_id = await queue.publish(Job(payload={"input": "What are our SLA terms?", "user_id": "usr_1"}))
# ... later, from any process:
result = await queue.read_result(job_id)
```

## Graceful shutdown

`worker.stop()` stops popping new jobs and waits for in-flight ones to drain. Pass
`shutdown_timeout` to `RuntimeWorker(...)` to cap how long that drain waits — past the timeout,
remaining jobs keep running in the background but `stop()`/`run()` return anyway rather than
hanging indefinitely (default `None` waits forever, as before).

## Failure handling

A job that raises during execution gets an `{"error": ..., "error_type": ...}` result and is
still acknowledged — dead-lettering and retry policy belong to the queue configuration, not the
worker, so a bad job can't wedge the consumer group.

## Batch / test runs

`await worker.run_until_idle()` (or `run(until_idle=True)`) consumes until the queue stays empty
and nothing is in flight, then returns — useful for batch jobs and tests instead of running the
worker forever.
