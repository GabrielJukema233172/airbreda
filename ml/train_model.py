"""
AirBreda - Day 4: train the NO2 regression on ml/training_data.csv.

    docker run --rm -v "${PWD}:/work" -w /work python:3.12-slim sh -c \
      "pip install -q -r ml/requirements.txt && python ml/train_model.py"

Writes
  services/dashboard/model.pkl   the LinearRegression the dashboard serves (baked into its image)
  ml/metrics.json                 what we know about it (and what we don't)
  ml/no2_vs_traffic.png           NO2 against total intensity

Evaluation with very few rows: a held-out test set of 3-4 rows says nothing,
so we report in-sample fit AND leave-one-out cross-validation (every row is
predicted by a model that never saw it), next to a baseline that always
predicts the mean. A model that can't beat that baseline has learned nothing.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.dummy import DummyRegressor
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import LeaveOneOut, cross_val_predict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "services" / "dashboard"))
from common.features import FEATURES  # noqa: E402
from predict import EXCEEDANCE_THRESHOLD_UG_M3, exceedance_risk  # noqa: E402

DATA = ROOT / "ml" / "training_data.csv"
MODEL = ROOT / "services" / "dashboard" / "model.pkl"
METRICS = ROOT / "ml" / "metrics.json"
PLOT = ROOT / "ml" / "no2_vs_traffic.png"


def loo_mae(estimator, X, y) -> float:
    pred = cross_val_predict(estimator, X, y, cv=LeaveOneOut())
    return float(mean_absolute_error(y, pred))


def plot(df: pd.DataFrame, model: LinearRegression) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ink, muted, grid, blue = "#0b0b0b", "#52514e", "#e4e3df", "#2a78d6"
    fig, ax = plt.subplots(figsize=(7, 4.2), dpi=150)
    fig.patch.set_facecolor("#fcfcfb")
    ax.set_facecolor("#fcfcfb")
    ax.scatter(df["total_intensity_veh_per_hr"], df["no2_ug_m3"], s=36, color=blue,
               edgecolor="#fcfcfb", linewidth=1.5, zorder=3)
    # Selective labels: busy hours individually; the low-traffic cluster gets one label.
    quiet = df[df["total_intensity_veh_per_hr"] < 1000]
    for _, r in df.drop(quiet.index).iterrows():
        ax.annotate(f"{int(r.hour_of_day):02d}h", (r.total_intensity_veh_per_hr, r.no2_ug_m3),
                    xytext=(5, 3), textcoords="offset points", fontsize=7, color=muted)
    if len(quiet):
        hours = ", ".join(f"{h:02d}" for h in sorted(quiet["hour_of_day"].astype(int)))
        ax.annotate(f"quiet hours ({hours}h)", (quiet["total_intensity_veh_per_hr"].max(), quiet["no2_ug_m3"].min()),
                    xytext=(12, -6), textcoords="offset points", fontsize=7, color=muted)
    # Fitted line at the median hour, to show the traffic coefficient's direction.
    xs = np.linspace(0, df["total_intensity_veh_per_hr"].max() * 1.05, 50)
    line = model.predict(pd.DataFrame({"total_intensity_veh_per_hr": xs,
                                       "hour_of_day": np.median(df["hour_of_day"])}))
    ax.plot(xs, line, color=ink, linewidth=1.5, linestyle="--", zorder=2,
            label=f"linear fit at hour {int(np.median(df['hour_of_day']))}")
    ax.set_xlabel("Total intensity, 4 A27 sites (vehicles/hour)", color=muted, fontsize=9)
    ax.set_ylabel("NO₂ at NL10240 (µg/m³)", color=muted, fontsize=9)
    ax.set_title(f"NO₂ vs traffic, {len(df)} joined hours "
                 f"({df['timestamp'].min():%d %b %H:%M} – {df['timestamp'].max():%d %b %H:%M} UTC)\n"
                 "points labelled with the local hour of the traffic snapshot",
                 color=ink, fontsize=10, loc="left")
    ax.grid(color=grid, linewidth=0.8, zorder=0)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(grid)
    ax.tick_params(colors=muted, labelsize=8)
    ax.legend(frameon=False, fontsize=8, labelcolor=muted, loc="lower right")
    fig.tight_layout()
    fig.savefig(PLOT)


def main() -> int:
    df = pd.read_csv(DATA, parse_dates=["timestamp"])
    X, y = df[FEATURES], df["no2_ug_m3"]
    n = len(df)
    if n < 5:
        print(f"only {n} rows - not enough to fit and cross-validate", file=sys.stderr)
        return 1

    model = LinearRegression().fit(X, y)
    fitted = model.predict(X)
    traffic_only = LinearRegression().fit(X[["total_intensity_veh_per_hr"]], y)

    metrics = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": n,
        "data_from": df["timestamp"].min().isoformat(),
        "data_to": df["timestamp"].max().isoformat(),
        "features": FEATURES,
        "scikit_learn": sklearn.__version__,
        "coefficients": dict(zip(FEATURES, map(float, model.coef_))),
        "intercept": float(model.intercept_),
        "in_sample": {"r2": float(r2_score(y, fitted)), "mae": float(mean_absolute_error(y, fitted))},
        "leave_one_out": {
            "mae_linear": loo_mae(LinearRegression(), X, y),
            "mae_traffic_only": loo_mae(LinearRegression(), X[["total_intensity_veh_per_hr"]], y),
            "mae_baseline_mean": loo_mae(DummyRegressor(strategy="mean"), X, y),
        },
        "traffic_only_model": {"coefficient": float(traffic_only.coef_[0]),
                               "r2_in_sample": float(traffic_only.score(X[["total_intensity_veh_per_hr"]], y))},
        "no2_range": [float(y.min()), float(y.max())],
        "exceedance_threshold_ug_m3": EXCEEDANCE_THRESHOLD_UG_M3,
    }

    # Stretch goal: a classifier trained directly on "did this hour exceed the threshold?"
    label = (y > EXCEEDANCE_THRESHOLD_UG_M3).astype(int)
    if label.nunique() < 2:
        metrics["logistic_regression"] = {
            "trained": False,
            "reason": f"{int(label.sum())} of {n} hours exceed {EXCEEDANCE_THRESHOLD_UG_M3} µg/m³: "
                      "a classifier needs examples of both classes",
        }
    else:
        clf = LogisticRegression(max_iter=1000).fit(X, label)
        metrics["logistic_regression"] = {"trained": True, "positives": int(label.sum())}
        comparison = df.assign(sigmoid_risk=[exceedance_risk(p) for p in fitted],
                               logistic_risk=clf.predict_proba(X)[:, 1])
        metrics["logistic_regression"]["comparison"] = comparison[
            ["timestamp", "no2_ug_m3", "sigmoid_risk", "logistic_risk"]].head(5).astype(str).to_dict("records")

    MODEL.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL)
    METRICS.write_text(json.dumps(metrics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    plot(df, model)

    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    print("\nrow-by-row (in-sample):")
    print(df.assign(predicted=fitted.round(2), risk=[round(exceedance_risk(p), 3) for p in fitted])
            [["timestamp", "no2_ug_m3", "total_intensity_veh_per_hr", "hour_of_day", "predicted", "risk"]]
            .to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
