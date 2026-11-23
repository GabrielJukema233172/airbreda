"""
AirBreda - air-quality ingestion (Day 1, Labs 1 and 2).

Fetches recent NO2 readings for RIVM Luchtmeetnet station NL10240
(Breda-Tilburgseweg), prints the most recent one and, when DATABASE_URL is
set, writes them idempotently into the `sensor_readings` table.

Builds on get_latest_no2() from scripts/getNO2Readings.py.

    python ingest_air.py                 # Lab 1: print the latest reading
    DATABASE_URL=postgresql://... python ingest_air.py   # Lab 2: also store
    docker compose run --rm air          # Lab 2 with the Postgres container

CAP trade-off (required comment)
--------------------------------
The Luchtmeetnet sensor network behaves like an AP system: when a station
loses its connection to the central database (a network partition), the API
stays *available* and keeps answering, but it cannot be *consistent* with
what the sensor actually measured. In practice the reading for that hour is
missing, null, or arrives late and is filled in afterwards. The API never
returns an error for this - it returns HTTP 200 with whatever it has. Even
asking for a component the station does not measure (e.g. formula=SO2)
returns HTTP 200 with an empty "data" list.

How a production pipeline should handle missing or null values:
  * Never invent a value. A null stays NULL in the database: dropping it
    hides the gap, and replacing it with 0 or the previous value is fake data
    that the model would learn from.
  * Keep the row, so the gap is visible and countable (data-quality
    monitoring, /health endpoint on Day 5).
  * Re-fetch a window of recent hours (the last 50) on every run, so an hour
    that was missing entirely - e.g. because our job or the API was down - is
    inserted on the next run, and an hour first stored as NULL is filled in
    once the source publishes the late value (see INSERT_SQL below).
  * Treat an empty response as a failure to alert on, not as "no data".
"""

from __future__ import annotations

import logging
import os
import sys
import time

import pandas as pd
import requests

STATION = "NL10240"
FORMULA = "NO2"
API_URL = "https://api.luchtmeetnet.nl/open_api/stations/{station}/measurements"

# Fair-use limit is 100 requests / 5 minutes, so retries are few and spaced out.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 5
TIMEOUT_SECONDS = 10

log = logging.getLogger("ingest_air")


class AirApiError(RuntimeError):
    """The Luchtmeetnet API could not be reached or returned nothing usable."""


# --------------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------------- #
def fetch_measurements(station: str = STATION, formula: str = FORMULA, page: int = 1) -> list[dict]:
    """Return one page (max 50 records) of measurements, newest first.

    Retries timeouts, connection errors and 5xx responses with a growing
    back-off. Raises AirApiError if every attempt fails.
    """
    params = {
        "formula": formula,
        "order_by": "timestamp_measured",
        "order_direction": "desc",
        "page": page,
    }
    url = API_URL.format(station=station)
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
            if response.status_code >= 500:
                raise requests.HTTPError(f"server error {response.status_code}")
            response.raise_for_status()  # 4xx = our request is wrong: no point retrying
            return response.json().get("data", [])
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            last_error = exc
            is_client_error = (
                isinstance(exc, requests.HTTPError)
                and exc.response is not None
                and 400 <= exc.response.status_code < 500
            )
            if is_client_error or attempt == MAX_ATTEMPTS:
                break
            wait = BACKOFF_SECONDS * attempt
            log.warning("Attempt %d/%d failed (%s); retrying in %ds", attempt, MAX_ATTEMPTS, exc, wait)
            time.sleep(wait)

    raise AirApiError(f"Luchtmeetnet API unreachable for {station}/{formula}: {last_error}")


# --------------------------------------------------------------------------- #
# Transform
# --------------------------------------------------------------------------- #
def to_dataframe(records: list[dict], station: str = STATION) -> pd.DataFrame:
    """Map API records onto the sensor_readings schema.

    API field             -> column
    formula               -> component
    timestamp_measured    -> timestamp  (end of the measured hour, UTC)
    value                 -> value      (may be None)
    """
    df = pd.DataFrame(records, columns=["formula", "timestamp_measured", "value"])
    df = df.rename(columns={"formula": "component", "timestamp_measured": "timestamp"})
    df.insert(0, "station_id", station)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")  # junk -> NaN, never a fake number
    return df


