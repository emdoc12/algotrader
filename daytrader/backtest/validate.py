"""Out-of-sample validation and robustness checks.

A backtest that only reports in-sample numbers is marketing, not evidence.
This module provides:

  * split_by_date    — chronological in-sample / out-of-sample split.
  * walk_forward     — score the same config on IS and OOS separately.
  * monte_carlo_dd   — bootstrap the drawdown distribution by reshuffling the
                       realized trade sequence, so "max DD < 10%" is judged
                       against a distribution, not one lucky ordering.
  * strategy_correlation — daily-PnL correlation across strategies, to confirm
                       the book is actually diversified.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from daytrader.backtest.engine import BacktestEngine, EngineConfig
from daytrader.backtest.metrics import Metrics, compute, max_drawdown
from daytrader.core.types import Trade


def split_by_date(data: dict[str, pd.DataFrame], oos_fraction: float = 0.35):
    """Split each symbol's frame at a shared date boundary.

    The boundary is chosen from the union of all trading days so IS and OOS
    cover the same calendar window across symbols.
    """
    all_days = sorted({ts.normalize() for df in data.values() for ts in df.index})
    if not all_days:
        return data, {}
    cut_idx = int(len(all_days) * (1 - oos_fraction))
    cut_day = all_days[cut_idx]
    is_data, oos_data = {}, {}
    for s, df in data.items():
        is_data[s] = df[df.index.normalize() < cut_day]
        oos_data[s] = df[df.index.normalize() >= cut_day]
    return is_data, oos_data


def _score(signals_fn, data, config, sizer) -> tuple[Metrics, list[Trade], pd.Series]:
    from daytrader.backtest.runner import benchmark_return
    signals = signals_fn(data)
    engine = BacktestEngine(config=config, sizer=sizer)
    trades, equity = engine.run(data, signals)
    bench = benchmark_return(data, "SPY")
    m = compute(trades, equity, config.starting_equity, benchmark_return_pct=bench)
    return m, trades, equity


def walk_forward(ensemble, data, config: EngineConfig | None = None,
                 sizer=None, oos_fraction: float = 0.35) -> dict:
    """Score IS and OOS with the SAME ensemble/config. No re-fitting on OOS."""
    config = config or EngineConfig()
    is_data, oos_data = split_by_date(data, oos_fraction)
    is_m, _, _ = _score(ensemble.generate, is_data, config, sizer)
    oos_m, oos_trades, oos_eq = _score(ensemble.generate, oos_data, config, sizer)
    return {
        "in_sample": is_m,
        "out_of_sample": oos_m,
        "oos_trades": oos_trades,
        "oos_equity": oos_eq,
    }


def monte_carlo_dd(trades: list[Trade], starting_equity: float,
                   n: int = 2000, seed: int = 7) -> dict:
    """Bootstrap max-drawdown by reshuffling realized trade P&L order.

    Returns percentiles of max drawdown (%) over n random orderings. If the
    95th-percentile DD is under target, the equity smoothness is robust, not
    a fluke of sequence.
    """
    pnls = np.array([t.net_pnl for t in trades if not t.is_open], dtype=float)
    if len(pnls) < 5:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "worst": 0.0, "n": len(pnls)}
    rng = np.random.default_rng(seed)
    dds = np.empty(n)
    for i in range(n):
        order = rng.permutation(len(pnls))
        eq = starting_equity + np.cumsum(pnls[order])
        eq = np.concatenate([[starting_equity], eq])
        running_max = np.maximum.accumulate(eq)
        dd = (eq - running_max) / running_max
        dds[i] = -dd.min() * 100.0
    return {
        "p50": float(np.percentile(dds, 50)),
        "p95": float(np.percentile(dds, 95)),
        "p99": float(np.percentile(dds, 99)),
        "worst": float(dds.max()),
        "n": len(pnls),
    }


def strategy_correlation(trades: list[Trade]) -> pd.DataFrame:
    """Daily-PnL correlation matrix across strategies (diversification check)."""
    rows = []
    for t in trades:
        if t.is_open or t.exit_ts is None:
            continue
        rows.append((t.exit_ts.normalize(), t.strategy, t.net_pnl))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["day", "strategy", "pnl"])
    pivot = df.pivot_table(index="day", columns="strategy", values="pnl",
                           aggfunc="sum").fillna(0.0)
    return pivot.corr()
