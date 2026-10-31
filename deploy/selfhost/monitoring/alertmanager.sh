#!/bin/sh
# Self-hosted Alertmanager: renders its configuration from the environment, then starts it.
#
# Alertmanager does not expand variables in its file. The reference VM's renderer
# (infra/compose/vm/bin/render-alertmanager.sh, mounted read-only with its template) writes the
# configuration and the SMTP password file to a tmpfs. Alarms go by plain SMTP through any
# provider: ALERT_SMTP_SMARTHOST (host:port), ALERT_SMTP_USERNAME and ALERT_SMTP_PASSWORD,
# ALERT_MAIL_FROM, to OPS_ALERT_EMAIL. Without them alarms are dropped and the log says why.
set -eu

dir=/run/alertmanager
sh /etc/alertmanager/kalkan/render-alertmanager.sh \
  /etc/alertmanager/kalkan/alertmanager.yml.tmpl "$dir"
unset ALERT_SMTP_PASSWORD

exec /bin/alertmanager --config.file="$dir/alertmanager.yml" --storage.path=/alertmanager \
  --cluster.listen-address= "$@"
