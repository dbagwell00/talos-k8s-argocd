#!/usr/bin/env python3
"""SpaceTraders pre-gate experiment watch: three alarms, observe-only.

Pre-registered 2026-10-20 for the 10-25 gate-floor A/B (spacetraders-erlang
docs/experiments/2026-10-25-gate-floor-ab.md). Thresholds are fixed here and
are not to be changed while the reset runs. This script NEVER acts on an agent:
it reads /erl/live/* and /bw/live/* through the st-readonly gateway and speaks.

Alarms (pre-gate only; each must HOLD for >= 1 h before it alerts):
  deadlock   the agent may buy a trader (`/live/ramp` may_buy_trader), wants
             more (home_haulers < trader_target) and its trader hull budget is 0
             -- the 2026-10-18 freeze: a reserved hauler never bought.
  starved    a gate material reads `starved_export` (`/live/gate`) -- the gate
             waits on a factory whose export is starved.
  sells      last hour's trade sales (`/live/pnl` fleet.hour trade_sales) under
             half this agent's own median hourly sales since ~h2 of this reset.

One alert when an incident starts (after the hour), one when it clears; no
hourly reminders. Each carries agent, ~reset hour (from `/live/clock`) and the
telemetry behind it.

  st_experiment_watch.py report   current readings for both agents, always prints
  st_experiment_watch.py check    the alarm pass (empty output = nothing to say)

Installed as st_experiment_watch_check.py for a script-only cron job, every 15m.
"""
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime

BASE = os.environ.get("ST_BASE", "http://st-readonly.hermes.svc.cluster.local:8080")
HOME = os.environ.get("HERMES_HOME", "/opt/data")
STATE = os.path.join(HOME, "cron", "st-experiment-watch-state.json")
AGENTS = {"erl": "GOBBLICIDE", "bw": "BLOODWATTLE"}
HOLD_S = 3600          # a condition must persist this long before it alerts
SELLS_FRAC = 0.5       # last hour < this x own median
SELLS_FROM_H = 2.0     # median of hourly samples from this reset hour on
SELLS_MIN_SAMPLES = 3


def get(prefix, ep):
    with urllib.request.urlopen(f"{BASE}/{prefix}/live/{ep}", timeout=30) as r:
        return json.load(r)


def reset_hour(clock):
    f, left = clock.get("elapsed_fraction"), clock.get("hours_left")
    if f is None or left is None or f >= 1:
        return None
    return left * f / (1 - f)


def readings(prefix):
    """The agent's readings, or None if it is post-gate / unreachable (raises)."""
    gate = get(prefix, "gate")
    mats = gate.get("materials") or {}
    if gate.get("complete") or not mats:
        return {"post_gate": True}
    ramp, cap, pnl, clock, reset = (get(prefix, e) for e in ("ramp", "capital", "pnl", "clock", "reset"))
    return {
        "post_gate": False,
        "reset": reset.get("server_reset"),
        "h": reset_hour(clock),
        "gate_pct": gate.get("percent"),
        "mats": {g: m.get("state") for g, m in mats.items()},
        "deficit": {g: m.get("deficit") for g, m in mats.items()},
        "ramp": ramp,
        "cap": cap,
        "sales_h": (((pnl.get("fleet") or {}).get("hour") or {}).get("by_category") or {}).get("trade_sales"),
    }


