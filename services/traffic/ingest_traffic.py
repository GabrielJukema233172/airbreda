"""
AirBreda - traffic ingestion (Day 1, Lab 2 Extend).

Downloads NDW's live site-configuration and measured-values feeds, extracts
the four A27 sites at the Breda interchange (hectometer 63), saves one CSV
per site under

    ndw/YYYY-MM-DD/HH-<site>.csv

and, when S3_BUCKET is set, uploads each file to the bucket under the same
key. YYYY-MM-DD and HH come from the *measurement time in the data* (UTC),
not from the clock of the machine running the script.

XML parsing reuses download_and_decompress, build_index_map and
extract_measurements from the course's getTrafficReadings.py (copied here
as ndw_parser.py so the Docker image is self-contained).

Why both a database AND a bucket? (required comment)
----------------------------------------------------
* The relational database (RDS Postgres) holds parsed, cleaned rows with a
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

from ndw_parser import (
    CONFIG_URL,
    MEASURED_URL,
    build_index_map,
    download_and_decompress,
    extract_measurements,
)

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
            log.warning("Download attempt %d failed (%s); retrying", attempt, exc)
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
    """Total flow (veh/h) and mean speed (km/h) over valid all-vehicle lanes."""
    flows = [r["value"] for r in rows
             if r["measurement_type"] == "flow" and r["vehicle_class"] == "anyVehicle" and r["is_valid"]]
    speeds = [r["value"] for r in rows
              if r["measurement_type"] == "speed" and r["vehicle_class"] == "anyVehicle"
              and r["is_valid"] and r["value"] > 0]
    return {
        "total_flow": sum(flows) if flows else None,
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


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    output_dir = Path(os.environ.get("OUTPUT_DIR", "output"))
    bucket = os.environ.get("S3_BUCKET")

    try:
        log.info("Downloading NDW site configuration...")
        config_bytes = download_with_retry(CONFIG_URL)
        log.info("Downloading NDW measured values...")
        measured_bytes = download_with_retry(MEASURED_URL)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    saved = 0
    for label, site_id in SITES.items():
        rows = parse_site(config_bytes, measured_bytes, label, site_id)
        if not rows or not rows[0]["measurement_time"]:
            log.warning("No data for %s (%s) in this feed", label, site_id)
            continue

        key = object_key(label, rows[0]["measurement_time"])
        path = output_dir / key
        write_csv(rows, path)
        saved += 1

        s = summarise(rows)
        flow = f"{s['total_flow']:.0f} veh/h" if s["total_flow"] is not None else "n/a"
        speed = f"{s['avg_speed']:.1f} km/h" if s["avg_speed"] is not None else "n/a"
        print(f"{label}: {flow}, {speed}, {s['invalid_values']} invalid values -> {key}")

        if bucket:
            try:
                upload_to_s3(path, bucket, key)
                print(f"     uploaded to s3://{bucket}/{key}")
            except Exception as exc:  # noqa: BLE001
                log.error("Upload of %s failed: %s", key, exc)
                return 1

    if saved == 0:
        log.error("None of the %d sites were found in the feed", len(SITES))
        return 1
    if not bucket:
        print("S3 upload skipped (S3_BUCKET not set)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
