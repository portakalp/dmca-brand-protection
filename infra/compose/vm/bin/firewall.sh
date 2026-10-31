#!/usr/bin/env bash
# Egress rules for the browser network and the metadata server (architecture.md §24; K82).
# Idempotent: run by kalkan-firewall.service before Docker starts (and on every Docker restart),
# by kalkan.service before the stack starts and by deploy.sh.
#
#   firewall.sh            apply the rules
#   firewall.sh --print    print the iptables-restore input (tests)
#
# DOCKER-USER sees forwarded traffic. From the browser bridge: replies pass, Redis passes,
# Prometheus's scrape of worker-browser (:9191) passes, and every other private, CGNAT and
# link-local destination (the metadata server 169.254.169.254 included) is rejected, so
# worker-browser cannot open connections to Prometheus; public destinations (proxies, Sentry, the
# edge) continue to Docker's own rules. INPUT sees traffic to the VM itself: from the browser
# bridge only replies pass, so no host port (sshd, the monitoring UIs, anything on the bridge
# gateway) is reachable.
# Containers on one bridge (worker-browser, Redis, Prometheus) talk at layer 2: their traffic
# passes the FORWARD chain only with br_netfilter and net.bridge.bridge-nf-call-iptables=1, which
# Docker turns on only for networks with icc=false. Apply mode turns it on (K82).
# DNS: Docker's embedded resolver (127.0.0.11) forwards lookups to the VM's resolver
# (169.254.169.254) from the host's network namespace, so they do not pass through these rules
# (the container's resolv.conf lists it as "ExtServers: [host(...)]"). Servers set with compose
# `dns:` would be queried from the container's namespace and be rejected if private.
# Metadata server: from the compose default network (br-kalkan-app), only the services that need
# Google credentials (the VM service account's token) reach 169.254.169.254, by their fixed
# addresses in docker-compose.yml; every other source address is rejected. DNS (above) and
# dockerd's own calls (gcplogs, image pulls) run in the host's namespace and are not affected.
# IPv4 only: neither network has IPv6 (enable_ipv6: false).
set -euo pipefail

readonly BRIDGE=br-kalkan-brw
readonly REDIS=172.31.250.10/32
readonly WORKER_BROWSER=172.31.250.11/32
readonly PROMETHEUS=172.31.250.12/32
readonly FORWARD_CHAIN=KALKAN-BROWSER
readonly INPUT_CHAIN=KALKAN-BROWSER-IN
readonly BLOCKED=(169.254.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10)
readonly APP_BRIDGE=br-kalkan-app
readonly METADATA=169.254.169.254/32
readonly METADATA_CHAIN=KALKAN-METADATA
# cloud-sql-proxy, worker-default, worker-ai, ops
readonly METADATA_CLIENTS=(172.31.251.10/32 172.31.251.11/32 172.31.251.12/32 172.31.251.13/32)

rules() {
  local network client
  echo "*filter"
  echo ":$FORWARD_CHAIN - [0:0]"
  echo ":$INPUT_CHAIN - [0:0]"
  echo ":$METADATA_CHAIN - [0:0]"
  echo "-A $FORWARD_CHAIN -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
  echo "-A $FORWARD_CHAIN -d $REDIS -p tcp -m tcp --dport 6379 -j RETURN"
  echo "-A $FORWARD_CHAIN -s $PROMETHEUS -d $WORKER_BROWSER -p tcp -m tcp --dport 9191 -j RETURN"
  for network in "${BLOCKED[@]}"; do
    echo "-A $FORWARD_CHAIN -d $network -j REJECT --reject-with icmp-port-unreachable"
  done
  echo "-A $INPUT_CHAIN -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN"
  echo "-A $INPUT_CHAIN -j DROP"
  for client in "${METADATA_CLIENTS[@]}"; do
    echo "-A $METADATA_CHAIN -s $client -j RETURN"
  done
  echo "-A $METADATA_CHAIN -j REJECT --reject-with icmp-port-unreachable"
  echo "COMMIT"
}

if [[ ${1:-} == --print ]]; then
  rules
  exit 0
fi

(( EUID == 0 )) || {
  echo "firewall: run as root" >&2
  exit 1
}

# Bridged traffic between the browser network's containers must pass KALKAN-BROWSER (see above).
modprobe br_netfilter
sysctl -q -w net.bridge.bridge-nf-call-iptables=1

ipt=(iptables -w 10)
# Docker creates DOCKER-USER when it starts and never changes rules already in it; when this runs
# first, it creates the chain itself.
"${ipt[@]}" -n -L DOCKER-USER >/dev/null 2>&1 || "${ipt[@]}" -N DOCKER-USER
# Declaring the chains replaces their contents in one transaction (--noflush keeps the rest).
rules | iptables-restore -w 10 --noflush
"${ipt[@]}" -C DOCKER-USER -i "$BRIDGE" -j "$FORWARD_CHAIN" 2>/dev/null ||
  "${ipt[@]}" -I DOCKER-USER 1 -i "$BRIDGE" -j "$FORWARD_CHAIN"
"${ipt[@]}" -C DOCKER-USER -i "$APP_BRIDGE" -d "$METADATA" -j "$METADATA_CHAIN" 2>/dev/null ||
  "${ipt[@]}" -I DOCKER-USER 1 -i "$APP_BRIDGE" -d "$METADATA" -j "$METADATA_CHAIN"
"${ipt[@]}" -C INPUT -i "$BRIDGE" -j "$INPUT_CHAIN" 2>/dev/null ||
  "${ipt[@]}" -I INPUT 1 -i "$BRIDGE" -j "$INPUT_CHAIN"
echo "firewall: rules for $BRIDGE and $APP_BRIDGE applied"
