"""VWAP trend pullback (buy dips to a rising VWAP, sell rips to a falling VWAP).

In a trending intraday session, session VWAP acts as dynamic support/resistance:
price stays on one side of VWAP and repeatedly pulls back to it before resuming.
This strategy trades those continuation pullbacks in the trend direction:

  * Require an established intraday trend. Three things must agree:
      - ADX above a floor (a directional move is genuinely underway),
      - session VWAP is sloping in the trade direction by at least
        `slope_atr_min` ATRs over the trailing window (a real drift, not noise),
      - price is on the trend side of a trend EMA (close > EMA for longs).
  * Wait for a pullback that touches/penetrates VWAP, then a *strong resumption
    bar*: price closes back across VWAP in the trend direction, prints a higher
    (lower) close than the prior bar, and closes in the upper (lower) portion of
    its own range (`strong_close`) -- i.e. buyers/sellers clearly stepped back in.
  * Protective stop an ATR multiple beyond VWAP (the level we expect to hold);
    profit target a modest reward-to-risk multiple (continuation pops are taken
    quickly -- a tight, high-fill-rate target beats a distant one that the EOD
    flatten never lets reach). Flat by EOD (engine force-flattens).
  * At most one long / one short entry per symbol per day, gated away from the
    open and the close.

Causal: VWAP, its slope, ATR, ADX and the EMA use only completed bars up to and
including i; the slope check compares vwap[i] to vwap `slope_lookback` bars
earlier, never future. The engine fills at the next bar's open.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import adx, atr, ema, vwap_session
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class VwapTrend(Strategy):
    name = "VWAP-Trend"

    def __init__(
        self,
        rr: float = 0.8,
        atr_period: int = 14,
        stop_atr_mult: float = 1.0,
        adx_period: int = 14,
        adx_min: float = 28.0,
        slope_lookback: int = 6,
        slope_atr_min: float = 0.15,
        trend_ema: int = 20,
        touch_atr: float = 0.25,
        strong_close: float = 0.55,
        min_atr_frac: float = 0.0005,
        no_entry_before: dtime = dtime(10, 0),
        no_entry_after: dtime = dtime(15, 0),
        max_per_dir: int = 1,
        allow_short: bool = True,
    ):
        super().__init__(
            rr=rr, atr_period=atr_period, stop_atr_mult=stop_atr_mult,
            adx_period=adx_period, adx_min=adx_min, slope_lookback=slope_lookback,
            slope_atr_min=slope_atr_min, trend_ema=trend_ema, touch_atr=touch_atr,
            strong_close=strong_close, min_atr_frac=min_atr_frac,
            no_entry_before=no_entry_before, no_entry_after=no_entry_after,
            max_per_dir=max_per_dir, allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.trend_ema + self.slope_lookback + 5:
            return []
        symbol = df["symbol"].iloc[0]

        vw = vwap_session(df)
        a = atr(df, self.atr_period)
        adx_ = adx(df, self.adx_period)
        em = ema(df["close"], self.trend_ema)

        idx = df.index
        day = df.index.normalize()
        close = df["close"].values
        low = df["low"].values
        high = df["high"].values
        vwv = vw.values
        av = a.values
        adv = adx_.values
        emv = em.values

        lb = self.slope_lookback
        signals: list[Signal] = []
        long_count: dict = {}
        short_count: dict = {}

        for i in range(lb + 1, len(df)):
            t = idx[i].time()
            if t < self.no_entry_before or t >= self.no_entry_after:
                continue
            if (np.isnan(vwv[i]) or np.isnan(vwv[i - lb]) or np.isnan(av[i])
                    or av[i] <= 0 or np.isnan(emv[i])):
                continue
            if av[i] / close[i] < self.min_atr_frac:
                continue
            # Require a trending regime.
            if adv[i] < self.adx_min:
                continue

            d = day[i]
            touch = self.touch_atr * av[i]
            # VWAP drift over the lookback, normalized by ATR (a unitless slope).
            slope = (vwv[i] - vwv[i - lb]) / av[i]
            rng = high[i] - low[i]
            if rng <= 0:
                continue
            close_pos = (close[i] - low[i]) / rng  # 0=closed on low, 1=on high

            up_trend = slope >= self.slope_atr_min and close[i] > emv[i]
            down_trend = slope <= -self.slope_atr_min and close[i] < emv[i]

            # LONG: rising VWAP + price above trend EMA; prior bar pulled back to
            # within `touch` of VWAP (or below); this bar resumes up -- closes
            # back above VWAP, higher than the prior close, strong into the high.
            if (long_count.get(d, 0) < self.max_per_dir
                    and up_trend
                    and low[i - 1] <= vwv[i - 1] + touch
                    and close[i] > vwv[i]
                    and close[i] > close[i - 1]
                    and close_pos >= self.strong_close):
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

            # SHORT: falling VWAP + price below trend EMA; prior bar rallied up to
            # VWAP, this bar resumes down -- closes back below VWAP, lower than the
            # prior close, weak into the low.
            if (self.allow_short
                    and short_count.get(d, 0) < self.max_per_dir
                    and down_trend
                    and high[i - 1] >= vwv[i - 1] - touch
                    and close[i] < vwv[i]
                    and close[i] < close[i - 1]
                    and (1.0 - close_pos) >= self.strong_close):
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
