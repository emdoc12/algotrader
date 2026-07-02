"""Self-serve strategy backtesting for the trading desks.

Exposes the project's validated backtest engine as an on-demand tool so a desk
can test a hypothesis ("does VWAP-trend in TREND regime with a 1.5x ATR stop
beat buy-and-hold over the last 30 days?") in seconds, instead of waiting one or
two live sessions to find out. It reuses the exact same engine, strategies,
regime gating, cost model, and metrics that validated the production book — so a
result here means the same thing it does in the offline backtests.

What it does NOT do (yet): run a brand-new strategy defined from arbitrary
entry/exit rules. v1 tests the 8 built-in setups with tunable parameters,
regime, time, ADX, and cost knobs — which covers the large majority of the
hypotheses the desks actually want to check. A custom-rule DSL is a future step.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from daytrader.backtest.engine import BacktestEngine, CostModel, EngineConfig
from daytrader.backtest import metrics as _metrics
from daytrader.data import loader
from daytrader.portfolio.ensemble import Allocation, Ensemble, Regime

# Friendly aliases -> (module, class, natural_regime). Natural regime is the one
# the production book runs the strategy in; used when the caller doesn't pin one.
_STRATEGIES: dict[str, tuple[str, str, str]] = {
    "orb":            ("daytrader.strategies.orb", "OpeningRangeBreakout", "any"),
    "vwap_reversion": ("daytrader.strategies.vwap_reversion", "VwapReversion", "range"),
    "vwap_trend":     ("daytrader.strategies.vwap_trend", "VwapTrend", "trend"),
    "rsi2":           ("daytrader.strategies.rsi2", "Rsi2Reversion", "range"),
    "bollinger":      ("daytrader.strategies.bollinger_fade", "BollingerFade", "range"),
    "ema_pullback":   ("daytrader.strategies.ema_pullback", "EmaPullback", "trend"),
    "macd":           ("daytrader.strategies.macd_trend", "MacdTrend", "trend"),
    "pivot":          ("daytrader.strategies.pivot_reversal", "PivotReversal", "range"),
    "gap_fade":       ("daytrader.strategies.gap_fade", "GapFade", "any"),
}

# Common alternate spellings the models might use.
_ALIASES = {
    "opening_range": "orb", "opening_range_breakout": "orb", "openingrangebreakout": "orb",
    "vwap": "vwap_trend", "vwaptrend": "vwap_trend", "vwapreversion": "vwap_reversion",
    "vwap_mean_reversion": "vwap_reversion",
    "rsi": "rsi2", "rsi2reversion": "rsi2",
    "bollinger_fade": "bollinger", "bollingerfade": "bollinger", "bollinger_band": "bollinger",
    "ema": "ema_pullback", "emapullback": "ema_pullback", "ema9": "ema_pullback",
    "macd_trend": "macd", "macdtrend": "macd",
    "pivot_reversal": "pivot", "pivotreversal": "pivot",
    "gap": "gap_fade", "gap_and_go": "gap_fade", "gapfade": "gap_fade", "gap_go": "gap_fade",
}

_PROFILES = {
    "trend": ["orb", "vwap_trend", "ema_pullback", "macd"],
    "momentum": ["orb", "vwap_trend", "ema_pullback", "macd", "gap_fade"],
    "all": list(_STRATEGIES.keys()),
}

_MAX_LOOKBACK = {"1m": 7, "2m": 55, "5m": 55, "15m": 55, "30m": 55, "1h": 700}


def available_strategies() -> list[str]:
    return list(_STRATEGIES.keys())


def _resolve_names(strategy) -> list[str]:
    """Turn a user value (name / alias / profile / list) into canonical keys."""
    if strategy is None:
        return list(_PROFILES["trend"])
    if isinstance(strategy, list):
        out = []
        for s in strategy:
            out.extend(_resolve_names(s))
        # de-dupe, preserve order
        seen, uniq = set(), []
        for s in out:
            if s not in seen:
                seen.add(s); uniq.append(s)
        return uniq
    key = str(strategy).strip().lower()
    if key in _PROFILES:
        return list(_PROFILES[key])
    key = _ALIASES.get(key, key)
    if key in _STRATEGIES:
        return [key]
    return []


def _coerce_params(strat_cls, params: dict):
    """Validate params against the constructor signature and coerce 'HH:MM'
    strings to datetime.time. Returns (clean_params, unknown_keys)."""
    import inspect
    from datetime import time as dtime
    try:
        valid = set(inspect.signature(strat_cls.__init__).parameters) - {"self"}
    except (TypeError, ValueError):
        valid = None
    clean, unknown = {}, []
    for k, v in (params or {}).items():
        if valid is not None and k not in valid:
            unknown.append(k)
            continue
        if isinstance(v, str) and ":" in v and v.replace(":", "").isdigit():
            try:
                hh, mm = v.split(":")[:2]
                v = dtime(int(hh), int(mm))
            except Exception:  # noqa: BLE001
                pass
        clean[k] = v
    return clean, unknown


def _instantiate(key: str, params: Optional[dict]):
    module, cls, natural = _STRATEGIES[key]
    import importlib
    mod = importlib.import_module(module)
    strat_cls = getattr(mod, cls)
    clean, unknown = _coerce_params(strat_cls, params or {})
    strat = strat_cls(**clean)
    return strat, natural, unknown


def run_backtest(
    strategy=None,
    symbols: Optional[list] = None,
    lookback_days: int = 30,
    interval: str = "5m",
    regimes: Optional[list] = None,
    adx_threshold: float = 25.0,
    market_filter: bool = True,
    starting_equity: float = 25_000.0,
    pessimistic_costs: bool = False,
    strategy_params: Optional[dict] = None,
    custom: Optional[object] = None,
) -> dict:
    """Backtest built-in strategies (by name/profile) OR a custom rule config.

    Pass ``custom`` (a config dict, or a list of them) to test an agent-authored
    strategy; otherwise ``strategy`` selects built-ins. Returns a compact dict:
    resolved config, summary metrics (win rate, profit factor, avg win/loss, max
    DD, expectancy, return, alpha vs SPY), a downsampled equity curve, and a
    sample of trades. Never raises.
    """
    custom_strats = []
    if custom is not None:
        from daytrader.strategies.custom import CustomRuleStrategy, StrategyConfigError
        configs = custom if isinstance(custom, list) else [custom]
        try:
            custom_strats = [CustomRuleStrategy(cfg) for cfg in configs]
        except StrategyConfigError as e:
            return {"error": f"invalid custom strategy: {e}"}
        except Exception as e:  # noqa: BLE001
            return {"error": f"could not build custom strategy: {e!r}"}
        names = [s.name for s in custom_strats]
    else:
        names = _resolve_names(strategy)
        if not names:
            return {"error": f"unknown strategy {strategy!r}",
                    "available": available_strategies() + list(_PROFILES.keys())}

    interval = "1h" if interval in ("60m", "1h") else interval
    cap = _MAX_LOOKBACK.get(interval, 55)
    lookback_days = max(1, min(int(lookback_days), cap))

    if not symbols:
        try:
            from daytrader.live.market_state import _default_symbols
            symbols = _default_symbols()
        except Exception:  # noqa: BLE001
            symbols = list(loader.DEFAULT_UNIVERSE)
    symbols = [str(s).upper() for s in symbols][:30]
    if "SPY" not in symbols:
        symbols = symbols + ["SPY"]

    rng = f"{lookback_days}d"
    try:
        data = loader.load_many(symbols, interval=interval, rng=rng, max_age_hours=12)
    except Exception as e:  # noqa: BLE001
        return {"error": f"data load failed: {e!r}"}
    if not data:
        return {"error": "no data loaded for the requested symbols/interval"}

    # Build the ensemble. Pin a regime if asked, else use each strategy's
    # natural production regime.
    pinned = None
    if regimes:
        pinned = {str(r).strip().lower() for r in regimes}
        valid = {Regime.TREND.value, Regime.RANGE.value, Regime.ANY.value}
        pinned = {r for r in pinned if r in valid} or None

    allocs = []
    unknown_params: list[str] = []
    if custom_strats:
        # Custom strategies fire in any regime unless the caller pins one.
        regset = pinned if pinned else {Regime.ANY.value}
        for strat in custom_strats:
            allocs.append(Allocation(strategy=strat, regimes=set(regset), weight=1.0))
    else:
        for key in names:
            strat, natural, unknown = _instantiate(key, strategy_params)
            unknown_params.extend(unknown)
            regset = pinned if pinned else {natural}
            allocs.append(Allocation(strategy=strat, regimes=regset, weight=1.0))
    # If the caller passed params that no strategy accepts, surface an error
    # instead of silently backtesting the DEFAULT config (a false-edge trap).
    if strategy_params and unknown_params and len(set(unknown_params)) == len(strategy_params):
        return {"error": f"unknown strategy_params: {sorted(set(unknown_params))}; "
                         "nothing was applied — check the parameter names",
                "hint": "params must match the strategy constructor's arguments"}

    try:
        ens = Ensemble(allocs, adx_threshold=adx_threshold, market_filter=market_filter)
        signals = ens.generate(data)
    except Exception as e:  # noqa: BLE001
        return {"error": f"signal generation failed: {e!r}"}

    cost = CostModel.pessimistic() if pessimistic_costs else CostModel()
    cfg = EngineConfig(starting_equity=float(starting_equity), cost=cost)
    try:
        engine = BacktestEngine(cfg)
        trades, equity = engine.run(data, signals)
    except Exception as e:  # noqa: BLE001
        return {"error": f"backtest run failed: {e!r}"}

    # SPY buy-and-hold benchmark over the same window.
    bench = 0.0
    spy = data.get("SPY")
    if spy is not None and len(spy) > 1:
        try:
            bench = (float(spy["close"].iloc[-1]) / float(spy["close"].iloc[0]) - 1) * 100
        except Exception:  # noqa: BLE001
            bench = 0.0

    m = _metrics.compute(trades, equity, float(starting_equity), benchmark_return_pct=bench)

    # Downsample the equity curve to keep the payload small.
    curve = []
    if len(equity):
        step = max(1, len(equity) // 40)
        for ts, val in list(equity.items())[::step]:
            curve.append({"ts": str(ts)[:16], "equity": round(float(val), 2)})

    closed = [t for t in trades if not t.is_open and t.exit_price is not None]
    sample = []
    for t in closed[-12:]:
        sample.append({
            "symbol": t.symbol,
            "side": getattr(t.side, "value", str(t.side)),
            "strategy": getattr(t, "strategy", ""),
            "entry": round(float(t.entry_price), 2) if t.entry_price is not None else None,
            "exit": round(float(t.exit_price), 2) if t.exit_price is not None else None,
            "pnl": round(float(t.net_pnl), 2),
            "reason": getattr(t, "exit_reason", ""),
            "hold_min": round(float(t.hold_minutes), 1),
        })

    return {
        "ok": True,
        "config": {
            "strategies": names,
            "regime": sorted(pinned) if pinned else "natural (per-strategy)",
            "symbols": [s for s in symbols if s in data],
            "lookback_days": lookback_days,
            "interval": interval,
            "adx_threshold": adx_threshold,
            "market_filter": market_filter,
            "costs": "pessimistic" if pessimistic_costs else "default",
            "starting_equity": float(starting_equity),
            "strategy_params": strategy_params or None,
            "ignored_params": sorted(set(unknown_params)) or None,
        },
        "metrics": {
            "n_trades": m.n_trades,
            "win_rate_pct": round(m.win_rate, 1),
            "profit_factor": round(m.profit_factor, 2) if m.profit_factor != float("inf") else "inf",
            "total_return_pct": round(m.total_return_pct, 2),
            "benchmark_spy_pct": round(bench, 2),
            "alpha_pts": round(m.alpha_pts, 2),
            "max_drawdown_pct": round(m.max_drawdown_pct, 2),
            "avg_win": round(m.avg_win, 2),
            "avg_loss": round(m.avg_loss, 2),
            "payoff_ratio": round(m.payoff_ratio, 2),
            "expectancy_per_trade": round(m.expectancy, 2),
            "sharpe": round(m.sharpe, 2),
            "avg_hold_min": round(m.avg_hold_min, 1),
        },
        "verdict": _verdict(m),
        "equity_curve": curve,
        "sample_trades": sample,
    }


def _verdict(m) -> str:
    """A one-line, honest read so the model doesn't over-interpret a tiny sample."""
    if m.n_trades == 0:
        return "No trades generated — strategy never triggered on this window/universe."
    if m.n_trades < 10:
        return (f"Only {m.n_trades} trades — too few to trust; treat as directional, "
                "not conclusive. Widen the window or universe.")
    pf = m.profit_factor
    if pf == float("inf") or pf >= 2.0:
        edge = "strong (PF>=2)"
    elif pf >= 1.3:
        edge = "modest"
    elif pf >= 1.0:
        edge = "marginal/breakeven"
    else:
        edge = "negative — loses money"
    pf_str = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (f"{m.n_trades} trades, PF {pf_str}, "
            f"{m.win_rate:.0f}% win, {m.alpha_pts:+.1f} pts vs SPY, "
            f"max DD {m.max_drawdown_pct:.1f}% — edge looks {edge}.")
