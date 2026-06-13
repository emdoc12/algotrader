"""Strategy ensemble with market-regime gating.

Running every strategy all the time is how you blow up a profit factor:
mean-reversion bleeds in trends, momentum chops out in ranges. The ensemble
classifies each bar's regime (trending vs range-bound, by ADX) and only lets a
strategy's signals through when the regime suits it. Each strategy also carries
a weight that scales the signal `strength`, which the risk manager turns into
position size.

This is where the system goes from "a pile of strategies" to "a book".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from daytrader.core.indicators import adx, ema
from daytrader.core.types import Side, Signal
from daytrader.strategies.base import Strategy


class Regime(str, Enum):
    TREND = "trend"
    RANGE = "range"
    ANY = "any"


def classify_regime(df: pd.DataFrame, adx_period: int = 14, adx_threshold: float = 25.0) -> pd.Series:
    """Per-bar regime label. ADX above threshold => TREND, else RANGE. Causal."""
    a = adx(df, adx_period)
    return pd.Series(
        [Regime.TREND.value if v >= adx_threshold else Regime.RANGE.value for v in a],
        index=df.index,
    )


@dataclass
class Allocation:
    strategy: Strategy
    regimes: set = field(default_factory=lambda: {Regime.ANY.value})
    weight: float = 1.0
    enabled: bool = True


class Ensemble:
    def __init__(self, allocations: list[Allocation], adx_threshold: float = 25.0,
                 market_filter: bool = False, market_symbol: str = "SPY",
                 market_ema: int = 50):
        self.allocations = [a for a in allocations if a.enabled]
        self.adx_threshold = adx_threshold
        self.market_filter = market_filter
        self.market_symbol = market_symbol
        self.market_ema = market_ema

    def _market_trend(self, data: dict[str, pd.DataFrame]) -> pd.Series | None:
        """+1 when the market is above its EMA (uptrend), -1 below. Causal."""
        df = data.get(self.market_symbol)
        if df is None or len(df) < self.market_ema:
            return None
        up = df["close"] > ema(df["close"], self.market_ema)
        return up.map({True: 1, False: -1})

    def generate(self, data: dict[str, pd.DataFrame]) -> list[Signal]:
        signals: list[Signal] = []
        regime_cache: dict[str, pd.Series] = {}
        market = self._market_trend(data) if self.market_filter else None

        for alloc in self.allocations:
            allow_any = Regime.ANY.value in alloc.regimes
            for sym, df in data.items():
                if len(df) == 0:
                    continue
                try:
                    sigs = alloc.strategy.generate(df)
                except Exception as e:  # noqa: BLE001
                    print(f"[ensemble] {alloc.strategy.name} failed on {sym}: {e}")
                    continue

                if not allow_any:
                    if sym not in regime_cache:
                        regime_cache[sym] = classify_regime(df, adx_threshold=self.adx_threshold)
                    reg = regime_cache[sym]

                for s in sigs:
                    if not allow_any:
                        bar_regime = reg.get(s.ts)
                        if bar_regime is None or bar_regime not in alloc.regimes:
                            continue
                    if market is not None:
                        # Only trade with the prevailing market direction.
                        try:
                            trend = market.asof(s.ts)
                        except Exception:  # noqa: BLE001
                            trend = None
                        if trend == 1 and s.side == Side.SHORT:
                            continue
                        if trend == -1 and s.side == Side.LONG:
                            continue
                    s.strength = max(0.0, min(1.0, s.strength * alloc.weight))
                    signals.append(s)

        signals.sort(key=lambda s: (s.ts, s.symbol))
        return signals

    @property
    def strategies(self) -> list[Strategy]:
        return [a.strategy for a in self.allocations]
