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
# Don't START a fresh (multi-minute) trade cycle right before the close, so the
# EOD flatten/review deadline is reliably reachable.
NO_NEW_TRADES_AFTER = dtime(15, 30)

# US equity market full-day closures (NYSE). Static table — extend yearly.
_MARKET_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def _today_et() -> str:
    return datetime.now(ET).date().isoformat()


def _is_market_holiday(d) -> bool:
    return d.isoformat() in _MARKET_HOLIDAYS

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
    team = Team(name=name, provider=provider, db=db, broker=broker, desk=desk,
                day_start_equity=broker.equity())
    _restore_risk_state(team)
    return team


def _restore_risk_state(team: Team) -> None:
    """Recover today's circuit-breaker baseline + halted flag across restarts,
    so a redeploy can't hand a team a fresh loss budget or un-halt it."""
    try:
        if team.db.kv_get("risk_date") == _today_et():
            dse = team.db.kv_get("day_start_equity")
            if dse:
                team.day_start_equity = float(dse)
            team.halted = team.db.kv_get("halted") == "1"
        else:
            team.day_start_equity = team.broker.equity()
            team.halted = False
    except Exception:  # noqa: BLE001
        pass


def build_teams(only_with_keys: bool = True) -> list[Team]:
    """Instantiate every contestant that has its API key configured."""
    _settings.apply_to_env()
    teams: list[Team] = []
    for name, provider in default_team_providers().items():
        if only_with_keys and not has_key(provider):
            continue
        teams.append(_build_team(name, provider))
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
        # None = undefined (no losing trades yet); the UI renders it as ∞.
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
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
        cost_today = 0.0
        cost_total = 0.0
        try:
            db = LiveDB(team_db_path(name))
            last = db.last_equity()
            if last:
                eq = float(last.get("equity", START_CASH))
                cash = float(last.get("cash", START_CASH))
                dd = float(last.get("drawdown_pct", 0.0))
            stats = _trade_stats(db.recent_trades(limit=1000))
            n_open = len(db.load_open_positions())
            try:
                cost_today = db.usage_totals(since_iso=datetime.now(ET).strftime("%Y-%m-%dT00:00:00"))["cost_usd"]
                cost_total = db.usage_totals()["cost_usd"]
            except Exception:  # noqa: BLE001
                pass
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
            "cost_today": round(cost_today, 2),
            "cost_total": round(cost_total, 2),
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
        # A concise summary of the tools the desk actually uses during live
        # cycles, built from the real tool list so the leader can accurately
        # answer capability questions in chat (this chat channel itself is
        # tool-less — the tools are attached during real trading cycles).
        caps = ""
        try:
            from daytrader.live.paper_broker import PaperBroker
            from daytrader.live.tools import build_tools
            schemas, _ = build_tools(PaperBroker(db, starting_equity=START_CASH), db)
            caps = "\n\nDuring live trading cycles you have these tools (not attached to this chat):\n" + \
                   "\n".join(f"- {t['name']}: {t.get('description','')[:130]}" for t in schemas)
        except Exception:  # noqa: BLE001
            pass
        system = (
            f"You are the LEADER of the '{team_name}' autonomous trading desk, which "
            f"trades a ${START_CASH:,.0f} paper account of US stocks/ETFs — day-trading by "
            f"default, but free to swing-trade or hold longer when warranted — in a "
            f"competition against rival AI desks. The owner is messaging you with a question or suggestion "
            f"about your trades and strategy. Answer directly and concisely as the desk "
            f"lead: explain your reasoning, own your results, and take the owner's "
            f"suggestions seriously (you can say you'll adjust the plan and note it in "
            f"the journal next session). If asked HOW you trade/execute, describe the tools "
            f"below accurately (you place market orders via place_trade with a required stop "
            f"and target, can set a day/swing/long horizon and a trailing stop, and backtest "
            f"ideas before deploying)."
            f"{caps}"
            f"\n\nHere is your current context:\n```json\n"
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
    @staticmethod
    def _record_usage(t: Team, role: str, res) -> None:
        """Persist token usage + estimated cost for an agent call."""
        try:
            u = getattr(res, "usage", None) or {}
            it, ot = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
            if it == 0 and ot == 0:
                return
            from daytrader.live import pricing
            cost = pricing.cost_usd(t.name, it, ot, u.get("cached_input_tokens", 0))
            t.db.record_usage(role, getattr(t.provider, "model", "?"), it, ot, cost)
        except Exception:  # noqa: BLE001
            pass

    def plan_all(self):
        market = market_only()
        d = _today_et()
        for t in self.teams:
            if t.db.kv_get("planned_date") == d:
                continue  # already planned today (idempotent across restarts)
            res = t.desk.plan_day(with_account(market, t.broker))
            self._record_usage(t, "strategist", res)
            t.db.kv_set("planned_date", d)

    @staticmethod
    def _held_symbol_data(team, base_quotes: dict, base_atr: dict):
        """Extend the cycle's quote + ATR maps with entries for any HELD symbol
        that fell off today's scanned watchlist, so trailing stops keep
        ratcheting on swing/long holds instead of silently freezing."""
        q, a = dict(base_quotes), dict(base_atr)
        try:
            held = [p.get("symbol") for p in team.broker.positions()]
        except Exception:  # noqa: BLE001
            held = []
        missing = [s for s in held if s and (s not in q or s not in a)]
        if not missing:
            return q, a
        from daytrader.data import loader as _loader
        from daytrader.core import indicators as _ind
        for sym in missing:
            try:
                if sym not in q:
                    from daytrader.data import quotes as _quotes
                    px = _quotes.get_quote(sym)
                    if px is not None:
                        q[sym] = px
                if sym not in a:
                    df = _loader.load(sym, interval="5m", max_age_hours=0.1)
                    if df is not None and len(df) >= 15:
                        a[sym] = float(_ind.atr(df, 14).iloc[-1])
            except Exception:  # noqa: BLE001
                continue
        return q, a

    def trade_all(self):
        market = market_only()
        # Pin the snapshot's quote map onto each broker for this cycle so the
        # broker fills at the exact prices the agent reasoned over (no more
        # feed-vs-broker drift flipping winners into losers).
        cycle_quotes = dict(market.get("quotes") or {})
        base_atr = {sym: m.get("atr14") for sym, m in (market.get("market") or {}).items()
                    if m.get("atr14") is not None}
        for t in self.teams:
            self._risk_check(t)  # may halt + flatten this team's DAY trades
            # Per-team maps that also cover held-outside-scan symbols.
            q, a = self._held_symbol_data(t, cycle_quotes, base_atr)
            t.broker.set_cycle_quotes(q)
            try:
                # Enforce server-side brackets EVERY cycle, even for halted teams
                # — their surviving swing/long holds still need their stops run.
                t.broker.manage_positions(q, a)
                if not t.halted:
                    res = t.desk.trade_cycle(with_account(market, t.broker))
                    self._record_usage(t, "trader", res)
                    if getattr(res, "error", None):
                        _notify(f"⚠️ Team {t.name} ({getattr(t.provider,'model','?')}) cycle error: {res.error}",
                                throttle_key=f"err_{t.name}")
            finally:
                t.broker.set_cycle_quotes(None)
            t.broker.db.record_equity(t.broker.cash(), t.broker.equity(),
                                      len(t.broker.positions()), t.broker.drawdown_pct())

    def review_all(self):
        market = None
        d = _today_et()
        for t in self.teams:
            # 1) Ensure DAY trades are flat — retry every cycle until they are
            #    (a failed flatten, e.g. Yahoo down, must not be treated as done).
            day_open = [p for p in t.broker.positions() if p.get("horizon", "day") == "day"]
            if day_open:
                t.broker.flatten_all(reason="eod_flat", horizons={"day"})
                day_open = [p for p in t.broker.positions() if p.get("horizon", "day") == "day"]
            # 2) Run the Reviewer exactly once per day, and only after the day
            #    book is actually flat.
            if t.db.kv_get("reviewed_date") == d:
                continue
            if day_open:
                continue  # flatten still failing; retry next cycle, don't review yet
            if market is None:
                market = market_only()
            res = t.desk.review_day(with_account(market, t.broker))
            self._record_usage(t, "reviewer", res)
            t.db.kv_set("reviewed_date", d)

    def _save_risk(self, t: Team, date_iso: str | None = None) -> None:
        try:
            t.db.kv_set("risk_date", date_iso or _today_et())
            t.db.kv_set("day_start_equity", f"{t.day_start_equity}")
            t.db.kv_set("halted", "1" if t.halted else "0")
        except Exception:  # noqa: BLE001
            pass

    def _risk_check(self, t: Team):
        if t.halted or t.day_start_equity <= 0:
            return
        day_pnl = (t.broker.equity() / t.day_start_equity - 1) * 100
        if day_pnl <= -DAILY_LOSS_LIMIT_PCT:
            t.halted = True
            # Stop the day-trading bleed; leave deliberate swing/long holds on
            # their own stops rather than force-closing a longer-term thesis.
            t.broker.flatten_all(reason="daily_loss_limit", horizons={"day"})
            self._save_risk(t)
            t.db.log_agent("runner", "circuit_breaker", f"{day_pnl:.2f}%")
            _notify(f"🛑 Team {t.name} hit the daily loss limit ({day_pnl:.1f}%) — flattened and halted for the day.")

    def _new_day(self, now):
        self._day = now.date()
        d = now.date().isoformat()
        for t in self.teams:
            t.halted = False
            t.day_start_equity = t.broker.equity()
            self._save_risk(t, d)
            t.db.log_agent("runner", "new_day", d)

    # -- the always-on loop ---------------------------------------------
    def run_forever(self):
        print(f"[competition] starting; teams online: {[t.name for t in self.teams]} "
              f"(others activate when their API key is set)")
        _notify(f"🤖 Trading desk competition online — teams: {[t.name for t in self.teams] or 'none yet'}")
        while True:
            try:
                self._sync_teams()
                now = datetime.now(ET)
                if self._day != now.date():
                    self._new_day(now)
                # Weekends and market holidays: idle. (Per-team planned/reviewed
                # state is persisted, so restarts never double-run either phase.)
                if now.weekday() >= 5 or _is_market_holiday(now.date()):
                    time.sleep(300); continue
                t = now.time()
                if t < OPEN:
                    time.sleep(60); continue
                # EOD is DEADLINE-based: once past 15:50 ET, flatten day trades +
                # review — reachable even if a trade cycle overran 16:00. review_all
                # is idempotent, so this is cheap once the day is done.
                if t >= EOD_FLAT:
                    self.review_all()
                    time.sleep(120); continue
                if t < PLAN_BY:
                    self.plan_all()
                elif t < NO_NEW_TRADES_AFTER:
                    self.trade_all()
                else:
                    # Between 15:30 and 15:50: hold — don't start a long cycle
                    # that would blow past the EOD deadline. Manage brackets only.
                    self._manage_only()
                time.sleep(INTERVAL_SEC)
            except Exception as e:  # noqa: BLE001 - never die
                print(f"[competition] loop error: {e!r}")
                time.sleep(60)

    def _manage_only(self):
        """Run server-side bracket enforcement (trailing stops, stop/target
        auto-exec) without a fresh LLM decision cycle — used in the pre-close
        window when starting a full cycle would risk missing the EOD deadline."""
        market = market_only()
        cycle_quotes = dict(market.get("quotes") or {})
        base_atr = {sym: m.get("atr14") for sym, m in (market.get("market") or {}).items()
                    if m.get("atr14") is not None}
        for t in self.teams:
            q, a = self._held_symbol_data(t, cycle_quotes, base_atr)
            t.broker.set_cycle_quotes(q)
            try:
                t.broker.manage_positions(q, a)
            finally:
                t.broker.set_cycle_quotes(None)
            t.broker.db.record_equity(t.broker.cash(), t.broker.equity(),
                                      len(t.broker.positions()), t.broker.drawdown_pct())
