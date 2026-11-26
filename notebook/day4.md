# Lab notebook — Day 4

> Numbers and factual answers are filled in from what was built and measured on 26 Nov 2026.
> Reflection questions are yours (✍️), with pointers.

## Rules of ML reflection ✍️
*What would you need to see in the data before you'd trust a more complex model over today's linear regression?*

Pointers (see ADR-0006):
- **Volume and coverage:** weeks of hourly rows (~500–1,000), including weekends, different weather and real high-NO₂ hours. Today: 17 rows from one evening, night and morning, max 21 µg/m³.
- **Evidence the linear model is wrong:** a time-ordered test (train on early weeks, test on later ones) where the residuals show a pattern: curvature at high traffic, or systematic errors at certain hours or in certain winds.
- **The complex model winning *out of sample*:** beating the linear model's error on the later weeks, not just fitting the training rows better.
- Google's rule: the gain usually comes from features (wind, boundary layer, sin/cos hour), not from model complexity.

My answer:

## Building the training dataset

**How many joined rows?** **17** (25 Nov 20:00 → 26 Nov 12:00 UTC).
- `sensor_readings` had 118 usable NO₂ hours, but traffic CSVs only exist since the VM started (Day 3).
- 2 more traffic hours had no published NO₂ yet.
- No hour was lost to an invalid (−1) flow lane.

That's below the ~24 rows of "a day's worth", so it's a limitation, not a production model (ADR-0006).

**Why we didn't backfill by running the ingesters every 5–10 minutes:** NO₂ is published once per hour, so extra traffic snapshots inside the same hour still join to the *same* NO₂ value. They would add near-duplicate rows, not information. NDW history can't be fetched afterwards, because the open feed only has the current minute.

**Is it roughly linear?** See `ml/no2_vs_traffic.png`. Yes, within this data: the night hours (23–05 h local, <1,000 veh/h) sit at 9–13 µg/m³, and the morning rush (06–08 h, 3,900–5,460 veh/h) at 19–21 µg/m³. The points scatter about ±2 µg/m³ around a straight line. Too little data to see any curvature.

## Lab 1 — the regression

| | |
|---|---|
| Split | None. With 17 rows a hold-out set is 3–4 points. **Leave-one-out CV** instead: each row predicted by a model trained on the other 16 |
| In-sample R² / MAE | 0.83 / 1.21 µg/m³ |
| LOO MAE | 1.44 µg/m³ (mean baseline: 3.28) |
| Coefficients | traffic **+0.0019 µg/m³ per veh/h** (expected sign); hour_of_day −0.08 (≈ 0); intercept 10.5 |

Higher traffic pushes NO₂ up, as expected. The hour coefficient is "wrong" only in the sense that it is meaningless: as a linear 0–23 number, hour 23 and hour 0 are maximally far apart. A traffic-only model scores the same (LOO MAE 1.45).

**Extend:** threshold 25 µg/m³ (WHO 24-h guideline; reasoning in `services/dashboard/predict.py` and ADR-0006), with a sigmoid of steepness 0.2. **Stretch (logistic regression): not trainable.** 0 of 17 hours exceed 25 µg/m³, so there are no positive examples. That is itself the answer to "which do you trust more": today only the regression + sigmoid can exist at all.

My reflection on the coefficients ✍️:

## Lab 2 — dashboard

- Local Compose test: `http://localhost:8000` showed real numbers on the first run (NO₂ 17.0 measured, 18.8 predicted, 4,920 veh/h).
- VM: **http://13.53.194.127:8000** (port 8000 open to the owner's two IPs only).
- Refresh every 60 s; "last updated" times plus a warning banner if traffic is > 90 min or NO₂ > 3 h old.
- Read-only DB login for the dashboard; IAM was already scoped on Day 3 and needed no change (ADR-0005).

## Day 4 wrap-up

**1. 31 rows, a teammate wants a random forest because "it's a better model".**
"Better" means better on data it hasn't seen, and 31 rows can't show that. A forest has hundreds of split points to fit from 31 points, so it will memorise them, including the noise. It also can't predict outside the traffic range it has seen (trees predict a constant beyond the last split), which is exactly where the exceedance hours we care about would be. I'd ask them to beat the linear model's leave-one-out (or time-ordered) error first, and to add data and features (weeks of hours, wind, sin/cos hour) before adding model complexity. The linear model's coefficient can also be sanity-checked against physics; a forest gives us nothing to check.

**2. Predicted NO₂ is shown, but the actual reading is blank. What to check, in order:**
1. **The API response itself:** `curl http://<ip>:8000/site/hrl`. Is `no2_ug_m3` null, and what does `errors` say? A filled value means the page/JS is wrong. `null` with `"database: …"` means the dashboard can't read Postgres. `"no NO2 readings yet"` means there is no data. (The prediction comes from S3 traffic, so it can be fine while NO₂ is missing.)
2. **Is air-ingest working?** `docker ps` (running? healthy?), `curl localhost:8001/health` (`stale`?), `docker logs air-ingest` (`fetch_failed`, `fetch_empty`, `db_write_failed`).
3. **The database and the source:** `SELECT MAX(timestamp) FROM sensor_readings WHERE component='NO2'` (old? only NULLs?), `docker ps` for `db`, and the Luchtmeetnet API directly. It may simply not have published the last hour yet, or NL10240 may be offline. The dashboard shows the newest *non-null* value, so a blank means there is no value at all.

**3. `model.pkl` baked into the image instead of loaded from S3 at start-up.**
*Gain:* the running model is exactly the one that was built, tested and deployed with this code; it starts with no network dependency (S3 down ≠ dashboard down); rollback means redeploying the previous image; no version mismatch between the pickle and the code or library that loads it.
*Cost:* every retrain needs a rebuild and redeploy (≈ 1 minute with `deploy.sh`, but a deploy, with its risk), and the model and the code can't be updated independently. If we retrained hourly, that would be 24 deploys a day. Then loading a *versioned* model from S3 (with a pinned version in config) would be the better trade.

**4. The VM reboots overnight. Which containers come back?**
In our deployment **all four** (`db`, `air-ingest`, `traffic-ingest`, `dashboard`) come back by themselves: every one runs with `--restart unless-stopped`, and the Docker service is enabled at boot. Tested with `aws ec2 reboot-instances` on Day 3 and Day 4. With the lab's literal commands, the dashboard would **not** come back: `docker run -d --name dashboard ...` has no restart policy. The cron-scheduled ingesters would resume at the next full hour, because cron runs at boot, but a `docker run` without `--restart` doesn't. The fix is `--restart unless-stopped` (or a systemd unit per container), plus `systemctl enable docker`.
