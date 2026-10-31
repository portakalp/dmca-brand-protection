#!/bin/sh
# Renders vm/alertmanager/alertmanager.yml.tmpl from environment variables (see the template for
# the list) into OUT_DIR/alertmanager.yml and OUT_DIR/smtp-password. POSIX sh on purpose: the
# reference VM runs it from load-secrets.sh, and a plain Docker Compose host can run it inside
# the busybox-based Alertmanager image before Alertmanager starts, with no other tooling.
#
#   render-alertmanager.sh TEMPLATE OUT_DIR
#
# ALERT_CONFIG_DIR is the directory Alertmanager sees OUT_DIR as (default: OUT_DIR); the
# configuration points at the password file there. Missing or malformed settings never fail the
# run: the output is a configuration that drops alerts, and the reason is logged, so monitoring
# still starts before mail is set up. Values are checked against plain forms before they reach
# the YAML (no quotes, no newlines); the password only ever goes to the password file. Only
# names are logged, never values.
set -eu

log() { echo "render-alertmanager: $*" >&2; }

[ $# -eq 2 ] || {
  log "usage: render-alertmanager.sh TEMPLATE OUT_DIR"
  exit 2
}
template=$1
out=$2
[ -f "$template" ] || {
  log "error: $template is missing"
  exit 1
}
[ -d "$out" ] || {
  log "error: $out is not a directory"
  exit 1
}
config_dir=${ALERT_CONFIG_DIR:-$out}

smarthost=${ALERT_SMTP_SMARTHOST:-}
username=${ALERT_SMTP_USERNAME:-}
password=${ALERT_SMTP_PASSWORD:-}
from=${ALERT_MAIL_FROM:-${MAIL_FROM:-}}
to=${OPS_ALERT_EMAIL:-}
require_tls=${ALERT_SMTP_REQUIRE_TLS:-true}

# matches VALUE ERE: grep -E on a single line, so a value with a newline never matches.
matches() { [ "$(printf '%s\n' "$1" | wc -l)" -eq 1 ] && printf '%s\n' "$1" | grep -Eq "$2"; }
email_re='^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'

reason=""
if [ -z "$smarthost" ]; then
  reason="ALERT_SMTP_SMARTHOST is not set"
elif ! matches "$smarthost" '^[A-Za-z0-9.-]+:[0-9]{1,5}$'; then
  reason="ALERT_SMTP_SMARTHOST must be host:port"
elif [ -n "$username" ] && ! matches "$username" '^[A-Za-z0-9._%+@=-]+$'; then
  reason="ALERT_SMTP_USERNAME has characters outside A-Z a-z 0-9 . _ % + @ = -"
elif [ -n "$username" ] && [ -z "$password" ]; then
  reason="ALERT_SMTP_USERNAME is set but ALERT_SMTP_PASSWORD is empty"
elif ! matches "$to" "$email_re"; then
  reason="OPS_ALERT_EMAIL is missing or invalid"
elif ! matches "$from" "$email_re"; then
  reason="ALERT_MAIL_FROM (or MAIL_FROM) is missing or invalid"
elif [ "$require_tls" != true ] && [ "$require_tls" != false ]; then
  reason="ALERT_SMTP_REQUIRE_TLS must be true or false"
elif ! matches "$config_dir" '^/[A-Za-z0-9._/-]*$'; then
  reason="ALERT_CONFIG_DIR must be an absolute path"
fi

umask 077
tmp_config="$out/.alertmanager.yml.$$"
tmp_password="$out/.smtp-password.$$"
trap 'rm -f "$tmp_config" "$tmp_password"' EXIT

# Without a user name there is no AUTH, so no password file content either.
[ -n "$username" ] || password=""

if [ -z "$reason" ]; then
  # No auth lines without a user name: Alertmanager then sends without AUTH.
  if [ -z "$username" ]; then
    grep -v 'smtp_auth_' "$template"
  else
    cat "$template"
  fi | sed \
    -e "s|\${ALERT_SMTP_SMARTHOST}|$smarthost|g" \
    -e "s|\${ALERT_MAIL_FROM}|$from|g" \
    -e "s|\${ALERT_SMTP_USERNAME}|$username|g" \
    -e "s|\${ALERT_SMTP_PASSWORD_FILE}|$config_dir/smtp-password|g" \
    -e "s|\${ALERT_SMTP_REQUIRE_TLS}|$require_tls|g" \
    -e "s|\${OPS_ALERT_EMAIL}|$to|g" >"$tmp_config"
  # shellcheck disable=SC2016 # a literal "${" left over from the template
  if grep -v '^[[:space:]]*#' "$tmp_config" | grep -q '\${'; then
    log "error: $template has a placeholder this script does not know"
    exit 1
  fi
  log "e-mail to OPS_ALERT_EMAIL through $smarthost"
else
  password=""
  cat >"$tmp_config" <<EOF
# Alert e-mail is off: ${reason}.
# Set the ALERT_SMTP_* settings, ALERT_MAIL_FROM and OPS_ALERT_EMAIL
# (vm/alertmanager/alertmanager.yml.tmpl) and render again.
route:
  receiver: drop
receivers:
  - name: drop
EOF
  log "warning: alert e-mail is off: $reason"
fi
# printf is a builtin: the password never appears on a command line.
printf '%s' "$password" >"$tmp_password"
chmod 0400 "$tmp_config" "$tmp_password"
mv -f "$tmp_config" "$out/alertmanager.yml"
mv -f "$tmp_password" "$out/smtp-password"
