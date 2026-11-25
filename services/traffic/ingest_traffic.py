"""
AirBreda - traffic ingestion service.

Day 1: download NDW's live site-configuration and measured-values feeds,
extract the four A27 sites at the Breda interchange (hectometer 63), save one
CSV per site under

    ndw/YYYY-MM-DD/HH-<site>.csv

and upload each file to S3 under the same key. YYYY-MM-DD and HH come from
the *measurement time in the data* (UTC), not from the machine's clock.

Day 2: run continuously; write per-site totals (flow, speed) to
`sensor_readings`, skipping NDW's -1 sentinel values; publish one message per
site to the Redis list `readings`; log structured JSON; serve GET /health.

XML parsing reuses download_and_decompress, build_index_map and
extract_measurements from the course's getTrafficReadings.py (copied here
as ndw_parser.py so the Docker image is self-contained).

Environment: S3_BUCKET, DATABASE_URL, REDIS_HOST, POLL_INTERVAL_SECONDS
(default 3600), HEALTH_PORT (default 8000), OUTPUT_DIR, RUN_ONCE.

Why both a database AND a bucket? (required comment)
----------------------------------------------------
* The relational database (PostgreSQL) holds parsed, cleaned rows with a
  primary key and an index on (station, timestamp). It answers questions
  like "NO2 and traffic at NL10240 for the last 24 hours" in milliseconds,
  and it can JOIN air quality with traffic. That is what the API and the
  dashboard read from. It is the *current interpretation* of the data.
* The object-storage bucket (S3) holds the files exactly as we parsed them
  at ingestion time, cheaply (~$0.023/GB-month), durably (11 nines) and
  append-only. It cannot be queried quickly or joined, but it is the
  *audit trail*: what the source said, when.
* Retraining the model six months from now: by then we will have changed
  the cleaning rules (e.g. how -1 values are handled), maybe the schema,
  and maybe we want a feature we never put in the database (per-lane flow
  instead of the total). With only the database we could only retrain on
  data shaped by today's assumptions - and if a bug corrupted rows, the
  originals would be gone. With the bucket we re-run the new parsing code
  over six months of files and get a fresh, reproducible training set. The
  database is fast but derived; the bucket is slow but the source of truth.
Polling interval (required comment): every hour
------------------------------------------------
NDW refreshes every minute, but we predict *hourly* NO2, so one traffic
snapshot per hour matches the resolution the model works at. One run
downloads two gzipped feeds and parses ~230 MB of XML, which takes 1-3
minutes.

What would break if we polled every minute instead?
  * A run takes longer than a minute, so runs would go back-to-back forever:
    100% CPU on a small burstable VM (t3.small), which then runs out of CPU
    credits and gets throttled - slowing down Postgres, Redis and the API on
    the same machine too.
  * Our S3 key is ndw/YYYY-MM-DD/HH-<site>.csv, so 59 of every 60 uploads
    would overwrite the previous one: lots of work, nothing extra kept.
  * The database would get 60x the rows (11,520/day instead of 192) at a
    resolution the hourly model never uses.
If we wanted traffic that better represents the whole hour (see the Day 1
reflection: one minute is not the hourly average), the fix is polling every
5-15 minutes and averaging per hour - not polling every minute.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from common.db import upsert_readings
from common.observability import ServiceState, log_event, run_forever, setup_logging, start_health_server
from ndw_parser import (
    CONFIG_URL,
    MEASURED_URL,
    build_index_map,
    download_and_decompress,
    extract_measurements,
)

SOURCE = "NDW"
log = logging.getLogger("ingest_traffic")

SITES = {
    "hrl": "RWS01_MONIBAS_0271hrl0063ra",  # A27 mainline, direction 1
    "hrr": "RWS01_MONIBAS_0271hrr0063ra",  # A27 mainline, direction 2
    "vwd": "RWS01_MONIBAS_0270vwd0063ra",  # entry slip road (traffic leaving Breda)
    "vwa": "RWS01_MONIBAS_0270vwa0063ra",  # exit slip road (traffic entering Breda)
}

CSV_FIELDS = [
    "site_label", "site_id", "measurement_time", "index",
    "measurement_type", "vehicle_class", "raw_value", "value", "is_valid",
]

MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 10

log = logging.getLogger("ingest_traffic")


# --------------------------------------------------------------------------- #
# Extract
# --------------------------------------------------------------------------- #
def download_with_retry(url: str) -> bytes:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return download_and_decompress(url).read()
        except Exception as exc:  # noqa: BLE001 - network, HTTP and gzip errors alike
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"NDW download failed after {attempt} attempts: {url}: {exc}") from exc
            log_event(log, logging.WARNING, "fetch_retry", source=SOURCE, url=url, attempt=attempt, error=str(exc))
            time.sleep(BACKOFF_SECONDS * attempt)


# --------------------------------------------------------------------------- #
# Transform
# --------------------------------------------------------------------------- #
def clean_value(raw: str | None) -> float | None:
    """NDW uses -1 (and occasionally empty/garbage) for 'no valid measurement',
    e.g. speed = -1 when no vehicle passed or the loop detector failed.
    Those become None: kept as a row (so the gap is visible), never used as a number."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if value < 0 else value


