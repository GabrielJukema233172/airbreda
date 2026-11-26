-- AirBreda - Day 4: a read-only database login for the dashboard.
-- The ingesters own sensor_readings (INSERT/UPDATE, schema changes); the
-- dashboard only ever SELECTs, so a bug or injection in the web-facing
-- container can't change or delete readings.
--
-- Run by infra/vm/deploy.sh (idempotent):
--   docker exec -i db psql -U airbreda -d airbreda -v pw="$DASHBOARD_DB_PASSWORD" < sql/dashboard_role.sql
SELECT 'CREATE ROLE dashboard_ro LOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dashboard_ro') \gexec
ALTER ROLE dashboard_ro WITH LOGIN PASSWORD :'pw';
GRANT CONNECT ON DATABASE airbreda TO dashboard_ro;
GRANT USAGE ON SCHEMA public TO dashboard_ro;
GRANT SELECT ON sensor_readings TO dashboard_ro;
