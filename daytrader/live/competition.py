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

from daytrader.core.types import Side
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
# Between full (LLM) trade cycles, poll stops/targets/auto-scale this often so a
# fast move can't run far past a stop before the next check (tightens the
# cycle-polled stop from ~15 min to this — reduces stop-through severity).
STOP_POLL_SEC = int(os.environ.get("STOP_POLL_SECONDS", "120"))

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


def _adx_info(df) -> dict:
    """{"adx14", "adx_slope"} from a 5m frame — the inputs an ADX-decay exit needs.

    Slope is measured over the same ~3-bar span the snapshot uses, so a live
    decay exit and the snapshot's ``adx_rising`` agree on direction.
    """
    from daytrader.core import indicators as _ind
    try:
        s = _ind.adx(df, 14)
        cur = float(s.iloc[-1])
        prev = float(s.iloc[-4]) if len(s) >= 4 else cur
        return {"adx14": cur, "adx_slope": round(cur - prev, 2)}
    except Exception:  # noqa: BLE001
        return {}

START_CASH = float(os.environ.get("START_EQUITY", "25000"))
INTERVAL_SEC = int(os.environ.get("AGENT_INTERVAL_SECONDS", "900"))
# Off-session (nights, weekends, holidays) the desks still get crypto decision
# cycles — but THROTTLED. In-session they decide every ~15 min because the
# equity tape demands it; running that cadence 24/7 would roughly 5x the LLM
# bill (168 open hours a week vs ~32.5) for a three-symbol book. Every few
# hours is enough for swing-scale crypto; the ~2-min stop poll (free — quotes
# only) is what actually guards the positions in between.
CRYPTO_CYCLE_MIN = float(os.environ.get("CRYPTO_CYCLE_MINUTES", "180"))
# SEPTEMBER 2026 TRIAL: the owner opened the off-hours lane to a full 15-minute
# cadence for every desk — same cadence the equity session gets — to see whether
# 12x the decision frequency produces anything. Given to ALL desks rather than
# the leaders, deliberately: handing two desks 12x the cycles would confound the
# leaderboard permanently, and the competition's whole premise is identical
# resources.
#
# It EXPIRES ON ITS OWN. A temporary cost increase that silently becomes
# permanent is the standard way a trial turns into a bill nobody chose — at
# these cadences it is the difference between ~$41 and ~$493 a month across
# seven desks. Past CRYPTO_FAST_UNTIL the cadence reverts to CRYPTO_CYCLE_MIN
# with no action required; extend the date (or clear it) to change that.
# Trial CANCELLED before it ran a full day: with the research surface the
# desks asked for, the 15-minute cadence priced out at ~$745/month across
# seven desks — against desks that have returned under 1% each over months of
# trading and researching. Paying 12x for more of a process that has not yet
# produced a result is not a trial, it is a subscription to hope. The window
# mechanism stays (set CRYPTO_FAST_UNTIL to a date to run it) because it is
# the right shape for any future cost experiment: it expires by itself.
CRYPTO_FAST_CYCLE_MIN = float(os.environ.get("CRYPTO_FAST_CYCLE_MINUTES", "15"))
CRYPTO_FAST_UNTIL = os.environ.get("CRYPTO_FAST_UNTIL", "").strip()


def crypto_cadence_min() -> float:
    """Minutes between off-hours crypto cycles, honoring the trial window."""
    if not CRYPTO_FAST_UNTIL:
        return CRYPTO_CYCLE_MIN
    try:
        from datetime import date as _date
        if datetime.now(ET).date() <= _date.fromisoformat(CRYPTO_FAST_UNTIL):
            return CRYPTO_FAST_CYCLE_MIN
    except ValueError:      # malformed date: fail to the CHEAP cadence
        return CRYPTO_CYCLE_MIN
    return CRYPTO_CYCLE_MIN


def crypto_trial_active() -> bool:
    return crypto_cadence_min() == CRYPTO_FAST_CYCLE_MIN and CRYPTO_FAST_CYCLE_MIN < CRYPTO_CYCLE_MIN
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "3.0"))
WATCHLIST_SIZE = int(os.environ.get("WATCHLIST_SIZE", "18"))
DATA_DIR = os.environ.get("DAYTRADER_DATA_DIR") or os.path.dirname(
    os.environ.get("DAYTRADER_DB_PATH", "")) or "/home/user/algotrader/cache"


def team_db_path(name: str) -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, f"team_{name}.db")


# Desks that have been RETIRED from the competition. They stop trading — no
# cycles, no spend — but nothing is deleted: their database, trades, journal and
# equity curve stay exactly as they were on the day they were cut, and the
# leaderboard keeps showing them struck through with their final numbers. A
# retired desk is a record, not a gap.
#
# Claude is retired as of the first quarter's results: $1,074.65 of model spend
# against −$961.89 of P&L, the worst return and 57% of the competition's entire
# API bill. Clear this env var to bring a desk back; its history resumes where
# it stopped rather than starting over.
RETIRED_TEAMS = {s.strip().lower() for s in
                 os.environ.get("RETIRED_TEAMS", "claude").split(",") if s.strip()}


def is_retired(name: str) -> bool:
    return str(name or "").lower() in RETIRED_TEAMS