def parse_site(config_bytes: bytes, measured_bytes: bytes, label: str, site_id: str) -> list[dict]:
    """One row per measured value (lane x flow/speed) for a single site."""
    index_map = build_index_map(io.BytesIO(config_bytes), site_id)
    readings = extract_measurements(io.BytesIO(measured_bytes), site_id)

    rows = []
    for r in readings:
        info = index_map.get(r["index"], {})
        measurement_type = {"TrafficFlow": "flow", "TrafficSpeed": "speed"}.get(r.get("type"), r.get("type"))
        value = clean_value(r.get("value"))
        rows.append({
            "site_label": label,
            "site_id": site_id,
            "measurement_time": r.get("timestamp"),
            "index": int(r["index"]),
            "measurement_type": measurement_type,
            "vehicle_class": info.get("vehicle", "unknown"),
            "raw_value": r.get("value"),
            "value": value,
            "is_valid": value is not None,
        })
    return rows


def summarise(rows: list[dict]) -> dict:
    """Site totals from the all-vehicle lanes.

    * total_flow (veh/h): the sum over lanes, but ONLY if every flow lane is
      valid. With one lane at -1 the sum would silently under-count traffic,
      which is worse than no value.
    * avg_speed (km/h): the mean over lanes with a valid speed > 0.
    Either is None when it can't be computed; that metric is then not
    written to the database.
    """
    flow_rows = [r for r in rows if r["measurement_type"] == "flow" and r["vehicle_class"] == "anyVehicle"]
    speeds = [r["value"] for r in rows
              if r["measurement_type"] == "speed" and r["vehicle_class"] == "anyVehicle"
              and r["is_valid"] and r["value"] > 0]
    flows_ok = bool(flow_rows) and all(r["is_valid"] for r in flow_rows)
    return {
        "total_flow": sum(r["value"] for r in flow_rows) if flows_ok else None,
        "avg_speed": sum(speeds) / len(speeds) if speeds else None,
        "invalid_values": sum(1 for r in rows if not r["is_valid"]),
    }


def object_key(label: str, measurement_time: str) -> str:
    """ndw/YYYY-MM-DD/HH-<label>.csv from the measurement time (UTC)."""
    ts = datetime.fromisoformat(measurement_time.replace("Z", "+00:00"))
    return f"ndw/{ts:%Y-%m-%d}/{ts:%H}-{label}.csv"


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def upload_to_s3(path: Path, bucket: str, key: str) -> None:
    import boto3  # imported here so the script also runs without AWS

    boto3.client("s3").upload_file(str(path), bucket, key)


def to_db_rows(site_id: str, measurement_time: str, summary: dict) -> list[tuple]:
    """Per-site totals for sensor_readings. A metric that couldn't be computed
    from valid lanes is left out entirely: no sentinel values in the table."""
    ts = datetime.fromisoformat(measurement_time.replace("Z", "+00:00"))
    rows = []
    if summary["total_flow"] is not None:
        rows.append((site_id, ts, "flow", float(summary["total_flow"]), False))
    if summary["avg_speed"] is not None:
        rows.append((site_id, ts, "speed", round(float(summary["avg_speed"]), 2), False))
    return rows


