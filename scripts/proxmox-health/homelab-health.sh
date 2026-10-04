#!/bin/bash
# Prometheus textfile collector for the Samba/CTDB/CephFS/Ceph failure modes
# this cluster has actually had. Runs every minute from homelab-health.timer;
# node-exporter serves the result, Prometheus (talos-cilium) scrapes it as
# job proxmox-node-exporter, and Hermes/Grafana/Alertmanager read it there.
#
# What each block catches:
#   ctdb status / generation   `ctdb status` is a snapshot and has looked
#                              perfect during two incidents. The recovery
#                              count and a moving Generation are what showed
#                              the 1/sec recovery loop (2026-08-13) and the
#                              split-brain (2026-09-09).
#   public IP vs :445 listener smbd binds once at startup; after a CTDB
#                              takeover it can hold an IP it isn't listening
#                              on (stale bind, 2026-08-13).
#   cephfs readable            an evicted CephFS mount stays in /proc/mounts
#                              but returns EACCES, killing the CTDB lock.
#   samba cores                a panic storm wrote 41 GB of cores in 7 min.
#   ceph health checks         SLOW_OPS etc. (the mgr prometheus module is off).
#   ceph capacity              per-pool fill; Blue Iris alone stores ~86 TB on CephFS.
#   blue iris recording        Blue Iris (VM 107) records every camera continuously
#                              to CephFS in hourly .bvr segments. The open segment's
#                              mtime advances while the camera streams, so its age
#                              is "stopped streaming / not recording" with no BI
#                              login. Also: alert JPEGs (AI/motion still firing) and
#                              files stuck in new/ (rotation to stored/ broken).
#
# Read-only: it runs status commands and reads files, and changes nothing.
set -u
T=5
start=$(date +%s.%N)

m() { printf '%s\n' "$*"; }
help() { m "# HELP $1 $2"; m "# TYPE $1 ${3:-gauge}"; }

# --- systemd units -----------------------------------------------------------
help homelab_unit_active "1 if the systemd unit is active."
for u in ctdb smbd nmbd winbind; do
  systemctl is-active --quiet "$u" && v=1 || v=0
  m "homelab_unit_active{unit=\"$u\"} $v"
done

# --- CTDB --------------------------------------------------------------------
st=$(timeout -k 1 $T ctdb status 2>/dev/null); rc=$?
help homelab_ctdb_status_ok "1 if 'ctdb status' answered."
m "homelab_ctdb_status_ok $([ $rc -eq 0 ] && echo 1 || echo 0)"
if [ $rc -eq 0 ]; then
  gen=$(sed -n 's/^Generation:\([0-9]*\).*/\1/p' <<<"$st")
  recmode=$(sed -n 's/^Recovery mode:.*(\([0-9]*\)).*/\1/p' <<<"$st")
  nodes=$(sed -n 's/^Number of nodes:\([0-9]*\).*/\1/p' <<<"$st")
  ok=$(grep -cE '^pnn:[0-9]+ +[0-9.]+ +OK' <<<"$st")
  help homelab_ctdb_generation "CTDB database generation; changes on every recovery."
  m "homelab_ctdb_generation ${gen:-0}"
  help homelab_ctdb_recovery_mode "0 = NORMAL, 1 = ACTIVE (recovering)."
  m "homelab_ctdb_recovery_mode ${recmode:-1}"
  help homelab_ctdb_nodes "CTDB nodes by state as seen from this node."
  m "homelab_ctdb_nodes{state=\"total\"} ${nodes:-0}"
  m "homelab_ctdb_nodes{state=\"ok\"} ${ok:-0}"
fi
help homelab_ctdb_recoveries_5m "'Recovery has started' lines in the ctdb journal over the last 5 minutes."
m "homelab_ctdb_recoveries_5m $(journalctl -u ctdb --since -5min -q -o cat 2>/dev/null | grep -c 'Recovery has started')"

# --- public IPs vs smbd listeners ---------------------------------------------
listen=$(ss -ltnH '( sport = :445 )' 2>/dev/null | awk '{print $4}')
wild=0; grep -qE '^(0\.0\.0\.0|\*|\[::\]):445$' <<<"$listen" && wild=1
help homelab_ctdb_public_ip_held "1 if this node currently holds the CTDB public IP."
help homelab_smbd_listening "1 if smbd is listening on :445 on that IP."
if [ -r /etc/ctdb/public_addresses ]; then
  for ip in $(awk '{sub(/\/.*/, "", $1); print $1}' /etc/ctdb/public_addresses); do
    ip -4 -o addr show | grep -qE "inet ${ip//./\\.}/" && held=1 || held=0
    { [ $wild -eq 1 ] || grep -qx "${ip}:445" <<<"$listen"; } && l=1 || l=0
    m "homelab_ctdb_public_ip_held{ip=\"$ip\"} $held"
    m "homelab_smbd_listening{ip=\"$ip\"} $l"
  done
fi

# --- CephFS mount ---------------------------------------------------------------
help homelab_cephfs_mounted "1 if /mnt/cephfs is a mountpoint."
mountpoint -q /mnt/cephfs && m "homelab_cephfs_mounted 1" || m "homelab_cephfs_mounted 0"
help homelab_cephfs_readable "1 if the CTDB recovery lock file on CephFS can be stat'ed."
timeout -k 1 $T stat -t /mnt/cephfs/.ctdb/recovery >/dev/null 2>&1 && v=1 || v=0
m "homelab_cephfs_readable $v"

