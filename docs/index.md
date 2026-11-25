# AirBreda — Architecture Design Document

## 1. Overview
_What the system does and for whom._

## 2. Architecture diagram
_Updated at the end of every day._

### Day 1 — initial design
![AirBreda architecture after Day 1](diagrams/day1-architecture.svg)

Solid lines exist today; dashed boxes and lines are planned for Days 2–5.

### Day 2 — queue, structured logging, data quality
![AirBreda architecture after Day 2](diagrams/day2-architecture.svg)

Both ingesters run continuously (hourly) and publish to a shared Redis queue alongside their direct writes. Bad Luchtmeetnet readings are flagged and kept; NDW `-1` values never reach the database but stay in the raw S3 files. Every event is logged as JSON, and each ingester serves `GET /health`.

## 3. Architecture Decision Records
| # | Decision | Status |
|---|---|---|
| [0001](adr/0001-initial-data-storage-strategy.md) | Initial data storage strategy: PostgreSQL in a container + S3, idempotent writes | Accepted |
| [0002](adr/0002-messaging-architecture.md) | Messaging architecture: Redis queue alongside direct writes; flag vs drop bad data | Accepted |
| [0003](adr/0003-resilience-strategy.md) | Resilience strategy: 99% SLO for `/site/{id}`, Backup & Restore | Accepted |

## 4. Cloud provider rationale (for the municipality)
_Plain-language explanation of why AWS, written for a non-technical reader._

## 5. Cost estimate
| | 10 corridors | 50 corridors |
|---|---|---|
| Compute | | |
| Storage | | |
| Data transfer | | |
| **Total / month** | | |

## 6. Reflection
_What would you do differently?_
