-- AirBreda: parsed sensor readings.
-- Runs once when the Postgres container is first created. The ingestion
-- services also run the same statements on every start (common/db.py), which
-- upgrades a Day 1 database in place.
CREATE TABLE IF NOT EXISTS sensor_readings (
    station_id  VARCHAR(40)   NOT NULL,   -- 'NL10240' or an NDW site id (27 chars)
    timestamp   TIMESTAMPTZ   NOT NULL,   -- UTC; Luchtmeetnet: end of the measured hour
    component   VARCHAR(10)   NOT NULL,   -- 'NO2' | 'flow' (veh/h) | 'speed' (km/h)
    value       FLOAT,                    -- NULL = no valid measurement; never a guess
    is_flagged  BOOLEAN       NOT NULL DEFAULT FALSE,  -- stale or null reading (Day 2)
    PRIMARY KEY (station_id, timestamp, component)
);

-- Day 1 -> Day 2 migration (also what the lab asks you to run by hand):
ALTER TABLE sensor_readings ADD COLUMN IF NOT EXISTS is_flagged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE sensor_readings ALTER COLUMN station_id TYPE VARCHAR(40);

-- Useful checks
-- SELECT component, COUNT(*), COUNT(value), SUM(is_flagged::int), MAX(timestamp) FROM sensor_readings GROUP BY component;
-- SELECT * FROM sensor_readings WHERE is_flagged ORDER BY timestamp DESC;
