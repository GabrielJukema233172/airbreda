"""
AirBreda - air-quality ingestion service.

Day 1: fetch recent NO2 readings for RIVM Luchtmeetnet station NL10240
(Breda-Tilburgseweg) and write them idempotently into `sensor_readings`.
Day 2: run continuously, publish readings to the Redis list `readings`, log
structured JSON, flag stale/null readings, and serve GET /health.

    docker compose up -d --build              # run as a service (polls hourly)
    docker compose run --rm -e RUN_ONCE=1 air-ingest   # a single cycle

Environment: DATABASE_URL, REDIS_HOST, POLL_INTERVAL_SECONDS (default 3600),
HEALTH_PORT (default 8000), RUN_ONCE.

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
    once the source publishes the late value (see UPSERT_SQL in common/db.py).
  * Treat an empty response as a failure to alert on, not as "no data".

Polling interval (required comment): every hour
------------------------------------------------
Luchtmeetnet publishes one NO2 value per hour, 30-90 minutes after the hour
ends, and every run re-reads the last 50 hours. Polling hourly therefore never
misses a reading; the cost is freshness: a value can wait up to an hour before
we pick it up.

What would break if we polled every minute instead?
  * 1,440 API calls a day instead of 24, against a free public API with a
    fair-use limit of 100 requests / 5 minutes - for 59 of 60 calls that
    return exactly the same data. We would be a bad citizen for no gain.
  * Without de-duplication, each poll would push all 50 readings onto the
    queue: 72,000 messages a day. Nothing consumes the queue yet, so it would
    hit its 10,000-message cap within ~3.5 hours and start discarding real
    readings. (We publish only new or changed readings, which prevents this,
    but a naive version breaks exactly here.)
  * 60x the database round trips and 60x the log volume, which is noise that
    makes real problems harder to spot and costs money once logs are shipped
    to CloudWatch.
If fresher data mattered (e.g. a public alert), 15 minutes would be the
sensible step - not one minute.
"""

from __future__ import annotations

import logging
import os
import sys
import time

import pandas as pd
import requests

from common.db import upsert_readings
from common.observability import ServiceState, log_event, run_forever, setup_logging, start_health_server

STATION = "NL10240"
FORMULA = "NO2"
SOURCE = "Luchtmeetnet"
API_URL = "https://api.luchtmeetnet.nl/open_api/stations/{station}/measurements"

# Fair-use limit is 100 requests / 5 minutes, so retries are few and spaced out.
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 5
TIMEOUT_SECONDS = 10

# A value repeated for this many consecutive hours is treated as stale:
# a stuck sensor, or the API carrying an old value forward.
STALE_RUN_LENGTH = 3
# How long we remember what we already published / reported (the fetch window is 50 h).
MEMORY_HOURS = 72

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
            log_event(log, logging.WARNING, "fetch_retry", source=SOURCE, attempt=attempt,
                      max_attempts=MAX_ATTEMPTS, error=str(exc), retry_in_seconds=wait)
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
# Data quality (Day 2)
# --------------------------------------------------------------------------- #
def flag_stale_or_null(df: pd.DataFrame, min_run: int = STALE_RUN_LENGTH) -> pd.DataFrame:
    """Add is_flagged and flag_reason ("null" / "stale" / None).

    * null: the value is missing.
    * stale: the same value for `min_run`+ consecutive hourly timestamps. The
      3rd, 4th, ... rows of such a run are flagged; the first two are
      indistinguishable from real readings when they arrive. A gap in the
      hours, or a null, breaks the run.

    Flagged rows are KEPT and written to the database: for air quality a gap
    in the time series is worse than a visibly flagged value.
    """
    df = df.sort_values("timestamp").reset_index(drop=True)
    flags, reasons = [], []
    run_length, prev_value, prev_ts = 0, None, None
    for ts, value in zip(df["timestamp"], df["value"]):
        if pd.isna(value):
            flags.append(True)
            reasons.append("null")
            run_length, prev_value = 0, None
        else:
            contiguous = prev_ts is not None and ts - prev_ts == pd.Timedelta(hours=1)
            run_length = run_length + 1 if (contiguous and value == prev_value) else 1
            stale = run_length >= min_run
            flags.append(stale)
            reasons.append("stale" if stale else None)
            prev_value = value
        prev_ts = ts
    df["is_flagged"] = flags
    df["flag_reason"] = reasons
    return df