def filter_no2_readings(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only NO2 rows. Rows with a null value are KEPT, not dropped:
    a missing measurement is information (see the CAP comment above)."""
    return df[df["component"] == "NO2"].reset_index(drop=True)


def latest_reading(df: pd.DataFrame) -> pd.Series:
    """The newest row by timestamp (which may have a null value)."""
    return df.sort_values("timestamp", ascending=False).iloc[0]


# --------------------------------------------------------------------------- #
# Load (Lab 2)
# --------------------------------------------------------------------------- #
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sensor_readings (
    station_id  VARCHAR(20)  NOT NULL,
    timestamp   TIMESTAMPTZ  NOT NULL,
    component   VARCHAR(10)  NOT NULL,
    value       FLOAT,
    PRIMARY KEY (station_id, timestamp, component)
);
"""

# Idempotent write: the primary key makes a duplicate row impossible, so we can
# use at-least-once delivery (re-fetch the last 50 hours every run) safely.
#
# Deliberate deviation from the lab's ON CONFLICT DO NOTHING (see ADR-0001):
# DO NOTHING would keep a NULL forever, even after Luchtmeetnet publishes the
# late value. Here a conflicting row is updated ONLY when the stored value is
# NULL and the new one is not. Every other conflict is still a no-op, so running
# the script twice changes nothing - it stays idempotent - and a real
# measurement is never overwritten.
#
# RETURNING (xmax = 0) is a PostgreSQL detail: true for a freshly inserted row,
# false for an updated one. Skipped rows return nothing.
INSERT_SQL = """
INSERT INTO sensor_readings AS s (station_id, timestamp, component, value)
VALUES %s
ON CONFLICT (station_id, timestamp, component) DO UPDATE
    SET value = EXCLUDED.value
    WHERE s.value IS NULL AND EXCLUDED.value IS NOT NULL
RETURNING (xmax = 0) AS inserted;
"""


def write_to_db(df: pd.DataFrame, database_url: str) -> tuple[int, int]:
    """Upsert rows; return (rows inserted, NULL rows filled in)."""
    import psycopg2  # imported here so Lab 1 runs without a database driver
    from psycopg2.extras import execute_values

    rows = [
        (r.station_id, r.timestamp.to_pydatetime(), r.component, None if pd.isna(r.value) else float(r.value))
        for r in df.itertuples(index=False)
    ]
    with psycopg2.connect(database_url, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(CREATE_TABLE_SQL)
        result = execute_values(cur, INSERT_SQL, rows, fetch=True)
    inserted = sum(1 for (is_new,) in result if is_new)
    return inserted, len(result) - inserted


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        records = fetch_measurements()
    except AirApiError as exc:
        # The API being briefly down is expected. Log it and exit non-zero so the
        # scheduler/monitoring sees the failure. Nothing is lost: the next run
        # re-fetches the last 50 hours and fills the gap.
        log.error("%s", exc)
        return 1

    df = filter_no2_readings(to_dataframe(records))
    if df.empty:
        # HTTP 200 with no data: station offline, renamed, or wrong formula.
        log.error("API returned no NO2 readings for %s - treating as a failure", STATION)
        return 1

    latest = latest_reading(df)
    value = "NULL (missing)" if pd.isna(latest["value"]) else f"{latest['value']:.2f} µg/m³"
    print(f"Station:   {STATION}")
    print(f"Component: {latest['component']}")
    print(f"Value:     {value}")
    print(f"Timestamp: {latest['timestamp'].isoformat()}")
    print(f"Rows:      {len(df)} NO2 readings fetched ({int(df['value'].isna().sum())} null)")

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        try:
            inserted, filled = write_to_db(df, database_url)
        except Exception as exc:  # noqa: BLE001 - report any DB failure the same way
            log.error("Database write failed: %s", exc)
            return 1
        unchanged = len(df) - inserted - filled
        print(f"Database:  {inserted} inserted, {filled} NULLs filled in, {unchanged} unchanged")
    else:
        print("Database:  skipped (DATABASE_URL not set)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
