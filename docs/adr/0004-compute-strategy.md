# ADR 0004: Compute Strategy

- **Status:** Accepted
- **Date:** 2026-11-25
- **Course day:** Day 3
- **Amends:** ADR-0002 (queue not deployed), ADR-0003 (region)

## Context

Until Day 2 everything ran on the laptop with `docker compose`: Postgres, Redis and both ingesters. A laptop gets closed, so from Day 3 the ingesters have to run 24/7 in the cloud. Day 4 adds an API/dashboard container next to them.

Facts that shape the choice:

- **Workload:** two jobs per hour. The air job takes about 1 second (one JSON call, 50 rows). The traffic job takes about 1 minute: it downloads two gzipped NDW feeds and parses ~230 MB of XML. **Measured peak memory of one traffic cycle: ~700 MB** (`ru_maxrss`, 25 Nov 2026). Everything else is idle.
- **Data:** ~10 new rows per hour, Postgres in a container on the same host (ADR-0001), raw files in S3.
- **Account:** an AWS Free plan account ($100 credit, until May 2027). It is a member of an AWS Organization whose service control policy (SCP) **denies EC2, RDS, Lightsail and S3 bucket listing in every region except eu-north-1 (Stockholm)**. That includes eu-central-1, which ADR-0003 assumed, and eu-west-1, which the lab suggests.
- **Team:** one engineer, and the system has to be live for the Day 5 evaluation.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **A. One EC2 VM, containers started with `docker run`** | Full control; Postgres, ingesters and the Day 4 API share one host as ADR-0001 planned; free-tier eligible types run on the Free plan credit; nothing to learn beyond Docker and SSH | We patch the OS, run Docker and restart things; single point of failure (ADR-0003); we pay for 24 h of a machine that works ~1 minute per hour |
| B. ECS on Fargate, scheduled by EventBridge Scheduler | No servers to patch; pay per second of task runtime; the production pattern for scheduled jobs | Needs ECR, task definitions, a task IAM role and VPC networking; the database can't live in a Fargate task, so it forces RDS (≈ $14/month extra) at the same time; the Day 4 API would need its own always-on service or a load balancer |
| C. AWS Lambda on a schedule | Cheapest at this volume (well inside the free requests) | The traffic job needs ~700 MB and pandas-sized packages (container-image Lambda); still needs a database somewhere; doesn't fit the course deliverable of "a VM running three containers" |
| D. Lightsail instance | Fixed monthly price, simpler console | Denied by the organization's SCP in our region of choice; fewer networking and IAM controls than EC2 |

## Decision

**Option A: one EC2 instance in eu-north-1, `t3.small` (2 vCPU, 2 GB), Amazon Linux 2023, 20 GB gp3, with an IAM instance role. The containers run with plain `docker run` (`infra/vm/deploy.sh`), not `docker compose`.**

### Why a VM instead of a managed container service (for today)
- **Our database lives on it.** ADR-0001 put Postgres in a container to avoid RDS's bill and networking. A managed container service would force the database move now, adding a second paid service and a VM↔DB network path to secure, for a workload of ten rows an hour.
- **Lowest total effort for one engineer.** One machine, one SSH key, one script. Every moving part is visible with `docker ps` and `docker logs`, and the whole deployment is reproducible from the runbook (`docs/runbook.md`).
- **Cost:** it runs on the Free plan credit, and the bill is fixed and predictable (see below), not per invocation.
- **What we give up:** OS patching, Docker upgrades, disk space and recovery are our job (IaaS). ADR-0003 already accepts the single point of failure for a 99% SLO.

### Instance size: t3.small, not the lab's t3.micro
One traffic cycle peaks at ~700 MB. A t3.micro has 1 GB in total, and Amazon Linux plus Docker use ~250 MB before Postgres (~30–100 MB) and the air service (~60 MB) start. The traffic container would be OOM-killed in the middle of its parse, or the kernel would kill Postgres instead. A t3.small (2 GB) leaves ~1 GB of headroom for the Day 4 API. Each container has a memory limit (`db` 384 MB, `air-ingest` 256 MB, `traffic-ingest` 1 GB), so a runaway parse kills itself, not the database. CPU credits are set to **standard**, so a burstable t3 can never run up a surplus-credit bill.

