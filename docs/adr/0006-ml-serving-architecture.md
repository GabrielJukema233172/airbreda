# ADR 0006: ML Serving Architecture

- **Status:** Accepted
- **Date:** 2026-11-26
- **Course day:** Day 4

## Context

AirBreda's question is whether traffic at the A27 Breda interchange drives NO₂ at station NL10240. Day 4 adds a model that predicts NO₂ from traffic, and the dashboard (ADR-0005) that serves it.

**The training data is the pipeline's own output, and there is very little of it:**

| | |
|---|---|
| NO₂ hours in `sensor_readings` | 118 (since 21 Nov, because every run re-fetches 50 h) |
| Traffic hours in S3 | 19 (since the VM started on 25 Nov 19:43 UTC) |
| **Joined rows** (`ml/training_data.csv`) | **17**: 25 Nov 20:00 to 26 Nov 12:00 UTC, i.e. **one evening, one night and one morning** |
| NO₂ in those rows | 8.9–21.1 µg/m³ (the earlier, unjoinable days reached 64.5) |
| Traffic in those rows | 60–5,460 veh/h, total of the four sites |

`ml/build_training_data.py` builds the table: all NO₂ rows that aren't flagged, plus every NDW CSV in S3, joined per hour. NDW history can't be backfilled: the open feed only has the current minute.

**The join is the subtle part.** Luchtmeetnet stamps each hourly average with the **end** of the hour, so a traffic snapshot at 12:09 belongs to the NO₂ value stamped **13:00**. The lab's "round both to the nearest hour" would pair it with 12:00, the hour *before* the traffic was measured, and only for snapshots before :30. Our polls happen to run at :09, so rounding would have shifted every row by an hour. `common.features.no2_hour_for()` takes the floor and adds one hour.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| **A. Linear regression** on total intensity + hour of day | 3 parameters for 17 rows; coefficients you can read and check against physics; trains in milliseconds; LOO-CV is cheap | Can't model the curved daily cycle, wind or temperature inversions; extrapolates linearly beyond the data |
| B. Random forest / gradient boosting | Captures non-linearity and interactions | Dozens of splits learned from 17 points is memorising noise; can't extrapolate beyond the 5,460 veh/h it has seen; no coefficient to sanity-check |
| C. Neural network | Flexible | Hundreds of parameters, 17 rows: no |
| D. No model: "NO₂ = last published value" | Honest baseline, nothing to train | Ignores the traffic we're here to study; always an hour or more behind |

## Decision

**Option A: scikit-learn `LinearRegression` on `[total_intensity_veh_per_hr, hour_of_day]`, trained by `ml/train_model.py`, saved as `services/dashboard/model.pkl` and baked into the dashboard image. The exceedance risk is a sigmoid around 25 µg/m³ applied to the prediction.**

### Why linear regression is the right amount of model
With 17 rows, the question isn't which model fits best but which model we can *check*. A linear model has one number per feature, and that number can be judged against what we know about the physics:

| Result (26 Nov, 17 rows) | Value |
|---|---|
| Traffic coefficient | **+0.0019 µg/m³ per veh/h** (+1.9 per 1,000 veh/h): the expected sign |
| Hour-of-day coefficient | −0.08 per hour: about zero |
| Intercept | 10.5 µg/m³ (the night-time background) |
| In-sample R² / MAE | 0.83 / 1.21 µg/m³ |
| **Leave-one-out MAE** | **1.44 µg/m³** |
| Leave-one-out MAE, baseline "predict the mean" | 3.28 µg/m³ |
| Leave-one-out MAE, traffic as the only feature | 1.45 µg/m³ |

**What these numbers tell us:** within this one evening-night-morning, traffic explains most of the change in NO₂. The model predicts unseen hours of the *same period* about 2.3× better than guessing the mean. `hour_of_day` adds nothing: as a number running from 0 to 23, it says 23:00 and 00:00 are as far apart as possible.

