#!/bin/bash
# Install or update homelab-health on the Proxmox hosts (needs root ssh).
#   scripts/proxmox-health/install.sh [host ...]
set -euo pipefail
cd "$(dirname "$0")"
[ $# -gt 0 ] || set -- 192.168.1.230 192.168.1.232 192.168.1.234 192.168.1.236
for h in "$@"; do
  echo "== $h"
  scp -q homelab-health.sh "root@$h:/usr/local/sbin/homelab-health"
  scp -q homelab-health.service homelab-health.timer "root@$h:/etc/systemd/system/"
  ssh "root@$h" 'chmod 0755 /usr/local/sbin/homelab-health &&
    systemctl daemon-reload && systemctl enable --now homelab-health.timer &&
    systemctl start homelab-health.service &&
    grep -cv "^#" /var/lib/prometheus/node-exporter/homelab_health.prom | sed "s/^/metrics: /"'
done
