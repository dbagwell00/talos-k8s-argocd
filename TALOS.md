# Talos / OS layer

The layer *below* Argo CD: how the bare VMs and the Talos OS that Kubernetes runs on
are provisioned. Argo CD and everything in [`k8s/`](k8s/) assume a running cluster — this
is how that cluster comes to exist.

> **Scope note.** This documents `talos-cilium`. `talos-mesh` was brought up the same way
> (Talos on Proxmox, Cilium CNI, kube-proxy-less) with its own addresses/VLANs. Its VMs were
> built by hand and adopted into OpenTofu on 2026-09-24 (`mesh.tf`); its Talos patches are not
> in this repo.

## Layers, bottom to top

```
Proxmox (bare metal)
  └─ OpenTofu  ── creates VMs from a Talos Image Factory image   → talos/proxmox/
       └─ Talos machine config + per-node patches                → talos/patches/
            └─ Kubernetes (Talos-managed control plane)
                 └─ Cilium CNI  (installed by Argo CD, not Talos) → ../cilium-apps.yaml
                      └─ Argo CD app-of-apps                      → ../applicationset.yaml
```

## `talos/proxmox/` — VM provisioning (OpenTofu)

OpenTofu using the `bpg/proxmox` provider. It:

1. **Builds the image** (`image.tf`): POSTs [`image/schematic.yaml`](talos/proxmox/image/schematic.yaml)
   to the [Talos Image Factory](https://factory.talos.dev), gets a schematic ID, and downloads
   the resulting `nocloud` raw image to each Proxmox node. The schematic just adds the
   `qemu-guest-agent` extension on top of stock Talos `v1.8.3`.
2. **Creates the VMs** (`main.tf`): one `talos-cilium-N` VM per Proxmox node (`prox01`–`prox04`)
   — 8 cores, 16 GiB (24 GiB on node 4, see `vm_memory_mb`), a 100 GiB virtio disk on
   `local-lvm`, QEMU guest agent, and **two NICs**: external (VLAN 3, `192.168.4.0/24`)
   and internal (VLAN 2, `192.168.3.0/24`), with stable MACs. `ignore_changes` covers the NICs,
   so re-applies don't churn networking, and the disk's source image (`disk[0].file_id`), so a
   new Talos image or a VM that moved hosts never forces a rebuild; Talos upgrades happen in
   place with `talosctl upgrade`.
3. **Adopts the talos-mesh VMs** (`mesh.tf`): `talos-mesh-1..3` (VM 9001–9003 on `prox01`,
   `prox02`, `prox04`), 6 cores / 16 GiB, static addresses through a cloud-init drive on
   VLAN 5 (`192.168.6.0/24`) and VLAN 4 (`192.168.5.0/24`). The `import` block adopts the
   existing VMs; once it has been applied it can be deleted.
4. **Outputs** (`outputs.tf`): VM name → node / vmid / IPs.

**Keep VM disks on `local-lvm` (node-local NVMe), not Ceph.** etcd fsyncs every write, and a
durable 4k write on the HDD-backed Ceph pool measured ~100 ms against ~4.6 ms on local NVMe;
etcd wants its WAL fsync p99 under 10 ms. On Ceph, talos-mesh kept losing leader leases
(kube-controller-manager/kube-scheduler restarted ~3x a day each). etcd already replicates
across nodes, so node-local disks lose nothing.

Config is all variables ([`variables.tf`](talos/proxmox/variables.tf)); secrets come from
`terraform.tfvars` (gitignored). Copy [`terraform.tfvars.example`](talos/proxmox/terraform.tfvars.example)
to start.

```bash
cd talos/proxmox
cp terraform.tfvars.example terraform.tfvars   # fill in Proxmox API token
tofu init
tofu plan    # always read it: a stale config here once planned to destroy talos-cilium-2
tofu apply
```

The state file (`terraform.tfstate`, gitignored) lives only in `talos/proxmox/` on the admin
workstation. It is the single copy; back it up, or move it to a remote backend.

## `talos/patches/` — Talos machine config

Talos is configured declaratively. A base config is generated with `talosctl gen config`, then
these patches are layered on. They encode the decisions that make this cluster work:

| Patch | What it does | Why |
|---|---|---|
| `patch.yaml` | `cluster.network.cni.name: none` + `proxy.disabled: true` + `allowSchedulingOnControlPlanes: true` | Cilium is the CNI **and** the kube-proxy replacement — Talos must not install either. Every node is a control plane, so workloads must be allowed onto them (see below). |
| `node1-patch.yaml` … `node4-patch.yaml` | Per-node hostname, the two interfaces (eth0 external + eth1 internal), routes, nameservers, and the shared **VIP `192.168.4.10`** for the API server | Static addressing; the VIP is the stable control-plane endpoint. |
| `patch-drop-all.yaml` / `patch-drop-ipv6.yaml` | Kubelet capability + unsafe-sysctl *allowlist* (`src_valid_mark`, ipv6 toggles) | Lets pods request these sysctls. The allowlist is harmless; **setting** `net.ipv4.conf.all.src_valid_mark=1` at the node level is not -- see below. |

All four nodes are control plane (no separate workers). `allowSchedulingOnControlPlanes: true`
is what keeps Talos from tainting them `node-role.kubernetes.io/control-plane:NoSchedule`.
Without it the taint doesn't evict running pods, so it can go unnoticed for months, until a
reboot recreates pods and they all sit Pending. That happened on 2026-09-24.

**Ceph RBD uses the in-kernel client (krbd); no module or extension is needed.** The Talos
kernel has `CONFIG_BLK_DEV_RBD=y` and `CONFIG_CEPH_FS=y` built in. An earlier setup assumed
otherwise and mounted RBD through `rbd-nbd` with the `nbd` module loaded. That served every
volume's I/O from processes inside the `csi-rbdplugin` pods, so plugin restarts disrupted
mounted volumes, and sequential writes were about half as fast. talos-mesh never loaded `nbd`,
so ceph-csi quietly fell back to krbd there all along. Don't bring `nbd` back.

Apply (sketch):

```bash
talosctl gen config talos-cilium https://192.168.4.10:6443 --output-dir _out   # _out/ is gitignored
talosctl machineconfig patch _out/controlplane.yaml --patch @patches/patch.yaml \
  --patch @patches/node1-patch.yaml -o node1.yaml
talosctl apply-config --insecure --nodes 192.168.4.20 --file node1.yaml
# …repeat per node, then:
talosctl bootstrap --nodes 192.168.4.20
talosctl kubeconfig
```

## What is **not** in this repo (and why)

These are gitignored — they hold secrets or are per-environment, and this repo is public:

- **`terraform.tfvars`** — Proxmox API token.
- **`terraform.tfstate*`** — OpenTofu state.
- **`_out/`, generated `controlplane.yaml` / `worker.yaml`** — the Talos **secrets bundle**
  (cluster CA private keys, bootstrap token, secretbox/encryption keys, service-account key).
- **`talosconfig`, kubeconfigs** — client credentials.

These live only on the admin workstation. The cross-cluster **clustermesh** trust is likewise
established out-of-band (`cilium clustermesh connect`) — see the Cilium notes in the
[main README](README.md).

## Do not set `net.ipv4.conf.all.src_valid_mark` at the node level

It silently breaks Cilium's TPROXY delivery to the per-node Envoy, which is how
all Gateway API / L7 traffic reaches the proxy. Symptom: every Gateway reports
`Programmed: True`, Envoy holds the listener and is bound to the right per-node
proxy port, the `CILIUM_PRE_mangle` TPROXY rule matches the packet, and
`ip route get <svc> mark 0x200` resolves to `local ... dev lo table 2004` -- yet
connections hang. Envoy's `downstream_cx_total` stays at 0. There is no BPF drop
event and nothing appears on `lo`, because TPROXY assigns the socket in
PREROUTING and delivers via LOCAL_IN without re-transmitting over `lo`.

Confirmed by toggling it live on `talos-cilium-2`, twice in each direction:

| `all.src_valid_mark` | Gateway |
|---|---|
| `1` | TimeoutError |
| `0` | HTTP 200/404 |

`talos-mesh` has always had `0`, which is why its vault / registry / bitwarden
Gateways have always worked on the same Cilium 1.17.2, kernel 6.6.60 and Talos
v1.8.3.

It is not needed by the VPN egress pod. The mechanism is inheritance, not
per-pod configuration: a new pod netns copies `conf/all` from the host at
creation time, so while the host was `1` the `vpn-gateway` pod also read `1`,
and now that the host is `0` a freshly restarted pod reads `0`. It works either
way -- verified after the change by restarting the pod: WireGuard connected, and
all six media clients (qbittorrent, sonarr, radarr, prowlarr, profilarr,
flaresolverr) egress via the VPN exit IP rather than the WAN IP, with their LAN
UIs still reachable.

Keep the kubelet `allowed-unsafe-sysctls` entry so a pod that genuinely needs it
can request it per-pod instead of relying on host inheritance.

Apply the removal without a reboot:

```bash
talosctl -n <node> patch machineconfig --mode=no-reboot \
  --patch '[{"op":"remove","path":"/machine/sysctls/net.ipv4.conf.all.src_valid_mark"}]'
```
