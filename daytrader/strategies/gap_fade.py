"""Gap Fade (with optional Gap-and-Go) — trade the opening gap.

Each morning the first RTH bar opens some distance from the PRIOR day's close.
That gap, measured in ATR, drives two opposite plays:

  * MODERATE gap (gap_min_atr .. gap_max_atr): statistically prone to fill.
    Fade it back toward the prior close.
      - Gap UP  -> SHORT, target = prior close.
      - Gap DOWN -> LONG, target = prior close.
    We wait for the first few bars and require the early action to confirm a
    pullback toward the close (i.e. momentum is fading, not extending).

  * LARGE gap (>= go_min_atr) WITH momentum: gaps this big rarely fill same
    day; instead they often run. Go WITH the gap (gap-and-go).
      - Gap UP   + opening bars pushing higher  -> LONG.
      - Gap DOWN + opening bars pushing lower    -> SHORT.
    Target is an RR multiple of the ATR stop (no fill target).

Stops are an ATR multiple beyond entry. One trade per symbol per day; decisions
are made on the close of an early bar (within the first `confirm_bars`) and the
engine fills at the next bar's open.

CAUSALITY: the only prior-day input is yesterday's completed close, obtained by
aggregating `groupby(normalize()).last()` and `.shift(1)`. Today's open is the
FIRST bar's open and the confirmation uses only bars at/after the open up to the
current bar i — never a later bar, never today's full-day high/low/close.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import atr
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class GapFade(Strategy):
    name = "Gap"

    def __init__(
        self,
        atr_period: int = 14,
        gap_min_atr: float = 0.5,    # below this: no tradeable gap (fade)
        gap_max_atr: float = 3.0,    # above this: too big to fade -> go regime
        go_min_atr: float = 3.0,     # gap >= this is a gap-and-go candidate
        confirm_bars: int = 3,       # decide within the first N bars after open
        stop_atr_mult: float = 1.0,
        go_rr: float = 1.5,          # reward:risk target for gap-and-go
        no_entry_after: dtime = dtime(11, 0),  # gap edge lives in the morning
        allow_short: bool = True,
        allow_go: bool = True,       # enable the gap-and-go side
    ):
        super().__init__(
            atr_period=atr_period, gap_min_atr=gap_min_atr, gap_max_atr=gap_max_atr,
            go_min_atr=go_min_atr, confirm_bars=confirm_bars, stop_atr_mult=stop_atr_mult,
            go_rr=go_rr, no_entry_after=no_entry_after, allow_short=allow_short,
            allow_go=allow_go,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.atr_period + 5:
            return []
        symbol = df["symbol"].iloc[0]

        # --- prior-day close (causal) ----------------------------------
        day = df.index.normalize()
        daily_close = df.groupby(day)["close"].last()
        pc = daily_close.shift(1)        # each day -> prior day's close
        pcv = day.map(pc).to_numpy()

        # Today's open = the first bar's open for each day; mapped to all bars.
        day_open = df.groupby(day)["open"].first()
        tov = day.map(day_open).to_numpy()

        # Bar ordinal within the day (0 = the 09:30 open bar).
        bar_in_day = df.groupby(day).cumcount().to_numpy()

        a = atr(df, self.atr_period).to_numpy()

        idx = df.index
        o = df["open"].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()
        days = day.to_numpy()

        signals: list[Signal] = []
        done: set = set()

        for i in range(len(df)):
            d = days[i]
            if d in done:
                continue
            t = idx[i].time()
            if t >= self.no_entry_after:
                continue
            # Only decide within the first `confirm_bars` bars (need >=1 to
            # confirm, so skip the raw open bar itself).
            k = bar_in_day[i]
            if k < 1 or k >= self.confirm_bars:
                continue

            av = a[i]
            if np.isnan(av) or av <= 0 or np.isnan(pcv[i]):
                continue

            today_open = tov[i]
            gap = today_open - pcv[i]
            gap_atr = abs(gap) / av
            if gap_atr < self.gap_min_atr:
                continue

            cl = close[i]

            # ---- GAP-AND-GO: very large gap, price extending --------
            if self.allow_go and gap_atr >= self.go_min_atr:
                if gap > 0 and cl > today_open:   # gap up, pushing higher
                    stop = low[i] - self.stop_atr_mult * av
                    risk = cl - stop
                    if risk > 0:
                        signals.append(Signal(
                            ts=idx[i], symbol=symbol, side=Side.LONG,
                            type=SignalType.ENTRY, strategy=self.name,
                            stop=stop, target=cl + self.go_rr * risk,
                            reason=f"gap-and-go up {gap_atr:.1f}ATR",
                        ))
                        done.add(d)
                elif gap < 0 and cl < today_open and self.allow_short:
                    stop = high[i] + self.stop_atr_mult * av
                    risk = stop - cl
                    if risk > 0:
                        signals.append(Signal(
                            ts=idx[i], symbol=symbol, side=Side.SHORT,
                            type=SignalType.ENTRY, strategy=self.name,
                            stop=stop, target=cl - self.go_rr * risk,
                            reason=f"gap-and-go down {gap_atr:.1f}ATR",
                        ))
                        done.add(d)
                continue

            # ---- GAP-FADE: moderate gap, fade back to prior close ---
            if gap_atr > self.gap_max_atr:
                continue
            target = pcv[i]
            if gap > 0 and self.allow_short:
                # gap up: fade short only if the early bars are rolling over
                if cl < today_open and cl > target:
                    stop = high[i] + self.stop_atr_mult * av
                    risk = stop - cl
                    if risk > 0 and cl - target > 0:
                        signals.append(Signal(
                            ts=idx[i], symbol=symbol, side=Side.SHORT,
                            type=SignalType.ENTRY, strategy=self.name,
                            stop=stop, target=target,
                            reason=f"gap-fill short {gap_atr:.1f}ATR -> {target:.2f}",
                        ))
                        done.add(d)
            elif gap < 0:
                # gap down: fade long only if early bars are bouncing
                if cl > today_open and cl < target:
                    stop = low[i] - self.stop_atr_mult * av
                    risk = cl - stop
                    if risk > 0 and target - cl > 0:
                        signals.append(Signal(
                            ts=idx[i], symbol=symbol, side=Side.LONG,
                            type=SignalType.ENTRY, strategy=self.name,
                            stop=stop, target=target,
                            reason=f"gap-fill long {gap_atr:.1f}ATR -> {target:.2f}",
                        ))
                        done.add(d)

        return signals
