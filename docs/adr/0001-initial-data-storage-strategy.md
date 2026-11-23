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
- **Survive** duplicates, gaps and late data: the API can be down, our job can run twice, sensors report `-1` or `null`, and Luchtmeetnet sometimes publishes an hour late.

Volume is small: at hourly resolution, 1 station × 3 components + 4 sites × 2 metrics ≈ **96,000 rows/year**.

Constraints: one engineer, AWS Free plan credits, a 5-day build, and the final system has to run as containers on **one cloud VM** (confirmed with the lecturers: the database runs on the VM too).

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **A. PostgreSQL in a Docker container (laptop now, EC2 VM from Day 3) + S3 for raw files** | Fits the fixed schema; indexed time-range queries; SQL joins; the primary key enforces no duplicates; no extra cost (shares the VM); the same `docker-compose.yml` runs on the laptop and the VM; the port is only reachable from the host itself | Backups, upgrades and disk space are our job; if the VM's disk is lost, so is the database |
| B. Managed PostgreSQL (Amazon RDS) + S3 | Automated backups, patching and failover; no database to operate | Bills every hour on top of the VM (~$15/month at db.t4g.micro); needs a network path (security group, or a public endpoint during development); a second machine for a ~100k-row/year workload |
| C. DynamoDB only | Serverless, scales writes far beyond our needs | Time-range + station queries need careful key design; no joins (air ⋈ traffic must happen in application code); ad-hoc analysis for ML is awkward; scale we will never use |
| D. S3 only, queried with Athena | Cheapest storage, no server | Seconds of latency per query and pay-per-scan: wrong for an API called on every page load |

## Decision

**Option A.**

- **Relational database instead of NoSQL**: the readings are strongly structured, the main queries are `WHERE station_id = … AND timestamp BETWEEN …`, and air and traffic must be joined on time. These are what SQL and a B-tree index on the primary key do well. At ~96k rows/year one small Postgres has decades of headroom.
- **Self-hosted in a container instead of RDS**: at this size, RDS's managed backups and failover don't justify a second always-on bill or a second machine to secure. Running Postgres next to the ingestion containers keeps the whole system on one VM, as the assignment requires. The database port is bound to `127.0.0.1`, so it is never exposed to the internet; the containers reach it over Docker's internal network.
- **Raw files in object storage (S3)**: each NDW run is saved as `ndw/YYYY-MM-DD/HH-<site>.csv` (date/hour from the *measurement* time, UTC), one row per lane value including the raw value and an `is_valid` flag. S3 is the audit trail and the retraining dataset. Because the database now lives on a single VM disk, S3 also means a lost database can be rebuilt instead of being gone.
- **Duplicates**: delivery is **at-least-once with idempotent writes**, not exactly-once.
  - Database: primary key `(station_id, timestamp, component)`. Every run re-fetches the last 50 hours, so a missed run heals itself and re-inserting changes nothing.
  - S3: object keys are deterministic, so a re-run in the same hour overwrites the same file instead of adding a second one.
- **Late data — a deliberate deviation from the lab**: the lab asks for `ON CONFLICT DO NOTHING`. That would keep a `NULL` forever, even after Luchtmeetnet publishes the missing hour, and the 50-hour re-fetch would be pointless for exactly the case it exists for. We use instead:

  ```sql
  ON CONFLICT (station_id, timestamp, component) DO UPDATE
      SET value = EXCLUDED.value
      WHERE s.value IS NULL AND EXCLUDED.value IS NOT NULL
  ```

  This is still idempotent (a second identical run changes nothing), fills a `NULL` once the real value arrives, and never overwrites a real measurement. All of this was tested against PostgreSQL 16.
- **Bad values are kept, not dropped or guessed**: a `null` NO₂ value stays `NULL`; an NDW `-1` becomes `value = NULL, is_valid = false` with the raw value kept.

## Alternative considered and rejected

**DynamoDB (option C).** The usual argument is "it scales infinitely", but AirBreda writes roughly ten rows per hour. We would get its weaknesses without needing its strengths: partition/sort keys designed around every query in advance, no joins between air quality and traffic, and full-table scans for model training. It only becomes worth revisiting if the write rate grows by orders of magnitude (e.g. thousands of sensors at per-second resolution) and access stays purely key-based.

RDS (option B) was the managed-database alternative, and it is the first thing we would switch to if AirBreda had to promise uptime to the municipality or stored data that could not be rebuilt from S3.

## Consequences

**Easier:** fast, simple queries for the API and dashboard; deduplication enforced by the schema; late values corrected automatically; reproducible retraining from S3; zero extra hourly cost; one `docker compose` file from laptop to VM.

**Harder / known limitations (to revisit):**
1. **Backups are ours.** Nothing backs up the Postgres volume automatically. Plan: a nightly `pg_dump` uploaded to S3 (Day 3), plus S3 raw files as the last resort.
2. **Single point of failure.** One VM runs everything; if it stops, ingestion and the API both stop. Acceptable for a demo, not for a public service.
3. **Corrections to real values are ignored.** If Luchtmeetnet revises an already-published value (e.g. after validation), we keep the first one. Revisit if validated data matters to the model.
4. **NDW parsing is heavy.** The 160 MB site-config XML is parsed once per site (4×) every run. It changes rarely, so it could be cached daily.
5. **One snapshot per hour.** `HH-<site>.csv` keeps one minute of NDW data per hour (the last run in that hour wins). Enough for hourly NO₂ prediction; per-minute history would need minute-level keys.

**At 50 corridors:** still small for Postgres (~5 M rows/year at hourly resolution). The first pressure points would be NDW parsing time and VM memory/CPU shared between Postgres and the ingesters, which is where moving the database to RDS would start to pay off.
