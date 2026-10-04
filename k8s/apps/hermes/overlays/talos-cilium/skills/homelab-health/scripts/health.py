#!/usr/bin/env python3
"""Homelab health: Proxmox Samba/CTDB/CephFS/Ceph, Blue Iris recording, the
Home Assistant / Pi-hole / Vault / Bitwarden / registry endpoints, pods on both
clusters, backups, secret sync, Argo CD, UniFi firmware flashes, and the
SpaceTraders pods.

Reads only the st-readonly gateway's fixed Prometheus queries plus the two
agents' health views; holds no credentials and changes nothing.

  health.py report   full status, always prints (morning digest, "how's the lab")
  health.py check    only prints on change: a new problem, a resolved one, or
                     a reminder for one still open (CRIT every 6h, WARN daily).
                     Empty output = nothing to say; Hermes cron then stays quiet.

Installed twice: in the homelab-health skill, and in $HERMES_HOME/scripts as
health_check.py / health_report.py for script-only cron jobs, which can't
pass arguments -- so the file name picks the mode.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE = os.environ.get("ST_BASE", "http://st-readonly.hermes.svc.cluster.local:8080")
HOME = os.environ.get("HERMES_HOME", "/opt/data")
STATE = os.path.join(HOME, "cron", "homelab-health-state.json")
HOSTS = {"192.168.1.230": "prox01", "192.168.1.232": "prox02",
         "192.168.1.234": "prox03", "192.168.1.236": "prox04"}
REMIND = {"CRIT": 6 * 3600, "WARN": 24 * 3600}
ICON = {"CRIT": "🔴", "WARN": "🟡", "OK": "🟢", "INFO": "ℹ️"}

# Fixes from past incidents, shown next to the matching problem.
HINT = {
    "stale_bind": "smbd bound before a CTDB takeover; `systemctl restart smbd` on that host, then restart pods on SMB PVCs",
    "cephfs": "evicted CephFS mount: stop ctdb, `umount -l /mnt/cephfs && mount -a`, check `ls /mnt/cephfs/.ctdb/`, start ctdb, smbd, winbind",
    "recovery_loop": "CTDB recovery loop; usually a host that can't read the recovery lock on CephFS",
    "cores": "smbd panic storm, often ctdbd unreachable after a switch flap (03:20-04:00 = UniFi firmware upgrade); watch / on that host",
    "nmbd": "NetBIOS only, SMB unaffected; `systemctl start nmbd` on that host",
    "camera": "check the camera's feed in the Blue Iris UI (http://192.168.1.44:8081); a dead camera, a network drop, or Blue Iris itself stopped",
    "bi_rotation": "Blue Iris didn't move it to stored/; check Blue Iris clip storage settings, or move/delete the file by hand",
}
# blackbox `service` label -> (severity when down, what it means)
PROBES = {
    "homeassistant": ("CRIT", "Home Assistant (192.168.1.37:8123) not answering"),
    "blueiris": ("CRIT", "Blue Iris web server (192.168.1.44:8081) not answering"),
    "pihole-dns": ("CRIT", "Pi-hole (192.168.1.231) not resolving DNS"),
    "pihole-blocking": ("WARN", "Pi-hole answers but no longer blocks ads"),
    "pihole-web": ("WARN", "Pi-hole web UI not answering"),
    "vault": ("CRIT", "Vault sealed or down: ESO can't sync secrets on either cluster; unseal vault-0/1/2 on talos-mesh"),
    "registry": ("WARN", "registry.dlb.im not answering"),
    "bitwarden": ("WARN", "Bitwarden (bw.dlb.im) not answering"),
}
SKIP_NS = {"spacetraders", "spacetraders-erl"}  # covered by the SpaceTraders block
CAM_STALE = 300          # s without a write before a camera counts as not recording
ALERT_QUIET = 12 * 3600  # s without any alert image


def host(inst):
    return HOSTS.get(inst.split(":")[0], inst)


def get(path, timeout=20):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def prom(name):
    d = get("/prom/" + name)
    return [(r["metric"], float(r["value"][1])) for r in d["data"]["result"]]


def evaluate():
    """Returns (problems, facts): problems = {key: (sev, text)}; facts = [str]."""
    P, F = {}, []

    def bad(key, sev, text, hint=None):
        P[key] = (sev, text + (f" -> {HINT[hint]}" if hint else ""))

    try:
        get("/healthz", timeout=5)
    except Exception as e:
        bad("gateway", "CRIT", f"st-readonly gateway unreachable ({e}); nothing else could be checked")
        return P, F

    # ---- Proxmox hosts --------------------------------------------------------
    try:
        up = {host(m["instance"]): v for m, v in prom("host_up")}
        hl = prom("homelab")
    except Exception as e:
        bad("prometheus", "CRIT", f"Prometheus query failed ({e})")
        return P, F
    for h in HOSTS.values():
        if up.get(h, 0) < 1:
            bad(f"down:{h}", "CRIT", f"{h} node-exporter not answering (host down or off the network?)")

    by = {}
    for m, v in hl:
        by.setdefault(m["__name__"], []).append((m, v))

    def per_host(name):
        return {host(m["instance"]): v for m, v in by.get(name, [])}

    now = time.time()
    for h, ts in per_host("homelab_health_last_run_seconds").items():
        if now - ts > 300:
            bad(f"stale:{h}", "WARN", f"{h} health collector last ran {int((now - ts) / 60)} min ago (homelab-health.timer)")

    for m, v in by.get("homelab_unit_active", []):
        if v < 1:
            u, h = m["unit"], host(m["instance"])
            sev = "WARN" if u == "nmbd" else "CRIT"
            bad(f"unit:{h}:{u}", sev, f"{h}: {u} is not running", "nmbd" if u == "nmbd" else None)

    for h, v in per_host("homelab_ctdb_status_ok").items():
        if v < 1:
            bad(f"ctdb:{h}", "CRIT", f"{h}: `ctdb status` not answering")
    for h, v in per_host("homelab_ctdb_recovery_mode").items():
        if v >= 1:
            bad(f"recmode:{h}", "WARN", f"{h}: CTDB in recovery mode")
    nodes = {}
    for m, v in by.get("homelab_ctdb_nodes", []):
        nodes.setdefault(host(m["instance"]), {})[m["state"]] = v
    for h, n in nodes.items():
        if n.get("ok", 0) < n.get("total", 0):
            bad(f"ctdbnodes:{h}", "CRIT", f"{h} sees {int(n.get('ok', 0))}/{int(n.get('total', 0))} CTDB nodes OK")
    gens = set(per_host("homelab_ctdb_generation").values())
    if len(gens) > 1:
        bad("gen_split", "CRIT", f"CTDB nodes disagree on Generation ({len(gens)} values): split-brain")
    try:
        loop = max((v for _, v in prom("ctdb_recoveries_max_15m")), default=0)
        if loop > 3:
            bad("recovery_loop", "CRIT", f"CTDB started {int(loop)} recoveries within 5 min", "recovery_loop")
        changed = max((v for _, v in prom("ctdb_gen_changes_1h")), default=0)
        if changed > 0:
            bad("recovered", "WARN", f"CTDB ran {int(changed)} recovery(ies) in the last hour (Generation changed)")
    except Exception:
        pass

    held = {}
    listening = {}
    for m, v in by.get("homelab_ctdb_public_ip_held", []):
        if v >= 1:
            held.setdefault(m["ip"], []).append(host(m["instance"]))
    for m, v in by.get("homelab_smbd_listening", []):
        listening[(host(m["instance"]), m["ip"])] = v
    all_ips = {m["ip"] for m, _ in by.get("homelab_ctdb_public_ip_held", [])}
    for ip in sorted(all_ips):
        owners = held.get(ip, [])
        if not owners:
            bad(f"ip_unheld:{ip}", "CRIT", f"CTDB public IP {ip} is held by no host")
        elif len(owners) > 1:
            bad(f"ip_dup:{ip}", "CRIT", f"CTDB public IP {ip} held by {', '.join(owners)} at once")
        for h in owners:
            if listening.get((h, ip), 0) < 1:
                bad(f"stale_bind:{h}:{ip}", "CRIT", f"{h} holds {ip} but smbd isn't listening on it", "stale_bind")

    for h, v in per_host("homelab_cephfs_mounted").items():
        if v < 1:
            bad(f"cephfs_mount:{h}", "CRIT", f"{h}: /mnt/cephfs not mounted", "cephfs")
    for h, v in per_host("homelab_cephfs_readable").items():
        if v < 1 and per_host("homelab_cephfs_mounted").get(h, 0) >= 1:
            bad(f"cephfs:{h}", "CRIT", f"{h}: /mnt/cephfs mounted but unreadable (evicted?)", "cephfs")

    for h, v in per_host("homelab_samba_core_files_5m").items():
        if v >= 50:
            bad(f"cores:{h}", "CRIT", f"{h}: {int(v)} Samba cores in 5 min", "cores")
        elif v > 0:
            bad(f"cores:{h}", "WARN", f"{h}: {int(v)} Samba core(s) in the last 5 min", "cores")
    for h, v in per_host("homelab_samba_core_bytes").items():
        if v > 5e9:
            bad(f"corebytes:{h}", "WARN", f"{h}: /var/log/samba/cores holds {v / 1e9:.1f} GB")

    status = max(per_host("homelab_ceph_health_status").values(), default=None)
    checks = sorted({(m["check"], m["severity"]) for m, _ in by.get("homelab_ceph_health_check", [])})
    if status is None:
        bad("ceph_cli", "WARN", "no host could run `ceph health`")
    elif status >= 1:
        sev = "CRIT" if status >= 2 else "WARN"
        names = ", ".join(c for c, _ in checks) or "no detail"
        bad("ceph", sev, f"Ceph {'HEALTH_ERR' if status >= 2 else 'HEALTH_WARN'}: {names}")

    try:
        for m, v in prom("rootfs_free"):
            h = host(m["instance"])
            if v < 0.05:
                bad(f"rootfs:{h}", "CRIT", f"{h}: / is {100 * (1 - v):.0f}% full")
            elif v < 0.15:
                bad(f"rootfs:{h}", "WARN", f"{h}: / is {100 * (1 - v):.0f}% full")
            F.append(f"{h} / {100 * (1 - v):.0f}% used")
    except Exception:
        pass

    F.append(f"CTDB generation {', '.join(str(int(g)) for g in gens) or '?'}; "
             f"{sum(1 for n in nodes.values() if n.get('ok') == n.get('total'))}/{len(nodes)} hosts see all nodes OK")
    F.append("Ceph " + ({0: "HEALTH_OK", 1: "HEALTH_WARN", 2: "HEALTH_ERR"}.get(int(status), "?") if status is not None else "unknown"))
    cores = per_host("homelab_samba_core_bytes")
    if cores:
        F.append("Samba cores: " + ", ".join(f"{h} {v / 1e6:.0f} MB" for h, v in sorted(cores.items())))

    # ---- Ceph capacity --------------------------------------------------------
    pools = {}
    for m, v in by.get("homelab_ceph_pool_used_ratio", []):
        pools[m["pool"]] = max(pools.get(m["pool"], 0), v)
    for pool, v in pools.items():
        if v > 0.85:
            bad(f"pool:{pool}", "CRIT", f"Ceph pool {pool} {100 * v:.0f}% full")
        elif v > 0.75:
            bad(f"pool:{pool}", "WARN", f"Ceph pool {pool} {100 * v:.0f}% full")
    avail = {m["pool"]: v for m, v in by.get("homelab_ceph_pool_max_avail_bytes", [])}
    if "cephfs-data" in pools:
        F.append(f"CephFS data {100 * pools['cephfs-data']:.0f}% used, "
                 f"{avail.get('cephfs-data', 0) / 1e12:.0f} TB free")

    # ---- Blue Iris (clips on CephFS) ---------------------------------------------
    try:
        ages = {m["camera"]: v for m, v in prom("bi_camera_age")}
        rates = {m["camera"]: v for m, v in prom("bi_bitrate")}
    except Exception as e:
        ages, rates = {}, {}
        bad("bi_metrics", "WARN", f"Blue Iris metrics unavailable ({e})")
    if not ages and "bi_metrics" not in P:
        bad("bi_none", "CRIT", "no Blue Iris recordings seen on CephFS at all", "camera")
    stale = sorted(c for c, a in ages.items() if a > CAM_STALE)
    for c in stale:
        bad(f"cam:{c}", "CRIT", f"camera {c} stopped recording ({int(ages[c] / 60)} min since last write)", "camera")
    if ages:
        F.append(f"Blue Iris: {len(ages) - len(stale)}/{len(ages)} cameras recording"
                 + (f" ({', '.join(f'{c} {8 * r / 1e6:.1f} Mb/s' for c, r in sorted(rates.items()))})" if rates else ""))
    now_s = time.time()
    last_alert = max((v for _, v in by.get("homelab_blueiris_alert_last_seconds", [])), default=None)
    if last_alert is not None:
        if last_alert == 0 or now_s - last_alert > ALERT_QUIET:
            bad("bi_alerts", "WARN", "no Blue Iris alert images in 12h+ (AI/motion triggers may be broken)")
        else:
            F.append(f"Blue Iris last alert {int((now_s - last_alert) / 60)} min ago")
    oldest = min((v for _, v in by.get("homelab_blueiris_new_oldest_seconds", []) if v > 0), default=None)
    if oldest and now_s - oldest > 2 * 86400:
        since = datetime.fromtimestamp(oldest).astimezone().strftime("%b %-d")
        bad("bi_rotation", "WARN", f"a Blue Iris segment from {since} is still in new/", "bi_rotation")

    # ---- Services (blackbox probes) -------------------------------------------
    try:
        probes = {m.get("service", m.get("instance", "?")): v for m, v in prom("probes")}
        for svc, (sev, text) in PROBES.items():
            if svc not in probes:
                bad(f"probe_missing:{svc}", "WARN", f"no probe result for {svc} (blackbox exporter down?)")
            elif probes[svc] < 1:
                bad(f"probe:{svc}", sev, text)
        up = sorted(s_ for s_, v in probes.items() if v >= 1)
        F.append(f"Services up: {', '.join(up) or 'none'}")
        for m, days in prom("cert_days"):
            svc = m.get("service", "?")
            if days < 3:
                bad(f"cert:{svc}", "CRIT", f"TLS cert for {svc} expires in {days:.1f} days")
            elif days < 14:
                bad(f"cert:{svc}", "WARN", f"TLS cert for {svc} expires in {days:.0f} days (cert-manager renewal failing?)")
    except Exception as e:
        bad("probes", "WARN", f"service probes unavailable ({e})")

    # ---- Kubernetes, both clusters (kube-state-metrics) ---------------------------
    def where(m):
        return f"{m.get('cluster', 'talos-cilium')}/{m.get('namespace', '?')}"

    try:
        for name, label, what in (("k8s_deploy_unavail", "deployment", "replica(s) unavailable"),
                                  ("k8s_sts_unready", "statefulset", "replica(s) not ready"),
                                  ("k8s_ds_unavail", "daemonset", "pod(s) unavailable")):
            for m, v in prom(name):
                if m.get("namespace") in SKIP_NS:
                    continue
                bad(f"k8s:{where(m)}/{m.get(label)}", "CRIT",
                    f"{where(m)} {label} {m.get(label)}: {int(v)} {what} for 10+ min")
        for m, _ in prom("k8s_waiting"):
            if m.get("namespace") in SKIP_NS:
                continue
            bad(f"wait:{where(m)}/{m.get('pod')}", "CRIT",
                f"{where(m)} pod {m.get('pod')} stuck in {m.get('reason')}")
        for m, _ in prom("k8s_pending"):
            bad(f"pending:{where(m)}/{m.get('pod')}", "WARN", f"{where(m)} pod {m.get('pod')} Pending for 10+ min")
        for m, v in prom("pvc_used"):
            if m.get("namespace") in SKIP_NS:
                continue
            bad(f"pvc:{where(m)}/{m.get('persistentvolumeclaim')}", "CRIT" if v > 0.95 else "WARN",
                f"PVC {where(m)}/{m.get('persistentvolumeclaim')} {100 * v:.0f}% full")
    except Exception as e:
        bad("k8s_metrics", "WARN", f"cluster pod metrics unavailable ({e})")

    # ---- Backups, secrets, GitOps ----------------------------------------------------
    try:
        ages = prom("velero_age")
        failed = {m.get("schedule", "?"): v for m, v in prom("velero_failed_26h")}
        for sched, n in failed.items():
            if n >= 0.5:
                bad(f"velero_fail:{sched}", "WARN",
                    f"Velero schedule {sched}: {round(n)} backup(s) failed or partially failed in the last day "
                    f"(`velero backup describe <name> --details` in the velero pod shows the item)")
        if not ages and not failed:
            bad("velero_none", "WARN", "no Velero backup metrics (velero down?)")
        elif not ages:
            bad("velero_never", "WARN", "no fully successful Velero backup since the velero pod started")
        for m, age in ages:
            sched = m.get("schedule", "?")
            if age > 50 * 3600:
                bad(f"velero:{sched}", "CRIT", f"Velero schedule {sched}: last successful backup {age / 3600:.0f}h ago")
            elif age > 26 * 3600:
                bad(f"velero:{sched}", "WARN", f"Velero schedule {sched}: last successful backup {age / 3600:.0f}h ago")
            else:
                F.append(f"Velero {sched}: last good backup {age / 3600:.0f}h ago")
    except Exception as e:
        bad("velero_metrics", "WARN", f"Velero metrics unavailable ({e})")
    try:
        for m, _ in prom("eso_not_ready"):
            bad(f"eso:{m.get('namespace')}/{m.get('name')}", "WARN",
                f"ExternalSecret {m.get('namespace')}/{m.get('name')} not syncing from Vault")
    except Exception:
        pass
    try:
        apps = prom("argocd_apps")
        if not apps:
            raise RuntimeError("no argocd_app_info series")
        sick = [m for m, _ in apps if m.get("health_status") not in ("Healthy", "Progressing")]
        for m in sick:
            bad(f"argo:{m.get('name')}", "WARN", f"Argo CD app {m.get('name')} is {m.get('health_status')}")
        drift = sorted(m.get("name") for m, _ in apps if m.get("sync_status") == "OutOfSync")
        F.append(f"Argo CD: {len(apps) - len(sick)}/{len(apps)} apps healthy"
                 + (f"; OutOfSync: {', '.join(drift)}" if drift else ""))
    except Exception as e:
        bad("argo_metrics", "WARN", f"Argo CD app metrics unavailable ({e})")

    # ---- UniFi firmware flashes (Loki) --------------------------------------------
    try:
        res = get("/syslog/firmware").get("data", {}).get("result", [])
        hosts = {}
        for st in res:
            h = st.get("stream", {}).get("host", "?")
            for ts, _ in st.get("values", []):
                hosts[h] = min(hosts.get(h, int(ts)), int(ts))
        for h, ts in sorted(hosts.items()):
            t = datetime.fromtimestamp(ts / 1e9).astimezone().strftime("%a %-I:%M %p")
            bad(f"firmware:{h}", "WARN", f"UniFi {h} flashed firmware {t} (the nightly auto-upgrade; it has caused CTDB/Samba outages)")
    except Exception:
        pass

    # ---- SpaceTraders pods ------------------------------------------------------
    spec = {}
    try:
        spec = {m["deployment"]: v for m, v in prom("st_deploy_spec")}
        avail = {m["deployment"]: v for m, v in prom("st_deploy_avail")}
        for d in ("spacetraders", "spacetraders-erl"):
            s, a = spec.get(d), avail.get(d, 0)
            if s is None:
                bad(f"st:{d}", "CRIT", f"deployment {d} not found")
            elif s == 0:
                F.append(f"{d}: scaled to 0 (deliberate?)")
            elif a < s:
                bad(f"st:{d}", "CRIT", f"{d}: {int(a)}/{int(s)} replicas available")
            else:
                F.append(f"{d}: {int(a)}/{int(s)} up")
        ready = {m["pod"]: v for m, v in prom("st_pods_ready")}
        for want, label in (("spacetraders-pg-", "Postgres"), ("spacetraders-redis-master-", "Redis")):
            pods = {p: v for p, v in ready.items() if p.startswith(want)}
            if not pods:
                bad(f"st:{label}", "CRIT", f"{label}: no pod found")
            elif not any(v >= 1 for v in pods.values()):
                bad(f"st:{label}", "CRIT", f"{label}: not ready ({', '.join(pods)})")
            else:
                F.append(f"{label}: ready")
        for m, v in prom("st_restarts_1h"):
            bad(f"restart:{m['pod']}:{m.get('container', '')}", "WARN",
                f"{m['pod']} ({m.get('container', '?')}) restarted {v:.0f}x in the last hour")
        for m, v in prom("st_pvc_used"):
            if v > 0.85:
                bad(f"pvc:{m['persistentvolumeclaim']}", "WARN" if v < 0.95 else "CRIT",
                    f"PVC {m['persistentvolumeclaim']} {100 * v:.0f}% full")
    except Exception as e:
        bad("st_metrics", "WARN", f"SpaceTraders pod metrics unavailable ({e})")

    for label, path in (("Python agent API", "/py/healthz"), ("Erlang agent API", "/erl/live/clock")):
        if spec.get("spacetraders" if label.startswith("Python") else "spacetraders-erl", 1) == 0:
            continue
        try:
            get(path, timeout=10)
        except Exception as e:
            bad(f"api:{label}", "WARN", f"{label} not answering ({e})")
    return P, F


def stamp():
    return datetime.now().astimezone().strftime("%a %-I:%M %p %Z")


def report():
    P, F = evaluate()
    worst = "CRIT" if any(s == "CRIT" for s, _ in P.values()) else "WARN" if P else "OK"
    print(f"{ICON[worst]} Homelab health, {stamp()}")
    for sev in ("CRIT", "WARN"):
        for k, (s, t) in sorted(P.items()):
            if s == sev:
                print(f"{ICON[s]} {t}")
    if not P:
        print("No problems found.")
    for f in F:
        print(f"  · {f}")


def check():
    P, _ = evaluate()
    try:
        with open(STATE) as fh:
            state = json.load(fh)
    except Exception:
        state = {}
    now = time.time()
    out_new, out_still, out_done = [], [], []
    for k, (sev, text) in P.items():
        st = state.get(k)
        if st is None:
            out_new.append(f"{ICON[sev]} NEW: {text}")
            state[k] = {"first": now, "sent": now, "sev": sev, "text": text}
        elif sev == "CRIT" and st.get("sev") != "CRIT":
            out_new.append(f"{ICON[sev]} WORSE: {text}")
            st.update(sent=now, sev=sev, text=text)
        elif now - st.get("sent", 0) >= REMIND[sev]:
            since = datetime.fromtimestamp(st["first"]).astimezone().strftime("%a %-I:%M %p")
            out_still.append(f"{ICON[sev]} still, since {since}: {text}")
            st.update(sent=now, sev=sev)
    for k in [k for k in state if k not in P]:
        out_done.append(f"✅ resolved: {state[k].get('text', k).split(' -> ')[0]}")
        del state[k]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)
    if out_new or out_still or out_done:
        print(f"Homelab watch, {stamp()}")
        print("\n".join(out_new + out_still + out_done))


if __name__ == "__main__":
    base = os.path.basename(sys.argv[0])
    mode = sys.argv[1] if len(sys.argv) > 1 else ("check" if "check" in base else "report")
    {"check": check, "report": report}[mode]()
