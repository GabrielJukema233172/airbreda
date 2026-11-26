"""Day 4 Lab 2: /site/{id}, /health and the degrade-don't-fail rule. No database,
S3 or ingesters needed: the store functions are replaced with fakes."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import dashboard

SNAP = datetime(2026, 11, 26, 7, 9, tzinfo=timezone.utc)
TRAFFIC = {s: {"intensity": v, "measurement_time": SNAP, "key": f"ndw/2026-11-26/07-{s}.csv"}
           for s, v in {"hrl": 2460.0, "hrr": 2100.0, "vwd": 240.0, "vwa": 660.0}.items()}
NO2 = {"timestamp": datetime(2026, 11, 26, 7, tzinfo=timezone.utc), "value": 21.08, "is_flagged": False}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard, "latest_no2", lambda: NO2)
    monkeypatch.setattr(dashboard, "latest_traffic", lambda: TRAFFIC)
    return TestClient(dashboard.app)


def test_site_returns_real_values_and_prediction(client):
    r = client.get("/site/hrl")
    assert r.status_code == 200
    body = r.json()
    assert body["site_id"] == "hrl"
    assert body["no2_ug_m3"] == 21.08
    assert body["intensity_veh_per_hr"] == 2460.0
    assert body["total_intensity_veh_per_hr"] == 5460.0
    assert body["timestamp"] == "2026-11-26T07:09:00Z"
    assert body["prediction_for_hour"] == "2026-11-26T08:00:00Z"
    assert 0 <= body["no2_exceedance_risk"] <= 1
    assert body["errors"] == []


def test_prediction_is_the_same_for_every_site(client):
    preds = {client.get(f"/site/{s}").json()["no2_ug_m3_predicted"] for s in dashboard.SITES}
    assert len(preds) == 1


def test_unknown_site_is_404(client):
    assert client.get("/site/xyz").status_code == 404


def test_predict_failure_degrades_to_real_values(client, monkeypatch):
    def boom(*_):
        raise RuntimeError("model.pkl corrupt")
    monkeypatch.setattr(dashboard.predictor, "predict", boom)
    r = client.get("/site/vwa")
    assert r.status_code == 200
    body = r.json()
    assert body["no2_ug_m3"] == 21.08 and body["intensity_veh_per_hr"] == 660.0
    assert body["no2_ug_m3_predicted"] is None and body["no2_exceedance_risk"] is None
    assert any("prediction" in e for e in body["errors"])


def test_invalid_lane_means_no_total_and_no_prediction(client, monkeypatch):
    broken = {**TRAFFIC, "vwd": {**TRAFFIC["vwd"], "intensity": None}}
    monkeypatch.setattr(dashboard, "latest_traffic", lambda: broken)
    body = client.get("/site/hrl").json()
    assert body["total_intensity_veh_per_hr"] is None and body["no2_ug_m3_predicted"] is None
    assert body["intensity_veh_per_hr"] == 2460.0


def test_same_hour_different_minutes_still_gives_a_total(client, monkeypatch):
    # a re-run within the hour rewrote one site a few minutes later: still the same NO2 hour
    later = {**TRAFFIC, "hrl": {**TRAFFIC["hrl"], "measurement_time": SNAP.replace(minute=24)}}
    monkeypatch.setattr(dashboard, "latest_traffic", lambda: later)
    body = client.get("/site/vwd").json()
    assert body["total_intensity_veh_per_hr"] == 5460.0 and body["no2_ug_m3_predicted"] is not None


def test_sites_from_different_hours_give_no_total(client, monkeypatch):
    mixed = {**TRAFFIC, "hrl": {**TRAFFIC["hrl"], "measurement_time": SNAP.replace(hour=8)}}
    monkeypatch.setattr(dashboard, "latest_traffic", lambda: mixed)
    body = client.get("/site/vwd").json()
    assert body["total_intensity_veh_per_hr"] is None and body["no2_ug_m3_predicted"] is None


class FakeS3:
    """Hour 07 is complete; traffic-ingest is halfway through writing hour 08."""

    def __init__(self):
        self.files = {}
        for hour, sites, flow in [("07", ["hrl", "hrr", "vwd", "vwa"], 100), ("08", ["hrl", "hrr"], 999)]:
            for s in sites:
                self.files[f"ndw/2026-11-26/{hour}-{s}.csv"] = (
                    "site_label,site_id,measurement_time,index,measurement_type,vehicle_class,raw_value,value,is_valid\n"
                    f"{s},x,2026-11-26T{hour}:09:00Z,1,flow,anyVehicle,{flow},{flow}.0,True\n")

    def list_objects_v2(self, Bucket, Prefix):
        return {"Contents": [{"Key": k} for k in self.files if k.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.files[Key].encode())}


def test_fetch_uses_newest_complete_hour_while_an_upload_is_in_progress(monkeypatch):
    monkeypatch.setattr(dashboard, "_s3_client", lambda: FakeS3())
    traffic = dashboard._fetch_latest_traffic(now=datetime(2026, 11, 26, 8, 10, tzinfo=timezone.utc))
    assert {v["key"].split("/")[-1][:2] for v in traffic.values()} == {"07"}   # not a 07/08 mix
    total, _, reason = dashboard.total_intensity(traffic)
    assert total == 400.0 and reason is None


def test_both_stores_down_is_503(client, monkeypatch):
    def down():
        raise ConnectionError("down")
    monkeypatch.setattr(dashboard, "latest_no2", down)
    monkeypatch.setattr(dashboard, "latest_traffic", down)
    assert client.get("/site/hrl").status_code == 503


def test_health_aggregates_both_sources(client, monkeypatch):
    def fake(name, url):
        return {"source": name, "status": "ok", "last_successful_fetch": "2026-11-26T07:09:00+00:00",
                "bad_data_count": 3 if name == "NDW" else 0, "bad_data_count_last_hour": 0}
    monkeypatch.setattr(dashboard, "_source_health", fake)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["ndw_bad_data_count"] == 3 and body["luchtmeetnet_bad_data_count"] == 0


def test_health_is_503_when_a_source_is_stale(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_source_health",
                        lambda name, url: {"source": name, "status": "stale" if name == "NDW" else "ok"})
    assert client.get("/health").status_code == 503


def test_index_page_is_a_client_of_the_api(client):
    html = client.get("/").text
    assert "fetch(`/site/${id}`)" in html and "setInterval(refresh" in html
