"""Market-state snapshot for the agent team.

Each decision cycle, the agents need a compact, current view of the world:
where each name is trading, what the indicators and regime say, which of the
backtested strategies are firing right now, the account's live P&L, and the
team's own memory (journal) and outstanding dev requests. This module assembles
that into a plain dict the LLM tools can serialize.

It reuses the same causal indicators and strategies the backtester validated —
so the agents reason over the exact signals that were tested, not a parallel
re-implementation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from daytrader.core import indicators as ind
from daytrader.data import loader
from daytrader.portfolio.book import _SPEC, _load
from daytrader.portfolio.ensemble import Ensemble, Regime, classify_regime


def _latest_indicators(df: pd.DataFrame) -> dict:
    if len(df) < 30:
        return {}
    close = df["close"]
    ema9 = ind.ema(close, 9).iloc[-1]
    ema21 = ind.ema(close, 21).iloc[-1]
    rsi = ind.rsi(close, 14).iloc[-1]
    atr = ind.atr(df, 14).iloc[-1]
    adx = ind.adx(df, 14).iloc[-1]
    vwap = ind.vwap_session(df).iloc[-1]
    price = float(close.iloc[-1])
    day_open = float(df["open"][df.index.normalize() == df.index[-1].normalize()].iloc[0])
    return {
        "price": round(price, 2),
        "day_change_pct": round((price / day_open - 1) * 100, 2) if day_open else 0.0,
        "ema9": round(float(ema9), 2),
        "ema21": round(float(ema21), 2),
        "ema_trend": "up" if ema9 > ema21 else "down",
        "rsi14": round(float(rsi), 1),
        "atr14": round(float(atr), 2),
        "atr_pct": round(float(atr) / price * 100, 2) if price else 0.0,
        "adx14": round(float(adx), 1),
        "vwap": round(float(vwap), 2),
        "vs_vwap_pct": round((price / float(vwap) - 1) * 100, 2) if vwap else 0.0,
        "regime": Regime.TREND.value if adx >= 25 else Regime.RANGE.value,
    }


def _fresh_signals(data: dict[str, pd.DataFrame], lookback_bars: int = 2) -> list[dict]:
    """Run every strategy and return signals stamped on the last few bars.

    Mirrors the production ensemble (regime gating + SPY market filter) so the
    agents see exactly what the automated book would act on right now.
    """
    allocs = _load([(m, c, r, w) for m, c, r, w in _SPEC])
    ens = Ensemble(allocs, market_filter=True)
    all_sigs = ens.generate(data)
    if not all_sigs:
        return []
    # Keep only signals whose decision bar is among the most recent bars.
    cutoffs = {}
    for sym, df in data.items():
        if len(df) > lookback_bars:
            cutoffs[sym] = df.index[-lookback_bars]
    fresh = []
    for s in all_sigs:
        cut = cutoffs.get(s.symbol)
        if cut is not None and s.ts >= cut:
            fresh.append({
                "symbol": s.symbol,
                "side": s.side.value,
                "strategy": s.strategy,
                "stop": round(s.stop, 2) if s.stop else None,
                "target": round(s.target, 2) if s.target else None,
                "ts": s.ts.isoformat(),
                "reason": s.reason,
            })
    return fresh


def _default_symbols(top_n: int = 18) -> list[str]:
    """The day's watchlist from the scanner; falls back to the core universe."""
    try:
        from daytrader.data.universe import watchlist
        return watchlist(top_n=top_n)
    except Exception:  # noqa: BLE001 - universe module optional / scan hiccup
        return loader.DEFAULT_UNIVERSE


def market_only(symbols: list[str] | None = None, interval: str = "5m") -> dict:
    """The shared market view: prices, indicators, regime, fresh signals.

    Account/memory state is NOT included so this can be computed ONCE per cycle
    and reused across all competing teams (one data fetch, not N).
    """
    symbols = symbols or _default_symbols()
    data = loader.load_many(symbols, interval=interval, max_age_hours=0.1)
    per_symbol = {sym: _latest_indicators(df) for sym, df in data.items()}
    fresh = _fresh_signals(data)
    now_et = datetime.now(timezone.utc).astimezone()
    return {
        "timestamp": now_et.isoformat(),
        "universe": symbols,
        "interval": interval,
        "market": per_symbol,
        "fresh_signals": fresh,
    }


def with_account(market_snap: dict, broker) -> dict:
    """Overlay one team's account state + memory onto a shared market snapshot."""
    out = dict(market_snap)
    if broker is None:
        return out
    try:
        out["account"] = broker.snapshot()
        out["performance"] = broker.performance()
    except Exception as e:  # noqa: BLE001
        out["account_error"] = str(e)
    db = getattr(broker, "db", None)
    if db is not None:
        try:
            out["journal"] = db.recent_journal(limit=20)
        except Exception:  # noqa: BLE001
            pass
        try:
            out["open_dev_requests"] = db.open_dev_requests()
        except Exception:  # noqa: BLE001
            pass
    return out


def snapshot(broker=None, symbols: list[str] | None = None, interval: str = "5m") -> dict:
    """Full market + account snapshot for a single team (convenience wrapper)."""
    return with_account(market_only(symbols, interval), broker)