**What they don't tell us:** whether this holds on any other day. All 17 rows come from one weather situation and one daily cycle, so traffic, time of day and the night-time inversion all move together. The +1.9 per 1,000 vehicles is partly a "morning vs night" effect we can't separate from traffic yet. We used leave-one-out CV and no hold-out test set, because a 3-row test set is noise. And the in-sample R² of 0.83 is optimistic by definition. There is no weekend, no rain, no wind, and no hour above 21 µg/m³ in the data. The model has never seen an exceedance.

**What would change the answer:**
- **Weeks, not hours:** roughly 500–1,000 joined hours (3–6 weeks) covering weekdays and weekends, several weather types, and actual high-NO₂ hours.
- **A time-ordered validation that the linear model fails:** for example, train on weeks 1–3, test on week 4, and the residuals show a pattern (curvature at high traffic, or systematic errors at certain hours or in certain winds).
- **Features that carry the missing physics:** wind speed and direction and the boundary layer from KNMI, plus hour as sin/cos.

Only when a gradient-boosted model beats the linear one on the *later* weeks would we switch.

### How `no2_exceedance_risk` is derived
**Threshold: 25 µg/m³**, the WHO 2021 guideline for NO₂ as a **24-hour** mean.
- The EU's hourly limit (200 µg/m³) is far above anything NL10240 measures (max 64.5), so a score against it would always be about 0.
- The annual limits (EU 40, or 20 from 2030; WHO 10) aren't hourly.

Applying a daily guideline to an hourly value is a stated simplification: the score means "this hour runs at a level that, sustained for a day, would exceed the WHO guideline".

**Method: a sigmoid on the regression output**, `1 / (1 + e^(-0.2 · (predicted − 25)))`. At 15 µg/m³ the risk is 0.12, at 25 it is 0.5, and at 35 it is 0.88. We chose it because:
- It works with the data we have: the stretch-goal **logistic regression could not be trained**, because **0 of the 17 hours exceed 25 µg/m³**. A classifier needs examples of both classes. (`train_model.py` checks this and records the reason in `ml/metrics.json`.)
- It is monotone in the prediction, so the risk always agrees with the predicted NO₂.
- **Its weakness:** it is *not a calibrated probability*. The steepness 0.2 is a choice, not something estimated. "23%" means "predicted 18.8, about 6 below the threshold", not "it exceeds 23 times out of 100".

The logistic classifier becomes the better option once the data holds a few dozen exceedance hours. Then its probabilities can be checked against how often exceedances actually happen.

### Training-serving skew in this design, and how it is prevented
Training-serving skew means the model sees inputs at serving time that are computed differently from the inputs it was trained on. In AirBreda it could appear in five ways:

1. **Different feature code.** Intensity is computed in two places, from S3 for training and from S3 again in the dashboard. Both call the *same* `common.features.site_intensity()`, the same rule `ingest_traffic.py` uses: the sum of the `anyVehicle` flow lanes, and *unknown* if any lane is −1.
2. **A different feature definition: the trap in the lab text.** The lab's `/site/{id}` reads "that site's" intensity and calls `predict()`. But the model was trained on the **sum of all four sites** (~4,000 veh/h at rush hour). One site's ~300–2,000 veh/h would always look like night-time to the model. The dashboard therefore always predicts from the four-site total of the *same* snapshot, so the prediction is identical for every site. `predict()` documents this and a test checks it.
3. **A different time alignment.** The hour of day is the *local* (Europe/Amsterdam) hour of the traffic snapshot, both in training and serving (`common.features.hour_of_day()`). The prediction is labelled with the hour it predicts (`prediction_for_hour`), using the same end-of-hour rule as the training join.
4. **Different library versions.** A pickled scikit-learn model is only guaranteed to load in the version that wrote it. `ml/requirements.txt` *includes* `services/dashboard/requirements.txt`, so training and serving pin the same scikit-learn 1.5.2, numpy 2.1.3 and pandas 2.2.3.
5. **A different model.** **Baking `model.pkl` into the image** means the model that runs is exactly the model that was built, tested (`tests/test_model.py`) and deployed with that code. If we retrained live (e.g. hourly, inside the dashboard), the served model would change with no build, no test and no review. Two containers could disagree. A prediction from yesterday couldn't be reproduced. A bad night of data (a stuck sensor, an NDW format change) would flow straight into what users see. With baking, a new model is a new image: `train_model.py` → tests → `deploy.sh`. Rolling back means redeploying the previous image. `model.pkl`, `metrics.json` and the exact `training_data.csv` are committed together, so every deployed model can be traced to its data.

