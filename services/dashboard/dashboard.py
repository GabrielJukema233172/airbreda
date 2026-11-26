"""
AirBreda - Day 4: the dashboard and its JSON API, one FastAPI app.

    uvicorn dashboard:app --host 0.0.0.0 --port 8000

Routes
  GET /site/{site_id}   hrl | hrr | vwd | vwa -> real NO2, real traffic, predicted NO2 + risk
  GET /health           both ingestion sources (+ the database), aggregated
  GET /                 HTML page; its JavaScript is just another client of /site/{id}

Environment: DASHBOARD_DATABASE_URL (read-only role; falls back to DATABASE_URL),
S3_BUCKET, AWS_REGION, AIR_HEALTH_URL, TRAFFIC_HEALTH_URL.

Where each field of GET /site/{site_id} comes from (required comment)
---------------------------------------------------------------------
  site_id, location             the URL + common.features.SITES (fixed NDW site ids)
  no2_ug_m3, no2_timestamp,     PostgreSQL sensor_readings: the newest non-NULL NO2 row
  no2_is_flagged                for station NL10240, written by air-ingest. One real
                                station covers all four sites (same interchange), so
                                every site returns the same value.
  intensity_veh_per_hr          S3 bucket: the newest ndw/YYYY-MM-DD/HH-<site>.csv,
                                written by traffic-ingest; vehicles/hour summed over the
                                site's lanes with common.features.site_intensity(), the
                                same function that built the training data.
  timestamp                     the measurement time inside that CSV (when NDW measured
                                the traffic), i.e. the moment the prediction is about.
  total_intensity_veh_per_hr    the same S3 step for all four sites, summed. Only set
                                when all four files come from the same snapshot and
                                have no invalid (-1) flow lane.
  prediction_for_hour           common.features.no2_hour_for(timestamp): the Luchtmeetnet
                                hour the prediction will be compared with once published.
  no2_ug_m3_predicted,          predict.predict(total_intensity_veh_per_hr, hour_of_day):
  no2_exceedance_risk           the LinearRegression in model.pkl (baked into this image)
                                and a sigmoid around 25 µg/m³. The model was trained on
                                the TOTAL of all four sites, so it gets the total, never
                                one site's intensity: identical for every site.
  errors                        which of the above could not be filled in, and why.

What the route returns if predict() raises (required comment)
-------------------------------------------------------------
It DEGRADES, it doesn't fail: HTTP 200 with the real NO2 and intensity values,
no2_ug_m3_predicted = no2_exceedance_risk = null, the reason in "errors", and an
ERROR log line (event "prediction_failed"). The measured values are facts from
two working stores; a broken model is no reason to hide them, and a client can
tell "no prediction" (null) from "low risk" (a number). The same rule applies to
each store: if the database or S3 is down, that half is null. Only when NEITHER
real value is available does the route answer 503, because then there is
nothing true left to show.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import predict as predictor  # module import, so tests can replace predictor.predict
from common.features import SITES, hour_of_day, measurement_time, no2_hour_for, parse_ndw_csv, site_intensity
from common.observability import log_event, setup_logging

STATION = "NL10240"
DATABASE_URL = os.environ.get("DASHBOARD_DATABASE_URL") or os.environ.get("DATABASE_URL")
BUCKET = os.environ.get("S3_BUCKET")
HEALTH_URLS = {
    "Luchtmeetnet": os.environ.get("AIR_HEALTH_URL", "http://air-ingest:8000/health"),
    "NDW": os.environ.get("TRAFFIC_HEALTH_URL", "http://traffic-ingest:8000/health"),
}
TRAFFIC_CACHE_SECONDS = 60   # S3 changes once an hour; a page load calls /site/{id} four times

log = logging.getLogger("dashboard")
setup_logging("dashboard")
app = FastAPI(title="AirBreda", description="NO₂ and traffic at the A27 / Breda interchange")


def iso(ts: datetime | None) -> str | None:
    return None if ts is None else ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Stores
# --------------------------------------------------------------------------- #
def latest_no2() -> dict | None:
    """Newest real NO2 reading for NL10240 from PostgreSQL."""
    import psycopg2

    with closing(psycopg2.connect(DATABASE_URL, connect_timeout=5)) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT timestamp, value, is_flagged FROM sensor_readings
               WHERE station_id = %s AND component = 'NO2' AND value IS NOT NULL
               ORDER BY timestamp DESC LIMIT 1""",
            (STATION,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"timestamp": row[0], "value": float(row[1]), "is_flagged": bool(row[2])}


_s3 = None
_cache: dict = {"at": float("-inf"), "data": None}
_cache_lock = threading.Lock()


def _s3_client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    return _s3


