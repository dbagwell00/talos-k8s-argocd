#!/usr/bin/env python3
"""Read-only SpaceTraders agent helper for Hermes. Talks only to st-readonly.

Prints short human summaries, never raw multi-MB JSON, so a local model's
context survives the answer.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("ST_BASE", "http://st-readonly.hermes.svc.cluster.local:8080")
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Los_Angeles")
except Exception:  # no tzdata: fall back to UTC, labelled as such
    TZ = timezone.utc


def get(path, timeout=60):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}")
    except Exception as e:
        raise SystemExit(f"{path}: unreachable ({e})")


def try_get(path):
    try:
        return get(path)
    except SystemExit as e:
        return {"_error": str(e)}


def c(n):
    return "n/a" if n is None else f"{int(n):,}"


def signed(n):
    return "n/a" if n is None else f"{int(n):+,}"


def when(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%a %-I:%M %p %Z")


def erl_symbol(state):
    return ((state.get("agent") or {}).get("agent") or {}).get("symbol")


def py_symbol():
    groups = try_get("/py/api/mining/groups")
    for ship in (groups.get("asteroid_for_ship") or {}):
        return ship.rsplit("-", 1)[0]
    return "TURKEYBOI1"


def py_history():
    chart = get("/py/api/chart", timeout=90)
    agents = chart.get("agents") or {}
    out = {}
    for prefix, a in agents.items():
        h = a.get("credits_history") or []
        if h:
            out[prefix] = h
    return out


def delta_at(hist, seconds):
    """Credits now minus credits at the last point at or before now-seconds."""
    now_ts, now_c = hist[-1][0], hist[-1][1]
    cutoff = now_ts - seconds
    if hist[0][0] > cutoff:
        return None
    lo, hi = 0, len(hist) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if hist[mid][0] <= cutoff:
            lo = mid
        else:
            hi = mid - 1
    return now_c - hist[lo][1]


def cmd_pnl_erl(brief=False):
    p = get("/erl/live/pnl")
    fleet = p.get("fleet") or {}
    for window in ("hour", "day", "reset"):
        w = fleet.get(window)
        if not w:
            continue
        print(f"  {window:>5}: net {signed(w.get('net'))}  operating {signed(w.get('operating'))}"
              f"  ({signed(w.get('net_per_hour'))}/h net)")
        if not brief:
            cats = sorted((w.get("by_category") or {}).items(), key=lambda kv: -abs(kv[1]))
            print("         " + ", ".join(f"{k} {signed(v)}" for k, v in cats if v))


def cmd_pnl_py():
    hists = py_history()
    if not hists:
        print("  no credit history")
        return
    for prefix, h in hists.items():
        print(f"  {prefix}: {c(h[-1][1])} credits as of {when(h[-1][0])}")
        for label, secs in (("1h", 3600), ("6h", 21600), ("24h", 86400), ("7d", 604800)):
            d = delta_at(h, secs)
            print(f"    {label:>3}: " + (signed(d) if d is not None
                                          else f"n/a (history starts {when(h[0][0])})"))


def leaderboard_rows():
    lb = get("/st/leaderboard")
    return lb, (lb.get("leaderboards") or {}).get("mostCredits") or []


def rank_of(credits, rows):
    if credits is None:
        return "?"
    above = sum(1 for r in rows if r.get("credits", 0) > credits)
    return f"#{above + 1}" if above < len(rows) else f"below top {len(rows)}"


def cmd_summary(_):
    state = try_get("/erl/live/state")
    clock = try_get("/erl/live/clock")
    erl_sym = erl_symbol(state) if "_error" not in state else None
    py_sym = py_symbol()
    try:
        lb, rows = leaderboard_rows()
    except SystemExit as e:
        lb, rows = {}, []
        print(f"(leaderboard unavailable: {e})")

    print(f"== erl ({erl_sym or 'unknown'})")
    if "_error" in state:
        print(f"  {state['_error']}")
    else:
        a = state.get("agent") or {}
        print(f"  credits {c(a.get('credits'))}  ships {a.get('ships')} ({a.get('ships_idle')} idle)"
              f"  rank {rank_of(a.get('credits'), rows)}")
        try:
            cmd_pnl_erl(brief=True)
        except SystemExit as e:
            print(f"  pnl: {e}")
    if "_error" not in clock:
        print(f"  reset clock: phase {clock.get('phase')}, {clock.get('hours_left')}h left")

    print(f"== py ({py_sym})")
    pub = try_get(f"/st/agents/{py_sym}")
    pdata = pub.get("data") or {}
    if pdata:
        print(f"  credits {c(pdata.get('credits'))}  ships {pdata.get('shipCount')}"
              f"  rank {rank_of(pdata.get('credits'), rows)}")
    try:
        cmd_pnl_py()
    except SystemExit as e:
        print(f"  pnl: {e}")

    if lb:
        nxt = (lb.get("serverResets") or {}).get("next")
        print(f"== server: reset {lb.get('resetDate')}, next reset {nxt}")


def cmd_pnl(args):
    if args.agent == "erl":
        print("== erl P&L")
        cmd_pnl_erl()
    else:
        print("== py P&L (net credit change)")
        cmd_pnl_py()


def cmd_leaderboard(_):
    lb, rows = leaderboard_rows()
    print(f"Reset {lb.get('resetDate')}; next {(lb.get('serverResets') or {}).get('next')}")
    print("Most credits:")
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}. {r.get('agentSymbol'):<16} {c(r.get('credits'))}")
    charts = (lb.get("leaderboards") or {}).get("mostSubmittedCharts") or []
    if charts:
        print("Most charts:")
        for i, r in enumerate(charts, 1):
            print(f"  {i:>2}. {r.get('agentSymbol'):<16} {c(r.get('chartCount'))}")
    state = try_get("/erl/live/state")
    ours = []
    if "_error" not in state:
        ours.append((erl_symbol(state), ((state.get("agent") or {}).get("credits"))))
    py_sym = py_symbol()
    ours.append((py_sym, ((try_get(f"/st/agents/{py_sym}").get("data") or {}).get("credits"))))
    print("Ours:")
    for sym, cr in ours:
        print(f"  {sym:<16} {c(cr)}  -> {rank_of(cr, rows)}")


def cmd_logs(args):
    q = {"minutes": args.minutes, "limit": args.limit}
    if args.grep:
        q["grep"] = args.grep
    d = get(f"/logs/{args.agent}?" + urllib.parse.urlencode(q))
    lines = []
    for stream in (d.get("data") or {}).get("result") or []:
        for ts, line in stream.get("values") or []:
            lines.append((int(ts), line))
    lines.sort()
    if not lines:
        print(f"no {args.agent} log lines in the last {args.minutes} min"
              + (f" matching {args.grep!r}" if args.grep else ""))
        return
    for ts, line in lines:
        t = datetime.fromtimestamp(ts / 1e9, TZ).strftime("%-I:%M:%S %p")
        print(f"{t} {line[:400]}")


def raw(path):
    d = get(path)
    s = json.dumps(d, indent=1)
    print(s[:6000] + ("\n... (truncated)" if len(s) > 6000 else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary").set_defaults(fn=cmd_summary)
    p = sub.add_parser("pnl")
    p.add_argument("agent", choices=["erl", "py"])
    p.set_defaults(fn=cmd_pnl)
    sub.add_parser("leaderboard").set_defaults(fn=cmd_leaderboard)
    p = sub.add_parser("logs")
    p.add_argument("agent", choices=["erl", "py"])
    p.add_argument("--minutes", type=int, default=15)
    p.add_argument("--grep", default="")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=cmd_logs)
    p = sub.add_parser("erl")
    p.add_argument("view")
    p.set_defaults(fn=lambda a: raw(f"/erl/live/{a.view}"))
    p = sub.add_parser("py")
    p.add_argument("path")
    p.set_defaults(fn=lambda a: raw(f"/py/{a.path.lstrip('/')}"))
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
