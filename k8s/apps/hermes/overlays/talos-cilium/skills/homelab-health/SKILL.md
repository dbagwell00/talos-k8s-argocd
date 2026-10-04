---
name: homelab-health
description: Health of the whole homelab — Proxmox hosts, Samba/SMB, CTDB, CephFS, Ceph capacity, Blue Iris recording per camera, Home Assistant, Pi-hole, Vault (sealed?), Bitwarden, the registry, TLS certs, pods on both Kubernetes clusters, Velero backups, External Secrets sync, Argo CD apps, UniFi firmware upgrades, and the SpaceTraders Postgres, Redis, and agents. Use when the user asks whether anything in the homelab is broken or healthy, about any of those services, or about a homelab watch alert.
version: 1.0.0
metadata:
  hermes:
    category: homelab
    tags: [proxmox, samba, ctdb, ceph, monitoring]
---

# Homelab health (read-only)

```
python3 /opt/data/skills/homelab/homelab-health/scripts/health.py report
```

Prints the full status: any problems first (🔴 critical, 🟡 warning), each
with the fix that worked last time, then facts (disk use, CTDB generation,
Ceph state, Samba core-dump sizes, SpaceTraders deployments, Postgres, Redis).

Two scheduled jobs run the same script without you (no model involved):
`homelab-watch` every 15 minutes, which messages only when a problem appears,
clears, or is still open (critical every 6h, warning daily), and
`homelab-digest` every morning at 8am with the full report.

## What it watches, and why

The checks come from real incidents on this cluster:

- **CTDB recovery loop / split-brain.** `ctdb status` looks fine during both,
  so the check uses the recovery count and whether hosts agree on Generation.
- **Stale smbd bind.** After a CTDB IP takeover, smbd can hold a public IP
  it isn't listening on, so some SMB mounts fail with "host is down".
- **Evicted CephFS.** `/mnt/cephfs` stays mounted but unreadable, and
  CTDB loses its recovery lock.
- **Samba panic storms.** These show up as core files piling up.
  Between 03:20 and 04:00 they are usually a UniFi switch firmware upgrade.
- **Ceph** health checks (SLOW_OPS etc.), pool capacity, root-disk space on prox01-04.
- **Ceph OSDs** (32 HDDs, no flash DB/WAL): down/out, slow ops per daemon, and
  15-min apply latency per OSD against the cluster median. Slow OSDs have
  preceded past Samba incidents.
- **Blue Iris cameras.** Blue Iris records every camera continuously to
  CephFS. A camera with no write for 5 minutes has stopped streaming or
  recording. Also watched: alert images (AI/motion still firing) and old
  files stuck in `new/` (rotation to `stored/` broken). The per-camera
  bitrate is in the report. Camera names: AptDoor, BDPTZ, BPTZ, BackDoor,
  DPTZ, FWide, FrontDoor, GWide, IGW, SWide.
- **Services** (blackbox probes): Home Assistant, Blue Iris web, Pi-hole
  (resolving *and* still blocking), Vault (a sealed Vault means no secret
  syncs anywhere; it needs a manual unseal), Bitwarden, the registry, and
  TLS expiry on the `*.dlb.im` certs.
- **Kubernetes, both clusters**: any deployment/statefulset/daemonset short
  of replicas, pods crash-looping or failing to pull, Pending 10+ min, or
  any PVC over 85%. Problems must last 10 min, so rollouts don't alarm.
- **Velero** last successful backup per schedule, **ExternalSecrets** not
  syncing, **Argo CD** apps not Healthy (OutOfSync is listed, not alarmed).
- **UniFi firmware flashes** in the last 24h. The nightly auto-upgrade
  reboots switches and has caused two CTDB/Samba outages.
- **SpaceTraders**: deployment replicas, Postgres/Redis readiness, restarts,
  PVC fill, and whether both agents' APIs answer. An agent scaled to 0 is
  reported, not alarmed: they're stopped that way on purpose.

## How to answer

- Lead with the verdict (all clear / N warnings / N critical), then the problems.
- Pass the fix hints on, but you cannot run them: there's no shell access to
  the hosts and no write path to the cluster. Tell the user what to run.
- If the user wants money or leaderboard numbers, that's the spacetraders-ops skill.