def _mark_retired(name: str) -> None:
    """Settle the desk's book, then stamp the date the record ends.

    A retired desk never runs another cycle, so anything still open would sit
    on the dashboard forever: marked at whatever the last quote happened to be,
    never stopped out, never taken — an unsettled number pretending to be a
    position. So the book is FLATTENED once, at the moment of retirement, at
    the then-current market. That way the final equity is a real settled figure
    and the curve ends on it, which is the whole point of keeping the record.

    The settlement is guarded by its OWN key, not by retired_ts. Those were the
    same key once, and it was a bug with teeth: the first version of this
    function only stamped retired_ts, so a desk retired under it came back
    under this version already stamped — and the settle step, keyed on the same
    flag, skipped forever. Claude sat with an open SPY position on the
    dashboard because of exactly that. A desk retired before settling existed
    still needs settling, so the two facts get two keys.

    Every close is attempted individually: one symbol whose quote cannot be
    fetched must not leave the rest of the book open.
    """
    try:
        db = LiveDB(team_db_path(name))
        try:
            if not db.kv_get("retired_ts"):
                db.kv_set("retired_ts", _today_et())
            if db.kv_get("retired_settled_ts"):
                return
            closed, failed = [], []
            try:
                broker = PaperBroker(db, starting_equity=START_CASH)
                for sym in [p["symbol"] for p in broker.positions()]:
                    try:
                        res = broker.close(sym, reason="retired")
                        closed.append(f"{sym} ({res.get('pnl', 0):+.2f})"
                                      if res.get("ok") else f"{sym} (refused)")
                    except Exception as e:  # noqa: BLE001
                        failed.append(f"{sym}: {e!r}"[:120])
                for opt in broker.options.positions():
                    try:
                        broker.options.close_structure(int(opt["id"]), reason="retired")
                        closed.append(f"option #{opt['id']}")
                    except Exception as e:  # noqa: BLE001
                        failed.append(f"option #{opt.get('id')}: {e!r}"[:120])
                # Final snapshot, so the equity curve's last point is the
                # settled book rather than the last mid-cycle mark.
                db.record_equity(broker.cash(), broker.equity(),
                                 len(broker.positions()), broker.drawdown_pct())
            except Exception as e:  # noqa: BLE001
                failed.append(f"settle failed: {e!r}"[:160])
            detail = f"{name} retired — trading stopped, record preserved"
            if closed:
                detail += f"; settled {len(closed)}: {', '.join(closed)[:300]}"
            if failed:
                detail += f"; COULD NOT SETTLE: {'; '.join(failed)[:300]}"
            db.kv_set("retired_settled_ts", _today_et())
            db.log_agent("system", "retired", detail)
            print(f"[retire] {detail}")
            if failed:
                # Left open despite retiring is worth an alert: it is the one
                # case where the frozen record is not actually final.
                _notify(f"⚠️ {name} retired but could not settle everything: "
                        f"{'; '.join(failed)[:400]}")
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - never block startup on bookkeeping
        pass


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
    # Set when the PROVIDER cannot serve this desk at all (out of credit, bad
    # key, bad model). Distinct from `halted`, which is a risk decision the desk
    # earned; this one is an account problem only the owner can clear.
    provider_down: str | None = None


# One-time capital injection, applied once per team and guarded by a state key
# so a restart can never repeat it. Recorded as a CAPITAL EVENT, so it lifts the
# return denominator instead of showing up as profit.
CAPITAL_TOPUP = float(os.environ.get("CAPITAL_TOPUP", "25000"))
CAPITAL_TOPUP_ID = os.environ.get("CAPITAL_TOPUP_ID", "topup_50k_v1")


def _apply_capital_topup(team: "Team") -> None:
    if CAPITAL_TOPUP <= 0:
        return
    try:
        if team.db.kv_get(f"capital_{CAPITAL_TOPUP_ID}"):
            return
        res = team.broker.deposit(CAPITAL_TOPUP, reason=f"owner top-up ({CAPITAL_TOPUP_ID})")
        team.db.kv_set(f"capital_{CAPITAL_TOPUP_ID}", "done")
        # The day's risk anchor must move with the deposit, or the injection
        # reads as a winning day and hands the desk a fresh loss budget.
        team.day_start_equity = float(team.day_start_equity) + CAPITAL_TOPUP
        team.db.kv_set("risk_date", _today_et())
        team.db.kv_set("day_start_equity", f"{team.day_start_equity}")
        team.db.log_agent("system", "capital_event",
                          f"+${CAPITAL_TOPUP:,.0f} capital (not P&L); base now "
                          f"${res.get('capital_base', 0):,.0f}")
        print(f"[capital] {team.name}: +${CAPITAL_TOPUP:,.0f} "
              f"-> equity ${res.get('equity_after', 0):,.2f}, "
              f"base ${res.get('capital_base', 0):,.2f}")
    except Exception as e:  # noqa: BLE001 - never block startup on this
        print(f"[capital] {team.name}: top-up failed: {e!r}")


