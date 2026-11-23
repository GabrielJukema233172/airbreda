# ADR 0001: Initial Data Storage Strategy

- **Status:** Accepted
- **Date:** 2026-11-23
- **Course day:** Day 1

## Context

AirBreda ingests two live open-data streams:

| Source | Shape | Rate |
|---|---|---|
| RIVM Luchtmeetnet, station NL10240 | `(station, timestamp, component, value)`, JSON | 1 value per component per hour, published ~30–90 min after the hour |
| NDW, 4 A27 sites (hrl, hrr, vwd, vwa) | flow + speed per lane, gzipped DATEX II XML (~160 MB site config + ~70 MB measurements uncompressed) | refreshed every minute |

What the system has to do with the data:

- **Serve recent readings fast** to `GET /site/{id}` and the dashboard: "last 24 h for this station/site".
- **Join** air quality with traffic by time to build model features.
- **Retrain** the regression model later on months of history, possibly with different cleaning rules.
- **Survive** duplicates and gaps: the API can be down, our job can run twice, sensors report `-1` or `null`.

Volume is small: at hourly resolution, 1 station × 3 components + 4 sites × 2 metrics ≈ **96,000 rows/year**.
Constraints: one engineer, AWS Free plan credits, a 5-day build.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **A. RDS PostgreSQL for parsed readings + S3 for raw files** | Fits the fixed schema; indexed time-range queries; SQL joins; primary key enforces no duplicates; S3 is cheap and keeps the source data for reprocessing | Two stores to manage; RDS runs (and bills) 24/7 |
| B. DynamoDB only | Serverless, no instance to manage, scales writes far beyond our needs | Time-range + station queries need careful key design; no joins (air ⋈ traffic must happen in application code); ad-hoc analysis for ML is awkward; scale we will never use |
| C. S3 only, queried with Athena | Cheapest storage, no server | Seconds of latency per query and pay-per-scan: wrong for an API that is called on every page load |
| D. PostgreSQL only (no raw files) | One store, simplest | Raw data is lost after parsing; a parsing bug or a new feature idea can't be fixed retroactively |

## Decision

**Option A.**

- **Relational database (RDS PostgreSQL, db.t4g.micro) instead of NoSQL**: the readings are strongly structured, the main queries are `WHERE station_id = … AND timestamp BETWEEN …`, and air and traffic must be joined on time. These are what SQL and a B-tree index on the primary key do well. At ~96k rows/year a single small Postgres instance has decades of headroom.
- **Raw files in object storage (S3)**: each NDW run is saved as `ndw/YYYY-MM-DD/HH-<site>.csv` (date/hour from the *measurement* time, UTC), one row per lane value including the raw value and an `is_valid` flag. S3 is the audit trail and the retraining dataset: when cleaning rules or features change, we re-run the new code over the files instead of trusting rows shaped by old assumptions.
- **Duplicates**: delivery is **at-least-once with idempotent writes**, not exactly-once.
  - Database: primary key `(station_id, timestamp, component)` + `INSERT … ON CONFLICT DO NOTHING`. Every run re-fetches the last 50 hours, so a missed run is healed automatically and re-inserting is a no-op (tested: 1st run 3 rows inserted, 2nd run 0).
  - S3: object keys are deterministic, so a re-run in the same hour overwrites the same file instead of creating a second one.
- **Bad values are kept, not dropped or guessed**: a `null` NO₂ value stays `NULL`; an NDW `-1` becomes `value = NULL, is_valid = false` with the raw value kept.

## Alternative considered and rejected

**DynamoDB (option B).** The usual argument is "it scales infinitely", but AirBreda writes roughly ten rows per hour. We would pay for DynamoDB's strengths with its weaknesses: designing partition/sort keys around every query in advance, no joins between air quality and traffic, and clumsy full-table scans for model training. It becomes worth revisiting only if the write rate grows by orders of magnitude (e.g. thousands of sensors at per-second resolution) and access is purely key-based.

## Consequences

**Easier:** fast, simple queries for the API and dashboard; schema-enforced deduplication; reproducible retraining from S3; the pipeline recovers from outages on its own.

**Harder / known limitations (to revisit):**
1. **Late corrections are ignored.** With `DO NOTHING`, an hour first stored as `NULL` is never updated if Luchtmeetnet fills it in later. Fix: `ON CONFLICT … DO UPDATE SET value = EXCLUDED.value WHERE sensor_readings.value IS NULL` (still idempotent).
2. **Always-on cost.** RDS bills per hour whether queried or not (covered by credits now; stop it when not in use).
3. **Public database endpoint.** Needed so the laptop can connect on Day 1; restricted to one IP by the security group. Once ingestion runs on EC2 (Day 3), make RDS private and allow only the EC2 security group.
4. **NDW parsing is heavy.** The 160 MB site-config XML is parsed once per site (4×) every run. It changes rarely, so it could be cached daily.
5. **One snapshot per hour.** `HH-<site>.csv` keeps one minute of NDW data per hour (the last run in that hour wins). Enough for hourly NO₂ prediction; per-minute history would need minute-level keys.

**At 50 corridors:** still small for Postgres (~5 M rows/year at hourly resolution). The first pressure points would be the NDW parsing time and the single always-on instance, not the storage choice.