def _fetch_latest_traffic(now: datetime | None = None) -> dict[str, dict | None]:
    """{site: {"intensity", "measurement_time", "key"} or None} from the newest CSV per site.

    Keys are ndw/YYYY-MM-DD/HH-<site>.csv, so the newest is the lexicographic
    maximum; today's and yesterday's folders are enough (the VM writes hourly).
    """
    s3 = _s3_client()
    now = now or datetime.now(timezone.utc)
    keys: dict[str, list[str]] = {}
    for day in (now, now - timedelta(days=1)):
        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"ndw/{day:%Y-%m-%d}/")
        for obj in resp.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1].removesuffix(".csv")   # "12-hrl"
            site = name.split("-", 1)[-1]
            if site in SITES:
                keys.setdefault(site, []).append(obj["Key"])
    result: dict[str, dict | None] = {}
    for site in SITES:
        if not keys.get(site):
            result[site] = None
            continue
        key = max(keys[site])
        rows = parse_ndw_csv(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8"))
        result[site] = {"intensity": site_intensity(rows), "measurement_time": measurement_time(rows), "key": key}
    return result


def latest_traffic() -> dict[str, dict | None]:
    with _cache_lock:
        if _cache["data"] is not None and time.monotonic() - _cache["at"] < TRAFFIC_CACHE_SECONDS:
            return _cache["data"]
    data = _fetch_latest_traffic()
    with _cache_lock:
        _cache.update(at=time.monotonic(), data=data)
    return data


def total_intensity(traffic: dict[str, dict | None]) -> tuple[float | None, datetime | None, str | None]:
    """(sum over all four sites, snapshot time, reason if unavailable)."""
    if any(traffic.get(s) is None for s in SITES):
        return None, None, "total intensity unavailable: no recent file for " + \
            ", ".join(s for s in SITES if traffic.get(s) is None)
    times = {traffic[s]["measurement_time"] for s in SITES}
    if len(times) != 1:
        return None, None, "total intensity unavailable: the four sites' latest files are from different snapshots"
    invalid = [s for s in SITES if traffic[s]["intensity"] is None]
    if invalid:
        return None, None, "total intensity unavailable: invalid (-1) flow lane at " + ", ".join(invalid)
    return sum(traffic[s]["intensity"] for s in SITES), times.pop(), None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/site/{site_id}")
def site(site_id: str):
    if site_id not in SITES:
        raise HTTPException(status_code=404, detail=f"Unknown site '{site_id}'. Use one of: {', '.join(SITES)}")
    errors: list[str] = []

    try:
        no2 = latest_no2()
        if no2 is None:
            errors.append("database: no NO2 readings yet")
    except Exception as exc:  # noqa: BLE001 - any database failure degrades the same way
        no2 = None
        errors.append(f"database: {exc.__class__.__name__}")
        log_event(log, logging.ERROR, "source_failed", store="postgres", error=str(exc))

    try:
        traffic = latest_traffic()
    except Exception as exc:  # noqa: BLE001 - S3/credentials/network
        traffic = {}
        errors.append(f"s3: {exc.__class__.__name__}")
        log_event(log, logging.ERROR, "source_failed", store="s3", error=str(exc))
    mine = traffic.get(site_id)
    if traffic and mine is None:
        errors.append(f"s3: no recent file for {site_id}")

    total, snapshot, reason = total_intensity(traffic) if traffic else (None, None, None)
    prediction = {"no2_ug_m3_predicted": None, "no2_exceedance_risk": None}
    if reason:
        errors.append(f"prediction: {reason}")
    elif total is not None:
        try:
            prediction = predictor.predict(total, hour_of_day(snapshot))
        except Exception as exc:  # noqa: BLE001 - degrade: see the module docstring
            errors.append(f"prediction: {exc.__class__.__name__}: {exc}")
            log_event(log, logging.ERROR, "prediction_failed", site=site_id, error=str(exc))

    if no2 is None and (mine is None or mine["intensity"] is None):
        return JSONResponse(status_code=503, content={"site_id": site_id, "errors": errors})

    traffic_time = mine["measurement_time"] if mine else None
    return {
        "site_id": site_id,
        "location": SITES[site_id],
        "no2_ug_m3": no2["value"] if no2 else None,
        "no2_timestamp": iso(no2["timestamp"]) if no2 else None,
        "no2_is_flagged": no2["is_flagged"] if no2 else None,
        "intensity_veh_per_hr": mine["intensity"] if mine else None,
        "total_intensity_veh_per_hr": total,
        "no2_ug_m3_predicted": prediction["no2_ug_m3_predicted"],
        "no2_exceedance_risk": prediction["no2_exceedance_risk"],
        "timestamp": iso(traffic_time),
        "prediction_for_hour": iso(no2_hour_for(snapshot)) if snapshot and prediction["no2_ug_m3_predicted"] is not None else None,
        "errors": errors,
    }


def _source_health(name: str, url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:   # 503 "stale" still carries the JSON body
        try:
            return json.load(exc)
        except Exception:  # noqa: BLE001
            return {"source": name, "status": "unreachable", "error": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"source": name, "status": "unreachable", "error": str(exc)}


@app.get("/health")
def health():
    """Day 2's per-ingester /health, aggregated: worst status wins.
    ok (200) | starting (200, an ingester is in its first cycle) | degraded (503)."""
    sources = {name: _source_health(name, url) for name, url in HEALTH_URLS.items()}
    try:
        latest_no2()
        database = "ok"
    except Exception as exc:  # noqa: BLE001
        database = f"error: {exc.__class__.__name__}"
    statuses = [s.get("status") for s in sources.values()]
    if database != "ok" or any(s not in ("ok", "starting") for s in statuses):
        status, code = "degraded", 503
    elif "starting" in statuses:
        status, code = "starting", 200
    else:
        status, code = "ok", 200
    body = {
        "status": status,
        "database": database,
        "sources": list(sources.values()),
        "luchtmeetnet_bad_data_count": sources["Luchtmeetnet"].get("bad_data_count"),
        "ndw_bad_data_count": sources["NDW"].get("bad_data_count"),
    }
    return JSONResponse(status_code=code, content=body)


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AirBreda</title>
<style>
  :root { --bg:#f6f6f4; --card:#fcfcfb; --ink:#0b0b0b; --muted:#52514e; --line:#e4e3df;
          --accent:#2a78d6; --track:#e9e8e4; --warn-bg:#fff4d6; --warn-ink:#6b4a00; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#121211; --card:#1a1a19; --ink:#f0efec; --muted:#c3c2b7; --line:#383835;
            --accent:#3987e5; --track:#2a2a28; --warn-bg:#3a2e10; --warn-ink:#fad27a; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  main { max-width:960px; margin:0 auto; padding:24px 16px 48px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub, .muted { color:var(--muted); }
  .sub { margin:0 0 20px; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-bottom:16px; }
  .tile, .panel { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
  .label { font-size:13px; color:var(--muted); margin-bottom:6px; }
  .big { font-size:32px; font-weight:650; font-variant-numeric:tabular-nums; }
  .unit { font-size:14px; font-weight:400; color:var(--muted); margin-left:4px; }
  .note { font-size:12.5px; color:var(--muted); margin-top:6px; }
  table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
  th, td { text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); }
  th { font-size:12.5px; font-weight:600; color:var(--muted); }
  td.num, th.num { text-align:right; }
  .bar { display:inline-block; width:70px; height:6px; background:var(--track); border-radius:3px;
         vertical-align:middle; margin-left:8px; overflow:hidden; }
  .bar > span { display:block; height:100%; background:var(--accent); border-radius:3px; }
  .badge { display:inline-block; font-size:12px; padding:1px 7px; border-radius:999px;
           background:var(--warn-bg); color:var(--warn-ink); margin-left:6px; }
  .warn { background:var(--warn-bg); color:var(--warn-ink); border-radius:8px; padding:10px 12px; margin:0 0 16px; }
  .hidden { display:none; }
  .scroll { overflow-x:auto; }
  footer { margin-top:16px; font-size:12.5px; color:var(--muted); }
  a { color:var(--accent); }
</style>
</head>
<body>
<main>
  <h1>AirBreda: NO₂ and traffic at the A27 / Breda interchange</h1>
  <p class="sub">Measured NO₂ at Luchtmeetnet station NL10240 (Breda-Tilburgseweg), live NDW traffic at four
     A27 sites near hectometer 63, and a regression model's prediction of NO₂ from that traffic.</p>

  <div id="stale" class="warn hidden" role="status"></div>

  <section class="tiles" aria-label="Summary">
    <div class="tile">
      <div class="label">Measured NO₂ (latest published)</div>
      <div class="big"><span id="no2">–</span><span class="unit">µg/m³</span><span id="flag" class="badge hidden">flagged</span></div>
      <div class="note" id="no2-time">&nbsp;</div>
    </div>
    <div class="tile">
      <div class="label">Predicted NO₂ (from current traffic)</div>
      <div class="big"><span id="pred">–</span><span class="unit">µg/m³</span></div>
      <div class="note" id="pred-note">&nbsp;</div>
    </div>
    <div class="tile">
      <div class="label">Total traffic, all four sites</div>
      <div class="big"><span id="total">–</span><span class="unit">veh/h</span></div>
      <div class="note" id="traffic-time">&nbsp;</div>
    </div>
  </section>

  <section class="panel" aria-label="Per site">
    <div class="scroll">
    <table>
      <thead><tr>
        <th>Site</th><th class="num">Intensity (veh/h)</th><th class="num">Predicted NO₂ (µg/m³)</th>
        <th>Exceedance risk</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
    </div>
    <p class="note">One air-quality station covers the whole interchange, and the model predicts it from the
      <em>total</em> of all four sites, so the prediction and risk are the same on every row.
      Risk = how likely this hour is above 25 µg/m³ (the WHO 24-hour guideline).</p>
  </section>

  <ul id="errors" class="note"></ul>
  <footer>Updated <span id="refreshed">–</span> · refreshes every 60 s ·
    JSON: <a href="/site/hrl">/site/hrl</a> · <a href="/health">/health</a></footer>
</main>
<script>
const SITES = [
  ["hrl", "A27 mainline, direction 1"], ["hrr", "A27 mainline, direction 2"],
  ["vwd", "Entry slip road (leaving Breda)"], ["vwa", "Exit slip road (entering Breda)"],
];
const fmt = (v, d = 0) => v === null || v === undefined ? "–"
  : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
const ageMin = iso => iso ? (Date.now() - Date.parse(iso)) / 60000 : null;
const when = iso => {
  if (!iso) return "no data";
  const m = Math.round(ageMin(iso));
  const t = new Date(iso).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" });
  return `${t} (${m < 90 ? m + " min" : (m / 60).toFixed(1) + " h"} ago)`;
};
const text = (id, v) => { document.getElementById(id).textContent = v; };

async function refresh() {
  const results = await Promise.all(SITES.map(([id]) =>
    fetch(`/site/${id}`).then(r => r.json()).catch(e => ({ site_id: id, errors: [String(e)] }))));
  const any = results.find(r => r.no2_ug_m3 !== undefined) || {};
  const withPred = results.find(r => r.no2_ug_m3_predicted !== null && r.no2_ug_m3_predicted !== undefined);

  text("no2", fmt(any.no2_ug_m3, 1));
  text("no2-time", any.no2_timestamp ? "hour ending " + when(any.no2_timestamp) : "no reading");
  document.getElementById("flag").classList.toggle("hidden", !any.no2_is_flagged);
  text("pred", withPred ? fmt(withPred.no2_ug_m3_predicted, 1) : "–");
  text("pred-note", withPred ? `risk ${fmt(withPred.no2_exceedance_risk * 100)}% · for the hour ending ` +
       new Date(withPred.prediction_for_hour).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })
       : "no prediction available");
  const total = results.reduce((s, r) => s + (r.intensity_veh_per_hr ?? 0), 0);
  const complete = results.every(r => r.intensity_veh_per_hr !== null && r.intensity_veh_per_hr !== undefined);
  text("total", complete ? fmt(total) : "–");
  text("traffic-time", any.timestamp ? "measured " + when(any.timestamp) : "no traffic data");

  const tbody = document.getElementById("rows");
  tbody.replaceChildren(...results.map((r, i) => {
    const tr = document.createElement("tr");
    const risk = r.no2_exceedance_risk;
    const cells = [
      `<strong>${SITES[i][0]}</strong> <span class="muted">${SITES[i][1]}</span>`,
      fmt(r.intensity_veh_per_hr), fmt(r.no2_ug_m3_predicted, 1),
      risk === null || risk === undefined ? "–"
        : `${fmt(risk * 100)}%<span class="bar" aria-hidden="true"><span style="width:${Math.round(risk * 100)}%"></span></span>`,
    ];
    tr.innerHTML = cells.map((c, j) => `<td class="${j === 1 || j === 2 ? "num" : ""}">${c}</td>`).join("");
    return tr;
  }));

  // "Last updated": make a stopped ingester visible instead of showing old numbers as current.
  const warnings = [];
  const tAge = ageMin(any.timestamp), nAge = ageMin(any.no2_timestamp);
  if (tAge === null || tAge > 90) warnings.push("Traffic data is more than 90 minutes old: traffic-ingest may have stopped.");
  if (nAge === null || nAge > 180) warnings.push("The latest NO₂ reading is more than 3 hours old: air-ingest may have stopped (Luchtmeetnet normally publishes within ~90 minutes).");
  const stale = document.getElementById("stale");
  stale.textContent = warnings.join(" ");
  stale.classList.toggle("hidden", warnings.length === 0);

  const errs = [...new Set(results.flatMap(r => r.errors || []))];
  document.getElementById("errors").replaceChildren(...errs.map(e => {
    const li = document.createElement("li"); li.textContent = e; return li; }));
  text("refreshed", new Date().toLocaleTimeString("en-GB"));
}
refresh();
setInterval(refresh, 60000);
</script>
</body>
</html>
"""
