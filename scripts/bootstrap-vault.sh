#!/usr/bin/env bash
#
# One-time Vault bootstrap for External Secrets Operator.
# Run AFTER `vault operator init` + unseal of all replicas.
#
# Configures:
#   - KV v2 secrets engine at  secret/
#   - read-only policy         external-secrets
#   - kubernetes auth mount    kubernetes         (talos-mesh, in-cluster)
#   - kubernetes auth mount    kubernetes-cilium  (talos-cilium, cross-cluster)
#   - role  external-secrets   on each mount, bound to the ESO ServiceAccount
#
# No secrets are embedded: the cilium reviewer JWT + CA are read from the
# cluster at runtime. Requires a Vault token with admin rights.
#
# Usage:
#   VAULT_TOKEN=<root-or-admin-token> ./scripts/bootstrap-vault.sh
#
set -euo pipefail

: "${VAULT_TOKEN:?set VAULT_TOKEN to a Vault admin/root token}"
MESH_KUBECONFIG="${MESH_KUBECONFIG:-$HOME/.kube/config-talos-mesh}"
CIL_KUBECONFIG="${CIL_KUBECONFIG:-$HOME/.kube/config-talos-cilium}"
CILIUM_API="${CILIUM_API:-https://192.168.4.10:6443}"   # talos-cilium API, reachable from mesh

# run a vault command inside vault-0 with the admin token in env
vex() { kubectl --kubeconfig "$MESH_KUBECONFIG" exec -i -n vault vault-0 -- \
          env VAULT_TOKEN="$VAULT_TOKEN" "$@"; }

echo ">> KV v2 at secret/"
vex vault secrets enable -path=secret -version=2 kv 2>/dev/null || echo "   (already enabled)"

echo ">> external-secrets read policy"
vex sh -c 'env VAULT_TOKEN="'"$VAULT_TOKEN"'" vault policy write external-secrets -' <<'POLICY'
path "secret/data/*"     { capabilities = ["read"] }
path "secret/metadata/*" { capabilities = ["read", "list"] }
POLICY

echo ">> mesh (in-cluster) kubernetes auth"
vex vault auth enable kubernetes 2>/dev/null || echo "   (already enabled)"
vex vault write auth/kubernetes/config kubernetes_host="https://kubernetes.default.svc:443"
vex vault write auth/kubernetes/role/external-secrets \
    bound_service_account_names=external-secrets \
    bound_service_account_namespaces=external-secrets \
    policies=external-secrets ttl=1h

echo ">> ensure cilium token-reviewer token Secret exists"
kubectl --kubeconfig "$CIL_KUBECONFIG" apply -f - >/dev/null <<'SECRET'
apiVersion: v1
kind: Secret
metadata:
  name: vault-token-reviewer-token
  namespace: external-secrets
  annotations:
    kubernetes.io/service-account.name: vault-token-reviewer
type: kubernetes.io/service-account-token
SECRET
sleep 3

echo ">> cilium (cross-cluster) kubernetes auth"
REVIEWER_JWT="$(kubectl --kubeconfig "$CIL_KUBECONFIG" get secret vault-token-reviewer-token \
                  -n external-secrets -o jsonpath='{.data.token}' | base64 -d)"
CILIUM_CA="$(kubectl --kubeconfig "$CIL_KUBECONFIG" get secret vault-token-reviewer-token \
                  -n external-secrets -o jsonpath='{.data.ca\.crt}' | base64 -d)"
vex vault auth enable -path=kubernetes-cilium kubernetes 2>/dev/null || echo "   (already enabled)"
kubectl --kubeconfig "$MESH_KUBECONFIG" exec -i -n vault vault-0 -- \
  env VAULT_TOKEN="$VAULT_TOKEN" CA="$CILIUM_CA" JWT="$REVIEWER_JWT" API="$CILIUM_API" sh -c '
    vault write auth/kubernetes-cilium/config \
      kubernetes_host="$API" \
      kubernetes_ca_cert="$CA" \
      token_reviewer_jwt="$JWT" \
      disable_local_ca_jwt=true'
vex vault write auth/kubernetes-cilium/role/external-secrets \
    bound_service_account_names=external-secrets \
    bound_service_account_namespaces=external-secrets \
    policies=external-secrets ttl=1h

echo ">> done. Both ClusterSecretStores should report Ready shortly."
