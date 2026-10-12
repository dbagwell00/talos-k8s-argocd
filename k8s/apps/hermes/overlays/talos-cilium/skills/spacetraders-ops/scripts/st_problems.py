#!/usr/bin/env python3
"""SpaceTraders problem watch: both agents' problem detectors (`/live/problems`).

Each agent computes its own findings (spacetraders-erlang `st_problems`): a
failing planner, markets left unread, error loops, thin or collapsing trade,
a starved gate, idle haulers, data-integrity and recorder trouble. Each finding
has a stable key, a severity, a text and a hint. This script only fetches them
through the st-readonly gateway and decides whether anything is worth saying,
so the hourly job never calls the model.

  st_problems.py report   every open finding, always prints (ask in chat)
  st_problems.py check    only prints on change: a new finding, a WARN that
                          became CRIT, a reminder for one still open (CRIT every
                          2h, WARN every 12h), or one that cleared. Empty
                          output = nothing to say; Hermes cron then stays quiet.

Installed in $HERMES_HOME/scripts as st_problems_check.py for the script-only
cron job (no arguments there, so the file name picks the mode).
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

BASE = os.environ.get("ST_BASE", "http://st-readonly.hermes.svc.cluster.local:8080")
HOME = os.environ.get("HERMES_HOME", "/opt/data")
STATE = os.path.join(HOME, "cron", "st-problems-state.json")
AGENTS = {"erl": "GOBBLICIDE", "bw": "BLOODWATTLE"}
REMIND = {"CRIT": 2 * 3600, "WARN": 12 * 3600}
ICON = {"CRIT": "🔴", "WARN": "🟡", "OK": "🟢"}


def fetch():
    """{finding id: (sev, line, hint, replay)}, plus the agents that didn't answer."""
    out, down = {}, []
    for prefix, name in AGENTS.items():
        try:
            with urllib.request.urlopen(f"{BASE}/{prefix}/live/problems", timeout=30) as r:
                d = json.load(r)
        except Exception as e:  # an agent down is itself worth knowing
            down.append(f"{name}: no answer ({type(e).__name__})")
            continue
        for f in d.get("findings", []):
            out[f"{name}:{f['key']}"] = (f["sev"], f"{d.get('agent', name)}: {f['text']}",
                                         f.get("hint", ""), d.get("replay", ""))
    return out, down


def stamp():
    return datetime.now().astimezone().strftime("%a %-I:%M %p %Z")


def line(sev, text, hint, prefix=""):
    s = f"{ICON.get(sev, '•')} {prefix}{text}"
    return s + (f"\n    ↳ {hint}" if hint else "")


def replays(keys, found):
    links = sorted({found[k][3] for k in keys if k in found and found[k][3]})
    return ["Replay: " + "  ".join(links)] if links else []


def report():
    found, down = fetch()
    worst = "CRIT" if any(v[0] == "CRIT" for v in found.values()) or down else "WARN" if found else "OK"
    print(f"{ICON[worst]} SpaceTraders problems, {stamp()}")
    for sev in ("CRIT", "WARN"):
        for k, (s, text, hint, _) in sorted(found.items()):
            if s == sev:
                print(line(s, text, hint))
    for d in down:
        print(f"{ICON['CRIT']} {d}")
    if not found and not down:
        print("No problems found on either agent.")
    print("\n".join(replays(found, found)))


def check():
    found, down = fetch()
    for d in down:  # an unreachable agent is a CRIT finding of its own
        found["down:" + d.split(":")[0]] = ("CRIT", d, "check the pod, then st-readonly", "")
    try:
        with open(STATE) as fh:
            state = json.load(fh)
    except Exception:
        state = {}
    now = time.time()
    new, still, done = [], [], []
    for k, (sev, text, hint, _) in found.items():
        st = state.get(k)
        if st is None:
            new.append(line(sev, text, hint, "NEW: "))
            state[k] = {"first": now, "sent": now, "sev": sev, "text": text}
        elif sev == "CRIT" and st.get("sev") != "CRIT":
            new.append(line(sev, text, hint, "WORSE: "))
            st.update(sent=now, sev=sev, text=text)
        elif now - st.get("sent", 0) >= REMIND.get(sev, REMIND["WARN"]):
            since = datetime.fromtimestamp(st["first"]).astimezone().strftime("%a %-I:%M %p")
            still.append(line(sev, text, "", f"still, since {since}: "))
            st.update(sent=now, sev=sev, text=text)
        else:
            st.update(text=text)  # numbers move; keep the latest for the 'resolved' line
    for k in [k for k in state if k not in found]:
        done.append(f"✅ resolved: {state[k].get('text', k)}")
        del state[k]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)
    if new or still or done:
        print(f"SpaceTraders watch, {stamp()}")
        print("\n".join(new + still + done + replays([k for k in found if k in state], found)))


if __name__ == "__main__":
    base = os.path.basename(sys.argv[0])
    mode = sys.argv[1] if len(sys.argv) > 1 else ("check" if "check" in base else "report")
    {"check": check, "report": report}[mode]()
