# ADR 0005: Compute & Deployment Strategy

- **Status:** Accepted
- **Date:** 2026-11-26
- **Course day:** Day 4
- **Extends:** ADR-0004 (does not supersede it)

## Context

Day 4 adds the first user-facing component: a dashboard + JSON API (`GET /`, `GET /site/{id}`, `GET /health`) that serves real NO₂, real traffic and a prediction from a model we trained ourselves (ADR-0006). It has to run 24/7 next to the two ingesters, be reachable from a browser, and survive the same failures they do.

What it needs at runtime:

| Need | Where it comes from |
|---|---|
| Latest NO₂ | PostgreSQL `sensor_readings`, on the same VM (`db:5432` on the Docker network) |
| Latest traffic per site | S3 `ndw/YYYY-MM-DD/HH-<site>.csv` (List + Get) |
| The model | `model.pkl`, ~1 KB |
| Ingester health | `http://air-ingest:8000/health` and `http://traffic-ingest:8000/health` on the Docker network |

Measured on the VM (26 Nov, after a traffic cycle): dashboard 138 MB, traffic-ingest 279 MB (peaks ~700 MB while parsing), air-ingest 55 MB, db 40 MB. The VM uses ~0.8 GB of 1.9 GB with all four containers running.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **A. A third application container on the existing VM** | Zero new infrastructure or cost; Postgres and the ingesters' `/health` are a Docker-network hop away; same deploy script, same restart behaviour, same logs | One more thing on the single point of failure; the dashboard shares CPU with the NDW parse (minute-long spikes, hourly) |
| B. Dashboard on ECS Fargate / App Runner, ingesters stay on the VM | Managed, scales horizontally, HTTPS built in (App Runner) | It needs a network path to a database that is deliberately unreachable from outside the VM (ADR-0001), so it would force RDS or opening Postgres; ≈ $15–30/month more; two deployment mechanisms for one small system |
| C. Lambda + API Gateway | Pay per request, near zero at our traffic | Same database-reachability problem (Lambda in a VPC + RDS); cold starts loading pandas/scikit-learn; the HTML page and the API would be split over two services |

## Decision

**Option A: the dashboard is the third application container on the same EC2 VM, deployed by the same `infra/vm/deploy.sh` and started with `docker run -d --restart unless-stopped`.**

### Why a third container on the same VM, rather than a managed service
- **The data is here.** Postgres lives on this VM and has no published port. Any compute outside the VM would first need RDS or an opened database, and that is the expensive part of options B and C, not the dashboard itself.
- **The load is trivial.** At most a few page loads per minute, each making four API calls. S3 results are cached for 60 s, so a page load costs a handful of millisecond-level Postgres queries.
- **One deployment mechanism.** Building, running, logging and restarting the dashboard works exactly like the ingesters. One person can hold the whole system in their head.

