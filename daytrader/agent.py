"""CLI entrypoint for the autonomous, competing paper-trading desks.

    python -m daytrader.agent serve        # web dashboard + run all teams (the default service)
    python -m daytrader.agent compete       # run the competition loop headless (no UI)
    python -m daytrader.agent leaderboard    # print current standings and exit
    python -m daytrader.agent status         # print one team's market+account snapshot (no LLM, no key)

Each team is a full multi-agent desk (Strategist, Trader, Reviewer) driven by its
OWN model — Claude, OpenAI, Grok, Qwen — with identical $10k cash and tools.
Teams trade only if their API key is set; configure any subset.
Requires the relevant provider API keys at runtime (ANTHROPIC_API_KEY,
OPENAI_API_KEY, XAI_API_KEY, DASHSCOPE_API_KEY).
"""
from __future__ import annotations

import argparse
import os

# Default to 3737 to match the legacy container's port (avoids breaking an
# existing Unraid port mapping). Override with DASHBOARD_PORT if desired.
DEFAULT_PORT = int(os.environ.get("DASHBOARD_PORT", "3737"))


def cmd_serve(args):
    from daytrader.live.dashboard import serve
    serve(port=args.port)


def cmd_compete(_args):
    from daytrader.live.competition import Competition
    Competition().run_forever()


def cmd_leaderboard(_args):
    from daytrader.live.competition import leaderboard
    rows = leaderboard()
    if not rows:
        print("No teams configured.")
        return
    print(f"{'#':>2}  {'TEAM':<8} {'MODEL':<20} {'EQUITY':>10} {'RET%':>7} "
          f"{'DD%':>6} {'PF':>5} {'WIN%':>6} {'TRADES':>7} {'OPEN':>5}")
    for r in rows:
        print(f"{r['rank']:>2}  {r['team']:<8} {r['model'][:20]:<20} "
              f"${r['equity']:>9,.0f} {r['return_pct']:>6.2f}% {r['drawdown_pct']:>5.1f}% "
              f"{r['profit_factor']:>5.2f} {r['win_rate']:>5.1f}% {r['n_trades']:>7} {r['open_positions']:>5}")


def cmd_check(_args):
    from daytrader.live.healthcheck import check_providers
    rows = check_providers()
    print(f"{'TEAM':<8} {'MODEL':<22} {'STATUS':<8} {'LATENCY':>8}  DETAIL")
    for r in rows:
        status = "OK" if r["ok"] else ("— " if not r["configured"] else "FAIL")
        lat = f"{r['latency_ms']}ms" if r["configured"] else "-"
        print(f"{r['team']:<8} {r['model'][:22]:<22} {status:<8} {lat:>8}  {r['detail']}")
    working = [r["team"] for r in rows if r["ok"]]
    print(f"\nWorking teams: {working or 'none'}")


def cmd_reset(_args):
    """Wipe per-team DBs so the competition restarts clean (e.g. after a
    starting-cash change). Only deletes paper data; never touches settings."""
    import glob, os
    from daytrader.live.competition import DATA_DIR, START_CASH, team_names
    removed = 0
    for name in team_names():
        for f in glob.glob(os.path.join(DATA_DIR, f"team_{name}.db*")):
            try:
                os.remove(f); removed += 1
            except OSError:
                pass
    print(f"Reset complete: removed {removed} db file(s). Teams will start fresh at ${START_CASH:,.0f}.")


def cmd_status(_args):
    from daytrader.live.db import LiveDB
    from daytrader.live.market_state import snapshot
    from daytrader.live.paper_broker import PaperBroker
    import json
    db = LiveDB()
    print(json.dumps(snapshot(PaperBroker(db, starting_equity=25000)), indent=2, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="daytrader.agent", description="Competing autonomous paper-trading desks")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=DEFAULT_PORT); s.set_defaults(func=cmd_serve)
    sub.add_parser("compete").set_defaults(func=cmd_compete)
    sub.add_parser("leaderboard").set_defaults(func=cmd_leaderboard)
    sub.add_parser("reset").set_defaults(func=cmd_reset)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