### Scheduling: a long-running loop with a restart policy, not cron
The lab's Option A is cron + `docker run --rm` every hour. We keep the Day 2 design instead: each ingester runs one cycle at start and then every hour in-process, started with `--restart unless-stopped`. The Docker daemon is enabled at boot, so Docker restarts the containers after a crash or a reboot. **We tested this:** after `aws ec2 reboot-instances`, all three containers were back and the air service had completed a cycle within ~1 minute.

- It keeps `GET /health` (127.0.0.1:8001/8002) answering between cycles. A cron container only exists for one minute an hour, so there is nothing to ask whether ingestion is healthy.
- The rolling one-hour bad-data counter and its single `BAD_DATA_THRESHOLD_EXCEEDED` alert (ADR-0003) only make sense in a process that lives longer than an hour.
- Cron *is* used for the one genuinely batch job: the nightly `pg_dump` to S3 (`infra/vm/backup.sh`, 02:15 UTC). Restoring it into a scratch database was tested on Day 3 (125 of 125 rows).
- Trade-off: the cycle runs at "start time + n hours", not on the hour, and a hung download would block that service's loop. Neither matters for hourly data that is published 30–90 minutes late.

### Network paths and permissions
- **Inbound:** only TCP 22, only from the developer's IP (`/32`), in security group `airbreda-vm-sg`. The health ports are bound to `127.0.0.1` on the VM. Postgres publishes no port at all: the ingesters reach it as `db:5432` on the Docker network `airbreda`.
- **VM → database:** never leaves the host (Docker bridge network). This is why the lab's "allow the VM's IP in the database security group" step doesn't apply to us.
- **VM → S3:** outbound HTTPS to the regional S3 endpoint. Credentials come from the **IAM role `airbreda-ec2-role`** through the instance metadata service (IMDSv2, hop limit 2 so that containers can reach it). There are no access keys on the VM; the log line `Found credentials from IAM Role: airbreda-ec2-role` proves it.
- **Least privilege now, not on Day 4:** instead of the lab's `AmazonS3FullAccess`, the role may only `PutObject`/`GetObject` under `ndw/*` and `backups/*` of our own bucket (`infra/aws/airbreda-s3-policy.json`). Writing to another prefix or listing the bucket root returns `AccessDenied` (tested). The role can't delete objects, so a compromised VM can add files but not wipe the audit trail.

## What it costs per month

On-demand prices for eu-north-1 from the AWS Price List API (25 Nov 2026), 730 h/month:

| Item | Price | Per month |
|---|---|---|
| EC2 t3.small | $0.0216/h | $15.77 |
| EBS gp3, 20 GB | $0.0836/GB-month | $1.67 |
| Public IPv4 address | $0.005/h | $3.65 |
| S3: ~100 small CSVs/day + nightly dump, ~3,000 PUTs | $0.023/GB-month + $0.005/1,000 PUT | < $0.05 |
| Data transfer out | Downloads from NDW/Luchtmeetnet are inbound (free) | ~$0 |
| **Total** | | **≈ $21/month** (≈ $0.70/day) |

This is paid from the Free plan credit: $99.99 at the start of Day 3, and $119.99 after the plan's activity credits for launching an EC2 instance and creating a budget. That covers about 5.7 months. Cost Explorer showed $0.00 actual spend at the end of Day 3. A t3.micro would have been ≈ $13/month in total, but see the memory argument above. An AWS Budget (`airbreda-monthly`, $11 ≈ €10) emails at 80% of actual and 100% of forecast spend. Because a running t3.small is forecast above $11, **the forecast alert is expected to fire**. That is deliberate: it is the reminder to stop the VM after the course.

## What we would switch to at 50 corridors with 5-minute ingestion

The load does not grow the way "50×" suggests. NDW publishes **one national feed**: one download contains all 50 corridors. The work that grows is parsing (today we parse the 160 MB site-config XML once *per site*) and the 12× more frequent runs: 12 runs/hour × ~1–3 minutes is 20–60% of a small VM, all the time. Luchtmeetnet only publishes hourly, so air ingestion stays hourly (50 stations ≈ 50 calls/hour, well under the 100 requests/5 min fair-use limit).

