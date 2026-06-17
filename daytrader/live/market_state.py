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
from daytrader.data import loader, quotes
from daytrader.portfolio.book import _SPEC, _load
from daytrader.portfolio.ensemble import Ensemble, Regime, classify_regime


def _latest_indicators(df: pd.DataFrame, live_price: float | None = None) -> dict:
    """Per-symbol indicator snapshot.

    ``price`` is the live quote (same one the broker fills at) when supplied,
    falling back to the last bar's close. ``bar_close`` always carries the
    underlying bar close for transparency.
    """
    if len(df) < 30:
        return {}
    close = df["close"]
    ema9 = ind.ema(close, 9).iloc[-1]
    ema21 = ind.ema(close, 21).iloc[-1]
    rsi = ind.rsi(close, 14).iloc[-1]
    atr = ind.atr(df, 14).iloc[-1]
    adx = ind.adx(df, 14).iloc[-1]
    vwap = ind.vwap_session(df).iloc[-1]
    bar_close = float(close.iloc[-1])
    price = float(live_price) if live_price is not None else bar_close
    day_open = float(df["open"][df.index.normalize() == df.index[-1].normalize()].iloc[0])
    return {
        "price": round(price, 2),
        "bar_close": round(bar_close, 2),
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


def _add_relative_strength(
    per_symbol: dict, data: dict, lookback_bars: int = 6, benchmark: str = "SPY"
) -> None:
    """Annotate each symbol's indicator block with relative strength vs SPY.

    RS = (symbol % change over the lookback) − (SPY % change over the same span).
    Computed from bars already loaded this cycle (no extra fetches). With 5m
    bars, the default 6 bars ≈ a 30-minute window. Adds ``rs_vs_spy_pct`` and a
    ``rs_rank`` (1 = strongest) to every symbol that has enough data.
    """
    def _pct(sym: str):
        df = data.get(sym)
        if df is None or len(df) <= lookback_bars:
            return None
        try:
            past = float(df["close"].iloc[-(lookback_bars + 1)])
            now = float(df["close"].iloc[-1])
            return ((now / past) - 1) * 100 if past else None
        except Exception:  # noqa: BLE001
            return None

    spy_pct = _pct(benchmark)
    if spy_pct is None:
        return
    scored = []
    for sym, inds in per_symbol.items():
        if not inds:
            continue
        p = _pct(sym)
        if p is None:
            continue
        rs = round(p - spy_pct, 2)
        inds["rs_vs_spy_pct"] = rs
        scored.append((sym, rs))
    scored.sort(key=lambda x: x[1], reverse=True)
    for rank, (sym, _rs) in enumerate(scored, 1):
        per_symbol[sym]["rs_rank"] = rank


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
    and reused across all competing teams (one data fetch, not N). The price
    inside each market[sym] entry is the live quote from
    :mod:`daytrader.data.quotes` — the same number the broker uses for fills,
    so there is no feed-vs-broker gap within a cycle.
    """
    symbols = symbols or _default_symbols()
    data = loader.load_many(symbols, interval=interval, max_age_hours=0.1)
    # Ensure SPY bars are available as the relative-strength benchmark even if
    # it isn't on the day's watchlist.
    if "SPY" not in data:
        try:
            data["SPY"] = loader.load("SPY", interval=interval, max_age_hours=0.1)
        except Exception:  # noqa: BLE001
            pass
    quote_map = quotes.get_quotes(symbols)
    per_symbol = {sym: _latest_indicators(df, live_price=quote_map.get(sym))
                  for sym, df in data.items() if sym in symbols}
    _add_relative_strength(per_symbol, data)
    fresh = _fresh_signals(data)
    now_et = datetime.now(timezone.utc).astimezone()
    out = {
        "timestamp": now_et.isoformat(),
        "universe": symbols,
        "interval": interval,
        "market": per_symbol,
        "fresh_signals": fresh,
        "quotes": quote_map,
    }
    # Optional enrichment: if the owner has configured tastytrade, overlay live
    # READ-ONLY quotes + option chains/Greeks. Degrades to Yahoo-only otherwise.
    try:
        from daytrader.live import tastytrade_data
        if tastytrade_data.is_configured():
            out = tastytrade_data.enrich_snapshot(out)
    except Exception:  # noqa: BLE001 - enrichment is best-effort, never fatal
        pass
    return out


def with_account(market_snap: dict, broker) -> dict:
    """Overlay one team's account state + memory onto a shared market snapshot.

    Also fetches indicators + a live quote for any HELD position whose symbol
    is not on the day's scanned universe, so the trader never has to manage a
    position blind.
    """
    out = dict(market_snap)
    if broker is None:
        return out
    try:
        out["account"] = broker.snapshot()
        out["performance"] = broker.performance()
    except Exception as e:  # noqa: BLE001
        out["account_error"] = str(e)

    # Held positions outside the day's scan need live indicators too, or the
    # trader is flying blind on what it already owns.
    try:
        positions = (out.get("account") or {}).get("positions") or []
        market = dict(out.get("market") or {})
        held_extra = sorted({(p.get("symbol") or "").upper()
                             for p in positions
                             if p.get("symbol") and p["symbol"].upper() not in market})
        if held_extra:
            interval = market_snap.get("interval", "5m")
            extra_data = loader.load_many(list(held_extra), interval=interval, max_age_hours=0.1)
            extra_quotes = quotes.get_quotes(held_extra)
            for sym, df in extra_data.items():
                inds = _latest_indicators(df, live_price=extra_quotes.get(sym))
                if inds:
                    market[sym] = inds
            out["market"] = market
            extra_quote_map = dict(out.get("quotes") or {})
            extra_quote_map.update(extra_quotes)
            out["quotes"] = extra_quote_map
            out["held_positions_added"] = list(held_extra)
    except Exception as e:  # noqa: BLE001 - never break the snapshot
        out["held_indicator_error"] = str(e)

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
