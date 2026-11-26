"""
AirBreda - Day 4: build the training dataset.

Joins two things the pipeline has been writing since Day 1/3:
  * NO2 per hour for NL10240         <- sensor_readings (PostgreSQL)
  * NDW intensity per site per hour  <- s3://$S3_BUCKET/ndw/YYYY-MM-DD/HH-<site>.csv

and writes ml/training_data.csv with one row per hour:

    timestamp, no2_ug_m3, intensity_hrl, intensity_hrr, intensity_vwd, intensity_vwa,
    total_intensity_veh_per_hr, hour_of_day, traffic_measurement_time

Environment: DATABASE_URL, S3_BUCKET, AWS_REGION. On the VM it runs as a one-off
container on the airbreda network (infra/vm/build-training-data.sh), using the
VM's IAM role for S3.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
from common.features import (  # noqa: E402
    SITES,
    hour_of_day,
    measurement_time,
    no2_hour_for,
    parse_ndw_csv,
    site_intensity,
)

STATION = "NL10240"
OUT = Path(__file__).with_name("training_data.csv")


def load_no2(database_url: str) -> pd.DataFrame:
    """Every usable NO2 hour. Flagged rows (null or stale, Day 2) are left out:
    a stuck sensor's repeated value would teach the model a fake relationship."""
    import psycopg2

    with psycopg2.connect(database_url, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT timestamp, value FROM sensor_readings
               WHERE station_id = %s AND component = 'NO2'
                 AND value IS NOT NULL AND NOT is_flagged
               ORDER BY timestamp""",
            (STATION,),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["timestamp", "no2_ug_m3"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def load_traffic(bucket: str) -> pd.DataFrame:
    """One row per (NO2 hour, site) from every CSV under ndw/."""
    import boto3

    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    records = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="ndw/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]                       # ndw/2026-11-26/12-hrl.csv
            label = key.rsplit("-", 1)[-1].removesuffix(".csv")
            if label not in SITES:
                continue
            rows = parse_ndw_csv(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
            t = measurement_time(rows)
            if t is None:
                continue
            records.append({
                "timestamp": no2_hour_for(t),
                "site": label,
                "intensity": site_intensity(rows),
                "traffic_measurement_time": t,
                "hour_of_day": hour_of_day(t),
            })
    return pd.DataFrame(records)


def build(no2: pd.DataFrame, traffic: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    traffic = traffic.copy()
    traffic["timestamp"] = pd.to_datetime(traffic["timestamp"], utc=True)
    wide = traffic.pivot_table(index="timestamp", columns="site", values="intensity", aggfunc="last")
    wide = wide.reindex(columns=list(SITES)).add_prefix("intensity_")
    meta = traffic.groupby("timestamp").agg(hour_of_day=("hour_of_day", "first"),
                                            traffic_measurement_time=("traffic_measurement_time", "max"))
    wide = wide.join(meta)

    complete = wide.dropna(subset=[f"intensity_{s}" for s in SITES])
    complete = complete.assign(total_intensity_veh_per_hr=complete[[f"intensity_{s}" for s in SITES]].sum(axis=1))

    df = no2.merge(complete.reset_index(), on="timestamp", how="inner").sort_values("timestamp")
    df = df[["timestamp", "no2_ug_m3", *[f"intensity_{s}" for s in SITES],
             "total_intensity_veh_per_hr", "hour_of_day", "traffic_measurement_time"]]
    stats = {
        "no2_hours": len(no2),
        "traffic_hours": len(wide),
        "traffic_hours_incomplete": len(wide) - len(complete),   # a site with a -1 flow lane
        "traffic_hours_without_no2_yet": int((~complete.index.isin(no2["timestamp"])).sum()),
        "joined_rows": len(df),
    }
    return df, stats


def main() -> int:
    no2 = load_no2(os.environ["DATABASE_URL"])
    traffic = load_traffic(os.environ["S3_BUCKET"])
    df, stats = build(no2, traffic)
    df.to_csv(OUT, index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    for k, v in stats.items():
        print(f"{k:32s} {v}")
    print(f"wrote {OUT} ({len(df)} rows)")
    return 0 if len(df) else 1


if __name__ == "__main__":
    sys.exit(main())