1. **Database to Amazon RDS for PostgreSQL** (db.t4g.micro ≈ $11.68/month + storage, automated backups, Multi-AZ option). This comes first, because it separates data from compute, and every option below needs it.
2. **Ingestion as scheduled ECS Fargate tasks** (EventBridge Scheduler every 5 minutes). For example: a 1 vCPU / 2 GB task running ~2 minutes, 12×/hour ≈ 292 task-hours/month × ($0.0445 + 2 × $0.0049) ≈ **$16/month**. No VM to patch, and runs can't pile up on one machine.
3. **Split the traffic job into download → parse.** One task downloads the feed once to S3, and per-corridor parsing happens in parallel. That is the point where a queue comes back (see below).
4. The API/dashboard becomes an ECS service behind a load balancer, or stays on a small VM. Kubernetes only becomes worth it once there are many independently deployed services. Two jobs and an API don't justify it.

## Operational concerns we had not anticipated

1. **The region was not ours to choose.** The lab and ADR-0003 assumed eu-central-1 or eu-west-1. Both return `explicit deny in a service control policy` for every EC2 call, even `DescribeInstances`. We only found eu-north-1 by probing every region. Stockholm is still in the EU, so the GDPR argument holds. The extra network distance to the Netherlands is irrelevant for hourly batch jobs. ADR-0003's cost table and DR reasoning are otherwise unchanged; the prices differ by about 10%.
2. **Memory, not CPU, is the constraint.** The ~700 MB peak of one NDW parse decided the instance size (above). On paper, "hourly job, ten rows" looked like the smallest possible machine.
3. **Silent cron failures are real.** Amazon Linux 2023 ships *without* cron (we installed `cronie`). Our first attempt to install the crontab silently wrote an *empty* one: under `set -e`, `grep -v` on an empty crontab exits 1 and killed the subshell before the `echo`. Only `crontab -l` afterwards caught it. That is exactly the "a cron job fails silently" warning in the lab.
4. **Containers need IMDS hop limit 2.** With the IMDSv2-only default and a hop limit of 1, a container behind Docker's bridge can't reach the metadata service, and boto3 falls back to "no credentials". We set the hop limit to 2 at launch.

## Why the Redis queue from ADR-0002 is not deployed, and what would bring it back

ADR-0002 added Redis *alongside* direct writes, to decouple producers from future consumers. On the VM there are still **no consumers**. On Day 2 the list only grew: 158 messages after one afternoon, mostly duplicates, because the air service republishes its 50-hour window after every restart. A broker with no reader adds a container to run, persist and monitor, and gives nothing back. The ingesters already write to Postgres and S3 directly, so removing Redis is one missing environment variable (`REDIS_HOST`). No code changed, and `docker-compose.yml` still runs the Day 2 setup locally.

**What brings it back, and as what:**
- **A second consumer of new readings** that should react to them rather than poll the database, e.g. an inference service that predicts as soon as a reading lands, or an exceedance alerter.
- **Ingestion outgrowing one VM**: the download → parse split above, with parse workers on other machines.

In both cases it returns as **Amazon SQS** (with SNS for fan-out), not Redis. Consumers on other machines are exactly the trigger ADR-0002 named for switching to a managed queue.

## Consequences

**Easier:** the system now runs without the laptop (a scheduled cycle completed while SSH to the VM was unreachable); there is one documented, scripted path from an empty account to a running stack; there are no long-lived credentials anywhere on the VM; backups exist and have been restored once.

**Harder / to revisit:**
- OS and Docker patching is manual (`sudo dnf update`). Revisit with automatic security updates or SSM Patch Manager. SSM needs an instance-management role this account doesn't have yet (seen in the boot log).
- SSH from a single home IP: if that IP changes, the security group rule has to be updated (`docs/runbook.md` §7). EC2 Instance Connect or SSM Session Manager would remove the open port entirely.
- The Day 4 API will need one more inbound rule (its port, ideally behind HTTPS). That is the first part of the system exposed to the public internet.
- **The public IP is not fixed.** Stopping and starting the VM releases its auto-assigned IPv4 address and gives it a new one, and the Day 5 deliverable is "reachable at its public IP". Before Day 5, attach an Elastic IP (same $0.005/h while in use). **Done 26 Nov:** Elastic IP 16.192.119.40. Caveat: unlike the auto-assigned address, it keeps billing while the VM is stopped, so it must be released when the project ends.
- After the course: **stop** the VM (the disk and the data survive; ≈ $1.67/month for the disk only), or snapshot it and terminate (termination protection has to be switched off first).
