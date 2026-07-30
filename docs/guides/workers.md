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

"At-least-once" holds for a job that fails cleanly (caught, still acked — see
[Failure handling](#failure-handling) below) or one your process is still alive to retry. It does
**not** currently cover a worker process crashing mid-job: the entry stays claimed by that
consumer's now-dead name in Redis's pending-entries list, and nothing in `RedisStreamQueue` runs
`XCLAIM`/`XAUTOCLAIM` to reclaim it — a fresh worker process gets a fresh random `consumer=` name
(unless you pass a stable one) and only ever reads new entries. If you need crash recovery,
either pass a stable `consumer=` per worker slot and run your own periodic `XCLAIM`/`XAUTOCLAIM`
against `stream`/`group`, or accept that a hard crash mid-job loses that job — same tradeoff
you'd make explicitly, not one that should surprise you coming from a queue (Celery, SQS) that
reclaims automatically.

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

`worker.run()` returning doesn't close the queue's own connection (e.g. `RedisStreamQueue`'s
Redis client) — `RuntimeWorker` doesn't own the queue's lifecycle, since you may be sharing it
with a publisher elsewhere in the same process. Call `await queue.close()` yourself once you're
done with it, the same way `Runtime.close()` isn't implicit either.

## Failure handling

A job that raises during execution gets an `{"error": ..., "error_type": ...}` result and is
still acknowledged — dead-lettering and retry policy belong to the queue configuration, not the
worker, so a bad job can't wedge the consumer group. `worker.processed`/`worker.failed` count
these outcomes.

A separate, rarer failure is the *result delivery* itself — `queue.push_result()`/`queue.ack()`
raising after the runtime already produced an answer (e.g. a transient Redis blip). That's not
representable as a job result (there's no result to push), so it's logged
(`logger.exception(...)` on the `byoai.workers` logger) and counted on `worker.errors` instead —
check it alongside `processed`/`failed` if you're tracking worker health.

## Batch / test runs

`await worker.run_until_idle()` (or `run(until_idle=True)`) consumes until the queue stays empty
and nothing is in flight, then returns — useful for batch jobs and tests instead of running the
worker forever.
