# Day 1 — setup and run steps

> **Superseded for running the stack:** from Day 2 the services are called `air-ingest` and
> `traffic-ingest` and run continuously. Use [day2-run.md](day2-run.md). The S3 bucket
> steps below are still valid.

All commands are **PowerShell**, run from the **`airbreda`** folder
(`cd "C:\Users\gabri\Downloads\Elective 2\airbreda"`).

The database is **PostgreSQL in a Docker container** (see ADR-0001), defined in
`docker-compose.yml`. On Day 1 it runs on your laptop; on Day 3 the same file runs on
the EC2 VM. The only AWS resource today is the S3 bucket.

## 0. Check the tools (Lab 1 Follow)

```powershell
aws sts get-caller-identity   # prints your account and the gabriel-admin user
docker run hello-world        # prints "Hello from Docker!"
docker compose version        # Docker Compose v2 ships with Docker Desktop
```

## 1. Lab 1 — air-quality container on its own

```powershell
docker build -t airbreda-air services\air
docker run --rm airbreda-air
```

You should see the latest NO₂ value for NL10240 and `Database: skipped`.

Run the tests:

```powershell
pip install pytest pandas requests
pytest
```

## 2. Secrets file

```powershell
Copy-Item .env.example .env
notepad .env
```

Set `POSTGRES_PASSWORD` to a long random string (letters and digits are easiest).
Leave `S3_BUCKET` for step 4. `.env` is git-ignored — check with `git status` that it
never shows up.

## 3. Lab 2 — Postgres container + idempotent writes

```powershell
docker compose up -d db                 # starts Postgres, creates sensor_readings from sql/schema.sql
docker compose ps                       # db should become "healthy"

docker compose run --rm air             # "50 inserted, 0 NULLs filled in, 0 unchanged"
docker compose run --rm air             # "0 inserted, 0 NULLs filled in, 50 unchanged"  <- idempotent
```

Look inside the database:

```powershell
docker compose exec db psql -U airbreda -c "SELECT COUNT(*), COUNT(value), MIN(timestamp), MAX(timestamp) FROM sensor_readings;"
docker compose exec db psql -U airbreda -c "SELECT * FROM sensor_readings ORDER BY timestamp DESC LIMIT 5;"
```

The data lives in the Docker volume `airbreda_pgdata`, so it survives
`docker compose down` and restarts. (`docker compose down -v` deletes it.)

## 4. Lab 2 Extend — S3 bucket + traffic container

```powershell
$Bucket = "airbreda-raw-" + (Get-Random -Maximum 999999)   # bucket names are global
aws s3 mb "s3://$Bucket" --region eu-central-1
$Bucket   # put this name in .env as S3_BUCKET
notepad .env

docker compose run --rm traffic         # takes 1-3 minutes (160 MB of NDW XML)
aws s3 ls "s3://$Bucket/ndw/" --recursive
```

New S3 buckets block all public access by default — keep it that way.

## 5. End of the day

```powershell
docker compose stop        # stops Postgres; data is kept in the volume
```

Nothing in AWS bills by the hour today: S3 costs fractions of a cent for these files.
