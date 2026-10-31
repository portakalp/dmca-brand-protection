#!/usr/bin/env bash
# Prepares the Cloud SQL database for the migrations (architecture.md §17, §22; K78, K81), as
# kalkan_owner through cloud-sql-proxy (compose service psql):
#   1. the extensions the schema uses (citext, pg_trgm), which no migration creates;
#   2. sql/bootstrap_roles.sql: creates kalkan_api and kalkan_worker if missing and sets their
#      passwords from Secret Manager (db-api-password, db-worker-password).
# Safe to re-run. deploy.sh runs it before every migration; run it alone after a password
# rotation (infra/terraform/README.md): sudo bash /opt/kalkan/vm/bin/db-bootstrap.sh
set -euo pipefail

readonly ROOT=${KALKAN_ROOT:-/opt/kalkan}
readonly RUN_DIR=${KALKAN_RUN_DIR:-/run/kalkan}

[[ ":$PATH:" == *:/snap/bin:* ]] || PATH="$PATH:/snap/bin"
export HOME=/root

log() { echo "db-bootstrap: $*" >&2; }
die() {
  log "error: $*"
  exit 1
}

(( EUID == 0 )) || die "run as root"
[[ -f $ROOT/docker-compose.yml && -f $ROOT/sql/bootstrap_roles.sql ]] ||
  die "no deployed bundle in $ROOT"

# Always fresh: after a rotation the passwords in Secret Manager are the new ones.
bash "$ROOT/vm/bin/load-secrets.sh" psql
for key in PGPASSWORD DB_API_PASSWORD DB_WORKER_PASSWORD; do
  grep -q "^$key=." "$RUN_DIR/psql.env" ||
    die "$key is empty: its secret has no version (Terraform writes db-*-password)"
done

# The passwords stay in the psql container's environment (psql.env): psql copies them into its
# variables with \getenv, so they appear in no command line, on the host or in the container.
# One session, in order; ON_ERROR_STOP ends it at the first error.
log "creating extensions and application roles"
docker compose -f "$ROOT/docker-compose.yml" run --rm -T psql psql -X -q -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS citext" \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" \
  -c '\getenv api_password DB_API_PASSWORD' \
  -c '\getenv worker_password DB_WORKER_PASSWORD' \
  -f /sql/bootstrap_roles.sql
log "done"
