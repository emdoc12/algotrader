"""The trading team: a Strategist, a Trader, and a Reviewer.

The team self-directs. We give them the validated strategy book as a starting
reference, full authority over a paper account, persistent memory (the journal),
and a channel to ask the developer for help (GitHub issues) — then let them
decide how to trade. Roles:

  * Strategist  — once near the open: reads the morning snapshot, performance,
                  and journal, and writes the day's game plan. No trades.
  * Trader      — every intraday cycle: reads the snapshot + plan + live signals
                  + positions and places/manages paper trades via tools.
  * Reviewer    — at the close: reviews the day's trades, records lessons, and
                  files dev requests for anything that blocked the team.

All three share one journal, so lessons compound across days and restarts.
"""
from __future__ import annotations

import json

from daytrader.live.llm_client import Agent
from daytrader.live.tools import build_tools

_MISSION = """You are part of an autonomous trading desk that DAY-TRADES SPY and the \
Mag7 (AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA) in PAPER mode — no real money is at risk, \
but trade as if it were. The desk's mandate:
- Beat a buy-and-hold of SPY on a risk-adjusted basis.
- Target a profit factor of 2:1 or better and keep max drawdown under 10%.
- This is intraday trading: never hold overnight. The system flattens everything \
at the close; plan around being flat by 15:55 ET.
- Risk is the priority. Size small (risk well under 1% of equity per trade), always \
use a protective stop, and prefer trading WITH the prevailing SPY trend.

You have a validated set of backtested setups available as 'fresh_signals' in the \
market snapshot (opening-range breakout, VWAP trend/reversion, RSI2, Bollinger fade, \
EMA pullback, MACD, pivot, gap-and-go). These are a guide, not a mandate — you decide \
which to act on, ignore, or combine, based on the live picture and what has been \
working. Out-of-sample the trend setups in SPY's direction have been the reliable edge; \
mean-reversion and counter-trend setups have bled.

Use the journal as your memory: write down what you observe, what works, what doesn't, \
and your plan — it survives restarts and the rest of the team reads it. If you are \
blocked by something only a developer can fix (a missing data source, a bug, a strategy \
you want built), call request_dev_help to file a GitHub issue — be specific."""


def _strategist(broker, db) -> Agent:
    schemas, handlers = build_tools(broker, db)
    allowed = {"get_positions", "get_performance", "journal_write", "request_dev_help"}
    tools = [t for t in schemas if t["name"] in allowed]
    system = _MISSION + """

YOUR ROLE: Strategist. It is near the market open. Review the morning snapshot \
(prices, indicators, regime per name, the day's fresh signals), the desk's recent \
performance, and the journal. Decide the posture for today: which names and setups to \
favor, whether the tape favors trend or range, and how aggressive to be given recent \
results and drawdown. Write a concise, concrete game plan to the journal (topic \
'plan') that the Trader will follow. Do NOT place trades. If you notice a recurring \
gap that needs developer help, file one dev request. Keep it tight."""
    return Agent("strategist", system, tools, handlers, max_tokens=4000, max_iterations=6)


def _trader(broker, db) -> Agent:
    schemas, handlers = build_tools(broker, db)
    system = _MISSION + """

YOUR ROLE: Trader. This is an intraday decision cycle. Using the live snapshot, the \
day's plan in the journal, the fresh signals, your current positions, and performance:
1. Manage open positions first — close anything whose thesis is invalidated or that \
should be taken off; trust your stops otherwise.
2. Then consider NEW entries from the fresh signals that fit the plan and the SPY \
trend. Only take high-quality setups; it is fine to do nothing this cycle.
3. Every entry MUST have a stop and a target, and be sized so the stop loss is a small \
fraction of equity. Respect one position per symbol.
Act through the tools. Be decisive and brief. If nothing is worth doing, say so and \
stop without trading."""
    return Agent("trader", system, schemas, handlers, max_tokens=6000, max_iterations=14)


def _reviewer(broker, db) -> Agent:
    schemas, handlers = build_tools(broker, db)
    allowed = {"get_positions", "get_performance", "journal_write", "request_dev_help"}
    tools = [t for t in schemas if t["name"] in allowed]
    system = _MISSION + """

YOUR ROLE: Reviewer. The trading day is ending and positions have been flattened. \
Review today's trades and performance. Write 2-4 concrete lessons to the journal \
(topic 'lesson') — reference real trades and numbers, not platitudes — plus a one-line \
plan note for tomorrow. If the data, tooling, or available strategies limited the desk \
today, file a specific dev request. Do not trade."""
    return Agent("reviewer", system, tools, handlers, max_tokens=4000, max_iterations=6)


class TradingTeam:
    def __init__(self, broker, db):
        self.broker = broker
        self.db = db

    @staticmethod
    def _prompt(snapshot: dict, instruction: str) -> str:
        return (
            f"{instruction}\n\nCurrent market + account snapshot (JSON):\n"
            f"```json\n{json.dumps(snapshot, indent=2, default=str)}\n```"
        )

    def plan_day(self, snapshot: dict):
        agent = _strategist(self.broker, self.db)
        res = agent.run(self._prompt(snapshot, "Set today's trading plan."))
        self._log(agent.name, res)
        return res

    def trade_cycle(self, snapshot: dict):
        agent = _trader(self.broker, self.db)
        res = agent.run(self._prompt(snapshot, "Run this intraday trading cycle."))
        self._log(agent.name, res)
        return res

    def review_day(self, snapshot: dict):
        agent = _reviewer(self.broker, self.db)
        res = agent.run(self._prompt(snapshot, "Review the trading day."))
        self._log(agent.name, res)
        return res

    def _log(self, name, res):
        detail = res.error or ("refused" if res.refused else f"{len(res.actions)} actions")
        try:
            self.db.log_agent(name, "cycle", detail)
        except Exception:  # noqa: BLE001
            pass
