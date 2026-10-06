# Blocking database calls: measurement and fix

## The problem

The Supabase Python client is synchronous: every query is a blocking HTTP request. Most
endpoints were declared `async def` and called it directly, so while one request waited for the
database, the event loop could not run anything else. With a single server process this meant
that requests were handled strictly one after another during every database round trip.

## The fix

Two rules, applied to the whole of `main.py`:

- **Endpoints that await nothing became plain `def`** (29 of 77). FastAPI runs these in its
  thread pool, so a blocking query only blocks that thread. The function bodies did not change.
- **In the remaining `async def` endpoints and helpers, every query goes through
  `run_query`**, which runs `query.execute()` in the thread pool, and the synchronous helpers
  that query the database are called through `run_in_threadpool`. That is 196 queries and 23
  helper calls. One existing `asyncio.to_thread` call was moved to the same pool.

Why this and not one of the alternatives:

- *Only `run_in_threadpool` / `asyncio.to_thread` at every call site:* would also work, but
  for endpoints with no `await` at all, `def` is the standard FastAPI way and leaves the body
  untouched.
- *Only `def` everywhere:* not possible, because 48 endpoints await Redis, other HTTP calls or
  WebSocket broadcasts.
- *`asyncio.to_thread` instead of `run_in_threadpool`:* `to_thread` uses the event loop's
  default executor (at most 32 threads, fewer on small machines), while `def` endpoints use
  anyio's thread pool. Using `run_in_threadpool` everywhere keeps a single pool with a single
  size to reason about.
- *The async Supabase client:* the cleanest long-term option, but it changes how the client is
  created and how every query is called, which is a larger change than this one.

The rewrite was done with a script working on the syntax tree, not by hand. As a check, undoing
the wrapping (and treating `async def` and `def` as the same) gives a syntax tree identical to
the original file, and all 67 tests pass before and after.

## How it was measured

`scripts/load_test.py` starts the real app with uvicorn (one worker) in a separate process. The
database is the in-memory client from `tests/fakes.py` with an artificial **50 ms delay per
query**, slept in the calling thread as the real client would block. Redis is fakeredis, the
Mapbox call returns a fixed answer, and rate limits are switched off in that process. The
client sends a fixed number of requests per endpoint with a fixed number in flight and reports
the median (p50) and 95th percentile (p95) response time and requests per second.

Three endpoints that are called often:

| Endpoint | Used by | Queries per request |
|---|---|---|
| `GET /firma-talepleri` | the desk agent and operations panels, on every refresh | about 6 |
| `GET /vale-gorevi` | the valet app, polled | about 4 |
| `GET /talep-detay/{token}` | the customer tracking page | about 4, plus an awaited ETA call |

Conditions: Apple M4 (10 cores), Python 3.12.13, client and server on the same machine, 100
requests per endpoint (200 at 100 in flight). Times are milliseconds; each cell is
p50 / p95 / requests per second.

## Results

### Before and after (default thread pool, 40 threads)

| In flight | Endpoint | Before | After |
|---|---|---|---|
| 1 | `/firma-talepleri` | 332 / 337 / 3.0 | 350 / 366 / 2.9 |
| 1 | `/vale-gorevi` | 222 / 226 / 4.5 | 236 / 243 / 4.3 |
| 1 | `/talep-detay` | 222 / 226 / 4.5 | 235 / 246 / 4.3 |
| 10 | `/firma-talepleri` | 3291 / 3309 / 3.0 | 351 / 363 / 28.2 |
| 10 | `/vale-gorevi` | 2191 / 2217 / 4.6 | 234 / 249 / 41.9 |
| 10 | `/talep-detay` | 2181 / 2199 / 4.6 | 236 / 246 / 42.0 |
| 50 | `/firma-talepleri` | 16404 / 16859 / 3.0 | 370 / 683 / 92.4 |
| 50 | `/vale-gorevi` | 10912 / 11213 / 4.5 | 254 / 465 / 134.5 |
| 50 | `/talep-detay` | 10870 / 11163 / 4.6 | 304 / 345 / 143.1 |

Before the fix, throughput stayed the same whatever the number of requests in flight: the
server handled one request at a time, and the waiting time grew in a straight line with the
queue. After the fix, requests wait for the database in parallel.

A single request on its own got about 5% slower (for example 332 → 350 ms): handing each query
to a thread has a cost. With more than one request in flight the fix is far ahead.

A repeat run of the 50-in-flight case gave figures within about 10% of the table
(`/firma-talepleri` 408 / 703 / 86.4).

### Thread pool size

At 50 in flight, `/firma-talepleri` holds a thread for its whole run (it is a `def` endpoint),
so 40 threads are not enough for 50 requests and the p95 rises. A larger pool was tried:

| In flight | Threads | `/firma-talepleri` | `/vale-gorevi` | `/talep-detay` |
|---|---|---|---|---|
| 50 | 40 | 370 / 683 / 92.4 | 254 / 465 / 134.5 | 304 / 345 / 143.1 |
| 50 | 100 | 399 / 458 / 110.8 | 288 / 399 / 144.0 | 281 / 395 / 145.1 |
| 100 | 40 | 738 / 1080 / 107.1 | 476 / 687 / 162.4 | 614 / 1602 / 88.4 |
| 100 | 100 | 521 / 1725 / 87.5 | 410 / 1754 / 90.8 | 415 / 1739 / 91.9 |

100 threads helped the `def` endpoint a little at 50 in flight, but at 100 in flight it made the
p95 and the throughput worse in both runs (the repeat run: 40 threads 109 / 161 / 92 requests per
second, 100 threads 97 / 94 / 100). Past a point the threads compete for the interpreter rather
than wait for the database. **The default of 40 was kept.** On a real server the better lever
for more load would be more uvicorn worker processes, which this measurement did not cover.

## Limits of this measurement

- The database is simulated. A fixed 50 ms sleep stands in for the round trip; a real Supabase
  project adds its own variation, connection limits and query times. The numbers show how the
  server handles waiting, not how fast the real system is.
- Client and server ran on one machine, with one uvicorn worker, and the samples are small (100
  or 200 requests per endpoint). The p95 of 100 samples is a single value and moves between runs.
- Only reads were measured. Writes follow the same pattern but were not timed.

## What changed in behaviour

The results of every endpoint are the same. What changed is *when* requests can interleave.
Before, a request that never awaited anything ran from start to finish without another request
running in the same process; now requests interleave at every database call.

The critical writes do not rely on that old accidental ordering. They carry their own condition
in the database query: the valet status update writes only if the status is still the one it read
(a compare-and-set), cancellation only if the task is still in a cancellable status, and the stop
completion skips passengers already in the target status. A test in `tests/test_valet_flow.py`
sends two requests at once against the status update.

Check-then-act sequences without such a condition (for example "a valet may have only one active
task" in `/yeni-talep`, or the quota checks) can now race within one process if two requests
arrive at the same moment. They could already race as soon as the app ran with more than one
worker. The proper fix is a constraint in the database, such as a partial unique index; it is
listed under known limitations.
