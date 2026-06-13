"""Strategy interface.

A strategy is a pure function of past-and-present bars to a list of Signals.
It must be *causal*: a signal stamped at bar t may only use information available
at or before the close of bar t. The engine enforces execution at t+1's open,
so strategies never need to think about fills, sizing, or cash.

Subclass `Strategy`, set a `name`, and implement `generate(df) -> list[Signal]`.
Use the helpers in `daytrader.core.indicators`; they are causal by construction.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from daytrader.core.types import Signal


class Strategy(ABC):
    name: str = "base"

    def __init__(self, **params):
        self.params = params
        for k, v in params.items():
            setattr(self, k, v)

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> list[Signal]:
        """Return ENTRY/EXIT signals for a single symbol's OHLCV frame.

        df is RTH-filtered, ascending, with columns open/high/low/close/volume
        and a 'symbol' column. Implementations should be vectorized where
        possible and must not peek at future bars.
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        p = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.__class__.__name__}({p})"
