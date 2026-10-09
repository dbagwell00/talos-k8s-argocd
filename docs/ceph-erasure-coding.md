# CephFS erasure coding (`cephfs-ec`)

Bulk video on CephFS (`blueiris/`, then `media/`) is moving from 3× replication to
erasure coding 2+2. Everything else stays replicated.

## Why

The cluster has 4 hosts of very different sizes: prox01/02 have 160 TiB each, prox03 146,
and **prox04 only 102**. Every pool was replicated `size 3` with one copy per host. When a
large host dies, each of the three survivors must hold a full copy of every PG, so
**prox04 alone must fit all stored data**.

On 2026-10-04 there were 79 TiB stored, while prox04's nearfull (85%) is about 87 TiB and its
backfillfull (90%) about 92 TiB. That leaves only about 7–12 TiB of growth before a host loss
can no longer re-heal, and before client writes could push prox04 OSDs to full and block the pool.
`ceph df`'s MAX AVAIL (97 TiB) doesn't account for this.

EC 2+2 with `crush-failure-domain=host` puts exactly one chunk on each host:

| | replicated size 3 | EC 2+2 (host) |
|---|---|---|
| raw cost | 3× | 2× |
| one host down | 2 copies left, re-heals if there's room | 3 of 4 chunks, one spare; **never needs room to re-heal** (there's no 5th host) |
| two hosts down | some PGs inactive | some PGs inactive (min_size 3) |
| host-failure-safe stored data | ~87 TiB (prox04) | ~170 TiB |
| good at | everything | large sequential files; poor at small random overwrites |

Blue Iris `.bvr` segments and media files are large and sequential, so they suit EC. Prometheus
TSDB, MinIO, VM disks and CephFS metadata stay replicated.

## How it fits together

There is no new mount, share, or PV. `cephfs-ec` is an extra data pool of the same filesystem
(`mycephfs`). A directory's `ceph.dir.layout.pool` attribute decides where **new** files
under it store their data, inheriting from the nearest ancestor that has the attribute set.

| Consumer | Effect |
|---|---|
| Samba / CTDB (hosts mount CephFS as `client.admin`) | none |
| Blue Iris (writes over SMB) | new segments land on EC |
| k8s SMB CSI PVs (`smb-media`, `smb-mnt`) | none, they go through Samba |
| k8s `ceph-cephfs` StorageClass (`pool: cephfs-data`, subvolumes under `/volumes/csi`) | none, stays replicated; `client.kubernetes` has no caps on `cephfs-ec` and doesn't need them |
| k8s `ceph-rbd` (`vm-storage`) | none |

**A rename never moves data.** Existing files stay on `cephfs-data` until they are rewritten.
Moving a file *into* an EC directory from somewhere else keeps its old pool. qBittorrent saves
under `media/Z/media/downloads/`, inside the `media/` tree, so downloads are written to EC and
Sonarr/Radarr renames keep them there.

## Done: step 1, pool + `blueiris/` (2026-10-04)

```sh
ceph osd erasure-code-profile set ec-2-2-host k=2 m=2 crush-failure-domain=host \
    crush-device-class=hdd plugin=jerasure technique=reed_sol_van
ceph osd pool create cephfs-ec 256 256 erasure ec-2-2-host --autoscale-mode on --bulk
ceph osd pool set cephfs-ec allow_ec_overwrites true
ceph fs add_data_pool mycephfs cephfs-ec            # also enables application cephfs
setfattr -n ceph.dir.layout.pool -v cephfs-ec /mnt/cephfs/blueiris
```

The result is pool 13: `size 4 min_size 3`, CRUSH rule `chooseleaf_indep host`, and 256 PGs
active+clean. A 256 MiB file written on prox02 read back on prox01 with a matching md5 and used
512 MiB raw. `ceph df` shows about 146 TiB MAX AVAIL for `cephfs-ec`, against 97 TiB for the
replicated pool.

