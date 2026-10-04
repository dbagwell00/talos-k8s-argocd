---
name: homelab-health
description: Health of the homelab's shared storage, the security cameras, and the SpaceTraders pods — Proxmox hosts, Samba/SMB, CTDB, CephFS, Ceph capacity, Blue Iris recording per camera, and the SpaceTraders Postgres, Redis, and agent deployments. Use when the user asks if the homelab, NAS, SMB shares, Samba, CTDB, Ceph, the cameras, Blue Iris, recordings, the Proxmox hosts, or the SpaceTraders infrastructure is healthy, or asks about a homelab watch alert.
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
- **Blue Iris cameras.** Blue Iris records every camera continuously to
  CephFS. A camera with no write for 5 minutes has stopped streaming or
  recording. Also watched: alert images (AI/motion still firing) and old
  files stuck in `new/` (rotation to `stored/` broken). The per-camera
  bitrate is in the report. Camera names: AptDoor, BDPTZ, BPTZ, BackDoor,
  DPTZ, FWide, FrontDoor, GWide, IGW, SWide.
- **SpaceTraders**: deployment replicas, Postgres/Redis readiness, restarts,
  PVC fill, and whether both agents' APIs answer. An agent scaled to 0 is
  reported, not alarmed: they're stopped that way on purpose.

## How to answer

- Lead with the verdict (all clear / N warnings / N critical), then the problems.
- Pass the fix hints on, but you cannot run them: there's no shell access to
  the hosts and no write path to the cluster. Tell the user what to run.
- If the user wants money or leaderboard numbers, that's the spacetraders-ops skill.
