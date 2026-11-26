# Day 2 — run and check the stack

PowerShell, from the `airbreda` folder. Your existing `.env` (POSTGRES_PASSWORD,
S3_BUCKET) works as-is; your Day 1 data is kept and upgraded automatically.

## 1. Rebuild and start everything

```powershell
docker compose down                 # removes the old Day 1 containers (data volume is kept)
docker compose up --build           # foreground, as in the lab: logs scroll in this window
```

Or in the background (recommended once it works):

```powershell
docker compose up -d --build
docker compose ps                   # db, redis, air-ingest, traffic-ingest -> "healthy" within ~1 min
```

Each ingester runs one cycle immediately at start, then every hour.

## 2. Lab 1 Follow — both containers reach Redis

```powershell
docker exec air-ingest redis-cli -h redis ping       # PONG
docker exec traffic-ingest redis-cli -h redis ping   # PONG
docker network ls                                    # a network named "airbreda"
docker network inspect airbreda --format "{{range .Containers}}{{.Name}} {{end}}"
```

## 3. Lab 1 Extend — inspect the queue

```powershell
docker exec -it redis redis-cli
> LLEN readings
> LRANGE readings 0 -1
> exit
```

The first cycle publishes up to 50 NO₂ messages plus one per NDW site (4). After that,
only new or changed readings are published.

## 4. Lab 2 — logs, health, flags

Structured logs (one JSON object per line):

```powershell
docker compose logs air-ingest
docker compose logs traffic-ingest
docker compose logs traffic-ingest | Select-String DATA_QUALITY_ERROR
```

Health endpoints (`curl.exe`, because plain `curl` in PowerShell is a different command):

```powershell
curl.exe http://localhost:8001/health
curl.exe http://localhost:8002/health
```

Database, including the new `is_flagged` column and the NDW rows:

```powershell
docker compose exec db psql -U airbreda -c "\d sensor_readings"
docker compose exec db psql -U airbreda -c "SELECT component, COUNT(*), COUNT(value), SUM(is_flagged::int) AS flagged, MAX(timestamp) FROM sensor_readings GROUP BY component;"
docker compose exec db psql -U airbreda -c "SELECT * FROM sensor_readings WHERE is_flagged ORDER BY timestamp DESC LIMIT 10;"
```

The lab's `ALTER TABLE sensor_readings ADD COLUMN is_flagged ...` has already been run
for you by the services (`common/db.py`, `IF NOT EXISTS`). Running it by hand as well
is harmless if you want to see it:

```powershell
docker compose exec db psql -U airbreda -c "ALTER TABLE sensor_readings ADD COLUMN IF NOT EXISTS is_flagged BOOLEAN DEFAULT FALSE;"
```

Run one extra cycle without waiting an hour:

```powershell
docker compose restart air-ingest          # a restart runs a cycle immediately
```

## 5. Tests

```powershell
docker run --rm -v "${PWD}:/app" -w /app python:3.12-slim sh -c "pip install -q -r requirements-dev.txt && pytest"
```

Expect `25 passed` on Day 2 (`47 passed` since Day 4 added the model and dashboard tests).

## 6. Stop

```powershell
docker compose stop        # everything stops; Postgres and Redis data are kept
```
