# Lab notebook — Day 1

> Factual and calculation answers are filled in (checked against the live API on 2026-11-23).
> Reflection questions are yours: they are marked ✍️ with pointers to get you started.

## Zalando reflection ✍️
*If you were an engineer at Zalando in 2016, what would have been your biggest concern about moving to cloud? What would have excited you?*

Pointers: cost surprises for teams who never saw a compute bill · compliance/data protection for customer data · 200+ team accounts = 200 ways to misconfigure IAM · vs. excitement: teams ship without waiting for the data-centre team, scale for sales peaks without buying hardware.

My answer: I would have been excited for the ease of scale especially for an online retailer, traffic varies widely during peak hours or black friday for instance. My biggest concern would have been reliance on an outside service. No control over pricing and cost if something ran wrong or too long, also concern about moving away from the provider later on.

## FinOps estimation exercise
Cluster runs 24/7 (€500/month) for a job that only runs 08:00–20:00.

- Needed: 12 of 24 hours → **50 % idle → ~€250/month wasted**.
- If the job only runs on weekdays: 5 × 12 = 60 of 168 hours used → 64 % idle → **~€320/month wasted**.
- Fix with cloud-native tooling: scale the node group to zero outside 08:00–20:00 (scheduled scaling / cluster autoscaler, or KEDA's cron scaler), or drop the always-on cluster and run the job on a schedule (EventBridge Scheduler → ECS Fargate / AWS Batch), paying only while it runs. Spot instances for the job itself if it can tolerate interruption.

## Lab 1 Follow — Luchtmeetnet API (station NL10240)

**What fields does a single measurement record contain?**
`formula` (e.g. `"NO2"`), `value` (number or `null`), `timestamp_measured`, `timestamp_measured_start`, `timestamp_measured_end` (ISO 8601, UTC). The **unit is not in the response** (it is µg/m³ — you have to know that), and the station ID is not in the record (it's in the URL).

**What is the time resolution?**
Hourly. `timestamp_measured` is the **end** of the hour: `13:00` means the average over 12:00–13:00 UTC. A page holds 50 records.

**What happens if you request a formula the station doesn't measure?**
HTTP **200** with `"data": []` and `last_page: 0` — for a real component the station lacks (`SO2`) and for nonsense (`XYZ`) alike. No error. So "empty" must be treated as a failure by our code, or a typo would silently produce no data forever. (The station measures NO, NO₂, NOx, PM10 and PM2.5.)

**How recent is the latest reading?**
At 14:15 Dutch time (13:15 UTC) the newest NO₂ value was for the hour ending 12:00 UTC (27.5 µg/m³); at 14:31 (13:31 UTC) the 13:00 UTC hour had appeared (22.6 µg/m³). So readings arrive **~30–90 minutes after the hour ends**. The prediction model must work with data that is at least an hour old.

## Spotify reflection ✍️
*At what scale does a single Postgres instance become the wrong answer? What signals would you watch?*

Pointers: signals — CPU/IO saturation, query latency p95 creeping up, replication lag on read replicas, connection count near the limit, disk growth rate, vacuum falling behind. Scale is less "N rows" and more "write rate + query mix": Spotify broke on hundreds of thousands of events per second.

My answer:It is not really about the single postgres instance by itself, but also the fact that everything was being sent to one central cluster, so if that went down, the whole service went with it. A Postgres server can hold billions of rows. It becomes the wrong answer when the workload outgrows one machine or one central point. 

## ADR reflection ✍️
*What architectural decision from today would you be least confident defending in a design review?*

Candidates from ADR-0001's "known limitations": running our own Postgres instead of managed RDS (backups are now our job) · deviating from the lab's `DO NOTHING` · parsing 160 MB of XML four times per run · one NDW snapshot per hour.

My answer: Postgres on the VM instead of RDS. Objection might be: No automatic backups; one VM is a single point of failure for both the API and the data. Rebuttal: Saves credits, and the raw files in S3 allow a rebuild. What would change it: Real users depend on uptime, or data that can't be rebuilt from S3.

## Wrap-up questions

**1. "Let's use DynamoDB — it scales infinitely." Three questions to ask first:**
1. *What are our access patterns?* We need time-range queries per station and a join between air and traffic. Can we express those with DynamoDB keys, or are we rebuilding SQL in Python?
2. *What scale do we actually need?* ~10 writes/hour and ~96k rows/year. What problem does infinite scale solve for us?
3. *How will we analyse and retrain?* Model training needs ad-hoc aggregations over all history. What does that cost in scans/exports, and who maintains it?

**2. Five years, 10 stations — row count in `sensor_readings`:**
Per station-corridor per year: 3 components × 8,760 h = 26,280 air rows + 4 sites × 2 metrics × 8,760 h = 70,080 traffic rows ≈ 96,360 rows.
× 10 stations × 5 years ≈ **4.8 million rows**. Postgres handles that on the smallest instance (indexes fit in memory; roughly 0.5–1 GB on disk). **No change of database.** It would only start to matter at per-minute NDW resolution (×60 → ~290 M rows), and even then the next step is partitioning by month or TimescaleDB, not NoSQL.

**3. Most recent NO₂ value, and what a null looks like:**
At 14:31 Dutch time: **22.61 µg/m³** for the hour ending 13:00 UTC (re-run `docker run --rm airbreda-air` for the value at your time).
A missing value looks like `{"formula": "NO2", "value": null, "timestamp_measured": "…"}` — or the hour is simply absent from the list. The pipeline stores the row with `value = NULL` (never 0, never the previous value), logs how many nulls it saw, and the next run re-fetches the last 50 hours so a missing hour is filled in when it appears.
