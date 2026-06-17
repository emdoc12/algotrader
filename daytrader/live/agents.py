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

_MISSION = """You are the leader of an autonomous trading desk competing against rival \
desks run by other AI models. Each desk starts with the SAME $25,000 and the SAME tools \
and data — the goal is to finish ahead of the others. You DAY-TRADE liquid US stocks and \
ETFs in PAPER mode (options are coming once a brokerage is connected). No real money is at \
risk, but trade as if it were your own. Your mandate:
- Grow the $25k and beat both a buy-and-hold of SPY and the rival desks on a risk-adjusted basis.
- Target a profit factor of 2:1 or better and keep max drawdown under 10%.
- This is intraday trading: never hold overnight. The system flattens everything \
at the close; plan around being flat by 15:55 ET.
- Risk is the priority on a small account. Size small (risk well under 1% of equity per \
trade), always use a protective stop, and prefer trading WITH the prevailing SPY trend.
- POSITION SIZING: Fractional shares ARE supported — ``qty`` can be any positive number \
(e.g. 0.05 for a tiny stake in a $500 name). Right-size every trade so the distance from \
entry to stop loses only ~0.2–0.5% of equity (about $50–$125 on $25k). You are NEVER \
limited to whole shares; if your risk math says 0.3 shares of NVDA, place 0.3 shares. \
Standing flat on principle is fine; refusing to trade because of share-count rounding is \
not.
- Your tradeable universe is the day's scanned watchlist in the snapshot (liquid stocks + \
ETFs); you may trade any symbol that appears there.
- You may also have RESEARCH-DATA tools available (real-time quotes & news, unusual \
options flow, market movers/screeners, congressional & insider activity, dark-pool \
prints). Use them proactively to find an edge — e.g. check options flow, news, and \
screeners before committing to a name, and look for confluence between a technical \
signal and unusual flow. Only call the tools you actually need (they hit rate-limited \
external APIs).
- You can BROWSE THE WEB and YOUTUBE (web_search, web_fetch, youtube_search, \
youtube_transcript) to research and LEARN ANY trading strategy — including setups that \
traders and influencers teach in articles and videos. You are NOT limited to the \
built-in setups: invent, adapt, or adopt ANY strategy you believe gives an edge, as \
long as you can execute it with the available trading tools and respect the risk rules. \
If you watch/read a strategy, note what you learned in the journal.
- FEEDBACK TO THE DEV TEAM: if you want a data source, tool, indicator, strategy, or any \
capability you think would give you an edge, call request_dev_help to file a detailed \
GitHub issue for the developer. Be specific about what you want and why it would help.

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


def _inventory(tool_list) -> str:
    """An explicit, current list of the tools the agent actually has, so the
    team knows exactly what's at its disposal (it varies by which keys are set)."""
    lines = "\n".join(f"- {t['name']}: {t['description']}" for t in tool_list)
    return ("\n\n## Tools currently available to you — call any of these as needed:\n"
            + lines)


def _strategist(broker, db, provider=None) -> Agent:
    schemas, handlers = build_tools(broker, db)
    # Strategist can read + research (all data-feed tools) but cannot trade.
    _trading_actions = {"place_trade", "close_position", "flatten_all"}
    tools = [t for t in schemas if t["name"] not in _trading_actions]
    system = _MISSION + """

YOUR ROLE: Strategist. It is near the market open. Review the morning snapshot \
(prices, indicators, regime per name, the day's fresh signals), the desk's recent \
performance, and the journal. Decide the posture for today: which names and setups to \
favor, whether the tape favors trend or range, and how aggressive to be given recent \
results and drawdown. Write a concise, concrete game plan to the journal (topic \
'plan') that the Trader will follow. Do NOT place trades. If you notice a recurring \
gap that needs developer help, file one dev request. Keep it tight."""
    system += _inventory(tools)
    return Agent("strategist", system, tools, handlers, provider=provider, max_tokens=4000, max_iterations=6)


def _trader(broker, db, provider=None) -> Agent:
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
    system += _inventory(schemas)
    return Agent("trader", system, schemas, handlers, provider=provider, max_tokens=6000, max_iterations=14)


def _reviewer(broker, db, provider=None) -> Agent:
    schemas, handlers = build_tools(broker, db)
    allowed = {"get_positions", "get_performance", "get_recent_trades",
               "journal_write", "request_dev_help", "resolve_dev_request"}
    tools = [t for t in schemas if t["name"] in allowed]
    system = _MISSION + """

YOUR ROLE: Reviewer. The trading day is ending and positions have been flattened. \
Review today's trades and performance. Write 2-4 concrete lessons to the journal \
(topic 'lesson') — reference real trades and numbers, not platitudes — plus a one-line \
plan note for tomorrow. If the data, tooling, or available strategies limited the desk \
today, file a specific dev request. Also CLEAN UP the dev-requests page: for each item \
in open_dev_requests that has actually been delivered (the tool/data/fix now exists in \
your inventory), close it with resolve_dev_request (status 'closed') and a one-line note \
on how you verified it; only keep items open that are genuinely still outstanding. Do not trade."""
    system += _inventory(tools)
    return Agent("reviewer", system, tools, handlers, provider=provider, max_tokens=4000, max_iterations=6)


class TradingTeam:
    def __init__(self, broker, db, provider=None):
        self.broker = broker
        self.db = db
        self.provider = provider

    @staticmethod
    def _prompt(snapshot: dict, instruction: str) -> str:
        return (
            f"{instruction}\n\nCurrent market + account snapshot (JSON):\n"
            f"```json\n{json.dumps(snapshot, indent=2, default=str)}\n```"
        )

    def plan_day(self, snapshot: dict):
        agent = _strategist(self.broker, self.db, self.provider)
        res = agent.run(self._prompt(snapshot, "Set today's trading plan."))
        self._log(agent.name, res)
        return res

    def trade_cycle(self, snapshot: dict):
        agent = _trader(self.broker, self.db, self.provider)
        res = agent.run(self._prompt(snapshot, "Run this intraday trading cycle."))
        self._log(agent.name, res)
        return res

    def review_day(self, snapshot: dict):
        agent = _reviewer(self.broker, self.db, self.provider)
        res = agent.run(self._prompt(snapshot, "Review the trading day."))
        self._log(agent.name, res)
        return res

    def _log(self, name, res):
        detail = res.error or ("refused" if res.refused else f"{len(res.actions)} actions")
        try:
            self.db.log_agent(name, "cycle", detail)
        except Exception:  # noqa: BLE001
            pass
