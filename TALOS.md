# Talos / OS layer

The layer *below* Argo CD: how the bare VMs and the Talos OS that Kubernetes runs on
are provisioned. Argo CD and everything in [`k8s/`](k8s/) assume a running cluster — this
is how that cluster comes to exist.

> **Scope note.** This documents `talos-cilium`. `talos-mesh` was brought up the same way
> (Talos on Proxmox, Cilium CNI, kube-proxy-less) with its own addresses/VLANs.

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
2. **Creates the VMs** (`main.tf`): one `talos-cilium-N` VM per Proxmox node — 4 cores, 16 GiB,
   100 GiB virtio disk, QEMU guest agent, and **two NICs**: external (VLAN 3, `192.168.4.0/24`)
   and internal (VLAN 2, `192.168.3.0/24`), with stable MACs. `ignore_changes` on the NICs so
   re-applies don't churn networking.
3. **Outputs** (`outputs.tf`): VM name → node / vmid / IPs.

Config is all variables ([`variables.tf`](talos/proxmox/variables.tf)); secrets come from
`terraform.tfvars` (gitignored). Copy [`terraform.tfvars.example`](talos/proxmox/terraform.tfvars.example)
to start.

```bash
cd talos/proxmox
cp terraform.tfvars.example terraform.tfvars   # fill in Proxmox API token
tofu init
tofu apply
```

## `talos/patches/` — Talos machine config

Talos is configured declaratively. A base config is generated with `talosctl gen config`, then
these patches are layered on. They encode the decisions that make this cluster work:

| Patch | What it does | Why |
|---|---|---|
| `patch.yaml` | `cluster.network.cni.name: none` + `proxy.disabled: true` | Cilium is the CNI **and** the kube-proxy replacement — Talos must not install either. |
| `node1-patch.yaml` … `node4-patch.yaml` | Per-node hostname, the two interfaces (eth0 external + eth1 internal), routes, nameservers, and the shared **VIP `192.168.4.10`** for the API server | Static addressing; the VIP is the stable control-plane endpoint. |
| `patch-nbd-module.yaml` | Loads the `nbd` kernel module | Talos has no in-kernel RBD; Ceph RBD volumes are mounted via **rbd-nbd**. |
| `patch-drop-all.yaml` / `patch-drop-ipv6.yaml` | Kubelet capability + unsafe-sysctl tweaks (`src_valid_mark`, ipv6 toggles) | Required for Cilium datapath / VPN egress pods. |

All four nodes are control plane (no separate workers); the control-plane taint is removed so
workloads schedule on them.

Apply (sketch):

```bash
talosctl gen config talos-cilium https://192.168.4.10:6443 --output-dir _out   # _out/ is gitignored
talosctl machineconfig patch _out/controlplane.yaml --patch @patches/patch.yaml \
  --patch @patches/node1-patch.yaml --patch @patches/patch-nbd-module.yaml -o node1.yaml
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