# --- Samba cores ----------------------------------------------------------------
cd_=/var/log/samba/cores
help homelab_samba_core_files "Core files under /var/log/samba/cores."
help homelab_samba_core_files_5m "Core files written in the last 5 minutes (panic rate)."
help homelab_samba_core_bytes "Bytes used by /var/log/samba/cores."
if [ -d "$cd_" ]; then
  m "homelab_samba_core_files $(find "$cd_" -type f 2>/dev/null | wc -l)"
  m "homelab_samba_core_files_5m $(find "$cd_" -type f -mmin -5 2>/dev/null | wc -l)"
  m "homelab_samba_core_bytes $(du -sb "$cd_" 2>/dev/null | cut -f1)"
fi

# --- Ceph -----------------------------------------------------------------------
cj=$(timeout -k 1 10 ceph health detail --format json 2>/dev/null); rc=$?
help homelab_ceph_cli_ok "1 if 'ceph health' answered from this host."
m "homelab_ceph_cli_ok $([ $rc -eq 0 ] && [ -n "$cj" ] && echo 1 || echo 0)"
if [ $rc -eq 0 ] && [ -n "$cj" ]; then
  help homelab_ceph_health_status "0 = HEALTH_OK, 1 = HEALTH_WARN, 2 = HEALTH_ERR."
  jq -r '"homelab_ceph_health_status " + ({"HEALTH_OK":"0","HEALTH_WARN":"1","HEALTH_ERR":"2"}[.status] // "2")' <<<"$cj"
  help homelab_ceph_health_check "1 for each active Ceph health check (e.g. SLOW_OPS)."
  jq -r '.checks // {} | to_entries[] |
    "homelab_ceph_health_check{check=\"" + .key + "\",severity=\"" + .value.severity + "\"} 1"' <<<"$cj"
fi

if [ $rc -eq 0 ] && [ -n "$cj" ]; then
  df=$(timeout -k 1 10 ceph df --format json 2>/dev/null)
  if [ -n "$df" ]; then
    help homelab_ceph_raw_used_ratio "Fraction of raw cluster capacity used."
    jq -r '"homelab_ceph_raw_used_ratio " + (.stats.total_used_raw_ratio|tostring)' <<<"$df"
    help homelab_ceph_pool_used_ratio "Pool percent_used as a fraction."
    help homelab_ceph_pool_max_avail_bytes "Bytes the pool can still take at its replication."
    help homelab_ceph_pool_stored_bytes "Bytes stored in the pool (before replication)."
    jq -r '.pools[] |
      "homelab_ceph_pool_used_ratio{pool=\"" + .name + "\"} " + (.stats.percent_used|tostring),
      "homelab_ceph_pool_max_avail_bytes{pool=\"" + .name + "\"} " + (.stats.max_avail|tostring),
      "homelab_ceph_pool_stored_bytes{pool=\"" + .name + "\"} " + (.stats.stored|tostring)' <<<"$df"
  fi
fi

# --- Blue Iris recordings on CephFS -------------------------------------------
bi=/mnt/cephfs/blueiris
if timeout -k 1 $T test -d "$bi/new"; then
  listing=$(timeout -k 1 15 find "$bi/new" -maxdepth 1 -type f -name '*.bvr' -printf '%T@ %s %f\n' 2>/dev/null)
  help homelab_blueiris_camera_last_write_seconds "Unix mtime of the camera's newest .bvr segment in new/."
  help homelab_blueiris_camera_segment_bytes "Size of the camera's newest .bvr segment (grows while recording)."
  # newest segment per camera: file name is <CAMERA>.<YYYYMMDD_HHMMSSZ>.bvr
  awk '{ split($3, a, "."); cam = a[1];
         if (!(cam in t) || $1 > t[cam]) { t[cam] = $1; sz[cam] = $2 } }
       END { for (c in t) {
         printf "homelab_blueiris_camera_last_write_seconds{camera=\"%s\"} %d\n", c, t[c];
         printf "homelab_blueiris_camera_segment_bytes{camera=\"%s\"} %d\n", c, sz[c] } }' <<<"$listing"
  help homelab_blueiris_new_files "Segments in new/ (normally ~1-2 per camera)."
  m "homelab_blueiris_new_files $(grep -c . <<<"$listing")"
  help homelab_blueiris_new_oldest_seconds "Unix mtime of the oldest segment in new/; old = rotation to stored/ stuck."
  m "homelab_blueiris_new_oldest_seconds $(awk 'NR==1||$1<min{min=$1} END{printf "%d", min+0}' <<<"$listing")"
  help homelab_blueiris_alert_last_seconds "Unix mtime of the newest alert image (AI/motion triggers firing)."
  m "homelab_blueiris_alert_last_seconds $(timeout -k 1 15 find "$bi/alerts" -maxdepth 1 -type f -mmin -1440 -printf '%T@\n' 2>/dev/null | sort -n | tail -1 | cut -d. -f1 | grep . || echo 0)"
fi

help homelab_health_last_run_seconds "Unix time this collector last finished."
m "homelab_health_last_run_seconds $(date +%s)"
help homelab_health_duration_seconds "How long this collector run took."
m "homelab_health_duration_seconds $(echo "$(date +%s.%N) - $start" | bc 2>/dev/null || echo 0)"
