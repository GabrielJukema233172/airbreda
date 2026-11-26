"""
AirBreda - Day 4: turn traffic into a predicted NO2 value and an exceedance risk.

    from predict import predict
    predict(total_intensity_veh_per_hr=3600, hour_of_day=8)
    # {"no2_ug_m3_predicted": ~17, "no2_exceedance_risk": ~0.16}   (exact values: see ml/metrics.json)

The model (model.pkl, a scikit-learn LinearRegression trained by ml/train_model.py)
is baked into the dashboard image next to this file, so the image always serves
exactly the model it was built with. No separate model server (ADR-0006).

Exceedance threshold (required reasoning)
-----------------------------------------
25 µg/m³ = the WHO 2021 air-quality guideline for NO2 as a 24-hour mean. The
alternatives are all either too lax or not hourly:
  * EU hourly limit value: 200 µg/m³ (Directive 2008/50/EC and 2024/2881).
    NL10240 has not come near it in our data (max 64.5 µg/m³), so a risk score
    against it would be ~0 forever and tell nobody anything.
  * EU annual limit: 40 µg/m³ today, 20 µg/m³ from 2030; WHO annual guideline: 10 µg/m³.
    These are annual averages: a single hour above them is not an exceedance.
Applying a 24-hour guideline to an hourly prediction is a simplification, stated
plainly: the score means "this hour is running at a level that, sustained over a
day, would exceed the WHO guideline", not "a legal limit is being broken".
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from pathlib import Path

EXCEEDANCE_THRESHOLD_UG_M3 = 25.0
STEEPNESS = 0.2            # risk 0.12 at 15 µg/m³, 0.5 at 25, 0.88 at 35
FEATURES = ["total_intensity_veh_per_hr", "hour_of_day"]   # same order as training (common/features.py)
MODEL_PATH = Path(os.environ.get("MODEL_PATH", Path(__file__).with_name("model.pkl")))


@lru_cache(maxsize=1)
def load_model():
    import joblib
    return joblib.load(MODEL_PATH)


def exceedance_risk(predicted_no2: float, threshold: float = EXCEEDANCE_THRESHOLD_UG_M3,
                    steepness: float = STEEPNESS) -> float:
    """Sigmoid centred on the threshold: 0.5 exactly at it, -> 0 well below, -> 1 well above."""
    return float(1 / (1 + math.exp(-steepness * (predicted_no2 - threshold))))


def predict(total_intensity_veh_per_hr: float, hour_of_day: int) -> dict:
    """Predicted NO2 (µg/m³) for the hour this traffic was measured in, and its risk.

    total_intensity_veh_per_hr must be the SUM over all four sites, like the
    training data: feeding one site's intensity would be training-serving skew.
    A linear model can go below zero for very low traffic; NO2 can't, so the
    prediction is clipped at 0.
    """
    import pandas as pd

    X = pd.DataFrame([[float(total_intensity_veh_per_hr), int(hour_of_day)]], columns=FEATURES)
    no2 = max(0.0, float(load_model().predict(X)[0]))
    return {
        "no2_ug_m3_predicted": round(no2, 2),
        "no2_exceedance_risk": round(exceedance_risk(no2), 3),
    }
