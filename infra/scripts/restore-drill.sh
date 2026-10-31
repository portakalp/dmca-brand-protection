#!/usr/bin/env bash
# Restore drill for the optional Google Cloud reference deployment (architecture.md §24 "Data
# loss", docs/operations.md "Restore drill"): restores a Cloud SQL backup of kalkan-db into a new,
# temporary instance, checks the data and deletes only that temporary instance. kalkan-db itself
# is only read (its backup list and settings).
#
#   PROJECT_ID=my-gcp-project infra/scripts/restore-drill.sh
#   PROJECT_ID=my-gcp-project BACKUP_ID=1730000000000 KEEP=1 infra/scripts/restore-drill.sh
#
# Needs gcloud signed in as the operator; for the checks also psql and cloud-sql-proxy (v2) on
# PATH (without them the checks are skipped and printed). Prints how long every step took.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID, e.g. PROJECT_ID=my-gcp-project}"
REGION="${REGION:-europe-central2}"
SOURCE="${SOURCE:-kalkan-db}"
DRILL="${DRILL:-kalkan-db-drill-$(date -u +%Y%m%d-%H%M)}"
PORT="${PORT:-54329}"
KEEP="${KEEP:-0}"

case "$DRILL" in
  kalkan-db-drill-*) ;;
  *) echo "restore-drill: the drill instance name must start with kalkan-db-drill-" >&2; exit 1 ;;
esac

gcloud_() { gcloud --project="$PROJECT_ID" "$@"; }

step_started=0
step() { step_started=$SECONDS; echo; echo "== $*"; }
took() { echo "   took $((SECONDS - step_started)) s"; TIMES+=("$1: $((SECONDS - step_started)) s"); }
TIMES=()

step "Backup to restore"
if [[ -z "${BACKUP_ID:-}" ]]; then
  BACKUP_ID=$(gcloud_ sql backups list --instance="$SOURCE" --filter='status=SUCCESSFUL' \
    --sort-by='~windowStartTime' --limit=1 --format='value(id)')
fi
[[ -n "$BACKUP_ID" ]] || { echo "restore-drill: no successful backup of $SOURCE" >&2; exit 1; }
gcloud_ sql backups describe "$BACKUP_ID" --instance="$SOURCE" \
  --format='table(id, type, status, windowStartTime, endTime)'
VERSION=$(gcloud_ sql instances describe "$SOURCE" --format='value(databaseVersion)')
TIER=$(gcloud_ sql instances describe "$SOURCE" --format='value(settings.tier)')
EDITION=$(gcloud_ sql instances describe "$SOURCE" --format='value(settings.edition)')
DISK_GB=$(gcloud_ sql instances describe "$SOURCE" --format='value(settings.dataDiskSizeGb)')

cat <<EOF

Plan:
  1. create $DRILL ($VERSION, ${EDITION:-ENTERPRISE}, $TIER, ${DISK_GB} GB SSD, $REGION;
     no backups, no authorized networks, encrypted connections only, no deletion protection)
  2. restore backup $BACKUP_ID of $SOURCE into $DRILL (kalkan-db is not changed)
  3. count rows through cloud-sql-proxy on 127.0.0.1:$PORT as kalkan_owner
  4. delete $DRILL (KEEP=1 keeps it)
The drill instance costs a few cents per hour while it exists.
EOF
read -r -p "Type 'drill' to go on: " answer
[[ "$answer" == "drill" ]] || { echo "restore-drill: stopped"; exit 1; }
total_started=$SECONDS

step "Create $DRILL"
gcloud_ sql instances create "$DRILL" \
  --region="$REGION" \
  --database-version="$VERSION" \
  --edition="$(tr '[:upper:]' '[:lower:]' <<<"${EDITION:-ENTERPRISE}")" \
  --tier="$TIER" \
  --storage-type=SSD \
  --storage-size="${DISK_GB}GB" \
  --no-backup \
  --ssl-mode=ENCRYPTED_ONLY \
  --no-deletion-protection
took "create"

step "Restore backup $BACKUP_ID into $DRILL"
gcloud_ sql backups restore "$BACKUP_ID" --restore-instance="$DRILL" --backup-instance="$SOURCE" \
  --quiet
took "restore"

step "Check the restored data"
if command -v cloud-sql-proxy >/dev/null && command -v psql >/dev/null; then
  cloud-sql-proxy --port "$PORT" "$PROJECT_ID:$REGION:$DRILL" >/dev/null 2>&1 &
  proxy=$!
  trap 'kill "$proxy" 2>/dev/null || true' EXIT
  sleep 5
  # kalkan_owner's password is the one of the backup's time; the secret's latest version
  # matches unless the password was rotated since.
  PGPASSWORD=$(gcloud_ secrets versions access latest --secret=db-owner-password)
  export PGPASSWORD
  psql "host=127.0.0.1 port=$PORT dbname=kalkan user=kalkan_owner sslmode=disable" \
    -v ON_ERROR_STOP=1 -c "
    SELECT 'customers' AS rows, count(*) FROM customers
    UNION ALL SELECT 'cases', count(*) FROM cases
    UNION ALL SELECT 'notices', count(*) FROM notices
    UNION ALL SELECT 'artifacts', count(*) FROM artifacts
    UNION ALL SELECT 'audit_log', count(*) FROM audit_log;" \
    -c "SELECT max(created_at) AS newest_case FROM cases;" \
    -c "SELECT version_num AS migration FROM alembic_version;"
  unset PGPASSWORD
  kill "$proxy" 2>/dev/null || true
  trap - EXIT
else
  echo "   psql or cloud-sql-proxy is missing; check by hand:"
  echo "   cloud-sql-proxy --port $PORT $PROJECT_ID:$REGION:$DRILL"
  echo "   psql 'host=127.0.0.1 port=$PORT dbname=kalkan user=kalkan_owner' -c 'SELECT count(*) FROM cases'"
fi
took "check"

if [[ "$KEEP" == "1" ]]; then
  echo
  echo "KEEP=1: $DRILL stays. Delete it later with:"
  echo "  gcloud --project=$PROJECT_ID sql instances delete $DRILL"
else
  step "Delete $DRILL"
  gcloud_ sql instances delete "$DRILL" --quiet
  took "delete"
fi

echo
echo "Restore drill of backup $BACKUP_ID: $((SECONDS - total_started)) s in all"
printf '  %s\n' "${TIMES[@]}"
echo "Record these times (docs/operations.md, \"Restore drill\")."
