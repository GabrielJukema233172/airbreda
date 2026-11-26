#!/usr/bin/env bash
# AirBreda - Day 3: build and (re)start the stack on the EC2 VM with plain
# `docker run` (no docker compose, no Redis: see ADR-0004).
#
#   cd ~/airbreda && infra/vm/deploy.sh
#
# Containers (all on the user-defined network "airbreda", restart unless-stopped):
#   db              postgres:16-alpine, data in volume airbreda_pgdata, no published port
#   air-ingest      Luchtmeetnet -> Postgres, hourly,        GET /health on 127.0.0.1:8001
#   traffic-ingest  NDW -> S3 (raw CSV) + Postgres, hourly,  GET /health on 127.0.0.1:8002
#   dashboard       Day 4: GET /, /site/{id}, /health on 0.0.0.0:8000 (model.pkl baked in)
#
# Secrets and settings come from ~/airbreda/.env (see infra/vm/env.example).
# S3 access comes from the VM's IAM role (airbreda-ec2-role), never from keys.
set -euo pipefail
cd "$(dirname "$0")/../.."          # repository root on the VM (~/airbreda)

[[ -f .env ]] || { echo "Missing .env - copy infra/vm/env.example to .env and fill it in" >&2; exit 1; }

# Day 4: a separate, read-only database login for the dashboard (sql/dashboard_role.sql).
if ! grep -q '^DASHBOARD_DB_PASSWORD=' .env; then
  # 32 hex characters. Not `tr </dev/urandom | head`: under `set -o pipefail` the
  # SIGPIPE that `head` causes makes the whole script exit silently.
  pw=$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')
  printf 'DASHBOARD_DB_PASSWORD=%s\nDASHBOARD_DATABASE_URL=postgresql://dashboard_ro:%s@db:5432/airbreda\n' "$pw" "$pw" >> .env
fi
set -a; . ./.env; set +a

echo "== Building images"
docker build -q -t airbreda-air       -f services/air/Dockerfile .
docker build -q -t airbreda-traffic   -f services/traffic/Dockerfile .
docker build -q -t airbreda-dashboard -f services/dashboard/Dockerfile .

docker network inspect airbreda >/dev/null 2>&1 || docker network create airbreda
docker volume create airbreda_pgdata >/dev/null

echo "== Database"
if ! docker container inspect db >/dev/null 2>&1; then
  # The database is created once and never recreated by a deploy: its data lives
  # in the volume, and schema.sql only runs on the very first start.
  docker run -d --name db \
    --network airbreda --restart unless-stopped \
    --env-file .env -e POSTGRES_DB=airbreda -e POSTGRES_USER=airbreda \
    -v airbreda_pgdata:/var/lib/postgresql/data \
    -v "$PWD/sql/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql:ro" \
    --health-cmd "pg_isready -U airbreda -d airbreda" --health-interval 5s --health-retries 10 \
    --memory 384m \
    postgres:16-alpine
fi
docker start db >/dev/null
until [[ "$(docker inspect -f '{{.State.Health.Status}}' db)" == "healthy" ]]; do sleep 2; done
echo "db is healthy"

docker exec -i db psql -q -U airbreda -d airbreda -v ON_ERROR_STOP=1 -v pw="$DASHBOARD_DB_PASSWORD" < sql/dashboard_role.sql

echo "== Application containers (recreated so they run the freshly built images)"
docker rm -f air-ingest traffic-ingest dashboard >/dev/null 2>&1 || true

docker run -d --name air-ingest \
  --network airbreda --restart unless-stopped \
  --env-file .env -p 127.0.0.1:8001:8000 \
  --log-opt max-size=10m --log-opt max-file=3 \
  --memory 256m \
  airbreda-air

docker run -d --name traffic-ingest \
  --network airbreda --restart unless-stopped \
  --env-file .env -p 127.0.0.1:8002:8000 \
  --log-opt max-size=10m --log-opt max-file=3 \
  --memory 1g \
  airbreda-traffic

# Day 4: the only container reachable from outside the VM (security group: port 8000).
# It gets ONLY the variables it needs (not --env-file .env, which also holds the
# owner's DATABASE_URL/POSTGRES_PASSWORD): a SELECT-only login and the bucket name.
# S3 reads use the VM's IAM role.
docker run -d --name dashboard \
  --network airbreda --restart unless-stopped \
  -e DASHBOARD_DATABASE_URL -e S3_BUCKET -e AWS_REGION -e AWS_DEFAULT_REGION \
  -p 8000:8000 \
  --log-opt max-size=10m --log-opt max-file=3 \
  --memory 384m \
  airbreda-dashboard

docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
