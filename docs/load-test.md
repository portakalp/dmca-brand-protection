# Load check: a CT burst

This is a local measurement only; never run a load test against a production deployment. The script is `backend/tests/load/ct_burst.py` and the command is `make load` (options: `make load LOAD_ARGS="--help"`).

## What was measured

10,000 brand matches an hour, mixed with 50 unrelated certificates a second, for 10 minutes, followed by 5 minutes of draining.

**Real code:**

- The CT processor: the matcher, `record_discovery` and the seen-domains store.
- Admission, enrichment and the HTTP pre-check: worker-default's actors with 8 threads, as in production.
- The capture job records in Redis.
- Postgres and Redis: the containers started by `make up-infra`.

**Fakes, with their latencies:**

| Component | Latency | Note |
| --- | --- | --- |
| DNS | 0.05 s per query | |
| RDAP | 0.8 s per query | |
| Candidate site | 0.5 s | Every site serves the fixture clone, so **every match goes to the browser (the worst case)** |
| Browser | 8 s per profile, 2 profiles per job | 2 processes × 1 thread, as in production |
| Claude | 6 s per call | 4 threads; one call after every capture |

The fake captures write no evidence. The run shows how jobs pile up in the browser and AI queues; it does not show the browser's own memory and CPU use. That is known from real captures during development: worker-browser runs within 2 GB of memory and 1.5 CPUs.

**Machine:** a development laptop (Intel i5-13500H, 16 cores). All threads ran in one Python process.

## Results

| Time (s) | Matches | Captures | Queued jobs | Process CPU | Process RSS | Postgres | Redis |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 30 | 81 | 2 | capture 74, others ≤ 3 | 9% | 180 MB | 65 MB | 6.9 MB |
| 222 | 617 | 26 | capture 583, enrich 6 | 9% | 183 MB | 67 MB | 7.8 MB |
| 414 | 1,151 | 50 | capture 1,090, precheck 5 | 9% | 184 MB | 70 MB | 9.2 MB |
| 606 (burst ends) | 1,665 | 74 | capture 1,591 | 8% | 184 MB | 72 MB | 10.2 MB |
| 927 (end of drain) | 1,665 | 114 | capture 1,551 | 1% | 184 MB | 72 MB | 10.5 MB |

CPU is a percentage of one core. Postgres CPU stayed below 10% in every sample. No queue had dead-lettered messages. The raw samples are written to `.local/load/ct_burst.json` (not in git).

## Findings

1. **Ingestion, enrichment and the pre-check keep up easily.** 1,665 matches arrived in 10 minutes (9,990 an hour). The `discovery`, `enrich` and `precheck` queues never held more than 10 messages. All of this used about 9% of one core and 184 MB of memory. Real RDAP and DNS are slower, but the threads mostly wait on I/O, so 8 threads remain enough: an enrichment that takes 1 second still allows 28,000 jobs an hour.
2. **The browser is the bottleneck.** With 2 processes it captures about 450 sites an hour (2 profiles × 8 s per job). In the worst case, where every match is a suspicious page, the capture queue grows by about 9,500 jobs an hour. In practice most matches end in `watching` at the pre-check (no DNS, a parked page, an empty page). If 10% go on to the browser that is 1,000 jobs an hour, about twice the capacity.
3. **AI load follows the browser.** Each capture is followed by one call, so the `ai` queue never built up. With 4 threads and 6-second calls the AI stage could handle about 2,400 calls an hour. AI cost is likewise capped by the browser's pace.
4. **Redis memory grows slowly.** Each queued job takes about 2.4 KB (the Dramatiq message and the capture record). A 192 MB `maxmemory` holds about 75,000 queued jobs, or 7 to 8 hours of an uninterrupted worst-case burst. When it is full (`noeviction`), writes are refused. The `QueueBacklog` alarm fires long before that, after 30 minutes.
5. **Postgres is not under pressure.** Its memory went from 65 MB to 72 MB and its CPU stayed low. The smallest managed database tier (about 0.6 GB of memory) is enough for this load.

## Sizing recommendations

The reference server has 2 vCPUs and 8 GB of memory. The self-hosted compose file sets Redis `maxmemory` with `REDIS_MAXMEMORY` (default 256 MB); the reference VM uses 192 MB.

| Recommendation | When | Change |
| --- | --- | --- |
| No change | One or two customers with a few brands | On the real CT stream a brand matches tens of domains an hour, at most a few hundred; 450 captures an hour is enough |
| On a `QueueBacklog` alarm for `capture`, look for the cause first | Any time | A new keyword may match far too widely. Narrow its variants or use the whitelist (see [QueueBacklog](operations.md#queuebacklog)). Adding capacity is the last resort |
| Run worker-browser with 3 processes | When captures pile up regularly | `processes=3` in `backend/src/kalkan/workers/specs.py`, and about 3 GB of memory and 2 CPUs for the container. On a 2 vCPU / 8 GB server the sum of memory limits would rise from 6.9 GB to 7.9 GB, so do this together with the next row |
| Move to 4 vCPUs and 16 GB | Three or more customers, or a backlog that never drains | The browser can then run 4 processes and Redis can get 384 MB |
| Raise Redis `maxmemory` to 384 MB | After the server grows | `REDIS_MAXMEMORY=384mb` (self-hosted) or the Redis limits in `infra/compose/docker-compose.vm.yml` |

On the reference VM the memory limits of the always-on services add up to 6.9 GB, 86% of 8 GB. Limits are ceilings and actual use is lower. With the `monitoring` profile, node-exporter, the `DiskAlmostFull` alarm and the Grafana "Queues and system" dashboard show the real numbers.

## Running it again

```bash
make load                                        # default: 10 min burst + 5 min drain
make load LOAD_ARGS="--minutes 2 --drain 1"      # short run
make load LOAD_ARGS="--capture 4 --ai 3"         # assume a faster browser and AI
```

It uses the `kalkan_test` database and Redis database 14 and empties both at the end. Do not run it at the same time as `make test`. Keep `ANTHROPIC_API_KEY` out of the environment (`env -u ANTHROPIC_API_KEY make load`); the script uses a fake AI client either way.
