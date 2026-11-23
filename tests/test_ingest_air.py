import pandas as pd
import pytest
import requests

import ingest_air
from ingest_air import filter_no2_readings, latest_reading, to_dataframe


def test_filter_no2_readings_handles_null_value():
    # Test given in the Day 1 lab
    df = pd.DataFrame({
        "component": ["NO2", "NO2", "PM10"],
        "value": [18.4, None, 22.1],
        "timestamp": ["2024-01-15T08:00:00Z", "2024-01-15T09:00:00Z", "2024-01-15T08:00:00Z"],
    })
    result = filter_no2_readings(df)
    assert len(result) == 2
    assert result["value"].isnull().sum() == 1  # null NO2 rows are kept, not silently dropped


def test_to_dataframe_maps_api_fields_to_table_columns():
    records = [
        {"formula": "NO2", "timestamp_measured": "2026-11-23T13:00:00+00:00", "value": 22.61},
        {"formula": "NO2", "timestamp_measured": "2026-11-23T12:00:00+00:00", "value": None},
    ]
    df = to_dataframe(records)
    assert list(df.columns) == ["station_id", "component", "timestamp", "value"]
    assert (df["station_id"] == "NL10240").all()
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df["value"].isna().sum() == 1


def test_latest_reading_is_newest_timestamp():
    df = to_dataframe([
        {"formula": "NO2", "timestamp_measured": "2026-11-23T11:00:00+00:00", "value": 30.0},
        {"formula": "NO2", "timestamp_measured": "2026-11-23T13:00:00+00:00", "value": 22.6},
        {"formula": "NO2", "timestamp_measured": "2026-11-23T12:00:00+00:00", "value": 27.5},
    ])
    assert latest_reading(df)["value"] == 22.6


def test_empty_api_response_gives_empty_frame():
    # The API answers HTTP 200 + empty list for a formula the station doesn't measure
    assert filter_no2_readings(to_dataframe([])).empty


def test_fetch_retries_then_raises_when_api_unreachable(monkeypatch):
    calls = []

    def always_timeout(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout("simulated timeout")

    monkeypatch.setattr(ingest_air.requests, "get", always_timeout)
    monkeypatch.setattr(ingest_air.time, "sleep", lambda s: None)  # don't actually wait

    with pytest.raises(ingest_air.AirApiError):
        ingest_air.fetch_measurements()
    assert len(calls) == ingest_air.MAX_ATTEMPTS


def test_main_returns_error_code_when_api_down(monkeypatch):
    def boom(*a, **k):
        raise ingest_air.AirApiError("down")

    monkeypatch.setattr(ingest_air, "fetch_measurements", boom)
    assert ingest_air.main() == 1
