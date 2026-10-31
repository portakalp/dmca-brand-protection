#!/usr/bin/env bash
# Deploys a bundle on the VM (architecture.md §22; K81). The deploy workflow unpacks the bundle
# into /tmp/kalkan-bundle and runs, as root over IAP SSH:
#
#   bash /tmp/kalkan-bundle/vm/bin/deploy.sh <image_tag> [--skip-migrations]
#
# Run by hand as /opt/kalkan/vm/bin/deploy.sh it redeploys the installed bundle with another tag.
# That is for emergencies only: it leaves Cloud Run untouched, so the api may then run another
# image than the VM; deploys and rollbacks go through the deploy workflow.
# Steps: copy the bundle into /opt/kalkan (volumes and /run/kalkan untouched), systemd units,
# firewall, secrets, image pull, db-bootstrap and migrations, docker compose up, config reloads,
# image prune, status. --skip-migrations is for rollbacks: an older image's alembic fails on a
# newer schema, and schema downgrades are manual. Safe to re-run.
set -euo pipefail

readonly ROOT=/opt/kalkan
readonly REDIS_ACL=/run/kalkan/redis/users.acl
readonly ARTIFACTS_DIR=/var/lib/kalkan/artifacts-tmp
# The kalkan user of the backend image and pwuser of the browser image share this uid.
readonly APP_UID=10001

[[ ":$PATH:" == *:/snap/bin:* ]] || PATH="$PATH:/snap/bin"
# Root's Docker credential helper for Artifact Registry and gcloud's configuration live here.
export HOME=/root

log() { printf '%s deploy: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() {
  log "error: $*"
  exit 1
}
usage() {
  echo "usage: deploy.sh <image_tag> [--skip-migrations]" >&2
  exit 2
}

image_tag=""
skip_migrations=0
for arg in "$@"; do
  case $arg in
    --skip-migrations) skip_migrations=1 ;;
    -*) usage ;;
    *)
      [[ -z $image_tag ]] || usage
      image_tag=$arg
      ;;
  esac
done
[[ -n $image_tag ]] || usage
[[ $image_tag =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] || die "invalid image tag: $image_tag"
(( EUID == 0 )) || die "run as root (sudo)"

bundle=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)
exec 9>/run/lock/kalkan-deploy.lock
flock -n 9 || die "another deploy is running"

compose() { docker compose -f "$ROOT/docker-compose.yml" "$@"; }

# Value of KEY in a KEY=value file (last one wins), read as text and never evaluated.
file_value() {
  [[ -f $1 ]] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1
}

# Digest of files below ROOT, to restart a service only when its configuration changed.
config_digest() {
  (cd "$ROOT" 2>/dev/null && find "$@" -type f -print0 2>/dev/null | sort -z |
    xargs -0r sha256sum || true) |
    sha256sum | cut -d ' ' -f 1
}

file_digest() { sha256sum "$1" 2>/dev/null | cut -d ' ' -f 1 || true; }

# The bundle's app.env (deploy settings appended), else the static one.
bundle_app_env() {
  if [[ -f $bundle/app.env ]]; then
    echo "$bundle/app.env"
  else
    echo "$bundle/vm/app.env"
  fi
}

