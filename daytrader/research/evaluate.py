"""Evaluator — pure compute, zero tokens.

Splits history into N contiguous, non-overlapping periods and scores the SAME
frozen rule on each. Nothing here fits, tunes, or selects: a hypothesis arrives
fully specified and this module only measures it.

Two details that quietly decide whether the numbers mean anything:

* **Warmup.** Indicators (ema50, macd 26+9, adx 14) need history. Slicing a
  period and running it cold would leave the first ~50 bars signal-less and
  silently bias every period's start. Each period is therefore run with a
  warmup PREFIX of prior bars, and trades are then filtered to those *entered*
  inside the period window.
* **Independence.** Every period restarts from the same starting equity. Running
  one continuous backtest would let period 1's P&L change period 5's position
  sizes, coupling results that the gate treats as independent trials.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

WARMUP_BARS = 60          # covers ema50 / macd(26,9) / adx(14)
_MIN_BARS_PER_PERIOD = 40


def _periods(days: list, n_periods: int) -> list[tuple]:
    """Split trading days into N contiguous, equal, non-overlapping windows."""
    if len(days) < n_periods * 2:
        return []
    size = len(days) // n_periods
    out = []
    for i in range(n_periods):
        start = i * size
        end = (i + 1) * size if i < n_periods - 1 else len(days)
        out.append((days[start], days[end - 1]))
    return out


def _binomial_p(k: int, n: int, p: float = 0.5) -> float:
    """One-sided P(X >= k) for X~Binomial(n, p). Null: a period is a coin flip."""
    if n <= 0:
        return 1.0
    k = max(0, min(k, n))
    return float(sum(math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
                     for i in range(k, n + 1)))


def _bootstrap_p(pnls, n_boot: int = 5000, seed: int = 11) -> float:
    """One-sided bootstrap p-value that mean trade P&L is > 0.

    Null is "this rule's edge is zero". Resamples the realized trade P&L with
    replacement and reports the share of resamples whose mean is <= 0.
    """
    a = np.asarray([float(x) for x in pnls], dtype=float)
    if a.size < 5 or not np.isfinite(a).all():
        return 1.0
    if a.std(ddof=1) == 0:
        return 0.0 if a.mean() > 0 else 1.0
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, a.size, size=(n_boot, a.size))
    means = a[idx].mean(axis=1)
    return float((means <= 0).mean())


def _run_slice(rule: dict, data: dict, spy_close, start_day, end_day,
               starting_equity: float) -> dict:
    """Backtest one period with warmup, counting only trades entered in-window."""
    from daytrader.backtest.engine import BacktestEngine, CostModel, EngineConfig
    from daytrader.core.types import SignalType
    from daytrader.portfolio.ensemble import Allocation, Ensemble, Regime
    from daytrader.strategies.custom import CustomRuleStrategy

    sliced, spy_slice = {}, None
    for sym, df in data.items():
        days = df.index.normalize()
        in_win = np.asarray((days >= start_day) & (days <= end_day))
        if not in_win.any():
            continue
        first = int(np.argmax(in_win))
        lo = max(0, first - WARMUP_BARS)          # warmup prefix for indicators
        hi = int(len(df) - np.argmax(in_win[::-1]))
        part = df.iloc[lo:hi]
        if len(part) >= _MIN_BARS_PER_PERIOD:
            sliced[sym] = part
    if not sliced:
        return {"trades": [], "net_pnl": 0.0, "n_trades": 0}
    if spy_close is not None and "SPY" in sliced:
        spy_slice = sliced["SPY"]["close"]

    strat = CustomRuleStrategy(dict(rule))
    if spy_slice is not None:
        strat._spy_close = spy_slice
    ens = Ensemble([Allocation(strategy=strat, regimes={Regime.ANY.value}, weight=1.0)],
                   market_filter=False)
    signals = ens.generate(sliced)
    # Drop entries that fall in the warmup prefix — they belong to no period.
    signals = [s for s in signals
               if not (getattr(s, "type", None) == SignalType.ENTRY
                       and s.ts.normalize() < start_day)]
    engine = BacktestEngine(EngineConfig(starting_equity=float(starting_equity),
                                         cost=CostModel()))
    trades, _equity = engine.run(sliced, signals)
    closed = [t for t in trades
              if not t.is_open and t.entry_ts is not None
              and start_day <= t.entry_ts.normalize() <= end_day]
    pnls = [float(t.net_pnl) for t in closed]
    return {"trades": pnls, "net_pnl": float(sum(pnls)), "n_trades": len(pnls)}


def evaluate(canon: dict, criteria: dict, starting_equity: float = 25_000.0) -> dict:
    """Score a canonical hypothesis across its pre-registered period count.

    Returns per-period results, the pooled trade record, and the two p-values
    the gate needs. Never raises — an evaluation that cannot run reports why.
    """
    from daytrader.data import loader
    from daytrader.research.hypothesis import INTERVAL_HISTORY_DAYS

    rule = canon["rule"]
    interval = canon["interval"]
    n_periods = int(criteria["n_periods"])
    symbols = list(canon["universe"])
    if "SPY" not in symbols:
        symbols = symbols + ["SPY"]

    rng_days = INTERVAL_HISTORY_DAYS.get(interval, 60)
    try:
        data = loader.load_many(symbols, interval=interval, rng=f"{rng_days}d",
                                max_age_hours=12)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"data load failed: {e!r}"}
    data = {s: df for s, df in data.items() if df is not None and len(df) > 0}
    if not data:
        return {"ok": False, "error": "no data loaded"}

    traded = [s for s in canon["universe"] if s in data]
    if not traded:
        return {"ok": False, "error": f"no data for universe {canon['universe']}"}

    all_days = sorted({d for df in data.values() for d in df.index.normalize().unique()})
    windows = _periods(all_days, n_periods)
    if not windows:
        return {"ok": False, "error": (
            f"insufficient history: {len(all_days)} trading days at {interval} cannot "
            f"support {n_periods} periods")}

    spy_close = data["SPY"]["close"] if "SPY" in data else None
    per_period, pooled = [], []
    for i, (a, b) in enumerate(windows, 1):
        r = _run_slice(rule, data, spy_close, a, b, starting_equity)
        pooled.extend(r["trades"])
        per_period.append({
            "period": i,
            "start": str(a.date()), "end": str(b.date()),
            "net_pnl": round(r["net_pnl"], 2),
            "n_trades": r["n_trades"],
            "profitable": bool(r["net_pnl"] > 0),
        })

    n_profitable = sum(1 for p in per_period if p["profitable"])
    p_periods = _binomial_p(n_profitable, len(per_period))
    p_trades = _bootstrap_p(pooled)
    # The BOOTSTRAP is the significance test. The period-consistency evidence is
    # reported (p_periods) but is NOT folded into p_value, because it is already
    # enforced as a hard pre-registered criterion (min_periods_profitable) and
    # because a binomial over n periods has a hard floor of 0.5**n — with 5
    # periods the best attainable value is 0.031, which sits above the corrected
    # bar from the second test onward. Combining them would have made the gate
    # not merely strict but mathematically unpassable, rejecting real edges for
    # an arithmetic reason rather than an evidential one.
    p_value = p_trades

    total = float(sum(pooled))
    wins = [x for x in pooled if x > 0]
    return {
        "ok": True,
        "interval": interval,
        "universe": traded,
        "periods": per_period,
        "n_periods": len(per_period),
        "n_periods_profitable": n_profitable,
        "n_trades": len(pooled),
        "total_net_pnl": round(total, 2),
        "avg_trade": round(total / len(pooled), 4) if pooled else 0.0,
        "win_rate": round(100.0 * len(wins) / len(pooled), 1) if pooled else 0.0,
        "p_periods": round(p_periods, 6),
        "p_trades": round(p_trades, 6),
        "p_value": round(p_value, 6),
    }
