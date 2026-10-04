# hermes

[Hermes Agent](https://github.com/NousResearch/hermes-agent) (Nous Research), running in
gateway mode on `talos-cilium`. You talk to it over Telegram. Its model is a local Qwen
(`qwen3.8-27b`) served by vLLM outside the cluster, so no prompt or reply goes to a hosted
LLM. Telegram does carry the chat itself, and bot chats are not end-to-end encrypted.

It does three things:

- answers questions in chat
- reports on the SpaceTraders agents (P&L, credits, leaderboard, logs): skill `spacetraders-ops`
- watches the homelab (Samba/CTDB/CephFS/Ceph on the Proxmox hosts, SpaceTraders pods):
  skill `homelab-health`, plus two scheduled jobs

## Security model

The agent runs model-driven shell commands inside its own container by design, so the
container is treated as hostile and every boundary is enforced outside it:

```
Telegram ──(outbound long-poll only)──┐
                                      │
                    ┌─────────────────┴───────────────┐
 Qwen (vLLM) ◀──────│  hermes pod                     │  no Service, ingress default-deny,
 :18020             │  no SA token, minimal caps,     │  DNS answers only for Telegram
                    │  egress allowlist (CNP)         │  and st-readonly
                    └─────────────────┬───────────────┘
                                      │ GET only (Cilium L7 + nginx)
                    ┌─────────────────┴───────────────┐
                    │  st-readonly (nginx, non-root,  │  fixed route allowlist;
                    │  read-only rootfs)              │  everything else 404 / 405
                    └──┬──────┬──────┬──────┬──────┬──┘
           erl /live/*  py reads  Loki   Prometheus  api.spacetraders.io
                                (2 streams) (11 queries)  (public, no token)
```

- **No inbound path.** Telegram is polled outbound. Hermes's API server binds loopback
  only, and the web dashboard is off. For the TUI:
  `kubectl -n hermes exec -it deploy/hermes -- hermes`.
- **Egress allowlist** ([`network-policy.yaml`](overlays/talos-cilium/network-policy.yaml)):
  Telegram, Qwen, and `st-readonly`, nothing else. The DNS proxy refuses every other name,
  which also closes DNS as an exfiltration path. So web search, `pip install`, and the skill
  hub don't work; that's deliberate.
- **No write path to anything it monitors.** The agents' control APIs carry
  unauthenticated fleet-changing verbs (pause, halt, buy, scrap). Hermes can't reach them;
  it reaches [`st-readonly`](overlays/talos-cilium/st-readonly-configmap.yaml), which only
  forwards an explicit list of GET routes:
  - Loki queries use a stream selector fixed per agent, and the grep token is shape-checked.
  - Prometheus queries are picked by name from a pre-encoded menu, so callers never write
    PromQL.
- **No credentials for the hosts.** Proxmox health arrives as metrics the hosts publish
  themselves ([`scripts/proxmox-health`](../../../scripts/proxmox-health/)). Hermes never
  holds an SSH key.

## Configuration

| What | Where |
|---|---|
| Model, provider, context length | [`configmap.yaml`](overlays/talos-cilium/configmap.yaml) |
| Telegram bot token + allowed user IDs | Vault `secret/hermes/agent` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`) |
| Skills | [`overlays/talos-cilium/skills/`](overlays/talos-cilium/skills/) |
| Gateway routes | [`st-readonly-configmap.yaml`](overlays/talos-cilium/st-readonly-configmap.yaml) |

**Git is authoritative.** The init container overwrites `config.yaml` and both skills on the
PVC at every start, so a `hermes model` or skill edit made inside the pod lasts only until
the next restart.

How changes roll out:

- **Skills** come from hashed ConfigMaps (`configMapGenerator`), so a skill edit rolls the
  pod by itself.
- **`config.yaml`** does not: after changing it, run
  `kubectl -n hermes rollout restart deploy/hermes`.
- **nginx config** in `st-readonly`: bump the `config-rev` annotation in
  [`st-readonly.yaml`](overlays/talos-cilium/st-readonly.yaml).

**Pinned images:** `nousresearch/hermes-agent` and `nginx-unprivileged` are pinned by
digest. Upgrade by changing the tag and digest together.

### Telegram secret

```sh
KUBECONFIG=~/.kube/config-talos-mesh kubectl -n vault exec -it vault-0 -c vault -- sh
vault login
read -s -p "bot token: " T; echo
vault kv put secret/hermes/agent TELEGRAM_BOT_TOKEN="$T" TELEGRAM_ALLOWED_USERS=<numeric-id>
unset T; rm -f ~/.vault-token; exit
```

Then pick it up:

```sh
kubectl -n hermes annotate externalsecret hermes-agent force-sync=$(date +%s) --overwrite
# wait for READY True, then:
kubectl -n hermes rollout restart deploy/hermes
```

The secret is mounted `optional`, so Hermes runs without Telegram if it's missing.

## Scheduled jobs

Both jobs are script-only (`--no-agent`): they never call the model, so they keep working
when Qwen is down. They live on the PVC (`/opt/data/cron/jobs.json`), not in git. Recreate
them if the PVC is ever lost:

```sh
H='kubectl -n hermes exec deploy/hermes -c hermes -- /command/s6-setuidgid hermes hermes'
$H cron create "every 15m" --no-agent --script health_check.py  --name homelab-watch  --deliver telegram:<chat_id>
$H cron create "0 8 * * *" --no-agent --script health_report.py --name homelab-digest --deliver telegram:<chat_id>
$H cron list
$H cron run homelab-digest      # send one now
```

`<chat_id>` is your Telegram user ID: it appears in the gateway log as `chat=` on any
message you send. Schedules are in Pacific time (`TZ` is set on the pod).

What each job sends:

- **`homelab-watch`** runs every 15 minutes and stays silent unless a problem is new, got
  worse, cleared, or is still open. It reminds every 6h for critical and daily for warning.
  Its state is in `/opt/data/cron/homelab-health-state.json`; delete that file to reset.
- **`homelab-digest`** sends the full report at 8am.

## Operating notes

- **First reply after a restart takes about a minute.** vLLM prefills about 300 tokens/s
  cold, and Hermes's system prompt plus tools is close to 19k tokens. After that, vLLM's
  prefix cache makes replies fast.
- **`Running` doesn't mean the gateway is up.** s6-overlay supervises it inside the pod.
  Check with
  `kubectl -n hermes exec deploy/hermes -c hermes -- ps -eo user,args | grep "gateway run"`.
  Logs are in `/opt/data/logs/` (`gateway.log`, `agent.log`, `errors.log`).
- **Capabilities.** The container starts as root only so s6-overlay can fix ownership on
  `/opt/data` and drop to uid 10000. Without `DAC_OVERRIDE`, the pod shows `Running` while
  the gateway never starts.
- **Testing the gateway.** `st-readonly` is managed by Argo CD. To try route changes before
  merging, apply a renamed copy (`st-readonly-test`), port-forward to it, and point the
  scripts at it with `ST_BASE=http://127.0.0.1:<port>`.