# Owner-shipped capabilities announced straight to every desk's journal under
# the 'dev_resolved' topic — the same channel dev-request resolutions use, and
# the one the mission calls "not optional reading" (platform_updates). A
# capability that ships from an OWNER request has no closing issue to
# broadcast it, so without this a desk whose habit is "check platform_updates
# for what's new" would never see it there. kv-latched per announcement, like
# the capital top-up, so a restart can never repeat one.
_ANNOUNCEMENTS: list[tuple[str, str]] = [
    ("crypto_lane_v6_44",
     "NEW CAPABILITY (v6.44.0): CRYPTO TRADES 24/7. BTC-USD, ETH-USD and "
     "SOL-USD now trade through place_trade — fractional qty, long or short, "
     "any hour including weekends. See the snapshot's 'crypto' section for "
     "live indicators. Off-session you get a throttled decision cycle every "
     "few hours; stops/targets/trails are enforced every ~2 minutes around "
     "the clock, so every crypto position MUST carry a stop you would accept "
     "being filled on unattended. horizon='day' still flattens at the equity "
     "close; 'swing'/'long' runs through nights and weekends. Equity/option "
     "orders while the US session is closed are rejected — crypto is what "
     "trades then. Weekend liquidity is thinner: size down."),

    ("futures_backtest_sizing_v6_51",
     "BUG FIXED — RE-TEST ANY FUTURES IDEA YOU DISMISSED (v6.51.0). Every "
     "futures backtest you have ever run was sized by the contract's own "
     "multiplier TOO LARGE: the sizer divided risk dollars by price POINTS, "
     "so a 0.5% risk budget was really 2.5% on MES and 5% on MGC. Nothing "
     "errored — the numbers just described a position five times the size of "
     "the one you asked about. It was not a clean scaling error either: the "
     "oversized positions tripped the backtest's daily-loss breaker, which "
     "halted trading and changed WHICH TRADES HAPPENED. A re-run of the same "
     "MES/MACD test went from -3.73% return and 6.93% drawdown to -1.09% and "
     "1.52%, with the first trade's exit changing from daily_loss_limit to a "
     "normal stop. So if you ever backtested MES, MNQ, MGC, M2K, MYM or MCL "
     "and concluded futures bleed, that conclusion rests on a broken "
     "measurement — test it again before you keep excluding the whole asset "
     "class. Equity backtests were never affected (multiplier 1.0)."),

    ("futures_hours_v6_49",
     "NEW CAPABILITY (v6.49.0): FUTURES NOW TRADE THEIR REAL HOURS. MES=F, "
     "MNQ=F, MGC=F, M2K=F, MYM=F and MCL=F were previously gated to "
     "09:30-16:00 ET as if they were stocks; they now trade the actual CME "
     "session — Sunday 18:00 ET through Friday 17:00 ET, with the daily "
     "17:00-18:00 maintenance break. That is roughly 17 extra tradeable hours "
     "a day that were closed to you. Overnight index futures are a different "
     "instrument from the same index in cash hours; if you have an overnight "
     "or gap thesis you could never express, you can now. Backtests of these "
     "contracts also work (see the note above) and size in whole contracts "
     "with real multiplier, margin and per-contract commission."),

    ("competition_rules_v6_50",
     "THE COMPETITION HAS CHANGED — read your snapshot's 'standing' block. "
     "Three things. (1) You are now judged on an explicit mandate: beat a "
     "buy-and-hold of SPY, clear 8% annualized as a floor, target 15-30%. "
     "SPY's return over the same window is on the leaderboard as its own row "
     "— it is the bar, not a rival. (2) LAST PLACE IS RELEGATED, ranked on a "
     "ROLLING 90-day window by annualized return divided by max drawdown. The "
     "window moves with you, so there is no deadline to beat and no moment "
     "when extra risk becomes rational; and because drawdown divides the "
     "score, a lucky swing on bad risk ranks WORSE than a steady grind. "
     "Trading bigger to climb will sink you. Under 20 closed trades in the "
     "window marks you 'inactive', which is its own failure, not a safe "
     "middle. (3) The Claude desk has been retired — $1,075 of model spend "
     "against -$962 of P&L. Its record stays on the board struck through. "
     "This is real; the field is six."),
]


def _apply_announcements(team: "Team") -> None:
    for key, text in _ANNOUNCEMENTS:
        try:
            if team.db.kv_get(f"announced_{key}"):
                continue
            team.db.add_journal("system", "dev_resolved", text)
            team.db.kv_set(f"announced_{key}", "done")
        except Exception:  # noqa: BLE001 - never block startup on an announcement
            pass


def _build_team(name: str, provider) -> Team:
    db = LiveDB(team_db_path(name))
    broker = PaperBroker(db, starting_equity=START_CASH)
    desk = TradingTeam(broker, db, provider=provider)
    team = Team(name=name, provider=provider, db=db, broker=broker, desk=desk,
                day_start_equity=broker.equity())
    _restore_risk_state(team)
    _apply_capital_topup(team)
    _apply_announcements(team)
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
            team.provider_down = team.db.kv_get("provider_down") or None
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
        if is_retired(name):
            _mark_retired(name)     # stamp the date, then leave the record alone
            continue
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
            "capital_base": round(t.broker.capital_base(), 2),
            # Dollar P&L is unaffected by a deposit; return% is measured against
            # the new base so an injection can never read as performance.
            "pnl": round(eq - t.broker.capital_base(), 2),
            "return_pct": round((eq / t.broker.capital_base() - 1) * 100, 2),
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


_BENCH_CACHE: dict = {}