Verified at 17:00 PDT: the first post-change segments
(`AptDoor.20261005_000001Z.bvr`, `FrontDoor.20261005_000002Z.bvr`) have
`ceph.file.layout.pool = cephfs-ec`, all 10 cameras kept recording, and health stayed
HEALTH_OK. Each camera rolls to a new segment on its own schedule, not exactly on the hour,
so the other cameras moved over as their segments ended.

Blue Iris migrates itself: new segments land on EC, and the old ones age out of
`stored/` (about 13 weeks of retention, so by early January 2027).

## Step 2: watch (done)

- **Recording.** Hermes's homelab watch alerts if any camera stops writing for 5 min.
  `homelab_blueiris_camera_last_write_seconds` and the per-camera bitrate are in Prometheus.
- **Pool placement.** Check new segments are on EC (from any host):

  ```sh
  for f in $(ls -t /mnt/cephfs/blueiris/new/*.bvr | head -3); do
      getfattr --only-values -n ceph.file.layout.pool "$f"; echo "  $f"; done
  ```

- **Health.** `ceph -s`, plus Ceph OSD apply latency in Prometheus (job `ceph`) for a
  write-latency change on the HDDs.
- **Growth.** `ceph df` should show `cephfs-ec` growing by about 3 TiB/week and `cephfs-data`
  shrinking as old footage ages out.

## Done: step 3, `media/` (2026-10-04 → 2026-10-09)

The layout was set on `/mnt/cephfs/media` on 2026-10-04. `rewrite-to-pool.sh --bwlimit 150000`
then ran from 2026-10-04 17:29 to 2026-10-09 12:51 PDT (about 4.8 days): **294,648 files,
38.7 TiB, 0 skipped, 0 failed**, with no leftover `.ecrw.*` temp files. It moved about
8.7 TiB/day on large files and slowed to ~200 files/min on the tiny-file `Roms/eXoDOS` tree.

| | before | after |
|---|---|---|
| `cephfs-data` stored | 78 TiB | 38 TiB (Blue Iris footage from before the layout change, plus ~2 TiB of other data) |
| `cephfs-ec` stored | 0 | 41 TiB |
| raw used | 236 TiB (41.6%) | 196 TiB (34.5%) |

During the run, median OSD apply latency rose from ~35 ms to ~100 ms, evenly across hosts.
Squid's `BLUESTORE_SLOW_OP_ALERT` (threshold: one slow op, held for 24h) stayed on for the
whole run. No camera recording gaps were seen.

`cephfs-data` keeps shrinking by about 3 TiB/week as old Blue Iris segments age out, reaching
about 2 TiB by early January 2027.

### How it was run

```sh
setfattr -n ceph.dir.layout.pool -v cephfs-ec /mnt/cephfs/media
```

New files, downloads included, then go to EC. Existing files are moved with
[`scripts/ceph/rewrite-to-pool.sh`](../scripts/ceph/rewrite-to-pool.sh) on a Proxmox host.
It copies each file to a temp name in its own directory, verifies it, and renames it over the
original. It is resumable and skips files already on EC, files modified in the last hour,
and hardlinks (4 in media as of 2026-10-04).

```sh
scripts/ceph/rewrite-to-pool.sh --dry-run /mnt/cephfs/media/Z/media              # count only
scripts/ceph/rewrite-to-pool.sh --bwlimit 150000 \
    --exclude '*/downloads/docker/incomplete/*' /mnt/cephfs/media/Z/media        # ~150 MB/s
# progress: tail -f /var/log/ceph-rewrite-media.log
```

Run it in `tmux` (installed on prox01). About 38.7 TiB at 150 MB/s is roughly 3 days, and freeing replicated
space as it goes (3× → 2×) recovers about 39 TiB raw. The space is transiently one file larger
at a time.

## Rollback

- **Stop new data going to EC.** `setfattr -n ceph.dir.layout.pool -v cephfs-data <dir>`.
  Files already written to EC stay readable.
- **Remove the pool.** Every file on it must first be rewritten back (the same script, after
  pointing the layout at `cephfs-data`). Then run
  `ceph fs rm_data_pool mycephfs cephfs-ec` and delete the pool, which needs
  `mon_allow_pool_delete`, currently `false`.
