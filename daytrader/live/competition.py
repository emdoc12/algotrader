"""The model competition: four desks, identical resources, one leaderboard.

Each contestant (Claude, OpenAI, Grok, Qwen) runs the exact same desk — same
tools, same market data, same $25k starting cash — driven by its own model. We
build the market view ONCE per cycle and overlay each team's own account, so the
only variable is the model's decisions. The leaderboard ranks them on equity and
risk-adjusted performance.

Teams whose API key is absent are simply skipped (logged), so you can run any
subset by setting only the keys you have.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from daytrader.live.agents import TradingTeam
from daytrader.live.db import LiveDB
from daytrader.live.market_state import market_only, with_account
from daytrader.live.paper_broker import PaperBroker
from daytrader.live.providers import default_team_providers, has_key
from daytrader.live import settings as _settings

ET = ZoneInfo("America/New_York")
OPEN, PLAN_BY, EOD_FLAT, CLOSE = dtime(9, 30), dtime(9, 45), dtime(15, 50), dtime(16, 0)

START_CASH = float(os.environ.get("START_EQUITY", "25000"))
INTERVAL_SEC = int(os.environ.get("AGENT_INTERVAL_SECONDS", "900"))
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "3.0"))
WATCHLIST_SIZE = int(os.environ.get("WATCHLIST_SIZE", "18"))
DATA_DIR = os.environ.get("DAYTRADER_DATA_DIR") or os.path.dirname(
    os.environ.get("DAYTRADER_DB_PATH", "")) or "/home/user/algotrader/cache"


def team_db_path(name: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"team_{name}.db")


_LAST_NOTIFY: dict[str, float] = {}


def _notify(msg: str, throttle_key: str | None = None, throttle_sec: float = 1800):
    """Best-effort Discord alert (if DISCORD_WEBHOOK_URL is set). Optionally
    throttled per key so a recurring failure doesn't spam the channel."""
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    if throttle_key is not None:
        now = time.time()
        if now - _LAST_NOTIFY.get(throttle_key, 0) < throttle_sec:
            return
        _LAST_NOTIFY[throttle_key] = now
    try:
        data = json.dumps({"content": msg[:1900]}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class Team:
    name: str
    provider: object
    db: LiveDB
    broker: PaperBroker
    desk: TradingTeam
    day_start_equity: float = START_CASH
    halted: bool = False


def _build_team(name: str, provider) -> Team:
    db = LiveDB(team_db_path(name))
    broker = PaperBroker(db, starting_equity=START_CASH)
    desk = TradingTeam(broker, db, provider=provider)
    return Team(name=name, provider=provider, db=db, broker=broker, desk=desk,
                day_start_equity=broker.equity())


def build_teams(only_with_keys: bool = True) -> list[Team]:
    """Instantiate every contestant that has its API key configured."""
    _settings.apply_to_env()
    teams: list[Team] = []
    for name, provider in default_team_providers().items():
        if only_with_keys and not has_key(provider):
            continue
        db = LiveDB(team_db_path(name))
        broker = PaperBroker(db, starting_equity=START_CASH)
        desk = TradingTeam(broker, db, provider=provider)
        teams.append(Team(name=name, provider=provider, db=db, broker=broker, desk=desk,
                          day_start_equity=broker.equity()))
    return teams


def leaderboard(teams: list[Team] | None = None) -> list[dict]:
    """Ranked standings across teams (by equity, with risk-adjusted detail)."""
    own = teams is None
    teams = teams or build_teams(only_with_keys=False)
    rows = []
    for t in teams:
        perf = t.broker.performance()
        eq = t.broker.equity()
        rows.append({
            "team": t.name,
            "model": getattr(t.provider, "model", "?"),
            "equity": round(eq, 2),
            "return_pct": round((eq / START_CASH - 1) * 100, 2),
            "drawdown_pct": round(t.broker.drawdown_pct(), 2),
            "profit_factor": round(perf.get("profit_factor", 0), 2),
            "win_rate": round(perf.get("win_rate", 0), 1),
            "n_trades": perf.get("n_trades", 0),
            "open_positions": len(t.broker.positions()),
        })
    rows.sort(key=lambda r: r["equity"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    if own:
        for t in teams:
            t.db.close()
    return rows


def team_names() -> list[str]:
    return list(default_team_providers().keys())


def _trade_stats(trades: list[dict]) -> dict:
    pnls = [float(t.get("pnl") or 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gp, gl = sum(wins), -sum(losses)
    return {
        "n_trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
        "profit_factor": round(gp / gl, 2) if gl > 0 else (round(gp, 2) if gp else 0.0),
        "total_pnl": round(sum(pnls), 2),
    }


def db_standings() -> list[dict]:
    """Network-free standings read straight from each team's DB snapshots.

    Used by the dashboard so it never blocks on quotes or fights the trading
    loop for broker state. Shows all four teams even before any have a key.
    """
    providers = default_team_providers()
    rows = []
    for name, provider in providers.items():
        eq = START_CASH
        cash = START_CASH
        dd = 0.0
        stats = {"n_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "total_pnl": 0.0}
        n_open = 0
        try:
            db = LiveDB(team_db_path(name))
            last = db.last_equity()
            if last:
                eq = float(last.get("equity", START_CASH))
                cash = float(last.get("cash", START_CASH))
                dd = float(last.get("drawdown_pct", 0.0))
            stats = _trade_stats(db.recent_trades(limit=1000))
            n_open = len(db.load_open_positions())
            db.close()
        except Exception:  # noqa: BLE001
            pass
        rows.append({
            "team": name,
            "model": getattr(provider, "model", "?"),
            "has_key": has_key(provider),
            "equity": round(eq, 2),
            "cash": round(cash, 2),
            "return_pct": round((eq / START_CASH - 1) * 100, 2),
            "drawdown_pct": round(dd, 2),
            "open_positions": n_open,
            **stats,
        })
    rows.sort(key=lambda r: r["equity"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def chat_with_leader(team_name: str, message: str) -> dict:
    """Ask a team's leader a question; persists the exchange to the team DB.

    Runs on that team's own model with its trading context (no tools — just a
    reply). Returns {ok, reply, error}.
    """
    providers = default_team_providers()
    provider = providers.get(team_name)
    if provider is None:
        return {"ok": False, "reply": "", "error": f"unknown team {team_name}"}
    db = LiveDB(team_db_path(team_name))
    try:
        if not has_key(provider):
            return {"ok": False, "reply": "", "error": f"{team_name} has no API key configured"}
        positions = db.load_open_positions()
        trades = db.recent_trades(limit=15)
        journal = db.recent_journal(limit=12)
        last = db.last_equity() or {}
        history = db.recent_chat(limit=10)
        context = {
            "equity": last.get("equity", START_CASH),
            "open_positions": positions,
            "recent_trades": trades,
            "recent_journal": journal,
            "recent_chat": history,
        }
        import json
        system = (
            f"You are the LEADER of the '{team_name}' autonomous trading desk, which "
            f"day-trades a $10k paper account of US stocks/ETFs in a competition against "
            f"rival AI desks. The owner is messaging you with a question or suggestion "
            f"about your trades and strategy. Answer directly and concisely as the desk "
            f"lead: explain your reasoning, own your results, and take the owner's "
            f"suggestions seriously (you can say you'll adjust the plan and note it in "
            f"the journal next session). Here is your current context:\n```json\n"
            f"{json.dumps(context, indent=2, default=str)}\n```"
        )
        db.add_chat("owner", message)
        res = provider.run_loop(system, tools=[], handlers={}, user_message=message,
                                max_tokens=1500, max_iterations=1)
        reply = res.text or res.error or "(no reply)"
        db.add_chat("leader", reply)
        return {"ok": not bool(res.error), "reply": reply, "error": res.error}
    finally:
        db.close()


class Competition:
    """Runs all teams through the trading day against one shared market view."""

    def __init__(self):
        self.teams = build_teams()
        self._day = None

    def _sync_teams(self):
        """Activate any team whose API key has appeared (e.g. entered via the
        settings page) since startup — no restart needed."""
        _settings.apply_to_env()
        have = {t.name for t in self.teams}
        for name, provider in default_team_providers().items():
            if name not in have and has_key(provider):
                self.teams.append(_build_team(name, provider))
                print(f"[competition] activated team '{name}' ({getattr(provider,'model','?')})")

    # -- shared-cycle phases --------------------------------------------
    def _market(self):
        return market_only(symbols=None if WATCHLIST_SIZE else None)

    def plan_all(self):
        market = market_only()
        for t in self.teams:
            t.desk.plan_day(with_account(market, t.broker))

    def trade_all(self):
        market = market_only()
        # Pin the snapshot's quote map onto each broker for this cycle so the
        # broker fills at the exact prices the agent reasoned over (no more
        # feed-vs-broker drift flipping winners into losers).
        cycle_quotes = dict(market.get("quotes") or {})
        for t in self.teams:
            if t.halted:
                continue
            self._risk_check(t)
            if not t.halted:
                t.broker.set_cycle_quotes(cycle_quotes)
                try:
                    res = t.desk.trade_cycle(with_account(market, t.broker))
                    if getattr(res, "error", None):
                        _notify(f"⚠️ Team {t.name} ({getattr(t.provider,'model','?')}) cycle error: {res.error}",
                                throttle_key=f"err_{t.name}")
                finally:
                    t.broker.set_cycle_quotes(None)
                t.broker.db.record_equity(t.broker.cash(), t.broker.equity(),
                                          len(t.broker.positions()), t.broker.drawdown_pct())

    def review_all(self):
        market = market_only()
        for t in self.teams:
            if t.broker.positions():
                t.broker.flatten_all(reason="eod_flat")
            t.desk.review_day(with_account(market, t.broker))

    def _risk_check(self, t: Team):
        if t.halted or t.day_start_equity <= 0:
            return
        day_pnl = (t.broker.equity() / t.day_start_equity - 1) * 100
        if day_pnl <= -DAILY_LOSS_LIMIT_PCT:
            t.halted = True
            t.broker.flatten_all(reason="daily_loss_limit")
            t.db.log_agent("runner", "circuit_breaker", f"{day_pnl:.2f}%")
            _notify(f"🛑 Team {t.name} hit the daily loss limit ({day_pnl:.1f}%) — flattened and halted for the day.")

    def _new_day(self, now):
        self._day = now.date()
        for t in self.teams:
            t.halted = False
            t.day_start_equity = t.broker.equity()
            t.db.log_agent("runner", "new_day", str(self._day))

    # -- the always-on loop ---------------------------------------------
    def run_forever(self):
        print(f"[competition] starting; teams online: {[t.name for t in self.teams]} "
              f"(others activate when their API key is set)")
        _notify(f"🤖 Trading desk competition online — teams: {[t.name for t in self.teams] or 'none yet'}")
        planned = reviewed = False
        while True:
            try:
                self._sync_teams()
                now = datetime.now(ET)
                if self._day != now.date():
                    self._new_day(now)
                    planned = reviewed = False
                if now.weekday() >= 5 or not (OPEN <= now.time() < CLOSE):
                    time.sleep(60)
                    continue
                t = now.time()
                if not planned and t < PLAN_BY:
                    self.plan_all(); planned = True
                elif t >= EOD_FLAT:
                    if not reviewed:
                        self.review_all(); reviewed = True
                    time.sleep(120); continue
                else:
                    self.trade_all()
                time.sleep(INTERVAL_SEC)
            except Exception as e:  # noqa: BLE001 - never die
                print(f"[competition] loop error: {e!r}")
                time.sleep(60)