What baking does *not* prevent is **drift**: the world moving away from the training data (winter vs summer, roadworks, a new speed limit). That is a monitoring task: compare `no2_ug_m3_predicted` with the value Luchtmeetnet publishes for `prediction_for_hour`, and retrain when the error grows. The cost of baking is that every retrain means a rebuild and redeploy (about a minute with `deploy.sh`).

### Why one station (NL10240) is enough for all four sites
All four NDW sites (hrl, hrr, vwd, vwa) are at **the same interchange**, hectometer 63 of the A27, and NL10240 (Breda-Tilburgseweg) measures the air that interchange affects. Their combined traffic is one emission source for one measured concentration. So there is one real target and no stand-in:
- every `/site/{id}` returns the same measured NO₂;
- the model predicts one value for the interchange from the four sites' total.

In a multi-corridor design, a station at corridor A would have to stand in for corridors B and C. The model would then learn "traffic at B predicts air measured at A", which is a different relationship from the one it claims to describe.

**What forces a second station:** a second interchange. NO₂ near a motorway falls off steeply within a few hundred metres, so NL10240 can't measure the effect of traffic kilometres away. That traffic would be just background to it. Monitoring a second interchange needs a station (or a validated sensor) within that interchange's own footprint. Until there is one, predictions for it would be unvalidated and should be labelled that way rather than shown next to measured values.

### What `/site/{id}` returns if `predict()` fails
It **degrades instead of failing**: HTTP 200 with the real `no2_ug_m3` and `intensity_veh_per_hr`, `no2_ug_m3_predicted` and `no2_exceedance_risk` set to `null`, the reason in `errors`, and an `ERROR prediction_failed` log line (tested in `tests/test_dashboard.py`).
- **Why not fail the request:** the measured values come from two healthy stores and are the most trustworthy part of the response. A broken model is no reason to hide them, and it shouldn't count against the 99% SLO of an endpoint whose main job is to show measurements (ADR-0003).
- **Why `null` and not a fallback number** (the last prediction, or the measured value): a fabricated prediction looks real. `null` is unambiguous, and the dashboard shows "no prediction available".
- **Same rule for the stores:** if Postgres is down, NO₂ is `null`; if S3 is down, intensity and prediction are `null`. Only when *neither* real value is available does the route return 503, because then nothing true is left to show.

## Consequences

**Easier:** the whole model fits in a sentence ("about 10.5 µg/m³ at night, plus 1.9 per 1,000 vehicles per hour"), so anyone can sanity-check it. Retraining is two commands (see `infra/day4-deploy.md`), and every model can be traced to its data.

**Harder / to revisit:**
1. **It is a nowcast, not a forecast.** The case study asks for exceedance risk *in the next hour*. Today's model predicts the NO₂ of the hour the traffic was measured in. That is still useful, because Luchtmeetnet publishes that hour 30–90 minutes later, but it doesn't look ahead. A real forecast needs a target of NO₂(t+1) from traffic(t) and NO₂(t). With the data we have now, that would mean even fewer rows.
2. **Retrain soon and often while the data is growing.** Each day adds ~24 rows. Retraining on Day 5 roughly doubles the training set.
3. **Confounding.** Traffic, time of day and the night-time inversion move together in a single day. Only weeks of data and weather features can separate them.
4. The model's error against the hours Luchtmeetnet later publishes isn't monitored yet. That is the first ML-monitoring metric to add.
