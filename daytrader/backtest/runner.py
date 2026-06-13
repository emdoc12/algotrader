"""Backtest orchestration: load data, generate signals, simulate, score.

This is the glue the CLI and tuning loops call. It keeps strategy signal
generation separate from execution so the same signals can be replayed under
different cost models or risk settings.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from daytrader.backtest.engine import BacktestEngine, EngineConfig
from daytrader.backtest.metrics import Metrics, compute
from daytrader.core.types import Signal
from daytrader.data import loader
from daytrader.strategies.base import Strategy


@dataclass
class BacktestResult:
    metrics: Metrics
    trades: list
    equity: pd.Series
    signals: list
    data: dict
    interval: str


def benchmark_return(data: dict[str, pd.DataFrame], symbol: str = "SPY") -> float:
    """Buy-and-hold return of the benchmark over the backtest window (%)."""
    df = data.get(symbol)
    if df is None or len(df) < 2:
        return 0.0
    return (df["close"].iloc[-1] / df["close"].iloc[0] - 1.0) * 100.0


def generate_signals(strategies: list[Strategy], data: dict[str, pd.DataFrame]) -> list[Signal]:
    out: list[Signal] = []
    for strat in strategies:
        for sym, df in data.items():
            if len(df) == 0:
                continue
            try:
                out.extend(strat.generate(df))
            except Exception as e:  # noqa: BLE001
                print(f"[runner] {strat.name} failed on {sym}: {e}")
    out.sort(key=lambda s: (s.ts, s.symbol))
    return out


def run_backtest(
    strategies: list[Strategy],
    symbols: list[str] | None = None,
    interval: str = "5m",
    rng: str | None = None,
    config: EngineConfig | None = None,
    sizer=None,
    data: dict[str, pd.DataFrame] | None = None,
) -> BacktestResult:
    symbols = symbols or loader.DEFAULT_UNIVERSE
    if data is None:
        data = loader.load_many(symbols, interval=interval, rng=rng)
    config = config or EngineConfig()

    signals = generate_signals(strategies, data)
    engine = BacktestEngine(config=config, sizer=sizer)
    trades, equity = engine.run(data, signals)

    bench = benchmark_return(data, "SPY")
    metrics = compute(trades, equity, config.starting_equity, benchmark_return_pct=bench)
    return BacktestResult(metrics=metrics, trades=trades, equity=equity,
                          signals=signals, data=data, interval=interval)
