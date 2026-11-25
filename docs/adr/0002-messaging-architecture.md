# ADR 0002: Messaging Architecture

- **Status:** Accepted
- **Date:** 2026-11-24
- **Course day:** Day 2

## Context

On Day 1 each ingestion script did everything itself: fetch, then write straight to Postgres (air) or S3 (traffic). Every new use of a reading (the ML model on Day 4, a future anomaly detector) would have to be wired into the ingestion code, and each script would grow a dependency on every consumer.

Facts that shape the choice:

- **Volume is tiny:** ~1 NO₂ reading and 4 traffic summaries per hour, so roughly 5 messages/hour in steady state.
- **One VM, one engineer, Free plan credits.** Postgres already runs as a container on that VM (ADR-0001).
- **Both sources can be replayed.** Luchtmeetnet keeps years of history behind its API (the NL10240 NO₂ endpoint reports 2,204 pages of 50 readings ≈ 12 years), and every NDW snapshot is kept in S3. Losing a message is recoverable; losing a source is not.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **A. Direct writes only** (Day 1) | Simplest; nothing extra to run | Every new consumer means changing the producers; producers and consumers fail together |
| **B. Redis list on the VM, alongside the direct writes** | One container, zero cloud setup, sub-millisecond, free; producers no longer need to know who consumes | Not a managed service: durability is ours (we enable append-only persistence); a list is a *queue* (one consumer per message), not a topic |
| C. Amazon SQS (+ SNS for fan-out) | Managed, durable (multi-AZ), 1 M requests/month free; the standard production choice | Network calls and IAM setup for 5 messages/hour; a second place to monitor; nothing in AirBreda needs consumers on other machines yet |
| D. Kafka / Kinesis | Replayable stream: the Kappa architecture from Day 1 | Large operational cost (Kafka) or hourly shard cost (Kinesis) for a trickle of messages |

## Decision

**Option B: Redis (`redis:7-alpine`) as a container on the same Docker network, list `readings`.**

- **Why a queue at all, instead of only direct database writes:** it decouples producers from consumers. The ingesters publish a JSON message per reading and don't know who reads it. Adding the Day 4 inference service or an anomaly detector later means adding a consumer, not editing two ingestion scripts. This is the *event-driven* pattern from the Day 2 material.
- **Why alongside, not instead of, the direct writes:** nothing consumes the queue yet (the lab stops at publishing). Writing to Postgres and S3 directly means the queue is never on the critical path for data: if it fails, nothing is lost (see below). Once a database-writer consumer exists, the direct writes can be removed.
- **Why Redis over SQS for now:** at ~5 messages/hour on one VM, the deciding factors are simplicity and cost, not scale or managed durability. Redis gives the pattern with one container and no IAM, network or billing setup. **We would switch to SQS (+ SNS for fan-out)** as soon as a consumer runs on a different machine, or once the queue itself has to guarantee delivery, i.e. when the direct writes are removed.
- **Message format** (one per NO₂ reading, one per NDW site per poll):
  ```json
  {"source": "Luchtmeetnet", "station_id": "NL10240", "timestamp": "2026-11-23T13:00:00Z", "component": "NO2", "value": 22.61, "is_flagged": false}
  {"source": "NDW", "station_id": "RWS01_MONIBAS_0271hrl0063ra", "site": "hrl", "timestamp": "2026-11-23T13:11:00Z", "total_flow": 1200.0, "avg_speed": 106.0, "invalid_values": 1}
  ```
- **No flooding:** the air service re-fetches 50 hours every poll, but publishes only readings that are new or whose value changed (a late value filling a null). The list is capped at 10,000 messages (`LTRIM`) because nothing consumes it yet and Redis keeps everything in RAM.

## What happens to readings if the message broker goes down

1. **Nothing is lost from the database or S3.** Publishing is the *last* step of each cycle and runs after the direct writes; a Redis failure is caught, logged as `WARNING queue_publish_failed`, and the cycle still counts as successful.
2. **Air readings are retried.** The service only remembers a reading as published after Redis accepted it, so on the next hourly cycle the same readings are published again.
3. **Traffic messages for that cycle are dropped** (logged with a count). The data itself is in Postgres and in S3.
4. **A Redis restart keeps the queue.** Redis runs with append-only persistence on a Docker volume (`--appendonly yes`), so at most about a second of writes is lost on a crash.
5. **Long outage:** `/health` keeps reporting the *source* fetches as healthy (they are), so a broken broker is visible only through the `queue_publish_failed` log events. An alert on that event is planned with the Day 4/5 monitoring.

## Why Luchtmeetnet bad data is flagged and kept, while NDW bad data is dropped

- **Luchtmeetnet (kept, `is_flagged = TRUE`):** NO₂ is the *target* the model predicts and the line on the dashboard. A missing hour silently shortens the time series and shifts any "previous hour" feature by one. A flagged row keeps the hour visible ("we know this hour exists, and it is suspect"), lets the model and dashboard exclude or grey it out deliberately, and is corrected automatically if the real value arrives later.
- **NDW (dropped from `sensor_readings`):** `-1` is not a measurement but a sentinel for "no valid value" (e.g. no vehicles passed the loop). Writing it would corrupt every average and sum. A single `-1` lane also makes a site's *total* flow wrong (it silently under-counts), so the flow total is only written when every lane is valid. Nothing is really lost: the raw `-1` stays in the S3 CSV with `is_valid = false`, and a `DATA_QUALITY_ERROR` is logged.

**Would we apply the same rule to both if we started over? Yes: flag and keep, for both.** The underlying rule is the same: never invent a value, never hide a gap. For NDW that means writing the row with `value = NULL, is_flagged = TRUE` rather than skipping it. That keeps "we polled, and the value was invalid" distinguishable from "ingestion didn't run", which matters for the `/health` story and for model features that need to know traffic was missing. The schema already supports NULL values and the flag, so this would be a small change. We kept the lab's drop rule for NDW for now.

## Consequences

**Easier:** adding consumers without touching producers; inspecting the live event flow (`LRANGE readings 0 -1`); swapping Redis for SQS later only changes `common/messaging.py`.

**Harder / risks:**
- One more container to run and monitor on the same VM; if the VM dies, the broker dies with it.
- A Redis *list* gives each message to **one** consumer. The planned fan-out (database writer **and** ML service each seeing every reading) needs Redis Streams with consumer groups, Redis pub/sub, or SNS → multiple SQS queues. This is the first thing to change once real consumers exist.
- Duplicates are possible (at-least-once: a cycle can publish and then crash before remembering it). Consumers must stay idempotent, as the database writes already are.
