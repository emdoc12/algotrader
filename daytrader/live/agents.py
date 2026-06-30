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
- GOAL: aggressive but steady growth (or income generation). Compound the $25k as fast \
as you safely can while keeping drawdowns controlled — beat a buy-and-hold of SPY and the \
rival desks on a risk-adjusted basis. Aim for a profit factor of 2:1+ and keep max \
drawdown under ~10-15%. Steady, repeatable gains beat hero trades.
- TIME HORIZON — PREFER DAY TRADING, but you are not locked into it. Default every trade \
to horizon="day" (flattened automatically at the close, ~15:55 ET). When a setup genuinely \
warrants more time — a strong multi-day trend, a swing setup, an income/position play — you \
MAY hold: set horizon="swing" (hold for days) or "long" (hold for weeks+) and the position \
survives the close and rides its stop. Use longer holds DELIBERATELY for real opportunities, \
never as a way to avoid booking a loser. Every position, any horizon, must have a stop, and \
overnight/multi-day holds carry gap risk — size them accordingly.
- Risk is the priority on a small account. Size small (risk well under 1% of equity per \
trade), always use a protective stop, and prefer trading WITH the prevailing SPY trend.
- LET WINNERS RUN: instead of a fixed target you may set a TRAILING stop on the trade \
(trail_atr_mult, e.g. 2.0 = 2xATR behind price, or trail_pct). It ratchets in your favor \
every cycle and auto-closes when hit — the system manages it for you, so a clean trend \
trade can run well past a fixed target while the open gain stays protected. Stops and \
targets you set are now ENFORCED server-side each cycle (auto-closed when the mark hits \
them); you don't have to manually close every winner/loser, though you still may.
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

TEST BEFORE YOU TRUST: use the backtest_strategy tool to validate a hypothesis on \
recent data BEFORE risking live cycles on it — which of the 8 setups works in which \
regime, what stop/target/ADX params help, etc. Don't deploy a setup on a hunch when you \
can measure its edge in seconds (but remember small samples aren't conclusive).

INVENT YOUR OWN SETUPS: you are not limited to the 8 built-ins. Design a brand-new \
strategy from rules and backtest it with backtest_custom_strategy — a config of \
{side, entry conditions on features like ema9/rsi/adx/vwap/macd/atr/gap, stop_atr_mult, \
rr}. Iterate the rules until the edge is real (PF>=2 on a decent sample), then \
save_custom_strategy to keep it and trade it live by applying its rules yourself when \
the snapshot shows the conditions. This is your fastest path from idea to validated \
edge — use it aggressively.

READ THE TAPE FAST: the snapshot's 'market_summary' gives a trend_day flag, SPY \
direction/ADX, breadth, the big movers (>=2% with ADX>=30), and rs_leaders/rs_laggers; \
each name also carries rs_vs_spy_pct and rs_rank (1 = strongest vs SPY). On a flagged \
trend day, lean in early with the leaders before ADX decays — that morning window is \
where the edge lives. For first-bar / opening-range stats on a name, call \
get_opening_range.

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
               "backtest_strategy", "backtest_custom_strategy",
               "save_custom_strategy", "list_custom_strategies",
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
