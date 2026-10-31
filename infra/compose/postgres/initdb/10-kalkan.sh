#!/usr/bin/env bash
# Local development only. Runs once, when the postgres data volume is empty.
#
# Creates kalkan_owner (CREATEROLE, not a superuser) as the owner of two databases: `kalkan`
# for development and `kalkan_test` for the integration tests. In each it installs the
# extensions and runs backend/sql/bootstrap_roles.sql as kalkan_owner, the same way
# production does (vm/bin/db-bootstrap.sh, deploy/selfhost).
set -euo pipefail

: "${DB_OWNER_PASSWORD:?DB_OWNER_PASSWORD is required}"
: "${DB_API_PASSWORD:?DB_API_PASSWORD is required}"
: "${DB_WORKER_PASSWORD:?DB_WORKER_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set owner_password="$DB_OWNER_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE kalkan_owner LOGIN CREATEROLE PASSWORD %L', :'owner_password')
\gexec
CREATE DATABASE kalkan OWNER kalkan_owner;
CREATE DATABASE kalkan_test OWNER kalkan_owner;
SQL

for database in kalkan kalkan_test; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;
SQL

  psql -v ON_ERROR_STOP=1 --username kalkan_owner --dbname "$database" \
    --set api_password="$DB_API_PASSWORD" \
    --set worker_password="$DB_WORKER_PASSWORD" \
    --file /opt/kalkan/sql/bootstrap_roles.sql
done