def iso_utc(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def to_db_rows(df: pd.DataFrame) -> list[tuple]:
    return [
        (r.station_id, r.timestamp.to_pydatetime(), r.component,
         None if pd.isna(r.value) else float(r.value), bool(r.is_flagged))
        for r in df.itertuples(index=False)
    ]


def to_message(row) -> dict:
    return {
        "source": SOURCE,
        "station_id": row.station_id,
        "timestamp": iso_utc(row.timestamp),
        "component": row.component,
        "value": None if pd.isna(row.value) else float(row.value),
        "is_flagged": bool(row.is_flagged),
    }


# --------------------------------------------------------------------------- #
# One ingestion cycle
# --------------------------------------------------------------------------- #
class AirIngester:
    """fetch -> flag -> write to Postgres -> publish to Redis, once per cycle.

    writer(rows) -> (inserted, filled) and publisher.publish(messages) are
    injected so tests can replace the database and Redis with fakes.
    """

    def __init__(self, state: ServiceState, writer=None, publisher=None, fetch=None):
        self.state = state
        self.writer = writer
        self.publisher = publisher
        self.fetch = fetch or (lambda: fetch_measurements())
        self._published: dict[str, float | None] = {}   # timestamp -> value we published
        self._reported_bad: set[tuple[str, str]] = set()  # (timestamp, reason) already logged

    def run_cycle(self) -> bool:
        try:
            records = self.fetch()
        except AirApiError as exc:
            # The API being briefly down is expected. Nothing is lost: the next
            # cycle re-fetches the last 50 hours. /health turns "stale" if it lasts.
            log_event(log, logging.ERROR, "fetch_failed", source=SOURCE, station_id=STATION, error=str(exc))
            return False

        df = filter_no2_readings(to_dataframe(records))
        if df.empty:
            # HTTP 200 with no data: station offline, renamed, or wrong formula.
            log_event(log, logging.ERROR, "fetch_empty", source=SOURCE, station_id=STATION, formula=FORMULA)
            return False

        self.state.record_success()
        latest = latest_reading(df)
        log_event(log, logging.INFO, "fetch_success", source=SOURCE, station_id=STATION,
                  value=None if pd.isna(latest["value"]) else float(latest["value"]),
                  timestamp=iso_utc(latest["timestamp"]), rows=len(df))

        df = flag_stale_or_null(df)
        self._report_bad_data(df)
        self._write(df)
        self._publish(df)
        self._forget_old(df["timestamp"].max())
        return True

    def _report_bad_data(self, df: pd.DataFrame) -> None:
        """WARNING once per bad reading. The same 50-hour window is re-fetched
        every hour, so without remembering what we reported, one null would be
        counted 50 times and trip the threshold on its own."""
        new_bad = 0
        for row in df[df["is_flagged"]].itertuples(index=False):
            key = (iso_utc(row.timestamp), row.flag_reason)
            if key in self._reported_bad:
                continue
            self._reported_bad.add(key)
            new_bad += 1
            log_event(log, logging.WARNING, "DATA_QUALITY_ERROR", source=SOURCE, station_id=row.station_id,
                      field=row.component, reason="stale_or_null", detail=row.flag_reason,
                      value=None if pd.isna(row.value) else float(row.value), timestamp=key[0])
        if new_bad:
            self.state.record_bad_data(new_bad)

    def _write(self, df: pd.DataFrame) -> None:
        if self.writer is None:
            return
        try:
            inserted, filled = self.writer(to_db_rows(df))
        except Exception as exc:  # noqa: BLE001 - any DB failure is reported the same way
            log_event(log, logging.ERROR, "db_write_failed", source=SOURCE, error=str(exc))
            return
        log_event(log, logging.INFO, "db_write", source=SOURCE, inserted=inserted, filled=filled,
                  unchanged=len(df) - inserted - filled, flagged=int(df["is_flagged"].sum()))

    def _publish(self, df: pd.DataFrame) -> None:
        """Publish readings that are new, or whose value changed (a late value
        filling a null). Remembered only after a successful publish, so if
        Redis is down they are retried next cycle."""
        if self.publisher is None:
            return
        fresh = [r for r in df.itertuples(index=False)
                 if self._published.get(iso_utc(r.timestamp), "unseen") != (None if pd.isna(r.value) else float(r.value))]
        if not fresh:
            return
        try:
            self.publisher.publish([to_message(r) for r in fresh])
        except Exception as exc:  # noqa: BLE001 - redis.RedisError and connection errors
            log_event(log, logging.WARNING, "queue_publish_failed", source=SOURCE, error=str(exc), pending=len(fresh))
            return
        for r in fresh:
            self._published[iso_utc(r.timestamp)] = None if pd.isna(r.value) else float(r.value)
        log_event(log, logging.INFO, "queue_publish", source=SOURCE, queue="readings", messages=len(fresh))

    def _forget_old(self, newest: pd.Timestamp) -> None:
        cutoff = iso_utc(newest - pd.Timedelta(hours=MEMORY_HOURS))
        self._published = {k: v for k, v in self._published.items() if k >= cutoff}
        self._reported_bad = {k for k in self._reported_bad if k[0] >= cutoff}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_ingester(state: ServiceState) -> AirIngester:
    database_url = os.environ.get("DATABASE_URL")
    writer = (lambda rows: upsert_readings(rows, database_url)) if database_url else None
    publisher = None
    if os.environ.get("REDIS_HOST"):
        from common.messaging import RedisPublisher
        publisher = RedisPublisher(os.environ["REDIS_HOST"], int(os.environ.get("REDIS_PORT", 6379)))
    log_event(log, logging.INFO, "service_started", source=SOURCE, station_id=STATION,
              database=bool(writer), queue=bool(publisher), poll_interval_seconds=state.poll_interval)
    return AirIngester(state, writer=writer, publisher=publisher)


def main() -> int:
    setup_logging("air-ingest")
    interval = float(os.environ.get("POLL_INTERVAL_SECONDS", 3600))
    state = ServiceState(SOURCE, log, poll_interval=interval)
    ingester = build_ingester(state)

    if os.environ.get("RUN_ONCE"):
        return 0 if ingester.run_cycle() else 1

    start_health_server(state, int(os.environ.get("HEALTH_PORT", 8000)))
    run_forever(ingester.run_cycle, interval, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
