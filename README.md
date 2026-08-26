# talos-k8s-argocd

GitOps configuration for a two-cluster, bare-metal **Kubernetes** homelab running on
**Talos Linux**, continuously reconciled by **Argo CD**. Everything here is declarative —
the clusters are a pure function of this repository.

The OS/infrastructure layer beneath Argo CD — Proxmox VM provisioning (OpenTofu) and Talos
machine config — is documented in **[TALOS.md](TALOS.md)** (code under [`talos/`](talos/)).

## Clusters

| Cluster        | Role                                   | CNI / Networking            |
| -------------- | -------------------------------------- | --------------------------- |
| `talos-cilium` | Apps, media, monitoring, storage       | Cilium (kube-proxy-less)    |
| `talos-mesh`   | Bitwarden, Vault, cert-managed ingress  | Cilium + Gateway API        |

A single Argo CD instance (on `talos-cilium`) manages both clusters.

## How it's wired

A root **ApplicationSet** ([`applicationset.yaml`](applicationset.yaml)) walks
`k8s/<group>/<component>/overlays/<cluster>/` and generates one Argo CD Application per
overlay, named `<cluster>-<component>`. Adding a component is just adding a directory —
no Application boilerplate to maintain.

```
k8s/<group>/<component>/
  base/                      # optional shared manifests
  overlays/talos-cilium/     # per-cluster overlay (= one Argo CD Application)
  overlays/talos-mesh/
```

Components are rendered with Kustomize; Helm-based components use Kustomize's native
`helmCharts` (rendered server-side by Argo CD, charts pulled at sync time — not vendored).
The one exception is **Cilium** (the CNI): it's a multi-source Helm Application with custom
`ignoreDifferences` (to protect the live-patched `cilium-config`, e.g. `MTU: 1450`, and the
clustermesh/Hubble trust certificates), so it lives in standalone manifests applied directly
rather than via the ApplicationSet — [`cilium-apps.yaml`](cilium-apps.yaml) for `talos-cilium`
and [`cilium-mesh-apps.yaml`](cilium-mesh-apps.yaml) for `talos-mesh`.

The two clusters are **clustermesh-joined** (cluster id 1 ↔ 2, KVStoreMesh). Both value files
declare clustermesh explicitly so a Helm upgrade can never prune the apiserver "embassy"; the
hand-established trust secrets (`cilium-ca`, `cilium-clustermesh`, `clustermesh-apiserver-*-cert`)
are protected via `ignoreDifferences` + `RespectIgnoreDifferences` and are never regenerated.

## Components

