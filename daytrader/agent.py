"""CLI entrypoint for the autonomous paper-trading desk.

    python -m daytrader.agent run        # start the always-on market-hours loop
    python -m daytrader.agent once        # run a single trade cycle now (testing)
    python -m daytrader.agent plan         # run the Strategist once
    python -m daytrader.agent review       # run the Reviewer once
    python -m daytrader.agent status       # print the snapshot + account (no LLM, no key needed)

`run`/`once`/`plan`/`review` require ANTHROPIC_API_KEY. `status` does not.
"""
from __future__ import annotations

import argparse
import json


def cmd_run(_args):
    from daytrader.live.runner import DeskRunner
    DeskRunner().run_forever()


def cmd_once(_args):
    from daytrader.live.runner import DeskRunner
    res = DeskRunner().trade()
    print(res.text or "(no text)")
    for a in res.actions:
        print(" -", a["tool"], a["input"], "->", a["result"])


def cmd_plan(_args):
    from daytrader.live.runner import DeskRunner
    print(DeskRunner().plan().text)


def cmd_review(_args):
    from daytrader.live.runner import DeskRunner
    print(DeskRunner().review().text)


def cmd_status(_args):
    """No LLM: just show what the agents would see plus account state."""
    from daytrader.live.db import LiveDB
    from daytrader.live.market_state import snapshot
    from daytrader.live.paper_broker import PaperBroker
    db = LiveDB()
    broker = PaperBroker(db)
    print(json.dumps(snapshot(broker), indent=2, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="daytrader.agent", description="Autonomous paper-trading desk")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("once").set_defaults(func=cmd_once)
    sub.add_parser("plan").set_defaults(func=cmd_plan)
    sub.add_parser("review").set_defaults(func=cmd_review)
    sub.add_parser("status").set_defaults(func=cmd_status)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
