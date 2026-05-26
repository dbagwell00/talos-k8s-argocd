# talos-k8s-argocd

GitOps configuration for a two-cluster, bare-metal **Kubernetes** homelab running on
**Talos Linux**, continuously reconciled by **Argo CD**. Everything here is declarative —
the clusters are a pure function of this repository.

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
k8s/
  infrastructure/
    vault/overlays/talos-mesh/                 # HashiCorp Vault (HA, Raft)
    external-secrets/overlays/talos-mesh/      # External Secrets Operator
    external-secrets/overlays/talos-cilium/
```

Components are rendered with Kustomize; Helm-based components use Kustomize's native
`helmCharts` (rendered server-side by Argo CD, charts pulled at sync time — not vendored).

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
