#!/usr/bin/env bash
# AirBreda - nightly database backup (ADR-0003: Backup & Restore tier).
#
#   pg_dump of the airbreda database -> gzip -> s3://$S3_BUCKET/backups/postgres/YYYY-MM-DD.sql.gz
#
# Runs from ec2-user's crontab (see infra/day3-aws-setup.md). Cron has a minimal
# PATH, so every command is called by its absolute path. S3 access comes from
# the VM's IAM role; the role may only write under backups/ and ndw/.
#
# Restore (into the running db container):
#   aws s3 cp s3://$S3_BUCKET/backups/postgres/<date>.sql.gz - | gunzip | docker exec -i db psql -U airbreda -d airbreda
set -euo pipefail
cd "$(dirname "$0")/../.."
set -a; . ./.env; set +a

key="backups/postgres/$(/usr/bin/date -u +%F).sql.gz"
/usr/bin/docker exec db pg_dump -U airbreda -d airbreda --clean --if-exists \
  | /usr/bin/gzip \
  | /usr/bin/aws s3 cp - "s3://${S3_BUCKET}/${key}" --region "${AWS_REGION}" --only-show-errors

rows=$(/usr/bin/docker exec db psql -U airbreda -d airbreda -tAc "SELECT COUNT(*) FROM sensor_readings")
echo "{\"time\": \"$(/usr/bin/date -u +%FT%T+00:00)\", \"level\": \"INFO\", \"service\": \"backup\", \"event\": \"backup_uploaded\", \"key\": \"${key}\", \"rows\": ${rows}}"
