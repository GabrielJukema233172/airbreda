-- AirBreda: parsed sensor readings (Day 1, Lab 2).
-- ingest_air.py also runs this (CREATE TABLE IF NOT EXISTS) on every start.
CREATE TABLE IF NOT EXISTS sensor_readings (
    station_id  VARCHAR(20)   NOT NULL,
    timestamp   TIMESTAMPTZ   NOT NULL,   -- end of the measured hour, UTC
    component   VARCHAR(10)   NOT NULL,   -- 'NO2', 'PM10', ...
    value       FLOAT,                    -- NULL = no valid measurement; never replaced by a guess
    PRIMARY KEY (station_id, timestamp, component)
);

-- Useful checks
-- SELECT COUNT(*), COUNT(value), MIN(timestamp), MAX(timestamp) FROM sensor_readings;
-- SELECT * FROM sensor_readings ORDER BY timestamp DESC LIMIT 10;
