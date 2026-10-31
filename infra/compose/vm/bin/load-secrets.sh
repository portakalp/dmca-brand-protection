#!/usr/bin/env bash
# Fetches the VM's secrets from Secret Manager into per-service env files and renders the
# Alertmanager configuration and the Redis ACL file (architecture.md §26; K81-K83). Runs as
# root on the VM: kalkan.service before every start, deploy.sh and db-bootstrap.sh. Safe to
# re-run.
#
#   load-secrets.sh [service ...]     default: every service in vm/secrets.map
#   load-secrets.sh --print-redis-acl APP_SHA256 BROWSER_SHA256
#                                     print the Redis ACL file for these password hashes (tests)
#
# Each service gets /run/kalkan/<service>.env (tmpfs, 0600 root) with only its own secrets. A
# secret without an enabled version is skipped with a warning; any other error (permissions,
# network after retries, a multi-line value) fails the run, and so does a missing Redis password.
# Only names are printed, never values.
set -euo pipefail
set +x # values pass through variables; never trace them
shopt -u patsub_replacement 2>/dev/null || true

readonly ROOT=${KALKAN_ROOT:-/opt/kalkan}
readonly MAP=${KALKAN_SECRETS_MAP:-$ROOT/vm/secrets.map}
readonly RUN_DIR=${KALKAN_RUN_DIR:-/run/kalkan}
# Alertmanager runs as nobody (65534) in its image, Redis as redis (999).
readonly ALERTMANAGER_UID=65534
readonly REDIS_UID=999

# The gcloud CLI is a snap on Ubuntu's Compute Engine images.
[[ ":$PATH:" == *:/snap/bin:* ]] || PATH="$PATH:/snap/bin"
umask 077

log() { echo "load-secrets: $*" >&2; }
die() {
  log "error: $*"
  exit 1
}

# --- Redis ACL users --------------------------------------------------------------------------
# Redis ACL files take no comments, so the users are described here:
#   default  off: every connection must authenticate
#   kalkan   the application containers (redis-url): every key, channel and command
#   browser  worker-browser (redis-browser-url, §24; K82): only what Dramatiq's
#            Redis broker (2.2.x) runs for consuming, acking, nacking, requeueing, delayed
#            messages, heartbeats and maintenance (EVALSHA of dispatch.lua and maxstack.lua, SCRIPT
#            LOAD when Redis lost them, and the commands those scripts call; KEYS is one of them),
#            on the dramatiq key (the scripts' KEYS[1]) and dramatiq:* only; redis-py's CLIENT
#            SETINFO on connect; and, through a selector, GET, INCRBY and EXPIRE on the daily
#            proxy byte counters kalkan:proxy:*. No other key (capture:* job records,
#            dramatiq-results:*), no pub/sub channel, no transaction, admin or server command.
# backend/tests/integration/test_redis_acl.py runs Dramatiq against these rules.
# Only its own queue (capture) and the one it sends to (persist), plus Dramatiq's heartbeat and
# ack keys; worker-default still accepts nothing on persist but persist_capture (QueueGuard).
readonly REDIS_BROWSER_RULES="resetchannels -@all +evalsha +script|load +client|setinfo \
+zadd +zrangebyscore +zrem +sadd +srem +smembers +hset +hget +hmget +hexists +hdel +rpush +lpop \
+del +keys ~dramatiq ~dramatiq:__heartbeats__ ~dramatiq:__acks__.*.capture \
~dramatiq:__acks__.*.capture.DQ ~dramatiq:__acks__.*.persist ~dramatiq:capture ~dramatiq:capture.* \
~dramatiq:persist ~dramatiq:persist.* (%RW~kalkan:proxy:* +get +incrby +expire)"

# redis_acl_file APP_SHA256 BROWSER_SHA256: the ACL file's lines (password hashes only).
redis_acl_file() {
  echo "user default off"
  echo "user kalkan on #$1 ~* &* +@all"
  echo "user browser on #$2 $REDIS_BROWSER_RULES"
}