sync_bundle() {
  log "installing the bundle from $bundle into $ROOT"
  install -d -m 0755 -o root -g root "$ROOT" "$ROOT/vm" "$ROOT/sql"
  # In place, file by file: running containers bind-mount files below vm/, and replacing a file
  # or directory (new inode) would detach their mounts.
  rsync -rlt --checksum --inplace --delete "$bundle/vm/" "$ROOT/vm/"
  rsync -rlt --checksum --inplace --delete "$bundle/sql/" "$ROOT/sql/"
  install -m 0644 -o root -g root "$bundle/docker-compose.yml" "$ROOT/docker-compose.yml"
  install -m 0644 -o root -g root "$bundle/.env" "$ROOT/.env"
  install -m 0644 -o root -g root "$(bundle_app_env)" "$ROOT/app.env"
  # Readable by the containers' users (Prometheus and Alertmanager run as nobody, Grafana as 472).
  chown -R root:root "$ROOT/vm" "$ROOT/sql"
  find "$ROOT/vm" "$ROOT/sql" -type d -exec chmod 0755 {} +
  find "$ROOT/vm" "$ROOT/sql" -type f -exec chmod 0644 {} +
  chmod 0755 "$ROOT"/vm/bin/*.sh
}

set_image_tag() {
  local tmp
  tmp=$(mktemp "$ROOT/.env.XXXXXX")
  grep -v '^IMAGE_TAG=' "$ROOT/.env" >"$tmp" || true
  printf 'IMAGE_TAG=%s\n' "$image_tag" >>"$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$ROOT/.env"
}

# Checks a bundle before anything is installed: check_bundle DIR APP_ENV
check_bundle() {
  local dir=$1 app_env=$2 file key secret_vars
  for file in docker-compose.yml .env sql/bootstrap_roles.sql vm/secrets.map vm/bin/deploy.sh \
    vm/systemd/kalkan.service; do
    [[ -f $dir/$file ]] || die "the bundle in $dir has no $file"
  done
  for key in REGISTRY CLOUDSQL_INSTANCE GCP_PROJECT_ID; do
    [[ -n $(file_value "$dir/.env" "$key") ]] || die "$dir/.env has no $key"
  done
  [[ -f $app_env ]] || die "$app_env is missing"
  grep -qx 'ENV=prod' "$app_env" || die "app.env must set ENV=prod"
  # Fail closed: test-only and local-only settings never reach prod (§26), and secrets never
  # travel in app.env (every container on the VM reads it, except worker-browser).
  secret_vars=$(awk '!/^#/ && NF == 2 { sub(/=.*/, "", $2); print $2 }' "$dir/vm/secrets.map")
  while IFS= read -r key; do
    case $key in
      SSRF_ALLOW_CIDRS | STORAGE_EMULATOR_HOST | SMTP_URL | DATABASE_URL_*)
        die "app.env must not set $key"
        ;;
    esac
    if grep -qx "$key" <<<"$secret_vars"; then
      die "app.env must not carry the secret $key (vm/secrets.map)"
    fi
  done < <(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$app_env")
}

if (( skip_migrations )); then
  log "deploying $image_tag without db-bootstrap and migrations"
else
  log "deploying $image_tag"
fi

first_deploy=1
[[ -f $ROOT/docker-compose.yml ]] && first_deploy=0
certstream_before=$(config_digest vm/certstream.yaml)
grafana_before=$(config_digest vm/grafana)
redis_acl_before=$(file_digest "$REDIS_ACL")

if [[ $bundle -ef $ROOT ]]; then
  log "redeploying the installed bundle"
  check_bundle "$ROOT" "$ROOT/app.env"
else
  check_bundle "$bundle" "$(bundle_app_env)"
  sync_bundle
fi
set_image_tag

mkdir -p "$ARTIFACTS_DIR"
chown "$APP_UID:$APP_UID" "$ARTIFACTS_DIR"
chmod 0770 "$ARTIFACTS_DIR"

bash "$ROOT/vm/bin/install-units.sh"
bash "$ROOT/vm/bin/firewall.sh"
bash "$ROOT/vm/bin/load-secrets.sh"

log "pulling images"
compose pull --quiet
if (( ! skip_migrations )); then
  compose pull --quiet migrate psql
  bash "$ROOT/vm/bin/db-bootstrap.sh"
  log "running migrations"
  compose run --rm -T migrate
fi

# Redis is restarted rather than sent ACL LOAD, which would need a password inside the container.
# After a Redis password rotation the containers that
# compose up recreates (their REDIS_URL changed) need the new users; queued jobs are lost, as on
# any Redis restart.
if (( ! first_deploy )) && [[ $(file_digest "$REDIS_ACL") != "$redis_acl_before" ]]; then
  log "the Redis ACL changed: restarting redis"
  compose restart redis
fi

log "starting the stack"
if ! compose up -d --remove-orphans --wait --wait-timeout 300; then
  compose ps -a >&2 || true
  die "a service did not start or become healthy; on the VM: docker compose -f $ROOT/docker-compose.yml logs <service>"
fi

# Prometheus and Alertmanager reload their configuration (Alertmanager's is re-rendered by
# load-secrets.sh); certstream and Grafana read theirs only at start.
compose kill -s SIGHUP prometheus alertmanager >/dev/null
if (( ! first_deploy )); then
  [[ $(config_digest vm/certstream.yaml) == "$certstream_before" ]] || compose restart certstream
  [[ $(config_digest vm/grafana) == "$grafana_before" ]] || compose restart grafana
fi

# Boots go through kalkan.service; make sure it is active after the first deploy.
systemctl is-active --quiet kalkan.service || systemctl start kalkan.service

# Images not used by a container and older than a week (rollbacks pull again).
log "$(docker image prune -af --filter until=168h | tail -n 1)"
compose ps --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}'
log "deployed $image_tag"
