#!/usr/bin/env bash
# Self-hosted Postgres: runs once, when the postgres-data volume is empty.
#
# Creates kalkan_owner (CREATEROLE, not a superuser) as the owner of the `kalkan` database,
# installs the extensions and runs backend/sql/bootstrap_roles.sql as kalkan_owner, which creates
# kalkan_api and kalkan_worker with their row-level-security roles. The same steps as the local
# stack's infra/compose/postgres/initdb, without the tests' kalkan_test database.
set -euo pipefail

for name in POSTGRES_PASSWORD DB_OWNER_PASSWORD DB_API_PASSWORD DB_WORKER_PASSWORD; do
  value=${!name:-}
  # init-env.sh generates real values; the .env.example placeholders must never become a password.
  if [[ -z $value || $value == change-me* ]]; then
    echo "10-kalkan.sh: $name is empty or still a placeholder; run ./init-env.sh" >&2
    exit 1
  fi
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set owner_password="$DB_OWNER_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE kalkan_owner LOGIN CREATEROLE PASSWORD %L', :'owner_password')
\gexec
CREATE DATABASE kalkan OWNER kalkan_owner;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname kalkan <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;
SQL

psql -v ON_ERROR_STOP=1 --username kalkan_owner --dbname kalkan \
  --set api_password="$DB_API_PASSWORD" \
  --set worker_password="$DB_WORKER_PASSWORD" \
  --file /opt/kalkan/sql/bootstrap_roles.sql
