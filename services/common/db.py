"""PostgreSQL access shared by both ingestion services."""

from __future__ import annotations

# Runs on every service start. Every statement is safe to repeat, so it doubles
# as a tiny migration for databases created on Day 1 (before is_flagged existed
# and before NDW site IDs, which are 27 characters, had to fit in station_id).
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    station_id  VARCHAR(40)  NOT NULL,
    timestamp   TIMESTAMPTZ  NOT NULL,
    component   VARCHAR(10)  NOT NULL,
    value       FLOAT,
    is_flagged  BOOLEAN      NOT NULL DEFAULT FALSE,
    PRIMARY KEY (station_id, timestamp, component)
);
ALTER TABLE sensor_readings ADD COLUMN IF NOT EXISTS is_flagged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE sensor_readings ALTER COLUMN station_id TYPE VARCHAR(40);
"""

# Idempotent upsert (see ADR-0001):
#  - new (station, timestamp, component)          -> inserted
#  - stored value is NULL, new value is not NULL  -> value filled in, flag updated
#  - anything else                                -> no change
# RETURNING (xmax = 0) is true for an inserted row, false for an updated one.
UPSERT_SQL = """
INSERT INTO sensor_readings AS s (station_id, timestamp, component, value, is_flagged)
VALUES %s
ON CONFLICT (station_id, timestamp, component) DO UPDATE
    SET value = EXCLUDED.value,
        is_flagged = EXCLUDED.is_flagged
    WHERE s.value IS NULL AND EXCLUDED.value IS NOT NULL
RETURNING (xmax = 0) AS inserted;
"""


def upsert_readings(rows: list[tuple], database_url: str) -> tuple[int, int]:
    """rows: (station_id, timestamp, component, value, is_flagged).
    Returns (inserted, NULLs filled in)."""
    if not rows:
        return 0, 0
    import psycopg2  # imported lazily so the services' unit tests need no driver
    from psycopg2.extras import execute_values

    with psycopg2.connect(database_url, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        result = execute_values(cur, UPSERT_SQL, rows, fetch=True)
    inserted = sum(1 for (is_new,) in result if is_new)
    return inserted, len(result) - inserted
