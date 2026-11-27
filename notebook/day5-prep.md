# Day 5 — preparation checklist

Deliverables: **Architecture Design Document** (40%, [docs/index.md](../docs/index.md) on GitHub Pages)
and the **live deployed system** (60%, http://16.192.119.40:8000).

## A. Things only you can do (before the evaluation)

### Writing
- [ ] **Read and own §4–6 of `docs/index.md`** (trade-offs, provider rationale, cost estimate). They are drafted from the ADRs and real prices. Rewrite anything you wouldn't say yourself in your own words; you will be asked about it.
- [ ] **Write §7 Reflection** in `docs/index.md` ("what would you do differently?"). There are prompts in the HTML comment above it. Delete the "_To be written by the author._" line.
- [ ] **Fill in the empty ✍️ reflections:**
  - [ ] `notebook/day2.md`: Shopify reflection, AWS December 2021 reflection
  - [ ] `notebook/day3.md`: Things that surprised me
  - [ ] `notebook/day4.md`: Rules of ML reflection, "My reflection on the coefficients"
- [ ] **Check the 25 µg/m³ threshold** (`services/dashboard/predict.py`, ADR-0006). It's a judgement call: be ready to defend it, or change it and retrain.

### System
- [ ] **Morning of Day 5:** run `.\infra\retrain.ps1` (about 24 more rows per day), then commit `services/dashboard/model.pkl` + `ml/` together.
- [ ] **Commit and push.** Then open https://gabrieljukema233172.github.io/airbreda/ and check that the diagrams, the "How the architecture evolved" fold-out and the runbook link work.
- [ ] **Test from a different network** (e.g. phone on mobile data, Wi-Fi off) that http://16.192.119.40:8000 loads. That proves it is public.
- [ ] `curl.exe http://16.192.119.40:8000/health` shows `"status": "ok"`, and all four `/site/{id}` have `"errors": []`.
- [ ] **SSH works from where you present.** If you are on a new network, add its IP first (runbook §7).
- [ ] Check the Free plan credit and Cost Explorer (runbook §8) to quote the real spend.

### After the evaluation
- [ ] Close port 8000 to the public, stop the VM, release the Elastic IP (runbook §11).
- [ ] **Git history:** older commits in this *public* repo still contain your home IP address and AWS account ID; the current files no longer do. Low risk, but if you mind, make the repository private after grading. Rewriting history is possible but destructive (force-push).

## B. Demo script (≈ 5 minutes, in this order)

1. **Dashboard** http://16.192.119.40:8000: measured vs predicted NO₂, total traffic, the per-site table, "last updated" times. Explain why the prediction is the same on every row (one station, model trained on the four-site *total*).
2. **API** `/site/hrl`: walk through the fields and where each comes from (DB / S3 / model). Then `/health`: both sources, bad-data counts.
3. **On the VM** (`ssh`):
   - `docker ps`: 4 containers, restart policy.
   - `docker logs traffic-ingest | grep DATA_QUALITY_ERROR`: the `-1` handler.
   - `ls ~/.aws`: doesn't exist, so there are no keys; `aws sts get-caller-identity` shows the role.
4. **Data:** the `psql` count per component, and `aws s3 ls .../ndw/` for the raw files.
5. **Model:** `ml/no2_vs_traffic.png` + `ml/metrics.json` (coefficient, LOO MAE vs baseline). Be honest about the row count.
6. **Resilience:** the nightly backup + tested restore; reboot tested; the dashboard degrades instead of failing (`tests/test_dashboard.py`).

## C. Concepts to be able to explain, with *your* AirBreda example

| Concept | Your example | Where |
|---|---|---|
| CAP theorem | Luchtmeetnet is AP (HTTP 200 with nulls or late values); you keep NULLs, re-fetch 50 h, flag instead of drop; `/site/{id}` stays available with partial data | `ingest_air.py` docstring, ADR-0001/0006 |
| SQL vs NoSQL | Fixed schema + time-range queries + air⋈traffic join → Postgres; DynamoDB rejected | ADR-0001 |
| Object vs block storage | S3 = raw audit trail and retraining set; EBS = the database disk | ADR-0001, `ingest_traffic.py` docstring |
| Lambda vs Kappa | Hourly batch; raw S3 files make recomputation possible (Kappa's idea) without Kafka; no speed layer, because data is hourly | ADD §4 |
| Idempotency / at-least-once | PK `(station, timestamp, component)` + upsert that fills NULLs but never overwrites; deterministic S3 keys | ADR-0001 |
| Event-driven / queues | Redis on Day 2 decoupled producers; dropped on the VM because nobody consumed it (158 messages); returns as SQS once a second consumer exists | ADR-0002, ADR-0004 |
| IaaS vs PaaS vs serverless | EC2 (you patch the OS) vs ECS Fargate/App Runner vs Lambda, and why the database location decided it | ADR-0004/0005 |
| SLI / SLO / error budget | 99% → 7.2 h/month; AWS promises 99.5% for one EC2 instance | ADR-0003, notebook day 2 |
| DR tiers, RTO / RPO | Backup & Restore: RTO ~1 h, effective RPO ~1 h (re-fetch + S3) vs pilot light / warm standby | ADR-0003 |
| Observability | JSON logs, `/health` 200/503, `BAD_DATA_THRESHOLD_EXCEEDED` | ADR-0003 |
| Least privilege | IAM role via IMDSv2 (hop limit 2), prefix-scoped S3 policy, no Delete, SELECT-only DB login, SG | ADR-0004/0005 |
| Training-serving skew | Shared `common/features.py`, same library pins, total vs per-site intensity, end-of-hour join, model baked in | ADR-0006 |
| Model evaluation with tiny data | Leave-one-out CV vs a mean baseline; why no train/test split with 17 rows | ADR-0006 |
| Exceedance risk | Sigmoid at 25 µg/m³ (WHO 24-h), steepness 0.2; not a calibrated probability; logistic regression impossible (0 positives) | `predict.py`, ADR-0006 |
| FinOps | ≈ $21/month; 10 corridors ≈ $22, 50 ≈ $66; fixed costs dominate; budget alert | ADD §6 |

## D. Numbers to know by heart

- NL10240 · 4 sites (hrl, hrr = mainline; vwd, vwa = slip roads) · hectometer 63 · eu-north-1
- Poll hourly; NO₂ re-fetch window 50 h; NDW download ~230 MB XML, **~700 MB RAM peak** → t3.small (2 GB)
- 99% SLO = **7.2 h/month**; AWS single-instance SLA 99.5%
- Model: **+1.9 µg/m³ per 1,000 veh/h**, intercept ≈ 10.5; LOO MAE ≈ 1.3–1.4 vs 3.3 baseline; threshold **25 µg/m³**
- Cost: t3.small $0.0216/h → **≈ $21/month** total; 10 corridors ≈ $22; 50 ≈ $66
- Tests: 50

## E. Questions you are likely to get

- *Why not RDS / Kubernetes / Lambda?* (ADR-0001, 0004, 0005: the database location, cost, one engineer, and what would change your mind)
- *Your deployment has 4 containers; the brief says 3.* The 3 application containers are air, traffic and dashboard; Postgres is the 4th because of ADR-0001 (no RDS).
- *Why is the prediction the same for every site?* (training-serving skew, one station per interchange)
- *How much data is the model trained on, and do you trust it?* (row count; one day; confounded with time of day; what would make you trust a more complex model)
- *What happens if Luchtmeetnet or NDW is down? If the VM reboots? If the model file is corrupt?*
- *Why Stockholm and not Frankfurt/Ireland?* (the organization's SCP; still EU/GDPR)
- *Why did you drop the Redis queue?* (no consumer; what brings it back, and as what)
- *What did Compose testing catch, and what not?* (the silent `pipefail` bug in `deploy.sh`)
- *Why bake the model into the image?* (gain vs retraining cost)
- *Costs at 50 corridors: what grows and what doesn't?* (the national feed is downloaded once; reliability drives the cost)

## F. Reflection prompts (for ADD §7 and the presentation)

- **Start collecting traffic on Day 1, not Day 3:** NDW has no history, so every day missed is training data lost forever.
- **One deployment definition:** Compose locally plus `deploy.sh` on the VM drifted and hid a silent bug.
- **Check the cloud account's guardrails (allowed regions) before writing region-specific designs.**
- **Where the lab was wrong for this data:** rounding to the nearest hour (off by one hour), per-site prediction (skew), `ON CONFLICT DO NOTHING` (NULLs never filled).
- What you'd add with one more week: weather features, sin/cos hour, a forecast (NO₂ at t+1), monitoring the prediction error against later published values, HTTPS.
