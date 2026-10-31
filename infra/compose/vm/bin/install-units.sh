#!/usr/bin/env bash
# Installs the systemd units of the deployed bundle (vm/systemd) and enables them (architecture.md
# K81). deploy.sh runs it on every deploy, the startup script on every boot. Idempotent.
set -euo pipefail

readonly ROOT=${KALKAN_ROOT:-/opt/kalkan}
readonly SOURCE=$ROOT/vm/systemd
readonly TARGET=/etc/systemd/system

(( EUID == 0 )) || {
  echo "install-units: run as root" >&2
  exit 1
}

units=()
changed=()
for file in "$SOURCE"/*.service; do
  [[ -e $file ]] || continue
  unit=${file##*/}
  units+=("$unit")
  if ! cmp -s "$file" "$TARGET/$unit"; then
    install -m 0644 -o root -g root "$file" "$TARGET/$unit"
    echo "install-units: installed $unit"
    changed+=("$unit")
  fi
done
(( ${#units[@]} > 0 )) || {
  echo "install-units: no units in $SOURCE" >&2
  exit 1
}
if (( ${#changed[@]} > 0 )); then
  systemctl daemon-reload
  # Recreates the [Install] links of changed units and drops the ones an older [Install] section
  # made (such as WantedBy= links after a change to RequiredBy=).
  systemctl reenable --quiet "${changed[@]}"
fi
systemctl enable --quiet "${units[@]}"