if [[ ${1:-} == --print-redis-acl ]]; then
  [[ $# -eq 3 && $2 =~ ^[0-9a-f]{64}$ && $3 =~ ^[0-9a-f]{64}$ ]] ||
    die "usage: load-secrets.sh --print-redis-acl APP_SHA256 BROWSER_SHA256"
  redis_acl_file "$2" "$3"
  exit 0
fi

# Value of KEY in a KEY=value file (last one wins), read as text and never evaluated.
file_value() {
  [[ -f $1 ]] || return 0
  sed -n "s/^$2=//p" "$1" | tail -n 1
}

(( EUID == 0 )) || die "run as root"
[[ -f $MAP ]] || die "$MAP is missing"

project=$(file_value "$ROOT/.env" GCP_PROJECT_ID)
if [[ -z $project ]]; then
  project=$(curl -fsS --retry 3 -H 'Metadata-Flavor: Google' \
    http://metadata.google.internal/computeMetadata/v1/project/project-id) ||
    die "GCP_PROJECT_ID is not in $ROOT/.env and the metadata server did not answer"
fi
[[ $project =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "invalid project id: $project"

# --- vm/secrets.map ---------------------------------------------------------------------------
declare -a services=()
declare -A pairs=()
while read -r service pair rest; do
  [[ -z $service || $service == \#* ]] && continue
  [[ -z $rest && $service =~ ^[a-z][a-z0-9-]*$ && $pair =~ ^[A-Z][A-Z0-9_]*=[a-z0-9][a-z0-9_-]*$ ]] ||
    die "$MAP: cannot read the line for '$service'"
  [[ -v "pairs[$service]" ]] || services+=("$service")
  pairs[$service]+="$pair "
done <"$MAP"

declare -A wanted=()
for service in "$@"; do
  [[ -v "pairs[$service]" ]] || die "unknown service: $service (not in $MAP)"
  wanted[$service]=1
done
selected() { (( ${#wanted[@]} == 0 )) || [[ -v "wanted[$1]" ]]; }

# --- Secret Manager ---------------------------------------------------------------------------
declare -A cache=() absent=()
err_file=$(mktemp)
trap 'rm -f "$err_file"' EXIT

# fetch SECRET_ID: 0 with the value in REPLY, 3 when the secret has no enabled version, 1 on error.
fetch() {
  local id=$1 attempt
  if [[ -v "cache[$id]" ]]; then
    REPLY=${cache[$id]}
    return 0
  fi
  [[ -v "absent[$id]" ]] && return 3
  for attempt in 1 2 3; do
    if REPLY=$(gcloud secrets versions access latest --secret="$id" --project="$project" \
      --quiet 2>"$err_file"); then
      cache[$id]=$REPLY
      return 0
    fi
    # No version yet, or the newest one is disabled or destroyed.
    if grep -qE 'NOT_FOUND|FAILED_PRECONDITION' "$err_file"; then
      absent[$id]=1
      return 3
    fi
    (( attempt == 3 )) || sleep $((attempt * 5))
  done
  log "$id: $(head -n 3 "$err_file" | tr '\n' ' ')"
  return 1
}

install -d -m 0700 -o root -g root "$RUN_DIR"
chmod 0700 "$RUN_DIR"

failed=0
for service in "${services[@]}"; do
  selected "$service" || continue
  tmp=$(mktemp "$RUN_DIR/.$service.env.XXXXXX")
  written=() skipped=()
  read -ra service_pairs <<<"${pairs[$service]}"
  for pair in "${service_pairs[@]}"; do
    var=${pair%%=*}
    id=${pair#*=}
    status=0
    fetch "$id" || status=$?
    case $status in
      0)
        value=${REPLY%$'\r'}
        if [[ -z $value ]]; then
          skipped+=("$id (empty)")
        elif [[ $value == *[$'\n\r']* ]]; then
          log "$id: multi-line values are not supported (store JSON on one line)"
          failed=1
        else
          printf '%s=%s\n' "$var" "$value" >>"$tmp"
          written+=("$var")
        fi
        ;;
      3) skipped+=("$id") ;;
      *) failed=1 ;;
    esac
  done
  chmod 0600 "$tmp"
  mv -f "$tmp" "$RUN_DIR/$service.env"
  message="$service: ${#written[@]} set"
  if (( ${#skipped[@]} > 0 )); then
    message+="; no enabled version, skipped: ${skipped[*]}"
  fi
  log "$message"
done

# Files of services that left the map (full runs only).
if (( ${#wanted[@]} == 0 )); then
  for file in "$RUN_DIR"/*.env; do
    [[ -e $file ]] || continue
    name=${file##*/}
    [[ -v "pairs[${name%.env}]" ]] || rm -f "$file"
  done
fi

# --- Alertmanager -----------------------------------------------------------------------------
# Alertmanager does not expand variables and has no file option for the SMTP user name, so the
# configuration is rendered here by vm/bin/render-alertmanager.sh (template
# vm/alertmanager/alertmanager.yml.tmpl). The SMTP password is a secret (alert-smtp-password); the
# relay, user name, sender and recipient are deploy settings in app.env (ALERT_SMTP_SMARTHOST,
# ALERT_SMTP_USERNAME, ALERT_SMTP_REQUIRE_TLS, ALERT_MAIL_FROM or MAIL_FROM, OPS_ALERT_EMAIL). A
# value mapped in secrets.map for alertmanager wins over app.env.
render_alertmanager() {
  local template=$ROOT/vm/alertmanager/alertmanager.yml.tmpl dir=$RUN_DIR/alertmanager
  local renderer=$ROOT/vm/bin/render-alertmanager.sh name value file
  local -a settings=()
  [[ -f $template ]] || die "$template is missing"
  [[ -f $renderer ]] || die "$renderer is missing"
  for name in ALERT_SMTP_SMARTHOST ALERT_SMTP_USERNAME ALERT_SMTP_PASSWORD ALERT_SMTP_REQUIRE_TLS \
    ALERT_MAIL_FROM MAIL_FROM OPS_ALERT_EMAIL; do
    value=""
    for file in "$RUN_DIR/alertmanager.env" "$ROOT/app.env"; do
      value=$(file_value "$file" "$name")
      if [[ -n $value ]]; then break; fi
    done
    settings+=("$name=$value")
  done

  mkdir -p "$dir"
  chown "$ALERTMANAGER_UID:$ALERTMANAGER_UID" "$dir"
  chmod 0500 "$dir"
  # export is a builtin: the values reach the renderer through its environment (readable by root
  # only), never through a command line. Every name is exported, empty ones included, so nothing
  # from the caller's environment leaks in.
  (
    for value in "${settings[@]}"; do
      export "${value?}"
    done
    export ALERT_CONFIG_DIR=/etc/alertmanager/kalkan
    exec sh "$renderer" "$template" "$dir"
  ) || die "could not render the Alertmanager configuration"
  chown "$ALERTMANAGER_UID:$ALERTMANAGER_UID" "$dir/alertmanager.yml" "$dir/smtp-password"
  # The password now lives only in the rendered file.
  rm -f "$RUN_DIR/alertmanager.env"
}

# --- Redis ACL --------------------------------------------------------------------------------
# Redis reads the file at start as /etc/redis/users.acl (RUN_DIR/redis mounted read-only);
# deploy.sh restarts it when the file changed. The file holds SHA-256 hashes (#<hex>), never a
# password; the users are described at redis_acl_file above.
render_redis_acl() {
  local dir=$RUN_DIR/redis app browser tmp
  app=$(file_value "$RUN_DIR/redis.env" REDIS_APP_PASSWORD)
  browser=$(file_value "$RUN_DIR/redis.env" REDIS_BROWSER_PASSWORD)
  # The passwords live on only as hashes and inside the REDIS_URL secrets.
  rm -f "$RUN_DIR/redis.env"
  if [[ -z $app || -z $browser ]]; then
    log "redis-app-password and redis-browser-password need a version (Terraform writes them);" \
      "Redis does not start without its ACL file"
    failed=1
    return 0
  fi

  mkdir -p "$dir"
  chown "$REDIS_UID:$REDIS_UID" "$dir"
  chmod 0500 "$dir"
  tmp=$(mktemp "$dir/.users.acl.XXXXXX")
  redis_acl_file "$(sha256_hex "$app")" "$(sha256_hex "$browser")" >"$tmp"
  chown "$REDIS_UID:$REDIS_UID" "$tmp"
  chmod 0400 "$tmp"
  mv -f "$tmp" "$dir/users.acl"
  log "redis: ACL file with users kalkan and browser"
}

# printf is a builtin: the value reaches sha256sum through a pipe, never a command line.
sha256_hex() { printf '%s' "$1" | sha256sum | cut -d ' ' -f 1; }

if selected alertmanager; then
  render_alertmanager
fi
if selected redis; then
  render_redis_acl
fi

(( failed == 0 )) || die "some secrets could not be read (see above)"
