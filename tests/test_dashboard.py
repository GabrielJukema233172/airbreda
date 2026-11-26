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
