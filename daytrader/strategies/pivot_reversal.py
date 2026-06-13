"""Pivot Reversal — classic floor-trader pivots, fade-to-center.

Floor-trader (a.k.a. "classic") pivots are computed from the PRIOR day's
completed high/low/close:

    P  = (H + L + C) / 3
    R1 = 2P - L          S1 = 2P - H
    R2 = P + (H - L)     S2 = P - (H - L)

These are intraday support/resistance magnets. The mean-reversion play is to
*fade* tests of the outer levels back toward the central pivot P:

  * Price tags a support level (S1/S2), prints a rejection (a lower wick / bar
    that dips to the level but closes back above it) -> go LONG, target P.
  * Price tags a resistance level (R1/R2), prints a rejection (an upper wick /
    bar that pokes the level but closes back below it) -> go SHORT, target P.

Stops sit an ATR fraction beyond the tagged level (where the thesis is wrong).
At most one long and one short per symbol per day; no new entries late in the
session; the engine force-flattens at the close.

CAUSALITY: today's pivots are built only from yesterday's completed OHLC. We
aggregate daily H/L/C by `df.index.normalize()`, `.shift(1)` so each calendar
day maps to the PRIOR completed day's values, then broadcast back onto every
intraday bar. A bar at index i only ever consults its own OHLC plus these
prior-day-derived constants — never today's full-day extremes or a future bar.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import atr
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class PivotReversal(Strategy):
    name = "Pivot"

    def __init__(
        self,
        atr_period: int = 14,
        tag_atr: float = 0.12,        # how close to a level counts as a "tag"
        stop_atr_mult: float = 0.6,   # stop sits this many ATR beyond the level
        min_target_atr: float = 0.25,  # skip if P is too close (no edge)
        no_entry_after: dtime = dtime(15, 0),
        no_entry_before: dtime = dtime(9, 45),  # let the open settle
        allow_short: bool = True,
        use_r2s2: bool = True,        # also fade the outer R2/S2 levels
    ):
        super().__init__(
            atr_period=atr_period, tag_atr=tag_atr, stop_atr_mult=stop_atr_mult,
            min_target_atr=min_target_atr, no_entry_after=no_entry_after,
            no_entry_before=no_entry_before, allow_short=allow_short,
            use_r2s2=use_r2s2,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.atr_period + 5:
            return []
        symbol = df["symbol"].iloc[0]

        # --- prior-day pivots (causal) ---------------------------------
        day = df.index.normalize()
        daily_high = df["high"].groupby(day).max()
        daily_low = df["low"].groupby(day).min()
        daily_close = df.groupby(day)["close"].last()
        # shift(1): each calendar day sees only the PRIOR completed day.
        ph = daily_high.shift(1)
        pl = daily_low.shift(1)
        pc = daily_close.shift(1)

        P = (ph + pl + pc) / 3.0
        rng = ph - pl
        R1 = 2 * P - pl
        S1 = 2 * P - ph
        R2 = P + rng
        S2 = P - rng

        # Broadcast per-day pivot constants onto every intraday bar.
        Pv = day.map(P).to_numpy()
        R1v = day.map(R1).to_numpy()
        R2v = day.map(R2).to_numpy()
        S1v = day.map(S1).to_numpy()
        S2v = day.map(S2).to_numpy()

        a = atr(df, self.atr_period).to_numpy()

        idx = df.index
        o = df["open"].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()
        days = day.to_numpy()

        signals: list[Signal] = []
        long_done: set = set()
        short_done: set = set()

        for i in range(len(df)):
            d = days[i]
            t = idx[i].time()
            if t < self.no_entry_before or t >= self.no_entry_after:
                continue
            av = a[i]
            if np.isnan(av) or av <= 0 or np.isnan(Pv[i]):
                continue

            tag = self.tag_atr * av

            # ---- LONG: fade a support tag back up to P --------------
            if d not in long_done:
                supports = [("S1", S1v[i])]
                if self.use_r2s2:
                    supports.append(("S2", S2v[i]))
                for lbl, lvl in supports:
                    if np.isnan(lvl):
                        continue
                    # Rejection: bar dipped to/below the level but closed back above it.
                    tagged = low[i] <= lvl + tag and low[i] <= close[i]
                    rejected = close[i] > lvl and close[i] > o[i]
                    target = Pv[i]
                    if tagged and rejected and target - close[i] >= self.min_target_atr * av:
                        stop = min(low[i], lvl) - self.stop_atr_mult * av
                        risk = close[i] - stop
                        if risk > 0:
                            signals.append(Signal(
                                ts=idx[i], symbol=symbol, side=Side.LONG,
                                type=SignalType.ENTRY, strategy=self.name,
                                stop=stop, target=target,
                                reason=f"fade {lbl} {lvl:.2f} -> P {target:.2f}",
                            ))
                            long_done.add(d)
                            break

            # ---- SHORT: fade a resistance tag back down to P --------
            if self.allow_short and d not in short_done:
                resists = [("R1", R1v[i])]
                if self.use_r2s2:
                    resists.append(("R2", R2v[i]))
                for lbl, lvl in resists:
                    if np.isnan(lvl):
                        continue
                    tagged = high[i] >= lvl - tag and high[i] >= close[i]
                    rejected = close[i] < lvl and close[i] < o[i]
                    target = Pv[i]
                    if tagged and rejected and close[i] - target >= self.min_target_atr * av:
                        stop = max(high[i], lvl) + self.stop_atr_mult * av
                        risk = stop - close[i]
                        if risk > 0:
                            signals.append(Signal(
                                ts=idx[i], symbol=symbol, side=Side.SHORT,
                                type=SignalType.ENTRY, strategy=self.name,
                                stop=stop, target=target,
                                reason=f"fade {lbl} {lvl:.2f} -> P {target:.2f}",
                            ))
                            short_done.add(d)
                            break

        return signals
