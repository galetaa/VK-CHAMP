#!/bin/sh
set -eu
psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v app_password="$CASECHAMP_DB_APP_PASSWORD" -v maintenance_password="$CASECHAMP_DB_MAINTENANCE_PASSWORD" <<'SQL'
CREATE ROLE casechamp_app LOGIN PASSWORD :'app_password' NOINHERIT;
CREATE ROLE casechamp_maintenance LOGIN PASSWORD :'maintenance_password' NOINHERIT;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE casechamp TO casechamp_app, casechamp_maintenance;
SQL
