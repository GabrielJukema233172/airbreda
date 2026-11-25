# Lab notebook — Day 2

> Calculations and factual answers are filled in. Reflection questions are yours (✍️),
> with pointers to start from.

## Shopify reflection ✍️
*AirBreda has two ingestion services. Is a shared codebase or two separate services the right choice? What would make you change your answer in 12 months?*

Pointers:
- What we actually built is **both**: one repository with a shared `services/common` package (logging, health, database, Redis code), deployed as **two separate containers**. That's close to Shopify's "modular monolith" idea: shared code and clear module boundaries, without a network call between the modules.
- Reasons the split into two containers pays off: different failure modes (an NDW outage doesn't stop NO₂ ingestion), different resource needs (NDW parses ~230 MB of XML; the air service makes one small JSON call), separate health checks.
- Reasons not to split further: one developer, about 5 messages per hour, and every extra service is another thing to deploy, monitor and debug.
- What could change it in 12 months: more people working on it (team ownership is the real reason for microservices), dozens of corridors with very different polling needs, or a source that needs its own scaling.

My answer:

## AWS December 2021 reflection ✍️
*What is AirBreda's acceptable downtime? Write a realistic SLO for /site/{id}.*

Pointers: who uses the dashboard, and what happens to them if it is down for an hour? The data is hourly and already 30–90 min old. AWS only promises 99.5% uptime for a single EC2 instance. What did ADR-0003 settle on, and do you agree?

My answer:

## Wrap-up questions

**1. A colleague proposes seven microservices. Three questions to ask first:**
1. *What problem does each split solve that a module in one codebase doesn't?* Is there a different scaling need, failure mode or owning team behind each one, or is it splitting for its own sake? We are one developer.
2. *What does it cost to run?* Seven deployables, seven health checks, seven sets of logs, on one 2 GB VM. And every boundary between them turns a function call into a network call that can time out.
3. *Where do the data and transactions live?* "One service per storage type" means another service writes to the database for you: who owns the schema, and what happens to a reading when the writer service is down? Can we still keep idempotent upserts and late-value corrections in one place?

**2. `bad_data_count = 47` in the last hour — what to do:**
- **Check first: is 47 even possible?** One NDW poll covers 7 lanes × 2 metrics = 14 values, so even with every value bad, one hourly poll gives at most 14. The air service, which only counts *new* bad readings, adds about 0–2 per hour. 47 in an hour therefore means something changed: the service is restarting or polling much more often (check `docker compose ps` restart counts and `service_started` events), or the feed itself changed.
- **Then: which source, which sites, which field?** Group the `DATA_QUALITY_ERROR` events by `source`, `site` and `field`. A single lane or slip road with `speed = -1` overnight is normal (no cars → no speed). *Every* value from *every* site being invalid points to a broken feed or a parser bug after an NDW format change: compare with the raw CSV in S3 and the `fetch_success` totals.
- **Alert on:** the `BAD_DATA_THRESHOLD_EXCEEDED` error (already fires once above 10 per hour), a `/health` status of `stale` (no successful fetch), and `fetch_failed` repeating. Not on individual warnings: those are expected.
- **Acceptable to let through to the database:** flagged NO₂ rows (kept on purpose, visibly flagged); NDW totals computed only from valid lanes (speed averaged over valid lanes; flow only when every lane is valid). **Not acceptable:** `-1` sentinels, or flow totals summed over missing lanes. Those stay in S3 only.

**3. SLO 99.5% — error budget and the December 2021 outage:**
- Per month (30 days = 43,200 min): 0.5% × 43,200 = **216 minutes ≈ 3.6 hours**.
- Per year (525,600 min): 0.5% × 525,600 = **2,628 minutes ≈ 43.8 hours**.
- A ~5-hour outage = 300 minutes → **300 / 2,628 ≈ 11.4% of the annual budget**. That's also **139% of one month's budget** (300 / 216): that month's SLO is broken by a single event we couldn't have prevented on a single-region deployment.
- With ADR-0003's 99% SLO (7.2 h/month, 87.6 h/year) the same outage uses 69% of a month and 5.7% of the year.
