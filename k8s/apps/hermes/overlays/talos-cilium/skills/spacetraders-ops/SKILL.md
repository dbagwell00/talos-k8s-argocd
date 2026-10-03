---
name: spacetraders-ops
description: Check on our two SpaceTraders agents — profit and loss, credits, leaderboard standing, logs, and live fleet state. Use whenever the user asks how the agents, the fleet, GOBBLICIDE, TURKEYBOI, P&L, credits, earnings, or the leaderboard are doing, or wants agent logs.
version: 1.0.0
metadata:
  hermes:
    category: spacetraders
    tags: [spacetraders, monitoring, pnl, logs]
---

# SpaceTraders agent ops (read-only)

We run two SpaceTraders agents in Kubernetes:

| Name | Code | Notes |
|------|------|-------|
| **erl** | Erlang agent (`~/spacetraders-erlang`) | Callsign changes each weekly reset (e.g. GOBBLICIDE4). Has a detailed per-category P&L. |
| **py**  | Python agent (`~/spacetraders`)        | Callsign TURKEYBOI1. P&L is derived from its credit history. |

Everything goes through the helper script, which talks to a read-only
gateway. **You cannot change, pause, or command either agent** — there is no
write path, by design. If the user asks you to act on the fleet, say so and
tell them to use the agent's dashboard or `kubectl` themselves.

## The helper

```
python3 /opt/data/skills/spacetraders/spacetraders-ops/scripts/st.py <command>
```

| Command | What it gives you |
|---------|-------------------|
| `summary` | **Start here.** Both agents: credits, net P&L over 1h / 24h / since reset, leaderboard position, reset clock. |
| `pnl erl` | Erlang P&L by category (trade sales/buys, hulls, fuel, contracts, …) for the last hour, day, and since reset. |
| `pnl py` | Python agent's net credit change over 1h / 6h / 24h / 7d. |
| `leaderboard` | Top agents by credits and by charts, where ours would rank, next server reset. |
| `logs erl\|py [--minutes N] [--grep TOKEN] [--limit N]` | Recent log lines, newest last. `--grep` takes one token of letters, digits, `._:-` (no spaces), e.g. a ship symbol, `ERROR`, `WARNING`. Defaults: 15 minutes, 100 lines; max 500 lines. |
| `erl <view>` | One raw Erlang dashboard view, truncated. Useful views: `state`, `clock`, `pnl`, `capital`, `budget`, `limiter`, `contracts`, `ticks`, `gate`, `charting`, `intent`. Avoid `all`, `universe`, `markets`, `ships`: they are huge. |
| `py <path>` | One raw Python-agent view, truncated: `api/limiter`, `api/mining/groups`, `api/fleet/halts`, `api/fleet/competitors`, `api/probe-circuits`, `api/market-freshness`, `api/config`, `healthz`. |

## How to answer

- Run `summary` first for any "how are we doing" question; only drill down if asked.
- Report credits with thousands separators and say whether P&L is up or down.
- Erlang `net` includes capital spending (hulls); `operating` excludes it — mention both when hulls is large.
- Python P&L is a credit delta: buying ships shows up as a loss.
- Times from the script are already US Pacific.
- If a call fails, say which source is down (agent, logs, or public API) rather than guessing numbers.
