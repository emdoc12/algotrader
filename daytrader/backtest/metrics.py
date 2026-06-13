"""Performance metrics computed from a trade list and an equity curve.

The headline numbers mirror the example dashboard: return, vs-SPY alpha,
profit factor, win rate, max drawdown, plus risk-adjusted ratios. All metrics
are computed honestly from net (post-cost) P&L.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Sequence

import numpy as np
import pandas as pd

from daytrader.core.types import Trade


@dataclass
class Metrics:
    n_trades: int
    win_rate: float
    profit_factor: float
    total_return_pct: float
    final_equity: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    expectancy: float          # avg $ per trade
    avg_win: float
    avg_loss: float
    payoff_ratio: float        # avg win / avg loss
    gross_profit: float
    gross_loss: float
    total_commission: float
    total_slippage: float
    avg_hold_min: float
    cagr: float
    benchmark_return_pct: float = 0.0
    alpha_pts: float = 0.0     # strategy return - benchmark return (percentage pts)

    def as_dict(self) -> dict:
        return asdict(self)

    def passes_targets(self, min_pf: float = 2.0, max_dd: float = 10.0) -> bool:
        return self.profit_factor >= min_pf and self.max_drawdown_pct <= max_dd


def max_drawdown(equity: pd.Series) -> float:
    """Return max drawdown as a positive percentage of peak equity."""
    if len(equity) == 0:
        return 0.0
    running_max = equity.cummax()
    dd = (equity - running_max) / running_max
    return float(-dd.min() * 100.0)


def compute(
    trades: Sequence[Trade],
    equity: pd.Series,
    starting_equity: float,
    benchmark_return_pct: float = 0.0,
) -> Metrics:
    closed = [t for t in trades if not t.is_open and t.exit_price is not None]
    pnls = np.array([t.net_pnl for t in closed], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0

    final_equity = float(equity.iloc[-1]) if len(equity) else starting_equity
    total_return = (final_equity / starting_equity - 1.0) * 100.0

    # Risk ratios on DAILY returns (robust; per-bar curves have long flat
    # stretches that make intra-bar return stats meaningless).
    if isinstance(equity.index, pd.DatetimeIndex) and len(equity) > 2:
        daily_eq = equity.resample("1D").last().dropna()
        rets = daily_eq.pct_change().dropna()
    else:
        rets = equity.pct_change().dropna()
    ann = 252.0
    if len(rets) > 1 and rets.std(ddof=0) > 0:
        sharpe = float(rets.mean() / rets.std(ddof=0) * np.sqrt(ann))
        downside = rets[rets < 0]
        dstd = downside.std(ddof=0)
        sortino = float(rets.mean() / dstd * np.sqrt(ann)) if dstd > 0 else 0.0
    else:
        sharpe = sortino = 0.0

    # CAGR from elapsed wall-clock of the curve
    if isinstance(equity.index, pd.DatetimeIndex) and len(equity) > 1:
        years = (equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 86400)
        cagr = ((final_equity / starting_equity) ** (1 / years) - 1.0) * 100.0 if years > 0 else 0.0
    else:
        cagr = 0.0

    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(-losses.mean()) if len(losses) else 0.0
    payoff = avg_win / avg_loss if avg_loss > 0 else 0.0

    return Metrics(
        n_trades=len(closed),
        win_rate=win_rate,
        profit_factor=profit_factor,
        total_return_pct=total_return,
        final_equity=final_equity,
        max_drawdown_pct=max_drawdown(equity),
        sharpe=sharpe,
        sortino=sortino,
        expectancy=float(pnls.mean()) if len(pnls) else 0.0,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_commission=float(sum(t.commission for t in closed)),
        total_slippage=float(sum(t.slippage_cost for t in closed)),
        avg_hold_min=float(np.mean([t.hold_minutes for t in closed])) if closed else 0.0,
        cagr=cagr,
        benchmark_return_pct=benchmark_return_pct,
        alpha_pts=total_return - benchmark_return_pct,
    )


def format_report(m: Metrics, title: str = "Backtest") -> str:
    pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    lines = [
        f"=== {title} ===",
        f"  Trades            {m.n_trades}",
        f"  Win rate          {m.win_rate:.1f}%",
        f"  Profit factor     {pf}",
        f"  Total return      {m.total_return_pct:+.2f}%   (SPY {m.benchmark_return_pct:+.2f}%, alpha {m.alpha_pts:+.2f} pts)",
        f"  Final equity      ${m.final_equity:,.2f}",
        f"  Max drawdown      {m.max_drawdown_pct:.2f}%",
        f"  Sharpe / Sortino  {m.sharpe:.2f} / {m.sortino:.2f}",
        f"  CAGR              {m.cagr:+.2f}%",
        f"  Expectancy/trade  ${m.expectancy:+.2f}",
        f"  Avg win / loss    ${m.avg_win:,.2f} / ${m.avg_loss:,.2f}  (payoff {m.payoff_ratio:.2f})",
        f"  Costs             ${m.total_commission:,.2f} commission + ${m.total_slippage:,.2f} slippage",
        f"  Avg hold          {m.avg_hold_min:.0f} min",
        f"  TARGETS           PF>=2.0 {'PASS' if m.profit_factor>=2.0 else 'FAIL'} | "
        f"MaxDD<10% {'PASS' if m.max_drawdown_pct<10.0 else 'FAIL'} | "
        f"Beat SPY {'PASS' if m.alpha_pts>0 else 'FAIL'}",
    ]
    return "\n".join(lines)
