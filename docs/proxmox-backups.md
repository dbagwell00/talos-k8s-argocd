# Proxmox backups (vzdump)

Guest backups go to one ZFS pool on prox03. prox03 writes to it directly; the other hosts
reach it over NFS. **prox03 must never mount its own NFS export**: that deadlocked the host
on 2026-10-29 (see [below](#incident-2026-10-29-nfs-loopback-deadlock)).

## Layout

| | |
|---|---|
| Pool | ZFS `backup-pool` on prox03, mounted at `/mnt/backup-pool` (3.6T, no redundancy) |
| Backup files | `/mnt/backup-pool/dump` |
| NFS export (prox03 `/etc/exports`) | `/mnt/backup-pool *(rw,sync,no_root_squash,no_subtree_check)` |

Storages (`/etc/pve/storage.cfg`) point at the same `dump/` directory:

| Storage | Type | Nodes | Path |
|---|---|---|---|
| `shared-backup-pool` | nfs (server 192.168.1.234) | prox01, prox02, prox04 | `/mnt/pve/shared-backup-pool` |
| `backup-pool-prox03` | dir, `is_mountpoint yes` | prox03 | `/mnt/backup-pool` |

Both use the same prune policy (`keep-daily=1,keep-last=1,keep-monthly=1,keep-weekly=1,keep-yearly=1`).

A Proxmox backup job targets a single storage, so there is **one job per node**
(`/etc/pve/jobs.cfg`). Each job is `all 1` plus `node <host>`, runs at 21:00 with zstd and
snapshot mode, and uses the original job's notification settings. `all 1` is evaluated on the node at
run time, so a migrated guest is still backed up by whichever host it is on.

| Job | Node | Storage |
|---|---|---|
| `backup-8258756d-f0b0` (the original) | prox01 | `shared-backup-pool` |
| `16934548-…` | prox02 | `shared-backup-pool` |
| `91f87693-…` | prox04 | `shared-backup-pool` |
| `ee04d5ee-…` | prox03 | `backup-pool-prox03` |

**When adding a node:** create a job for it, or it gets no backups. If the backup pool ever
moves to another host, swap the roles: that host gets a `dir` storage and drops out of the
NFS storage's node list.

## Incident 2026-10-29: NFS loopback deadlock

Before the change, `shared-backup-pool` was enabled on all four nodes, so prox03 mounted
`192.168.1.234:/mnt/backup-pool` from itself.

### What happened

1. At 21:00 PDT the cluster-wide job started on every host at once. prox03 (62 GB RAM, 8 OSDs,
   a Talos VM, ZFS ARC) climbed past 80% memory use.
2. When memory is short, the kernel frees page cache in the context of whichever thread is
   allocating ("direct reclaim"). Dirty pages written through the NFS client can only be freed
   after the server commits them.
3. On prox03 that server is the local `nfsd`. All 16 nfsd threads went into reclaim and
   waited on NFS commits that only nfsd could complete. Ceph OSD threads entered reclaim and
   froze on the same pages (`nfs_release_folio → __nfs_commit_inode → wait_on_commit`).
4. There were no OOM kills. Nothing crashed; everything waited forever.

Clients on other hosts don't hit this, because the server they wait on isn't stuck in their
reclaim. That is why a host must never NFS-mount its own export.

### Impact (~21:14 PDT → 07:30 PDT next morning)

- osd.16–22 and osd.31 down (7 marked out). Ceph peaked at 18% objects degraded; every PG stayed
  active, so no data was unavailable.
- mon.prox03 out of quorum.
- The CephFS kernel client on prox03 was evicted. CTDB still reported prox03 healthy while it
  held a public IP.
- talos-cilium-3 (VM 8003) NotReady; its pods stuck Terminating.
- The NFS mount hung on every host; vzdump and `pvestatd` sat in D state for 10+ hours.

### Diagnosing a host in this state

- `ps`, `pgrep`, `df` and `qm list` hang. They read `/proc/<pid>/cmdline` or `smaps` and block
  on wedged mm locks, or stat the dead NFS mount. Read `/proc/<pid>/stat` instead, and probe
  mounts with `timeout 4 stat -f <mount>`.
- Get blocked-task stacks without touching `/proc/<pid>`:
  `echo w > /proc/sysrq-trigger; dmesg`.
- A hard reset is the only way out. The host will not shut down cleanly.

### Recovery

1. `ceph osd set noout`, then hard-reset prox03. The OSDs were back within 5 minutes.
   `mon_osd_auto_mark_auto_out_in` (default true) marks the auto-outed OSDs back in.
2. Once recovery finished, `ceph osd unset noout` and `ceph crash archive-all`.
3. Restart smbd on any host whose CTDB public IP isn't listening on :445. The takeover shuffles
   the IPs, and smbd (`bind interfaces only`) does not rebind on its own. Check with
   `for ip in 238 239 240 241; do timeout 4 bash -c "</dev/tcp/192.168.1.$ip/445" && echo .$ip ok; done`.
4. The stuck vzdump runs on other hosts finished on their own once NFS answered again.
5. After the hard reset, mon.prox03 stayed in `probing`. Its peers dropped its connections as
   `stale connection, peer_global_seq=91 existing->peer_global_seq=926`, because they still held
   the pre-reset instance's sequence number. Restarting mon.prox03 does not help. Restarting
   one peer (`systemctl restart ceph-mon@prox02`) cleared it: prox03 rejoined quorum within
   seconds. While the peer restarts, only one mon is up, so map changes pause; client IO doesn't.
