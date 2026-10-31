#!/usr/bin/env bash
# Self-hosted Redis: renders the ACL file from the two passwords, then starts Redis.
#
# Users (the same as on the reference VM, infra/compose/vm/bin/load-secrets.sh):
#   default  off: every connection must authenticate
#   kalkan   the application containers (REDIS_PASSWORD): every key, channel and command
#   browser  worker-browser (REDIS_BROWSER_PASSWORD): only what Dramatiq needs to consume its
#            capture queue and send to persist, plus the proxy byte counters. A compromised
#            browser cannot read other jobs or queue work for worker-default.
# The rules come from load-secrets.sh --print-redis-acl (mounted read-only), so there is one
# copy of them. The file holds SHA-256 hashes only and lives on a tmpfs.
set -euo pipefail
set +x

acl_dir=/run/redis
for name in REDIS_PASSWORD REDIS_BROWSER_PASSWORD; do
  value=${!name:-}
  if [[ -z $value || $value == change-me* ]]; then
    echo "redis-start: $name is empty or still a placeholder; run ./init-env.sh" >&2
    exit 1
  fi
done
sha256_hex() { printf '%s' "$1" | sha256sum | cut -d ' ' -f 1; }

umask 077
bash /opt/kalkan/load-secrets.sh --print-redis-acl \
  "$(sha256_hex "$REDIS_PASSWORD")" "$(sha256_hex "$REDIS_BROWSER_PASSWORD")" \
  >"$acl_dir/users.acl"
chown -R redis:redis "$acl_dir"
unset REDIS_PASSWORD REDIS_BROWSER_PASSWORD

# docker-entrypoint.sh drops to the redis user. Jobs only: state lives in Postgres, so no
# persistence; writes fail instead of the container being killed when the cap is reached.
exec docker-entrypoint.sh redis-server --aclfile "$acl_dir/users.acl" --save "" \
  --appendonly no --maxmemory "${REDIS_MAXMEMORY:-256mb}" --maxmemory-policy noeviction
