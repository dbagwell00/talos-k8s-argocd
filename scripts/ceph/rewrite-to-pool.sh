#!/bin/bash
# Move existing files under a CephFS directory onto the data pool its layout
# names, by rewriting them in place. Run on a Proxmox host, as root, against
# the kernel mount (/mnt/cephfs/...).
#
#   rewrite-to-pool.sh [--dry-run] [--bwlimit KBPS] [--min-age MIN] [--exclude GLOB]... DIR
#
# Why: a directory layout (ceph.dir.layout.pool) only applies to files created
# after it's set, and a rename inside CephFS never moves data. So each file is
# copied to a temp name in its own directory (the copy is a new file and lands
# on the target pool), checked, then renamed over the original. The rename is
# atomic; a reader with the old file open keeps reading the old data until it
# closes it.
#
# Safe to stop and re-run: files already on the target pool are skipped.
# Skipped on purpose: files modified in the last --min-age minutes (still being
# written), hardlinked files (a rewrite would split the link and double the
# space), and anything matching --exclude.
set -uo pipefail

dry=0; bw=0; minage=60; excludes=()
while [ $# -gt 1 ]; do
  case "$1" in
    --dry-run) dry=1; shift ;;
    --bwlimit) bw="$2"; shift 2 ;;
    --min-age) minage="$2"; shift 2 ;;
    --exclude) excludes+=("$2"); shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
dir="${1:?usage: $0 [--dry-run] [--bwlimit KBPS] [--min-age MIN] [--exclude GLOB]... DIR}"
dir="${dir%/}"

# The target is the pool on the nearest ancestor-or-self with an explicit layout.
target=""; d="$dir"
while [ -z "$target" ] && [ "$d" != "/" ]; do
  target=$(getfattr --only-values -n ceph.dir.layout.pool "$d" 2>/dev/null) || target=""
  d=$(dirname "$d")
done
[ -n "$target" ] || { echo "no ceph.dir.layout.pool on $dir or its parents; nothing to do" >&2; exit 1; }

exec 9>/run/ceph-rewrite.lock
flock -n 9 || { echo "another rewrite is running" >&2; exit 1; }

log=/var/log/ceph-rewrite-$(basename "$dir").log
echo "$(date -Is) start dir=$dir target=$target dry=$dry bwlimit=${bw}KB/s min-age=${minage}m" | tee -a "$log"

tmp=""
trap '[ -n "$tmp" ] && rm -f -- "$tmp"; echo "$(date -Is) interrupted" | tee -a "$log"; exit 130' INT TERM

done_n=0; done_b=0; skip_n=0; fail_n=0; ok_n=0
find_args=("$dir" -type f -mmin "+$minage" ! -name '.ecrw.*')
for g in "${excludes[@]}"; do find_args+=(! -path "$g"); done

while IFS= read -r -d '' f; do
  pool=$(getfattr --only-values -n ceph.file.layout.pool "$f" 2>/dev/null) || { echo "FAIL layout $f" >>"$log"; ((fail_n++)); continue; }
  if [ "$pool" = "$target" ]; then ((ok_n++)); continue; fi
  if [ "$(stat -c %h "$f")" -gt 1 ]; then echo "SKIP hardlink $f" >>"$log"; ((skip_n++)); continue; fi
  size=$(stat -c %s "$f")
  if [ $dry -eq 1 ]; then echo "WOULD $pool->$target $size $f" >>"$log"; ((done_n++)); done_b=$((done_b + size)); continue; fi

  before=$(stat -c '%s %Y %i' "$f")
  tmp="$(dirname "$f")/.ecrw.$(basename "$f").$$"
  if ! ionice -c3 rsync -aX --bwlimit="$bw" -- "$f" "$tmp" 2>>"$log"; then
    echo "FAIL copy $f" >>"$log"; rm -f -- "$tmp"; tmp=""; ((fail_n++)); continue
  fi
  after=$(stat -c '%s %Y %i' "$f")
  tpool=$(getfattr --only-values -n ceph.file.layout.pool "$tmp" 2>/dev/null)
  if [ "$before" != "$after" ] || [ "$(stat -c %s "$tmp")" != "$size" ] || [ "$tpool" != "$target" ]; then
    echo "FAIL verify $f (changed during copy, size mismatch, or copy on $tpool)" >>"$log"
    rm -f -- "$tmp"; tmp=""; ((fail_n++)); continue
  fi
  mv -f -- "$tmp" "$f" && tmp="" || { echo "FAIL rename $f" >>"$log"; rm -f -- "$tmp"; tmp=""; ((fail_n++)); continue; }
  echo "OK $size $f" >>"$log"
  ((done_n++)); done_b=$((done_b + size))
  if (( done_n % 200 == 0 )); then
    echo "$(date -Is) progress rewritten=$done_n ($((done_b / 1073741824)) GiB) skipped=$skip_n failed=$fail_n" | tee -a "$log"
  fi
done < <(find "${find_args[@]}" -print0)

echo "$(date -Is) done $([ $dry -eq 1 ] && echo '(dry run) would rewrite' || echo rewrote)=$done_n" \
     "($((done_b / 1073741824)) GiB) already-on-target=$ok_n skipped=$skip_n failed=$fail_n log=$log" | tee -a "$log"
