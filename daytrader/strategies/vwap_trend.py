"""VWAP trend pullback (buy dips to a rising VWAP, sell rips to a falling VWAP).

In a trending intraday session, session VWAP acts as dynamic support/resistance:
price stays on one side of VWAP and repeatedly pulls back to it before resuming.
This strategy trades those continuation pullbacks in the trend direction:

  * Require an established intraday trend: price persistently above (below) a
    *rising* (falling) session VWAP, confirmed by ADX above a floor.
  * Wait for a pullback that touches/penetrates VWAP, then a resumption bar that
    closes back in the trend direction. Enter on that resumption.
  * Protective stop an ATR multiple beyond VWAP (the level we expect to hold);
    profit target a fixed reward-to-risk multiple. Flat by EOD.
  * At most one long / one short re-entry per symbol per day, gated away from
    the open and the last hour.

Causal: VWAP and its slope use only completed bars up to and including i; the
"rising VWAP" check compares vwap[i] to vwap a few bars earlier, never future.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import adx, atr, vwap_session
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class VwapTrend(Strategy):
    name = "VWAP-Trend"

    def __init__(
        self,
        rr: float = 2.0,
        atr_period: int = 14,
        stop_atr_mult: float = 1.0,
        adx_period: int = 14,
        adx_min: float = 22.0,
        slope_lookback: int = 6,
        touch_atr: float = 0.25,
        resume_atr: float = 0.05,
        min_atr_frac: float = 0.0005,
        no_entry_before: dtime = dtime(10, 0),
        no_entry_after: dtime = dtime(15, 0),
        max_per_dir: int = 1,
        allow_short: bool = True,
    ):
        super().__init__(
            rr=rr, atr_period=atr_period, stop_atr_mult=stop_atr_mult,
            adx_period=adx_period, adx_min=adx_min, slope_lookback=slope_lookback,
            touch_atr=touch_atr, resume_atr=resume_atr, min_atr_frac=min_atr_frac,
            no_entry_before=no_entry_before, no_entry_after=no_entry_after,
            max_per_dir=max_per_dir, allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.atr_period + self.slope_lookback + 5:
            return []
        symbol = df["symbol"].iloc[0]

        vw = vwap_session(df)
        a = atr(df, self.atr_period)
        adx_ = adx(df, self.adx_period)

        idx = df.index
        day = df.index.normalize()
        close = df["close"].values
        low = df["low"].values
        high = df["high"].values
        vwv = vw.values
        av = a.values
        adv = adx_.values

        lb = self.slope_lookback
        signals: list[Signal] = []
        long_count: dict = {}
        short_count: dict = {}

        for i in range(lb + 1, len(df)):
            t = idx[i].time()
            if t < self.no_entry_before or t >= self.no_entry_after:
                continue
            if np.isnan(vwv[i]) or np.isnan(vwv[i - lb]) or np.isnan(av[i]) or av[i] <= 0:
                continue
            if av[i] / close[i] < self.min_atr_frac:
                continue
            # Require a trending regime.
            if adv[i] < self.adx_min:
                continue

            d = day[i]
            touch = self.touch_atr * av[i]
            vwap_rising = vwv[i] > vwv[i - lb]
            vwap_falling = vwv[i] < vwv[i - lb]

            # LONG: rising VWAP, prior bar pulled back to within `touch` of VWAP
            # (or below it), current bar resumes up and closes back above VWAP.
            if (long_count.get(d, 0) < self.max_per_dir
                    and vwap_rising
                    and low[i - 1] <= vwv[i - 1] + touch
                    and close[i - 1] > vwv[i - 1] - touch  # was hugging VWAP, not collapsed through it
                    and close[i] > vwv[i]
                    and close[i] > close[i - 1]):
                stop = vwv[i] - self.stop_atr_mult * av[i]
                risk = close[i] - stop
                if risk > 0:
                    target = close[i] + self.rr * risk
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.LONG,
                        type=SignalType.ENTRY, strategy=self.name,
                        stop=stop, target=target,
                        reason=f"VWAP-Trend long: pullback to rising VWAP {vwv[i]:.2f}",
                    ))
                    long_count[d] = long_count.get(d, 0) + 1

            # SHORT: falling VWAP, prior bar rallied up to VWAP, current bar
            # resumes down and closes back below VWAP.
            if (self.allow_short
                    and short_count.get(d, 0) < self.max_per_dir
                    and vwap_falling
                    and high[i - 1] >= vwv[i - 1] - touch
                    and close[i - 1] < vwv[i - 1] + touch
                    and close[i] < vwv[i]
                    and close[i] < close[i - 1]):
                stop = vwv[i] + self.stop_atr_mult * av[i]
                risk = stop - close[i]
                if risk > 0:
                    target = close[i] - self.rr * risk
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.SHORT,
                        type=SignalType.ENTRY, strategy=self.name,
                        stop=stop, target=target,
                        reason=f"VWAP-Trend short: pullback to falling VWAP {vwv[i]:.2f}",
                    ))
                    short_count[d] = short_count.get(d, 0) + 1

        return signals
