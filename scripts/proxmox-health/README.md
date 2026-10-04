# proxmox-health

A Prometheus textfile collector for the Proxmox hosts (prox01–04) that checks Samba, CTDB,
CephFS, and Ceph. Each check targets a failure mode this cluster has actually had.

`homelab-health.timer` runs `/usr/local/sbin/homelab-health` once a minute; the timer is
enabled and starts at boot. The script writes `homelab_health.prom` into
`/var/lib/prometheus/node-exporter/` through `sponge`. The node-exporter already running on
each host serves that file, and `talos-cilium`'s Prometheus scrapes it as job
`proxmox-node-exporter`.

It is read-only: it runs status commands and reads files, opens no port, and creates no
account. That's why the homelab watch needs no SSH access to the hosts.

## Metrics

| Metric | Catches |
|---|---|
| `homelab_ctdb_generation`, `homelab_ctdb_recoveries_5m` | **Recovery loops** (1/sec, 2026-08-13) and **split-brain** (2026-09-09). `ctdb status` showed every node OK through both, so use `changes(homelab_ctdb_generation[…])` and `max_over_time(homelab_ctdb_recoveries_5m[…])` instead. |
| `homelab_ctdb_nodes{state}`, `homelab_ctdb_recovery_mode`, `homelab_ctdb_status_ok` | Nodes missing or unhealthy, as seen from each host |
| `homelab_ctdb_public_ip_held{ip}` vs `homelab_smbd_listening{ip}` | **Stale smbd bind**: after a CTDB takeover, smbd holds a public IP it isn't listening on, and some SMB mounts fail with "host is down" |
| `homelab_cephfs_mounted`, `homelab_cephfs_readable` | **Evicted CephFS**: still mounted, but returns EACCES, so CTDB loses its recovery lock |
| `homelab_samba_core_files_5m`, `homelab_samba_core_bytes` | **Panic storms** (41 GB of cores in 7 min on 2026-09-09) |
| `homelab_ceph_health_status`, `homelab_ceph_health_check{check,severity}` | Ceph health (SLOW_OPS etc.); the mgr prometheus module is off |
| `homelab_ceph_raw_used_ratio`, `homelab_ceph_pool_{used_ratio,max_avail_bytes,stored_bytes}{pool}` | Capacity. Blue Iris keeps about 86 TB on `cephfs-data` |
| `homelab_blueiris_camera_last_write_seconds{camera}` | **Camera stopped streaming or recording.** Blue Iris (VM 107) records continuously to `/mnt/cephfs/blueiris/new/` in hourly `.bvr` segments, and the open segment's mtime advances while the camera streams. No Blue Iris login needed. |
| `homelab_blueiris_camera_segment_bytes{camera}` | Per-camera bitrate, via `rate()` |
| `homelab_blueiris_alert_last_seconds` | AI/motion alerts still firing (`alerts/` JPEGs) |
| `homelab_blueiris_new_files`, `homelab_blueiris_new_oldest_seconds` | Rotation from `new/` to `stored/` stuck |
| `homelab_unit_active{unit}` | ctdb, smbd, nmbd, winbind |
| `homelab_health_last_run_seconds` | Staleness of the collector itself |

## Install / update

The script needs root SSH to each host.

```sh
scripts/proxmox-health/install.sh                  # all four hosts
scripts/proxmox-health/install.sh 192.168.1.232    # just one
```

It copies the script and both units, reloads systemd, enables the timer, and runs the
collector once, printing the metric count.

## Check by hand

```sh
systemctl list-timers homelab-health.timer
systemctl status homelab-health.service
cat /var/lib/prometheus/node-exporter/homelab_health.prom
```

## Remove

```sh
systemctl disable --now homelab-health.timer
rm /etc/systemd/system/homelab-health.{service,timer} /usr/local/sbin/homelab-health \
   /var/lib/prometheus/node-exporter/homelab_health.prom
systemctl daemon-reload
```

Consumers: the Hermes `homelab-health` skill and its scheduled jobs
([`k8s/apps/hermes`](../../k8s/apps/hermes/README.md)). The same metrics are available to
Grafana and Alertmanager.

## Ceph mgr prometheus module

Separately from this collector, the Ceph manager's built-in exporter was turned on
(2026-10-04) for per-OSD metrics the collector can't cheaply produce:

```sh
ceph mgr module enable prometheus     # listens on :9283 on every mgr (prox01-03)
```

Enabling it restarts the active mgr once; the cluster stays HEALTH_OK. Only the active
mgr serves data, and standbys return an empty 200. Prometheus scrapes all three as job
`ceph` (in `kube-prometheus-stack` values), so failover needs no change. Useful series:
`ceph_osd_apply_latency_ms` / `ceph_osd_commit_latency_ms`, `ceph_osd_up` / `ceph_osd_in`,
`ceph_daemon_health_metrics{type="SLOW_OPS"}`, and `ceph_osd_metadata` (join on
`ceph_daemon` for `hostname`). To undo: `ceph mgr module disable prometheus`.
