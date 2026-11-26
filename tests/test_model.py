"""Day 4 Lab 1: the trained model loads and predict() returns sane values."""

import joblib
import pytest

import predict as predictor


def test_model_pkl_loads():
    model = joblib.load(predictor.MODEL_PATH)
    assert hasattr(model, "predict")
    assert len(model.coef_) == len(predictor.FEATURES)


@pytest.mark.parametrize("intensity, hour", [(0, 3), (1860, 20), (3600, 8), (5460, 8), (12000, 17)])
def test_predict_returns_plausible_no2_and_risk(intensity, hour):
    out = predictor.predict(intensity, hour)
    assert isinstance(out["no2_ug_m3_predicted"], float)
    assert 0 <= out["no2_ug_m3_predicted"] <= 200
    assert 0 <= out["no2_exceedance_risk"] <= 1


def test_more_traffic_means_more_no2_and_more_risk():
    low, high = predictor.predict(500, 8), predictor.predict(5000, 8)
    assert high["no2_ug_m3_predicted"] > low["no2_ug_m3_predicted"]
    assert high["no2_exceedance_risk"] > low["no2_exceedance_risk"]


def test_risk_sigmoid_is_centred_on_threshold():
    t = predictor.EXCEEDANCE_THRESHOLD_UG_M3
    assert predictor.exceedance_risk(t) == pytest.approx(0.5)
    assert predictor.exceedance_risk(t - 20) < 0.05
    assert predictor.exceedance_risk(t + 20) > 0.95
