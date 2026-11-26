# AirBreda

Cloud platform that ingests live NO₂ readings (RIVM Luchtmeetnet, station NL10240 Breda-Tilburgseweg) and live traffic counts (NDW, four A27 sites near hectometer 63), stores and processes them, and serves a regression model that predicts NO₂ near the A27 interchange.

Built for the System Design & Cloud Platforms elective (ADSAI, BUas). Cloud provider: **AWS** (region eu-north-1, Stockholm: the only region the account's organization policy allows; see ADR-0004).

Since Day 3 the stack runs on an EC2 VM: see [infra/day3-aws-setup.md](infra/day3-aws-setup.md). Since Day 4 the dashboard runs there too, at **http://16.192.119.40:8000** (Elastic IP; public until the Day 5 evaluation): see [infra/day4-deploy.md](infra/day4-deploy.md).

## Repository layout

| Path | What it holds |
|---|---|
| `services/common/` | Shared code: JSON logging, data-quality counters, `/health`, Postgres upsert, Redis publisher |
| `services/air/` | `ingest_air.py` + Dockerfile: Luchtmeetnet NO₂ → PostgreSQL + Redis, `/health` on :8001 |
| `services/traffic/` | `ingest_traffic.py` + Dockerfile: NDW traffic → CSV/S3 + PostgreSQL + Redis, `/health` on :8002 |
| `services/dashboard/` | Day 4: `dashboard.py` (FastAPI: `/`, `/site/{id}`, `/health`), `predict.py`, `model.pkl` (baked into the image), Dockerfile |
| `ml/` | Day 4: `build_training_data.py` (Postgres + S3 → `training_data.csv`), `train_model.py` (→ `model.pkl`, `metrics.json`, plot) |
| `docker-compose.yml` | Local setup: Postgres, Redis, both ingestion services and the dashboard on the laptop |
| `infra/vm/` | Day 3 VM deployment: `deploy.sh` (plain `docker run`, no Redis), `backup.sh` (nightly `pg_dump` → S3), `env.example` |
| `infra/aws/` | IAM trust and S3 policies for the VM's instance role |
| `tests/` | pytest suite (`pytest` from the repo root) |
| `sql/` | Database schema |
| `infra/` | Step-by-step setup and run commands |
| `notebook/` | Lab notebook answers per day |
| `scripts/` | Original course helper scripts (`getNO2Readings.py`, `getTrafficReadings.py`) |
| `docs/` | Architecture Design Document, published with GitHub Pages |
| `docs/adr/` | One Architecture Decision Record per decision |
| `docs/diagrams/` | Architecture diagrams |

## Data sources

| Source | Endpoint | Data |
|---|---|---|
| RIVM Luchtmeetnet | `https://api.luchtmeetnet.nl/open_api/stations/NL10240/measurements` | Hourly NO₂ (µg/m³), JSON |
| NDW Open Data | `http://opendata.ndw.nu/snelheden_en_intensiteiten_meetgegevens.xml.gz` | Per-minute flow and speed, gzipped DATEX II XML |
