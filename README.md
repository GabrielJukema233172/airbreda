# AirBreda

Cloud platform that ingests live NO₂ readings (RIVM Luchtmeetnet, station NL10240 Breda-Tilburgseweg) and live traffic counts (NDW, four A27 sites near hectometer 63), stores and processes them, and serves a regression model that predicts NO₂ near the A27 interchange.

Built for the System Design & Cloud Platforms elective (ADSAI, BUas). Cloud provider: **AWS** (region eu-central-1).

## Repository layout

| Path | What it holds |
|---|---|
| `scripts/` | Helper scripts that fetch the raw data (`getNO2Readings.py`, `getTrafficReadings.py`) |
| `docs/` | Architecture Design Document, published with GitHub Pages |
| `docs/adr/` | One Architecture Decision Record per decision |
| `docs/diagrams/` | Architecture diagrams |

## Data sources

| Source | Endpoint | Data |
|---|---|---|
| RIVM Luchtmeetnet | `https://api.luchtmeetnet.nl/open_api/stations/NL10240/measurements` | Hourly NO₂ (µg/m³), JSON |
| NDW Open Data | `http://opendata.ndw.nu/snelheden_en_intensiteiten_meetgegevens.xml.gz` | Per-minute flow and speed, gzipped DATEX II XML |