def spy_benchmark() -> dict | None:
    """SPY buy-and-hold over the competition's own lifetime. None if unavailable.

    The mission has always told the desks to beat a buy-and-hold of SPY, but
    nothing ever measured it — the leaderboard only compared desks to each
    other. That was survivable while seven desks ran under identical
    conditions, because they were each other's control. As the field narrows
    it stops being survivable: one desk's +2% floats free, and in a rising
    tape almost any long-biased or premium-selling book looks skilful.

    Costs nothing in model calls — daily bars the loader already caches —
    and answers the only question that outlives the competition: did picking
    stocks beat owning the index.
    """
    now = time.time()
    hit = _BENCH_CACHE.get("v")
    if hit and now - hit[0] < 900:
        return hit[1]
    try:
        start = None
        for name in team_names():
            try:
                db = LiveDB(team_db_path(name))
                try:
                    row = db.conn.execute(
                        "SELECT ts FROM equity_snapshots ORDER BY id ASC LIMIT 1"
                    ).fetchone()
                finally:
                    db.close()
                if row and row["ts"]:
                    d = str(row["ts"])[:10]
                    start = d if start is None else min(start, d)
            except Exception:  # noqa: BLE001
                continue
        if not start:
            return None
        from daytrader.data import loader
        df = loader.load("SPY", interval="1d", rng="2y", max_age_hours=12)
        if df is None or len(df) < 2:
            return None
        # First close ON OR AFTER the competition's first snapshot — the day it
        # actually started, not the nearest bar in either direction.
        after = df[df.index >= start]
        if len(after) < 2:
            return None
        first, last = float(after["close"].iloc[0]), float(after["close"].iloc[-1])
        if first <= 0:
            return None
        out = {
            "symbol": "SPY",
            "start_date": str(after.index[0].date()),
            "end_date": str(after.index[-1].date()),
            "start_price": round(first, 2),
            "end_price": round(last, 2),
            "return_pct": round((last / first - 1) * 100, 2),
            # What a desk's whole capital base would be worth having simply
            # bought and held instead — the comparison in dollars, not percent.
            "equity_if_held": round(START_CASH * 2 * (last / first), 2),
            "note": "Buy and hold SPY over the same window — the benchmark the "
                    "desks are asked to beat.",
        }
        _BENCH_CACHE["v"] = (now, out)
        return out
    except Exception:  # noqa: BLE001 - the benchmark is additive, never fatal
        return None


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
        provider_down = None
        retired_ts = None
        eq = START_CASH
        cash = START_CASH
        capital_base = START_CASH
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
            try:
                capital_base = START_CASH + db.capital_contributed()
            except Exception:  # noqa: BLE001
                capital_base = START_CASH
            stats = _trade_stats(db.recent_trades(limit=1000))
            n_open = len(db.load_open_positions())
            provider_down = db.kv_get("provider_down") or None
            retired_ts = db.kv_get("retired_ts") or None
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
            # Retired desks are shown, not hidden: the leaderboard is also the
            # record of who played. retired_ts is the day the line stops.
            "retired": is_retired(name),
            "retired_ts": retired_ts,
            # Why a desk is idle, when it is: an account problem the owner must
            # clear, not something the desk can trade its way out of.
            "provider_down": provider_down,
            "equity": round(eq, 2),
            "cash": round(cash, 2),
            "capital_base": round(capital_base, 2),
            # Return is measured against capital GIVEN, not the original stake —
            # an owner deposit must never read as profit. Dollar P&L is immune.
            "pnl": round(eq - capital_base, 2),
            "return_pct": round((eq / capital_base - 1) * 100, 2),
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
            f"trades a paper account of US stocks/ETFs/futures — any horizon, by "
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
        # Last off-hours crypto LLM cycle (module clock, not persisted: a
        # restart grants at most one early cycle, it cannot loop).
        self._last_crypto_cycle = 0.0
        self._crypto_fast: bool | None = None   # trial state, for the one-time notice

    def _sync_teams(self):
        """Activate any team whose API key has appeared (e.g. entered via the
        settings page) since startup — no restart needed."""
        _settings.apply_to_env()
        have = {t.name for t in self.teams}
        for name, provider in default_team_providers().items():
            if name not in have and has_key(provider) and not is_retired(name):
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

    def _note_provider_result(self, t, res) -> bool:
        """Record a cycle's provider outcome. Returns True if the desk is usable.

        A terminal provider failure pauses the desk: continuing to call an API
        that has no credit cannot succeed, and doing it every cycle only buries
        the real message under a wall of identical errors. A cycle that succeeds
        clears the pause automatically, so topping the account up is all the
        owner has to do — no restart, no button.
        """
        err = getattr(res, "error", None)
        if not err:
            if t.provider_down:
                t.provider_down = None
                try:
                    t.db.kv_set("provider_down", "")
                    t.db.log_agent("runner", "provider_recovered",
                                   f"{t.name}: provider is answering again — desk resumed")
                except Exception:  # noqa: BLE001
                    pass
                _notify(f"✅ Team {t.name} provider recovered — desk resumed.")
            return True
        if getattr(res, "terminal", False):
            code = getattr(res, "error_code", "provider_error")
            if t.provider_down != code:      # announce the transition once, not every cycle
                t.provider_down = code
                try:
                    t.db.kv_set("provider_down", code)
                    # Start the retry clock at the FAILURE. Without this the
                    # probe timer reads as "never probed" and lets the very next
                    # cycle straight back through to the dead endpoint.
                    import time as _t
                    t.db.kv_set("provider_down_probe", str(_t.time()))
                    t.db.log_agent("runner", "provider_down", f"{t.name}: {err}")
                except Exception:  # noqa: BLE001
                    pass
                _notify(f"🚫 Team {t.name} paused — {err}", throttle_key=f"down_{t.name}")
            return False
        _notify(f"⚠️ Team {t.name} ({getattr(t.provider,'model','?')}) cycle error: {err}",
                throttle_key=f"err_{t.name}")
        return True

    def _provider_paused(self, t) -> bool:
        """True if this desk should be skipped entirely this cycle.

        Retried once every RETRY_PROVIDER_MINUTES so a top-up is picked up on
        its own, without a restart and without hammering a dead endpoint.
        """
        if not t.provider_down:
            return False
        import time as _t
        every = float(os.environ.get("RETRY_PROVIDER_MINUTES", "20")) * 60.0
        last = 0.0
        try:
            last = float(t.db.kv_get("provider_down_probe") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        now = _t.time()
        if now - last >= every:
            try:
                t.db.kv_set("provider_down_probe", str(now))
            except Exception:  # noqa: BLE001
                pass
            return False          # let this cycle through as a probe
        return True

    def plan_all(self):
        market = market_only()
        d = _today_et()
        for t in self.teams:
            if t.db.kv_get("planned_date") == d:
                continue  # already planned today (idempotent across restarts)
            if self._provider_paused(t):
                continue
            res = t.desk.plan_day(with_account(market, t.broker))
            self._record_usage(t, "strategist", res)
            if not self._note_provider_result(t, res):
                continue          # don't mark the day planned on a dead provider
            t.db.kv_set("planned_date", d)

    @staticmethod
    def _held_symbol_data(team, base_quotes: dict, base_atr: dict, base_adx: dict | None = None):
        """Extend the cycle's quote / ATR / ADX maps with entries for any HELD
        symbol that fell off today's scanned watchlist, so trailing stops keep
        ratcheting — and ADX-decay exits keep evaluating — on swing/long holds
        instead of silently freezing."""
        q, a = dict(base_quotes), dict(base_atr)
        adx = dict(base_adx or {})
        try:
            held = [p.get("symbol") for p in team.broker.positions()]
        except Exception:  # noqa: BLE001
            held = []
        missing = [s for s in held if s and (s not in q or s not in a or s not in adx)]
        if not missing:
            return q, a, adx
        from daytrader.data import loader as _loader
        from daytrader.core import indicators as _ind
        for sym in missing:
            try:
                if sym not in q:
                    from daytrader.data import quotes as _quotes
                    px = _quotes.get_quote(sym)
                    if px is not None:
                        q[sym] = px
                if sym not in a or sym not in adx:
                    df = _loader.load(sym, interval="5m", max_age_hours=0.1)
                    if df is not None and len(df) >= 15:
                        if sym not in a:
                            a[sym] = float(_ind.atr(df, 14).iloc[-1])
                        if sym not in adx:
                            adx[sym] = _adx_info(df)
            except Exception:  # noqa: BLE001
                continue
        return q, a, adx

    def trade_all(self):
        market = market_only()
        # Pin the snapshot's quote map onto each broker for this cycle so the
        # broker fills at the exact prices the agent reasoned over (no more
        # feed-vs-broker drift flipping winners into losers).
        cycle_quotes = dict(market.get("quotes") or {})
        base_atr = {sym: m.get("atr14") for sym, m in (market.get("market") or {}).items()
                    if m.get("atr14") is not None}
        base_adx = {sym: {"adx14": m.get("adx14"), "adx_slope": m.get("adx_slope")}
                    for sym, m in (market.get("market") or {}).items()
                    if m.get("adx14") is not None}
        _summary = market.get("market_summary") or {}
        spy_dir = _summary.get("spy_direction")
        cycle_breadth = _summary.get("breadth")
        cycle_sectors = _summary.get("sector_clusters")
        for t in self.teams:
            self._risk_check(t)  # may halt + flatten this team's DAY trades
            # Per-team maps that also cover held-outside-scan symbols.
            q, a, x = self._held_symbol_data(t, cycle_quotes, base_atr, base_adx)
            t.broker.set_cycle_quotes(q)
            t.broker.set_cycle_context(spy_direction=spy_dir,
                                       breadth=cycle_breadth, sectors=cycle_sectors)
            try:
                # Enforce server-side brackets EVERY cycle, even for halted teams
                # — their surviving swing/long holds still need their stops run.
                t.broker.manage_positions(q, a, x)
                if not t.halted and not self._provider_paused(t):
                    res = t.desk.trade_cycle(with_account(market, t.broker))
                    self._record_usage(t, "trader", res)
                    self._note_provider_result(t, res)
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
            if self._provider_paused(t):
                continue
            if market is None:
                market = market_only()
            res = t.desk.review_day(with_account(market, t.broker))
            self._record_usage(t, "reviewer", res)
            if not self._note_provider_result(t, res):
                continue          # not reviewed; retry once the provider is back
            t.db.kv_set("reviewed_date", d)
        # 3) Drain the research queue once the desks have proposed — pure
        #    compute, no tokens. Reports only survivors; silence is expected.
        self._run_research(d)

    def _run_research(self, date_iso: str) -> None:
        """Evaluate pending hypotheses once per day, after the reviewers run."""
        if not self.teams:
            return
        state = self.teams[0].db  # any team's kv works as the shared day-latch
        try:
            if state.kv_get("research_date") == date_iso:
                return
            # Only run once every desk has had its chance to propose today.
            if any(t.db.kv_get("reviewed_date") != date_iso for t in self.teams):
                return
            from daytrader.research.loop import run_pending
            out = run_pending(starting_equity=START_CASH)
            state.kv_set("research_date", date_iso)
            print(f"[research] evaluated {out['tested']} hypotheses; "
                  f"{len(out['accepted'])} survived | {out['summary']}")
        except Exception as e:  # noqa: BLE001 - research must never break the trading loop
            print(f"[research] error: {e!r}")

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

    # -- the crypto lane (24/7, throttled off-session) -------------------
    @staticmethod
    def _crypto_set() -> set:
        try:
            from daytrader.live.market_state import crypto_universe
            return set(crypto_universe())
        except Exception:  # noqa: BLE001
            return set()

    def crypto_all(self):
        """One throttled off-session decision cycle, crypto only.

        The lean sibling of trade_all: a three-symbol snapshot instead of the
        full watchlist scan, no plan/review phases, and bracket enforcement
        restricted to crypto (see manage_positions' only_symbols — equity
        stops must never fire on an off-session print).
        """
        from daytrader.live.market_state import crypto_only
        market = crypto_only()
        cmarket = (market.get("crypto") or {}).get("market") or {}
        if not cmarket:
            return   # crypto lane disabled or data unavailable — skip quietly
        cycle_quotes = dict(market.get("quotes") or {})
        atr = {sym: m.get("atr14") for sym, m in cmarket.items()
               if m.get("atr14") is not None}
        adx = {sym: {"adx14": m.get("adx14"), "adx_slope": m.get("adx_slope")}
               for sym, m in cmarket.items() if m.get("adx14") is not None}
        cset = self._crypto_set()
        for t in self.teams:
            self._risk_check(t)   # the 3% daily breaker guards weekends too
            t.broker.set_cycle_quotes(cycle_quotes)
            try:
                t.broker.manage_positions(cycle_quotes, atr, adx, only_symbols=cset)
                if not t.halted and not self._provider_paused(t):
                    # The LEAN agent, not the full session trader: off-hours
                    # cycles run around the clock, so 92% of a full cycle's
                    # tokens being unusable equity/options boilerplate is a
                    # recurring bill, not a one-off inefficiency.
                    res = t.desk.crypto_cycle(with_account(market, t.broker))
                    self._record_usage(t, "crypto_trader", res)
                    self._note_provider_result(t, res)
            finally:
                t.broker.set_cycle_quotes(None)
            t.broker.db.record_equity(t.broker.cash(), t.broker.equity(),
                                      len(t.broker.positions()), t.broker.drawdown_pct())

    def _crypto_stop_poll(self):
        """Between off-session cycles: enforce brackets on HELD crypto only.

        Quotes-only (no LLM, no bars), so running it every STOP_POLL_SEC around
        the clock costs nothing — and it is the mechanism that makes an
        unattended weekend crypto position survivable. Staged equity orders
        deliberately do NOT fire here; they wait for the session poll.
        """
        cset = self._crypto_set()
        if not cset:
            return
        held: set = set()
        for t in self.teams:
            try:
                held |= {p["symbol"] for p in t.broker.positions()
                         if p["symbol"] in cset}
            except Exception:  # noqa: BLE001
                pass
        if not held:
            return
        from daytrader.data import quotes as _quotes
        qmap = _quotes.get_quotes(list(held))
        if not qmap:
            return
        for t in self.teams:
            try:
                t.broker.set_cycle_quotes(qmap)
                t.broker.manage_positions(qmap, {}, {}, only_symbols=cset)
            except Exception:  # noqa: BLE001
                pass
            finally:
                t.broker.set_cycle_quotes(None)

    def _offhours_idle(self, total: float):
        """Idle for `total` seconds off-session — but not blindly: run the
        throttled crypto decision cycle when it is due, and poll crypto
        brackets every STOP_POLL_SEC throughout."""
        cadence = crypto_cadence_min()
        # Announce the trial ending exactly once, so the change in behaviour is
        # explainable later rather than a mystery drop in activity.
        fast = crypto_trial_active()
        if self._crypto_fast is None:
            self._crypto_fast = fast
        elif self._crypto_fast and not fast:
            self._crypto_fast = False
            msg = (f"off-hours crypto cadence reverted to {CRYPTO_CYCLE_MIN:.0f} min "
                   f"(the {CRYPTO_FAST_CYCLE_MIN:.0f}-min trial ended {CRYPTO_FAST_UNTIL})")
            print(f"[competition] {msg}")
            _notify(f"⏱️ {msg}")
            for t in self.teams:
                try:
                    t.db.add_journal("system", "dev_resolved",
                                     "PLATFORM CHANGE: the 15-minute off-hours crypto "
                                     "cadence trial has ended. Off-session decision "
                                     f"cycles are back to every {CRYPTO_CYCLE_MIN:.0f} "
                                     "minutes. Stops are still enforced every couple of "
                                     "minutes, but any crypto position you carry now "
                                     "goes much longer between reassessments — check "
                                     "that your open stops still reflect that.")
                except Exception:  # noqa: BLE001
                    pass
        if (cadence > 0 and self._crypto_set()
                and time.time() - self._last_crypto_cycle >= cadence * 60.0):
            self._last_crypto_cycle = time.time()
            try:
                self.crypto_all()
            except Exception as e:  # noqa: BLE001
                print(f"[competition] crypto cycle error: {e!r}")
        slept = 0.0
        while slept < total:
            chunk = min(STOP_POLL_SEC, total - slept)
            time.sleep(chunk)
            slept += chunk
            try:
                self._crypto_stop_poll()
            except Exception as e:  # noqa: BLE001
                print(f"[competition] crypto stop-poll error: {e!r}")

    # -- the always-on loop ---------------------------------------------
    def run_forever(self):
        print(f"[competition] starting; teams online: {[t.name for t in self.teams]} "
              f"(others activate when their API key is set)")
        _notify(f"🤖 Trading desk competition online — teams: {[t.name for t in self.teams] or 'none yet'}")
        while True:
            try:
                self._sync_teams()
                # The GitHub sync (stranded-request backfill + closed-issue
                # broadcasts) belongs to the ALWAYS-ON loop, not the trading
                # window. It first lived inside trade_all, which only runs
                # 09:45-15:30 ET — so a fix shipped in the evening sat unseen
                # until the next trading morning, and the backfill could not
                # run after the close at all. Internally throttled (~15 min).
                try:
                    from daytrader.live.dev_requests import sync_github_resolutions
                    sync_github_resolutions()
                except Exception:  # noqa: BLE001 - never let GitHub break trading
                    pass
                now = datetime.now(ET)
                if self._day != now.date():
                    self._new_day(now)
                # Weekends and market holidays: idle. (Per-team planned/reviewed
                # state is persisted, so restarts never double-run either phase.)
                if now.weekday() >= 5 or _is_market_holiday(now.date()):
                    self._offhours_idle(300); continue
                t = now.time()
                if t < OPEN:
                    self._offhours_idle(60); continue
                # EOD is DEADLINE-based: once past 15:50 ET, flatten day trades +
                # review — reachable even if a trade cycle overran 16:00. review_all
                # is idempotent, so this is cheap once the day is done.
                if t >= EOD_FLAT:
                    self.review_all()
                    self._offhours_idle(120); continue
                if t < PLAN_BY:
                    self.plan_all()
                elif t < NO_NEW_TRADES_AFTER:
                    self.trade_all()
                else:
                    # Between 15:30 and 15:50: hold — don't start a long cycle
                    # that would blow past the EOD deadline. Manage brackets only.
                    self._manage_only()
                # Sleep to the next cycle, but keep enforcing stops/targets every
                # STOP_POLL_SEC in between so a fast move can't blow through a stop.
                self._interruptible_sleep(INTERVAL_SEC)
            except Exception as e:  # noqa: BLE001 - never die
                print(f"[competition] loop error: {e!r}")
                time.sleep(60)

    @staticmethod
    def _fire_staged_for_team(team):
        """Check each pending staged order; once its fire_after time has passed,
        re-verify the entry conditions (distance from EMA9, min ADX) and submit
        it — or skip it if the setup no longer holds. Runs off the stop-poll so
        orders fire within ~2 min of their target time."""
        try:
            pending = team.db.list_staged_orders(status="pending")
        except Exception:  # noqa: BLE001
            return
        if not pending:
            return
        from daytrader.core import indicators as _ind
        from daytrader.data import loader as _loader, quotes as _quotes
        now_t = datetime.now(ET).time()
        for o in pending:
            fa = o.get("fire_after")
            if fa:
                try:
                    hh, mm = str(fa).split(":")[:2]
                    if now_t < dtime(int(hh), int(mm)):
                        continue  # not due yet
                except Exception:  # noqa: BLE001
                    pass
            sym = o["symbol"]
            try:
                df = _loader.load(sym, interval="5m", max_age_hours=0.1)
                price = _quotes.get_quote(sym) or float(df["close"].iloc[-1])
                ema9 = float(_ind.ema(df["close"], 9).iloc[-1])
                atr = float(_ind.atr(df, 14).iloc[-1])
                adxv = float(_ind.adx(df, 14).iloc[-1])
            except Exception as e:  # noqa: BLE001
                team.db.update_staged_order(o["id"], "skipped", f"data unavailable: {e!r}")
                continue
            reasons = []
            if o.get("max_ema9_dist_atr") is not None and atr > 0:
                dist = abs(price - ema9) / atr
                if dist > float(o["max_ema9_dist_atr"]):
                    reasons.append(f"price {dist:.2f}xATR from EMA9 (> {o['max_ema9_dist_atr']})")
            if o.get("min_adx") is not None and adxv < float(o["min_adx"]):
                reasons.append(f"ADX {adxv:.0f} < min {o['min_adx']}")
            # General feature conditions (same DSL grammar), evaluated on the last bar.
            if o.get("conditions"):
                try:
                    import json as _json
                    from daytrader.strategies.custom import check_conditions
                    conds = _json.loads(o["conditions"])
                    cond_text = " ".join(
                        str(c.get("left", "")) + " " + str(c.get("right", "")) for c in conds)
                    # SPY needs its own OHLCV frame (rs_* only needs its close) —
                    # fetched on demand so a staged order with no market gate
                    # doesn't pay for it on every ~2-min stop-poll.
                    spy_close = spy_df = None
                    if "rs_" in cond_text or "spy_" in cond_text:
                        try:
                            spy_df = _loader.load("SPY", interval="5m", max_age_hours=0.1)
                            spy_close = spy_df["close"]
                        except Exception:  # noqa: BLE001
                            spy_close = spy_df = None
                    # breadth_*/sector_* need the traded universe's bars (issue #47:
                    # these were already in the DSL vocabulary but always NaN here,
                    # so a rule that gated on them could never fire). Same on-demand
                    # gate — only load the universe when a condition actually asks.
                    market_ctx = None
                    if "breadth_" in cond_text or "sector_" in cond_text:
                        try:
                            from daytrader.live.market_state import _default_symbols
                            from daytrader.live.strategy_lab import _market_context
                            syms = _default_symbols()
                            if sym not in syms:
                                syms = syms + [sym]
                            udata = _loader.load_many(syms, interval="5m", max_age_hours=0.1)
                            uctx = _market_context(udata, "5m")
                            market_ctx = dict(uctx.get("_global") or {})
                            if isinstance(uctx.get(sym), dict):
                                market_ctx.update(uctx[sym])
                        except Exception:  # noqa: BLE001
                            market_ctx = None
                    ok, detail = check_conditions(
                        df, conds, spy_close=spy_close, spy_df=spy_df, market=market_ctx)
                    if not ok:
                        reasons.append(detail)
                except Exception as e:  # noqa: BLE001
                    reasons.append(f"condition-eval error: {e!r}")
            if reasons:
                team.db.update_staged_order(o["id"], "skipped", "; ".join(reasons))
                continue
            res = team.broker.open(
                symbol=sym, side=Side.LONG if o["side"] == "long" else Side.SHORT,
                qty=float(o["qty"]), stop=o.get("stop"), target=o.get("target"),
                strategy=o.get("strategy") or "staged", rationale=o.get("rationale", ""),
                horizon=o.get("horizon", "day"))
            team.db.update_staged_order(
                o["id"], "fired" if res.get("ok") else "skipped",
                "filled @ %.4f" % res["fill_price"] if res.get("ok") else str(res.get("reason")))

    def _stop_poll(self):
        """Lightweight between-cycle bracket enforcement: fetch quotes for
        currently-held symbols only and run manage_positions (no LLM, no full
        snapshot). Stops/targets/auto-scale fire promptly; ATR-trailing still
        ratchets on the next full cycle."""
        # Fire any due pre-staged orders first (so they hit near their target
        # time, ~2-min granularity, not the 15-min trade cycle).
        for t in self.teams:
            if not t.halted:
                self._fire_staged_for_team(t)
        held: set = set()
        decay_syms: set = set()
        for t in self.teams:
            try:
                for p in t.broker.positions():
                    held.add(p["symbol"])
                    if p.get("adx_decay_exit"):
                        decay_syms.add(p["symbol"])
            except Exception:  # noqa: BLE001
                pass
        if not held:
            return
        from daytrader.data import quotes as _quotes
        qmap = _quotes.get_quotes(list(held))
        if not qmap:
            return
        # ADX is only needed for positions that actually opted into a decay
        # exit, so the common case stays a quotes-only poll.
        amap: dict = {}
        if decay_syms:
            from daytrader.data import loader as _loader
            for sym in decay_syms:
                try:
                    df = _loader.load(sym, interval="5m", max_age_hours=0.1)
                    if df is not None and len(df) >= 15:
                        amap[sym] = _adx_info(df)
                except Exception:  # noqa: BLE001
                    continue
        for t in self.teams:
            try:
                t.broker.set_cycle_quotes(qmap)
                t.broker.manage_positions(qmap, {}, amap)
            except Exception:  # noqa: BLE001
                pass
            finally:
                t.broker.set_cycle_quotes(None)

    def _interruptible_sleep(self, total: float):
        """Sleep `total` seconds, running a stop-poll every STOP_POLL_SEC."""
        slept = 0.0
        while slept < total:
            chunk = min(STOP_POLL_SEC, total - slept)
            time.sleep(chunk)
            slept += chunk
            if slept < total:
                try:
                    self._stop_poll()
                except Exception as e:  # noqa: BLE001
                    print(f"[competition] stop-poll error: {e!r}")

    def _manage_only(self):
        """Run server-side bracket enforcement (trailing stops, stop/target
        auto-exec) without a fresh LLM decision cycle — used in the pre-close
        window when starting a full cycle would risk missing the EOD deadline."""
        market = market_only()
        cycle_quotes = dict(market.get("quotes") or {})
        base_atr = {sym: m.get("atr14") for sym, m in (market.get("market") or {}).items()
                    if m.get("atr14") is not None}
        base_adx = {sym: {"adx14": m.get("adx14"), "adx_slope": m.get("adx_slope")}
                    for sym, m in (market.get("market") or {}).items()
                    if m.get("adx14") is not None}
        _summary = market.get("market_summary") or {}
        spy_dir = _summary.get("spy_direction")
        cycle_breadth = _summary.get("breadth")
        cycle_sectors = _summary.get("sector_clusters")
        for t in self.teams:
            q, a, x = self._held_symbol_data(t, cycle_quotes, base_atr, base_adx)
            t.broker.set_cycle_quotes(q)
            t.broker.set_cycle_context(spy_direction=spy_dir,
                                       breadth=cycle_breadth, sectors=cycle_sectors)
            try:
                t.broker.manage_positions(q, a, x)
            finally:
                t.broker.set_cycle_quotes(None)
            t.broker.db.record_equity(t.broker.cash(), t.broker.equity(),
                                      len(t.broker.positions()), t.broker.drawdown_pct())