def conditions(name, r, hist):
    """{key: text} for every alarm condition true right now."""
    out = {}
    h = r["h"]
    hs = f"~h{h:.1f}" if h is not None else "h?"
    ramp, cap = r["ramp"], r["cap"]
    if (ramp.get("may_buy_trader") and ramp.get("ship_budget_trader", 1) == 0
            and ramp.get("home_haulers", 0) < ramp.get("trader_target", 0)):
        out["deadlock"] = (f"{name} {hs}: trader DEADLOCK -- wants {ramp.get('trader_target')} haulers, has "
                           f"{ramp.get('home_haulers')}, hull budget 0 (binding {ramp.get('trader_binding')}); "
                           f"pending hauler reserve {cap.get('pending_hauler_reserve'):,}, gate spend floor "
                           f"{cap.get('gate_spend_floor'):,}, gate budget {(cap.get('budgets') or {}).get('gate'):,}; "
                           f"gate {r['gate_pct']}%")
    for g, s in r["mats"].items():
        if s == "starved_export":
            out[f"starved:{g}"] = (f"{name} {hs}: {g} export STARVED -- gate {r['gate_pct']}%, "
                                   f"{r['deficit'].get(g)} units short; check its factory's inputs")
    sales = r["sales_h"]
    base = [s for hh, s in hist if hh is not None and hh >= SELLS_FROM_H]
    if sales is not None and len(base) >= SELLS_MIN_SAMPLES:
        med = statistics.median(base)
        if med > 0 and sales < SELLS_FRAC * med:
            out["sells"] = (f"{name} {hs}: SELLS LOW -- last hour {sales:,} credits vs this reset's median "
                            f"{med:,.0f}/h (alarm under {SELLS_FRAC:.0%})")
    return out


def stamp():
    return datetime.now().astimezone().strftime("%a %-I:%M %p %Z")


def load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)


def check():
    state = load()
    now = time.time()
    msgs = []
    for prefix, name in AGENTS.items():
        try:
            r = readings(prefix)
        except Exception as e:
            r = None
            err = type(e).__name__
        a = state.setdefault(name, {"reset": None, "hist": [], "open": {}})
        if r is None:
            key = "unreachable"
            o = a["open"].setdefault(key, {"first": now, "alerted": False, "text": f"{name}: no answer ({err})"})
            if not o["alerted"] and now - o["first"] >= HOLD_S:
                msgs.append(f"🔴 {o['text']} for over an hour"); o["alerted"] = True
            continue
        a["open"].pop("unreachable", None)
        if r["post_gate"]:
            for k, o in list(a["open"].items()):
                if o.get("alerted"):
                    msgs.append(f"✅ {name}: gate complete; closing: {o['text']}")
            a["open"] = {}
            continue
        if r["reset"] != a["reset"]:          # new reset: fresh history and incidents
            a.update(reset=r["reset"], hist=[], open={})
        cur = conditions(name, r, a["hist"])
        if r["sales_h"] is not None:
            a["hist"].append([r["h"], r["sales_h"]])
            a["hist"] = a["hist"][-400:]
        for k, text in cur.items():
            o = a["open"].setdefault(k, {"first": now, "alerted": False})
            o["text"] = text
            if not o["alerted"] and now - o["first"] >= HOLD_S:
                held = (now - o["first"]) / 3600
                msgs.append(f"🔴 {text} (held {held:.1f} h)")
                o["alerted"] = True
        for k in [k for k in a["open"] if k not in cur]:
            o = a["open"].pop(k)
            if o.get("alerted"):
                msgs.append(f"✅ cleared after {(now - o['first']) / 3600:.1f} h: {o['text']}")
    save(state)
    if msgs:
        print(f"SpaceTraders experiment watch, {stamp()}")
        print("\n".join(msgs))


def report():
    print(f"SpaceTraders experiment watch (report), {stamp()}")
    state = load()
    for prefix, name in AGENTS.items():
        try:
            r = readings(prefix)
        except Exception as e:
            print(f"🔴 {name}: no answer ({type(e).__name__})"); continue
        if r["post_gate"]:
            print(f"🟢 {name}: gate complete, watch idle"); continue
        hist = (state.get(name) or {}).get("hist", [])
        cur = conditions(name, r, hist)
        hs = f"~h{r['h']:.1f}" if r["h"] is not None else "h?"
        print(f"{'🔴' if cur else '🟢'} {name} {hs}: gate {r['gate_pct']}% {r['mats']}; ramp haulers "
              f"{r['ramp'].get('home_haulers')}/{r['ramp'].get('trader_target')} budget "
              f"{r['ramp'].get('ship_budget_trader')}; sales last h {r['sales_h']}")
        for t in cur.values():
            print(f"   condition now: {t}")


if __name__ == "__main__":
    base = os.path.basename(sys.argv[0])
    mode = sys.argv[1] if len(sys.argv) > 1 else ("check" if "check" in base else "report")
    {"check": check, "report": report}[mode]()