**What would change our mind:**
- the dashboard needs to stay up while the VM is patched or replaced (an availability promise beyond ADR-0003's 99%);
- real public traffic, so it must scale out behind a load balancer;
- or the database moves to RDS (ADR-0004's 50-corridor plan).

Once the database is off the VM, the dashboard is a stateless container that can move to ECS or App Runner unchanged. We designed for that: its configuration is entirely environment variables, and its model is inside the image.

### How it runs long-term, and what happens on a reboot
`docker run -d --restart unless-stopped`, like the other three containers, with the Docker daemon enabled at boot (`systemctl enable docker`). After a reboot, Docker starts **all four** containers again. We tested this on Day 3 for db + ingesters, and again on Day 4 with the dashboard: after `aws ec2 reboot-instances`, all four were up within about a minute and `GET /site/vwd` answered from the laptop with fresh data. The only container that does *not* come back is one that was deliberately stopped with `docker stop` (that's what "unless-stopped" means).

We chose this over a systemd unit per container: a systemd unit would add ordering (`After=docker.service`) and log shipping to journald, but `--restart` already gives "start at boot, restart on crash". The one ordering gap is the dashboard starting before Postgres is ready after a boot. It is harmless: `/site/{id}` and `/health` report the database error for those few seconds and recover by themselves, with no crash loop.

Resource limits keep one container from starving the others: `db` 384 MB, `air-ingest` 256 MB, `traffic-ingest` 1 GB, `dashboard` 384 MB.

### Exposure and least privilege
- **Network:** the dashboard is the only container with a port on the VM's public interface (`0.0.0.0:8000`). The security group allows 8000 only from the developer's two IPs. For the Day 5 evaluation the grader's IP (or `0.0.0.0/0` for the day) will have to be added. The data is public open data, but the endpoint has no authentication and no HTTPS, so it shouldn't stay open to the world by default.
- **Database:** the dashboard logs in as **`dashboard_ro`**, which may only `SELECT` from `sensor_readings` (`sql/dashboard_role.sql`). It is *not* given the owner's `DATABASE_URL`/`POSTGRES_PASSWORD`: `deploy.sh` passes it four named variables instead of `--env-file .env`. Tested: `DELETE` and `CREATE TABLE` return `permission denied`.
- **S3 / IAM:** the lab's Day 4 step ("replace `AmazonS3FullAccess` with `PutObject`/`GetObject` on just your bucket") was done on Day 3, and narrower: the role may only `Put`/`Get` under `ndw/*` and `backups/*`, and `ListBucket` only for those prefixes. The dashboard needed no change: it uses `ListBucket` (prefix `ndw/YYYY-MM-DD/`) and `GetObject` on `ndw/*`. The ingesters still wrote successfully after deploying, and nothing failed with `AccessDenied`. **Remaining gap:** on plain EC2, every container shares the instance role, so the dashboard *could* write to `ndw/`. Per-container roles need ECS task roles. That is one more argument for ECS once we leave the single VM.

### What testing locally with Compose caught
We ran the full stack locally (`docker compose up --build`: db, Redis, both ingesters and the dashboard, reading the real S3 bucket with the laptop's credentials) and opened `http://localhost:8000` in a browser and with `curl`.

- **It confirmed:** the image builds, `model.pkl` loads inside the container (same scikit-learn 1.5.2 as training), the S3 listing and parsing work against the real bucket, the page renders real numbers, and the aggregated `/health` reaches both ingesters by container name. All of that was right on the first run. That counts as information, not luck: the model, predict function and API had already been tested with fakes (`tests/test_model.py`, `tests/test_dashboard.py`: 47 tests in total).
- **It could not catch the bug we actually hit:** the first VM deploy did nothing, silently. `deploy.sh`, which Compose never runs, generated the new database password with `tr </dev/urandom | head -c 32`. Under `set -o pipefail`, the SIGPIPE from `head` failed the pipeline, and `set -e` stopped the script without printing anything. We fixed it with `od -N16 /dev/urandom`. **Lesson:** Compose tests the *containers*. The VM's deployment script is a second, untested path. Next step: run `deploy.sh` itself in CI, or switch to one deployment definition (Compose on the VM too) so that what we test is what we ship.
- **Compose vs the lab's version:** the lab's Compose file points all three containers at "your real cloud database". Ours can't, by design: the database has no public endpoint (ADR-0001). Locally the dashboard reads NO₂ from the laptop's own Postgres (filled by the local air ingester from the same API) and traffic from the real bucket.

### Supersedes or extends ADR-0004?
**Extends.** The Day 3 compute decision is unchanged: one EC2 `t3.small` in eu-north-1, containers via `docker run`, a restart policy instead of cron, an IAM role and no Redis on the VM. Day 4 adds:
1. a third application container on the same VM;
2. one inbound security-group rule (TCP 8000 from the developer's IPs);
3. a read-only database role;
4. a second SSH/8000 source IP, because the developer works from two networks.

No IAM change was needed. Monthly cost is unchanged (≈ $21, ADR-0004).

## Consequences

**Easier:** one `deploy.sh` ships everything; the dashboard is stateless and portable (env vars + model in the image); the public surface is one port of one read-only container.

**Harder / to revisit:**
- **No HTTPS.** Before anyone but us uses it: a reverse proxy (Caddy with Let's Encrypt) or a load balancer with ACM.
- **CPU contention:** the hourly NDW parse makes the VM busy for about a minute, and the dashboard is slower during that minute. That's acceptable now. At 5-minute ingestion (ADR-0004), the two belong on separate compute.
- **Two deployment definitions** (`docker-compose.yml` locally, `deploy.sh` on the VM) can drift, as the silent deploy failure showed.
