#!/usr/bin/env bash
# Creates .env from .env.example with random secrets in place of every change-me value.
# Refuses to overwrite an existing .env: the database and TOTP secrets in it cannot be replaced
# once the stack has started. Needs only bash, od and /dev/urandom (no openssl, no Python).
set -euo pipefail
umask 077
cd "$(dirname "$0")"

if [[ -e .env ]]; then
  echo "init-env.sh: .env already exists; edit it instead" >&2
  exit 1
fi

random_hex() { od -An -vtx1 -N32 /dev/urandom | tr -d ' \n'; }

# A Fernet key: 32 random bytes in URL-safe base64.
fernet_key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; }

tmp=$(mktemp .env.XXXXXX)
trap 'rm -f "$tmp"' EXIT
while IFS= read -r line || [[ -n $line ]]; do
  case $line in
    TOTP_ENCRYPTION_KEY=change-me) line="TOTP_ENCRYPTION_KEY=$(fernet_key)" ;;
    *=change-me) line="${line%%=*}=$(random_hex)" ;;
  esac
  printf '%s\n' "$line"
done <.env.example >"$tmp"
mv "$tmp" .env
trap - EXIT
echo "init-env.sh: wrote .env with new secrets; now set APP_DOMAIN, EVIDENCE_DOMAIN, ACME_EMAIL,"
echo "SMTP_URL, MAIL_FROM and DRY_RUN_REDIRECT_EMAIL in it, then: docker compose up -d --build"