| Group | Component | Cluster(s) | Notes |
| ----- | --------- | ---------- | ----- |
| infrastructure | vault | mesh | HashiCorp Vault HA (Raft) · `vault.dlb.im` |
| infrastructure | external-secrets | both | ESO + `vault-backend` ClusterSecretStore |
| infrastructure | cert-manager | mesh | Let's Encrypt DNS-01 (Cloudflare) · Vault `secret/cloudflare` |
| infrastructure | gateway-api | mesh | Gateway API CRDs |
| infrastructure | snapshot-controller | both | CSI VolumeSnapshot controller |
| infrastructure | metrics-server | both | Metrics Server v0.8.0 (`kubectl top` / HPA) |
| infrastructure | network-policies | both | CiliumNetworkPolicies (default-deny + allow rules per namespace) |
| infrastructure | smb-csi | cilium | SMB CSI driver + PVs · Vault `secret/samba` |
| infrastructure | multus | both | Multus thick-plugin CNI shim (→ Cilium) + VPN NetworkAttachmentDefinitions (cilium) |
| infrastructure | ceph-csi | both | Ceph RBD + CephFS CSI (rbd-nbd) · Vault `secret/ceph-rbd` + `secret/ceph-cephfs` |
| infrastructure | cilium | both | CNI (Helm 1.17.2, kube-proxy-less, clustermesh-joined). cilium: MTU 1450 + LB pool/L2 via `cilium-apps.yaml`. mesh: Gateway API via `cilium-mesh-apps.yaml`; LB pool/L2 via appset |
| infrastructure | cloudnative-pg | cilium | CloudNativePG operator (chart 0.22.1) — manages PostgreSQL `Cluster` CRs |
| infrastructure | coredns | cilium | Cluster CoreDNS `Corefile` (hosts block for `smb.local`, `vault.dlb.im`, `registry.dlb.im`) under GitOps so live edits don't drift |
| apps | velero | both | Backups → MinIO (S3) · Vault `secret/velero` |
| apps | github-runner | cilium | Self-hosted GitHub Actions runners (myoung34/github-runner + docker:dind) for the `spacetraders`, `spacetraders-sim` and `spacetraders-erlang` repos · Vault `secret/github-runner` |
| apps | registry | mesh | Self-hosted Docker registry (`registry:2`) behind Cilium Gateway · LE cert for `registry.dlb.im` · 50Gi Ceph RBD |
| apps | spacetraders-pg | cilium | PostgreSQL 16 (CNPG, 1 instance, 20Gi RBD), LAN-exposed @ `192.168.4.140:5432` · Vault `secret/spacetraders/pg` |
| apps | spacetraders-redis | cilium | Redis 7 standalone (Bitnami, 4Gi RBD), LAN-exposed @ `192.168.4.141:6379` · Vault `secret/spacetraders/redis` |
| apps | spacetraders | cilium | Hub (planner + worker pool + Quart dashboard) from `registry.dlb.im/spacetraders:latest` · dashboard @ `192.168.4.142` · Vault `secret/spacetraders/agent` |
| apps | spacetraders-erl | cilium | Erlang/OTP agent (`BBPUGZINSPACE`) from `registry.dlb.im/spacetraders-erlang:latest` · own PSA-privileged ns · egress via multus → vpn-gateway · dashboard @ `192.168.4.144` · control API ClusterIP `:5173` · Vault `secret/spacetraders/agent-erl` |
| apps | vpn-gateway | cilium | gluetun (NordVPN/WireGuard) · Vault `secret/nordvpn` |
| apps | media | cilium | *arr stack + qBittorrent, VPN egress via multus |
| apps | bitwarden | mesh | Self-hosted Bitwarden (Helm self-host 1.0.4 + MSSQL) · Vault `secret/bitwarden` · standalone `bitwarden-apps.yaml` |
| apps | filebrowser | cilium | nginx SMB file browser |
| monitoring | loki | cilium | Log store (Loki, single-binary) |
| monitoring | promtail | cilium | Log shipper + UniFi syslog receiver |
| monitoring | grafana-dashboards | cilium | UniFi + Windows-exporter dashboard ConfigMaps |
| monitoring | logging-services | cilium | External LoadBalancer Services (Loki / syslog) |
| monitoring | kube-prometheus-stack | both | Prometheus/Grafana/Alertmanager · Vault `secret/grafana` + `secret/alertmanager` (mesh remote-writes to cilium) |

## Secrets

No secret material lives in this repository — by construction, so it is safe to publish.

- **HashiCorp Vault** (HA, integrated Raft storage) is the single source of truth for
  secrets, fronted by the **External Secrets Operator** in each cluster.
- Apps declare `ExternalSecret` resources that reference Vault paths; ESO syncs them into
  native Kubernetes `Secret`s at runtime.
- ESO authenticates to Vault via the **Kubernetes auth method** — the pod's ServiceAccount
  token, not a static credential. A central Vault on `talos-mesh` validates each cluster
  through its own auth mount.
- Vault unseal keys and root token are held offline and **never** committed.

## Stack

Talos Linux · Kubernetes · Cilium (eBPF) · Argo CD · Kustomize/Helm · HashiCorp Vault ·
External Secrets Operator · cert-manager · Gateway API · Ceph CSI (RBD/CephFS) · Velero
