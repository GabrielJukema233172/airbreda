"""Day 4: the shared feature code used by training AND serving."""

from datetime import datetime, timezone

import pandas as pd

from build_training_data import build
from common.features import hour_of_day, no2_hour_for, parse_ndw_csv, site_intensity

CSV = """site_label,site_id,measurement_time,index,measurement_type,vehicle_class,raw_value,value,is_valid
hrl,RWS01_MONIBAS_0271hrl0063ra,2026-11-26T03:09:00Z,1,flow,anyVehicle,60,60.0,True
hrl,RWS01_MONIBAS_0271hrl0063ra,2026-11-26T03:09:00Z,2,speed,anyVehicle,-1.0,,False
hrl,RWS01_MONIBAS_0271hrl0063ra,2026-11-26T03:09:00Z,3,flow,anyVehicle,120,120.0,True
"""


def test_site_intensity_sums_flow_lanes_and_ignores_invalid_speed():
    # speed = -1 (no car passed) does not make the FLOW unknown
    assert site_intensity(parse_ndw_csv(CSV)) == 180.0


def test_site_intensity_is_unknown_if_a_flow_lane_is_invalid():
    bad = CSV.replace("flow,anyVehicle,120,120.0,True", "flow,anyVehicle,-1,,False")
    assert site_intensity(parse_ndw_csv(bad)) is None


def test_traffic_snapshot_maps_to_the_no2_hour_that_contains_it():
    # Luchtmeetnet stamps the END of the hour: 12:09 and 12:58 both belong to 13:00
    assert no2_hour_for(datetime(2026, 11, 26, 12, 9, tzinfo=timezone.utc)) == datetime(2026, 11, 26, 13, tzinfo=timezone.utc)
    assert no2_hour_for(datetime(2026, 11, 26, 12, 58, tzinfo=timezone.utc)) == datetime(2026, 11, 26, 13, tzinfo=timezone.utc)


def test_hour_of_day_is_local_time_including_dst():
    assert hour_of_day(datetime(2026, 11, 26, 7, 9, tzinfo=timezone.utc)) == 8   # CET, UTC+1
    assert hour_of_day(datetime(2026, 7, 1, 7, 9, tzinfo=timezone.utc)) == 9     # CEST, UTC+2


def test_build_joins_on_hour_and_drops_incomplete_hours():
    t = lambda h, m=9: datetime(2026, 11, 26, h, m, tzinfo=timezone.utc)  # noqa: E731
    no2 = pd.DataFrame({"timestamp": pd.to_datetime([t(8, 0), t(9, 0), t(10, 0)], utc=True),
                        "no2_ug_m3": [18.0, 19.0, 20.0]})
    rows = []
    for hour, intensities in [(7, [100, 200, 300, 400]), (8, [100, None, 300, 400]), (9, [1, 2, 3, 4])]:
        for site, v in zip(["hrl", "hrr", "vwd", "vwa"], intensities):
            rows.append({"timestamp": no2_hour_for(t(hour)), "site": site, "intensity": v,
                         "traffic_measurement_time": t(hour), "hour_of_day": hour_of_day(t(hour))})
    df, stats = build(no2, pd.DataFrame(rows))
    assert list(df["no2_ug_m3"]) == [18.0, 20.0]             # the 08:09 snapshot (-> 09:00) had a bad site
    assert list(df["total_intensity_veh_per_hr"]) == [1000.0, 10.0]
    assert list(df["hour_of_day"]) == [8, 10]
    assert stats["traffic_hours_incomplete"] == 1
