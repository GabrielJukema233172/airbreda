"""Model features, shared by training (ml/build_training_data.py) and serving
(services/dashboard). One implementation for both is the main defence against
training-serving skew (ADR-0006): if the rule for "traffic intensity" ever
changes, it changes for the model and the dashboard at the same time.

Standard library only, so every image can import it.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SITES = {
    "hrl": "RWS01_MONIBAS_0271hrl0063ra",  # A27 mainline, direction 1
    "hrr": "RWS01_MONIBAS_0271hrr0063ra",  # A27 mainline, direction 2
    "vwd": "RWS01_MONIBAS_0270vwd0063ra",  # entry slip road (traffic leaving Breda)
    "vwa": "RWS01_MONIBAS_0270vwa0063ra",  # exit slip road (traffic entering Breda)
}
FEATURES = ["total_intensity_veh_per_hr", "hour_of_day"]
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")


def parse_ndw_csv(text: str) -> list[dict]:
    """Rows of one ndw/YYYY-MM-DD/HH-<site>.csv file (as written by ingest_traffic.py)."""
    return list(csv.DictReader(io.StringIO(text)))


def _is_valid(row: dict) -> bool:
    v = row.get("is_valid")
    return v is True or str(v).strip().lower() == "true"


def site_intensity(rows: list[dict]) -> float | None:
    """Vehicles per hour at one site: the sum of all `anyVehicle` flow lanes.

    Same rule as ingest_traffic.summarise(): if ANY flow lane is invalid
    (NDW -1), the total is unknown (None) rather than silently too low.
    """
    flows = [r for r in rows if r.get("measurement_type") == "flow" and r.get("vehicle_class") == "anyVehicle"]
    if not flows or not all(_is_valid(r) for r in flows):
        return None
    return float(sum(float(r["value"]) for r in flows))


def measurement_time(rows: list[dict]) -> datetime | None:
    if not rows or not rows[0].get("measurement_time"):
        return None
    return parse_utc(rows[0]["measurement_time"])


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def no2_hour_for(traffic_time: datetime) -> datetime:
    """The Luchtmeetnet timestamp whose hourly NO2 average contains this traffic snapshot.

    Luchtmeetnet labels an hourly average with the END of the hour (ADR-0001),
    so a snapshot at 12:09 belongs to the value stamped 13:00. Rounding to the
    *nearest* hour (the lab's suggestion) would pair 12:09 with 12:00, i.e. with
    the hour BEFORE the traffic was measured, depending on the minute the poll
    happened to run.
    """
    t = parse_utc(traffic_time).astimezone(timezone.utc)
    return t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def hour_of_day(traffic_time: datetime) -> int:
    """Local (Europe/Amsterdam) hour 0-23 of the traffic snapshot: rush hours
    follow the local clock, including daylight-saving time, not UTC."""
    return parse_utc(traffic_time).astimezone(LOCAL_TZ).hour
