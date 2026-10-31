#!/usr/bin/env bash
# Startup script of the worker VM kalkan-vm (architecture.md §22; K81). Terraform puts it in
# the instance metadata (startup-script); Compute Engine runs it as root at every boot, so every
# step is idempotent. Rerun by hand: sudo google_metadata_script_runner startup
#
# It installs Docker Engine and the compose plugin from Docker's apt repository (pinned), makes
# sure the gcloud CLI is there, lets root pull from Artifact Registry and creates /opt/kalkan.
# The stack arrives with the first deploy (/opt/kalkan/vm/bin/deploy.sh), which installs the
# systemd units kalkan-firewall.service and kalkan.service; on later boots this script installs
# them again from the deployed bundle and starts them.
set -euo pipefail

# Docker Engine for Ubuntu 24.04 (https://docs.docker.com/engine/install/ubuntu/). The packages are
# held, so unattended upgrades do not move them; change the versions here to upgrade.
readonly UBUNTU_CODENAME=noble
readonly DOCKER_VERSION="5:29.9.1-1~ubuntu.24.04~noble"
readonly CONTAINERD_VERSION="2.4.2-2~ubuntu.24.04~noble"
readonly COMPOSE_VERSION="5.6.0-1~ubuntu.24.04~noble"
# Fingerprint of Docker's apt signing key, as published on the install page.
readonly DOCKER_KEY_FINGERPRINT=9DC858229FC7DD38854AE2D88D81803C0EBFCD88
readonly KALKAN_ROOT=/opt/kalkan

export DEBIAN_FRONTEND=noninteractive
export HOME=/root
# The gcloud CLI is a snap on Ubuntu's Compute Engine images.
[[ ":$PATH:" == *:/snap/bin:* ]] || PATH="$PATH:/snap/bin"

log() { echo "kalkan-startup: $*"; }
die() {
  log "error: $*"
  exit 1
}

apt_get() { apt-get -q -o DPkg::Lock::Timeout=300 "$@"; }

metadata() {
  curl -fsS --retry 10 --retry-connrefused -H 'Metadata-Flavor: Google' \
    "http://metadata.google.internal/computeMetadata/v1/$1"
}

# Version of an installed package; empty when it is not installed.
installed_version() {
  local status
  status=$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$1" 2>/dev/null) || return 0
  [[ $status == installed\ * ]] && echo "${status#installed }"
  return 0
}

install_base_packages() {
  local missing=() package
  for package in ca-certificates curl gnupg rsync; do
    [[ -n $(installed_version "$package") ]] || missing+=("$package")
  done
  if (( ${#missing[@]} > 0 )); then
    log "installing ${missing[*]}"
    apt_get update
    apt_get install -y --no-install-recommends "${missing[@]}"
  fi
}

install_docker() {
  local codename fingerprint
  if [[ $(installed_version docker-ce) == "$DOCKER_VERSION" &&
    $(installed_version docker-ce-cli) == "$DOCKER_VERSION" &&
    $(installed_version containerd.io) == "$CONTAINERD_VERSION" &&
    $(installed_version docker-compose-plugin) == "$COMPOSE_VERSION" ]]; then
    log "Docker ${DOCKER_VERSION} and compose ${COMPOSE_VERSION} are installed"
    return
  fi
  codename=$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release)
  [[ $codename == "$UBUNTU_CODENAME" ]] ||
    die "the pinned Docker packages are for Ubuntu ${UBUNTU_CODENAME}, this is ${codename}"

  log "installing Docker ${DOCKER_VERSION} and compose ${COMPOSE_VERSION}"
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL --retry 5 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc.new
  fingerprint=$(gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc.new |
    awk -F: '$1 == "fpr" { print $10; exit }')
  if [[ $fingerprint != "$DOCKER_KEY_FINGERPRINT" ]]; then
    rm -f /etc/apt/keyrings/docker.asc.new
    die "Docker's signing key has an unexpected fingerprint: ${fingerprint:-none}"
  fi
  install -m 0644 /etc/apt/keyrings/docker.asc.new /etc/apt/keyrings/docker.asc
  rm -f /etc/apt/keyrings/docker.asc.new
  cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${codename}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt_get update
  apt_get install -y --no-install-recommends --allow-downgrades --allow-change-held-packages \
    "docker-ce=${DOCKER_VERSION}" "docker-ce-cli=${DOCKER_VERSION}" \
    "containerd.io=${CONTAINERD_VERSION}" "docker-compose-plugin=${COMPOSE_VERSION}"
  apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
}

install_gcloud() {
  # The snap's commands work only once snapd has seeded the image (first boot).
  if command -v snap >/dev/null; then
    snap wait system seed.loaded
  fi
  if ! command -v gcloud >/dev/null; then
    log "installing the Google Cloud CLI"
    snap install google-cloud-cli --classic
  fi
}

main() {
  local zone region
  (( EUID == 0 )) || die "run as root"

  install_base_packages
  install_docker
  systemctl enable --now docker.service
  install_gcloud

  # Artifact Registry in the VM's region; the zone is projects/<n>/zones/<region>-<letter>.
  zone=$(metadata instance/zone)
  zone=${zone##*/}
  region=${zone%-*}
  [[ $region =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "cannot derive the region from zone ${zone}"
  # Root runs docker compose (kalkan.service, deploy.sh); the credential helper uses the VM's
  # service account.
  gcloud auth configure-docker "${region}-docker.pkg.dev" --quiet
  command -v docker-credential-gcloud >/dev/null ||
    die "docker-credential-gcloud is not on PATH; docker cannot pull from Artifact Registry"

  install -d -m 0755 -o root -g root "$KALKAN_ROOT"

  if [[ -f $KALKAN_ROOT/vm/bin/install-units.sh ]]; then
    bash "$KALKAN_ROOT/vm/bin/install-units.sh"
    # Usually started by systemd already at this point; then this does nothing.
    systemctl start --no-block kalkan-firewall.service kalkan.service
    log "units installed; the stack starts through kalkan.service"
  else
    log "no deploy yet: the first deploy installs the units and starts the stack"
  fi
}

main "$@"