# --------------------------------------------------------------------------- #
# One ingestion cycle
# --------------------------------------------------------------------------- #
class TrafficIngester:
    """download -> parse 4 sites -> CSV/S3 -> Postgres -> Redis, once per cycle.

    download(url) -> bytes, writer(rows) -> (inserted, filled), publisher and
    uploader(path, key) are injected so tests can use fakes.
    """

    def __init__(self, state: ServiceState, output_dir: Path, writer=None, publisher=None,
                 uploader=None, download=None, sites: dict[str, str] | None = None):
        self.state = state
        self.output_dir = output_dir
        self.writer = writer
        self.publisher = publisher
        self.uploader = uploader
        self.download = download or download_with_retry
        self.sites = sites or SITES
        self._reported_bad: set[tuple] = set()

    def run_cycle(self) -> bool:
        try:
            config_bytes = self.download(CONFIG_URL)
            measured_bytes = self.download(MEASURED_URL)
        except RuntimeError as exc:
            log_event(log, logging.ERROR, "fetch_failed", source=SOURCE, error=str(exc))
            return False

        db_rows, messages, found = [], [], 0
        for label, site_id in self.sites.items():
            rows = parse_site(config_bytes, measured_bytes, label, site_id)
            if not rows or not rows[0]["measurement_time"]:
                log_event(log, logging.WARNING, "site_missing", source=SOURCE, site=label, location=site_id)
                continue
            found += 1
            measurement_time = rows[0]["measurement_time"]
            summary = summarise(rows)
            log_event(log, logging.INFO, "fetch_success", source=SOURCE, site=label, location=site_id,
                      timestamp=measurement_time, total_flow=summary["total_flow"],
                      avg_speed=None if summary["avg_speed"] is None else round(summary["avg_speed"], 1),
                      invalid_values=summary["invalid_values"])
            self._report_bad_data(label, site_id, rows)
            self._save_raw(label, measurement_time, rows)
            db_rows += to_db_rows(site_id, measurement_time, summary)
            messages.append({
                "source": SOURCE, "station_id": site_id, "site": label, "timestamp": measurement_time,
                "total_flow": summary["total_flow"],
                "avg_speed": None if summary["avg_speed"] is None else round(summary["avg_speed"], 2),
                "invalid_values": summary["invalid_values"],
            })

        if found == 0:
            log_event(log, logging.ERROR, "fetch_empty", source=SOURCE, sites=len(self.sites))
            return False

        self.state.record_success()
        self._write(db_rows)
        self._publish(messages)
        return True

    def _report_bad_data(self, label: str, site_id: str, rows: list[dict]) -> None:
        """WARNING per invalid lane value (e.g. speed = -1). The row stays in
        the raw CSV (audit trail) but never reaches sensor_readings."""
        new_bad = 0
        for r in rows:
            if r["is_valid"]:
                continue
            key = (site_id, r["measurement_time"], r["index"])
            if key in self._reported_bad:  # same feed minute seen twice: don't double count
                continue
            self._reported_bad.add(key)
            new_bad += 1
            try:
                raw = float(r["raw_value"])
            except (TypeError, ValueError):
                raw = r["raw_value"]
            log_event(log, logging.WARNING, "DATA_QUALITY_ERROR", source=SOURCE, location=site_id, site=label,
                      field=r["measurement_type"], value=raw, lane_index=r["index"], timestamp=r["measurement_time"])
        if new_bad:
            self.state.record_bad_data(new_bad)
        if len(self._reported_bad) > 5000:  # bounded memory
            self._reported_bad = set(list(self._reported_bad)[-1000:])

    def _save_raw(self, label: str, measurement_time: str, rows: list[dict]) -> None:
        key = object_key(label, measurement_time)
        path = self.output_dir / key
        write_csv(rows, path)
        if self.uploader is None:
            return
        try:
            self.uploader(path, key)
            log_event(log, logging.INFO, "s3_upload", source=SOURCE, site=label, key=key)
        except Exception as exc:  # noqa: BLE001
            log_event(log, logging.ERROR, "s3_upload_failed", source=SOURCE, site=label, key=key, error=str(exc))

    def _write(self, db_rows: list[tuple]) -> None:
        if self.writer is None:
            return
        try:
            inserted, filled = self.writer(db_rows)
        except Exception as exc:  # noqa: BLE001
            log_event(log, logging.ERROR, "db_write_failed", source=SOURCE, error=str(exc))
            return
        log_event(log, logging.INFO, "db_write", source=SOURCE, inserted=inserted, filled=filled,
                  unchanged=len(db_rows) - inserted - filled)

    def _publish(self, messages: list[dict]) -> None:
        if self.publisher is None:
            return
        try:
            self.publisher.publish(messages)
        except Exception as exc:  # noqa: BLE001 - redis.RedisError and connection errors
            log_event(log, logging.WARNING, "queue_publish_failed", source=SOURCE, error=str(exc), dropped=len(messages))
            return
        log_event(log, logging.INFO, "queue_publish", source=SOURCE, queue="readings", messages=len(messages))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build_ingester(state: ServiceState) -> TrafficIngester:
    database_url = os.environ.get("DATABASE_URL")
    bucket = os.environ.get("S3_BUCKET")
    writer = (lambda rows: upsert_readings(rows, database_url)) if database_url else None
    uploader = (lambda path, key: upload_to_s3(path, bucket, key)) if bucket else None
    publisher = None
    if os.environ.get("REDIS_HOST"):
        from common.messaging import RedisPublisher
        publisher = RedisPublisher(os.environ["REDIS_HOST"], int(os.environ.get("REDIS_PORT", 6379)))
    log_event(log, logging.INFO, "service_started", source=SOURCE, sites=list(SITES),
              database=bool(writer), queue=bool(publisher), s3_bucket=bucket or None,
              poll_interval_seconds=state.poll_interval)
    return TrafficIngester(state, Path(os.environ.get("OUTPUT_DIR", "output")),
                           writer=writer, publisher=publisher, uploader=uploader)


def main() -> int:
    setup_logging("traffic-ingest")
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
