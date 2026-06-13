"""Opening Range Breakout (ORB).

A classic, well-documented intraday momentum setup (popularized by Toby
Crabel and, more recently, the Carver/Quantik studies on the 5-minute ORB):

  * Define the opening range as the high/low of the first N minutes.
  * Go long when price breaks above the OR high; go short below the OR low.
  * Only take the FIRST breakout of the day in each direction.
  * Stop at the opposite side of the OR (or an ATR multiple, whichever tighter);
    target a fixed R multiple. Flat by end of day (engine enforces this).
  * Filters: require the breakout bar to close beyond the level, require the
    range to be a sane fraction of ATR (skip dead/erratic days), and trade
    only after the range forms and before the last 30 minutes.

This is the reference strategy used to validate the whole pipeline.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import atr, opening_range
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class OpeningRangeBreakout(Strategy):
    name = "ORB"

    def __init__(
        self,
        or_minutes: int = 30,
        rr: float = 2.0,
        atr_period: int = 14,
        min_range_atr: float = 0.3,
        max_range_atr: float = 3.0,
        stop_atr_mult: float = 1.0,
        no_entry_after: dtime = dtime(15, 0),
        allow_short: bool = True,
    ):
        super().__init__(
            or_minutes=or_minutes, rr=rr, atr_period=atr_period,
            min_range_atr=min_range_atr, max_range_atr=max_range_atr,
            stop_atr_mult=stop_atr_mult, no_entry_after=no_entry_after,
            allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.atr_period + 5:
            return []
        symbol = df["symbol"].iloc[0]
        or_high, or_low = opening_range(df, self.or_minutes)
        a = atr(df, self.atr_period)

        # Entries are only allowed once the opening range has fully formed.
        from datetime import datetime, timedelta
        or_end = (datetime(2000, 1, 1, 9, 30) + timedelta(minutes=self.or_minutes)).time()

        signals: list[Signal] = []
        day = df.index.normalize()
        long_done: set = set()
        short_done: set = set()

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        idx = df.index

        orh = or_high.values
        orl = or_low.values
        av = a.values

        for i in range(len(df)):
            d = day[i]
            t = idx[i].time()
            # Only consider bars after the opening range has fully formed and
            # before the no-new-entries cutoff.
            if t <= or_end or t >= self.no_entry_after:
                continue
            if np.isnan(orh[i]) or np.isnan(orl[i]) or np.isnan(av[i]) or av[i] <= 0:
                continue
            rng = orh[i] - orl[i]
            range_in_atr = rng / av[i]
            if range_in_atr < self.min_range_atr or range_in_atr > self.max_range_atr:
                continue

            # LONG breakout: close above OR high, first of the day.
            if d not in long_done and close[i] > orh[i] and high[i] > orh[i]:
                stop_atr = close[i] - self.stop_atr_mult * av[i]
                stop = max(orl[i], stop_atr)
                risk = close[i] - stop
                if risk > 0:
                    target = close[i] + self.rr * risk
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.LONG, type=SignalType.ENTRY,
                        strategy=self.name, stop=stop, target=target,
                        reason=f"ORB long break {orh[i]:.2f}",
                    ))
                    long_done.add(d)

            # SHORT breakout.
            if self.allow_short and d not in short_done and close[i] < orl[i] and low[i] < orl[i]:
                stop_atr = close[i] + self.stop_atr_mult * av[i]
                stop = min(orh[i], stop_atr)
                risk = stop - close[i]
                if risk > 0:
                    target = close[i] - self.rr * risk
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.SHORT, type=SignalType.ENTRY,
                        strategy=self.name, stop=stop, target=target,
                        reason=f"ORB short break {orl[i]:.2f}",
                    ))
                    short_done.add(d)

        return signals
