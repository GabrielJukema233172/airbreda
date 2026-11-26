# Lab notebook — Day 3

> Factual answers are filled in from what was actually built and measured on 25 Nov 2026.
> Reflection is yours (✍️).

## Before-you-start checklist (adapted to this repo)

| Lab item | Status |
|---|---|
| CLI authenticated | ✅ `aws sts get-caller-identity` → user `Gabriel-Admin`, account 702764210355 |
| Docker Desktop running | ✅ |
| `ingest_air.py` writes to `sensor_readings` | ✅ (reads the Luchtmeetnet JSON API, not a CSV) |
| `ingest_traffic.py` saves `ndw/YYYY-MM-DD/HH-site.csv` | ✅ locally and to S3 |
| Both run in containers as `airbreda-air` / `airbreda-traffic` | ✅ built from `services/*/Dockerfile` (the lab's `Dockerfile.traffic` = `services/traffic/Dockerfile`) |
| `sensor_readings` in the cloud database with rows | ✅ Postgres container on the EC2 VM (ADR-0001: not RDS) |
| ADR-001…003 committed | ✅ |

## Day 3 wrap-up

| Lab item | Status |
|---|---|
| VM running in the cloud | ✅ `i-0dd1b70fd3b8badb5`, t3.small, eu-north-1c, 13.53.194.127 |
| Both ingestion containers run on the VM | ✅ `air-ingest`, `traffic-ingest` (+ `db`), restart `unless-stopped`; came back by themselves after a reboot |
| `sensor_readings` has real rows from today | ✅ 125 rows after the first cycle (incl. the laptop's Day 1–2 history), growing hourly |
| ≥ 1 NDW CSV in the bucket | ✅ `s3://airbreda-raw-730631/ndw/2026-11-25/19-{hrl,hrr,vwd,vwa}.csv`, then 4 more per hour |
| IAM role, no hardcoded credentials | ✅ `airbreda-ec2-role`; no `~/.aws` on the VM; boto3 logs `Found credentials from IAM Role` |
| ADR-004 written | ✅ [docs/adr/0004-compute-strategy.md](../docs/adr/0004-compute-strategy.md) |
| Billing alert | ✅ Budget `airbreda-monthly`, $11 ≈ €10 |
| Extra: nightly backup + restore test | ✅ (promised in ADR-0003) |

## Cost check

- **Cost Explorer, November so far:** $0.00 (Cost Explorer lags by up to a day; the VM started at 19:27 UTC).
- **Expected run rate:** t3.small $0.0216/h + 20 GB gp3 $1.67/month + public IPv4 $0.005/h ≈ **$0.70/day, ≈ $21/month**. Everything is paid from the Free plan credit ($119.99 remaining).
- The $11 budget's **forecast** alert will fire, because a full month of the VM is forecast at ~$21. That is intentional: the email is the reminder to stop the VM after Day 5.

## Things that surprised me ✍️

Pointers: the region lock (eu-central-1 and eu-west-1 denied by the organization's SCP), the 700 MB memory peak of one NDW parse, cron missing from Amazon Linux 2023 and the empty crontab, IMDS hop limit 2 for containers, and SSH dropping from the laptop while the VM kept ingesting on its own.

My answer:
