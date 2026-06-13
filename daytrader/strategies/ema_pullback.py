"""EMA pullback (trend-following).

A continuation setup that buys shallow dips inside an established intraday
uptrend (and mirrors for shorts in a downtrend):

  * Define trend with two EMAs: fast > slow and BOTH rising => uptrend
    (mirror for downtrend). This filters out chop where the EMAs are flat or
    crossing. An ADX floor confirms a real directional regime, and a longer
    EMA slope keeps us aligned with the broader intraday drift.
  * Within an uptrend, wait for a shallow pullback: a recent bar dips to/through
    the fast EMA (the low pierces it) without breaking the slow EMA -- i.e. the
    trend structure stays intact.
  * Enter long only when momentum *resumes*: the current bar closes back above
    the fast EMA and prints a higher close than the prior bar (the pullback is
    over). Mirror for shorts.
  * Risk: ATR-based protective stop a multiple below entry; profit target at a
    fixed reward:risk multiple. Engine force-flattens at EOD.
  * Discipline: at most one entry per symbol/day/direction, gated away from the
    noisy first 30 minutes and the last hour.

All indicators (ema, adx, atr) are causal, and every decision on bar i uses only
data through bar i; the engine fills at the next bar's open.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import adx, atr, ema
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class EmaPullback(Strategy):
    name = "EMA-Pull"

    def __init__(
        self,
        fast: int = 9,
        slow: int = 21,
        trend_ema: int = 50,
        atr_period: int = 14,
        adx_period: int = 14,
        adx_min: float = 20.0,
        stop_atr_mult: float = 1.5,
        rr: float = 2.0,
        slope_lookback: int = 4,
        pullback_atr: float = 0.2,
        pullback_lookback: int = 3,
        min_atr_frac: float = 0.0006,
        max_entries_per_dir: int = 1,
        no_entry_before: dtime = dtime(10, 0),
        no_entry_after: dtime = dtime(15, 0),
        allow_short: bool = True,
    ):
        super().__init__(
            fast=fast, slow=slow, trend_ema=trend_ema, atr_period=atr_period,
            adx_period=adx_period, adx_min=adx_min,
            stop_atr_mult=stop_atr_mult, rr=rr, slope_lookback=slope_lookback,
            pullback_atr=pullback_atr, pullback_lookback=pullback_lookback,
            min_atr_frac=min_atr_frac, max_entries_per_dir=max_entries_per_dir,
            no_entry_before=no_entry_before, no_entry_after=no_entry_after,
            allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        n = len(df)
        warmup = max(self.slow, self.trend_ema) + self.slope_lookback + 5
        if n < warmup:
            return []
        symbol = df["symbol"].iloc[0]

        close = df["close"]
        ema_f = ema(close, self.fast)
        ema_s = ema(close, self.slow)
        ema_t = ema(close, self.trend_ema)
        a = atr(df, self.atr_period)
        adx_s = adx(df, self.adx_period)

        c = close.values
        hi = df["high"].values
        lo = df["low"].values
        ef = ema_f.values
        es = ema_s.values
        et = ema_t.values
        av = a.values
        adxv = adx_s.values
        idx = df.index
        day = df.index.normalize()
        lb = self.slope_lookback
        pb = self.pullback_lookback

        signals: list[Signal] = []
        long_count: dict = {}
        short_count: dict = {}

        for i in range(warmup, n):
            t = idx[i].time()
            if t < self.no_entry_before or t >= self.no_entry_after:
                continue
            if np.isnan(av[i]) or av[i] <= 0 or np.isnan(es[i - lb]) or np.isnan(et[i]):
                continue
            # Require enough intraday range to make a 2R target reachable.
            if av[i] / c[i] < self.min_atr_frac:
                continue
            # Only trade when a real directional regime exists.
            if adxv[i] < self.adx_min:
                continue
            d = day[i]

            # Trend definition: fast/slow stacked and both rising (or falling),
            # and price aligned with the longer trend EMA.
            up_trend = (
                ef[i] > es[i]
                and ef[i] > ef[i - lb]
                and es[i] > es[i - lb]
                and c[i] > et[i]
            )
            down_trend = (
                ef[i] < es[i]
                and ef[i] < ef[i - lb]
                and es[i] < es[i - lb]
                and c[i] < et[i]
            )

            # LONG: uptrend, a recent bar pulled back to/through the fast EMA but
            # held above the slow EMA, and this bar resumes (closes back above
            # the fast EMA with a higher close => momentum returning).
            if (
                up_trend
                and long_count.get(d, 0) < self.max_entries_per_dir
            ):
                touched = any(
                    lo[i - k] <= ef[i - k] + self.pullback_atr * av[i]
                    for k in range(1, pb + 1)
                )
                held = all(lo[i - k] > es[i - k] for k in range(1, pb + 1))
                resume = c[i] > ef[i] and c[i] > c[i - 1] and c[i - 1] <= ef[i - 1]
                if touched and held and resume:
                    stop = c[i] - self.stop_atr_mult * av[i]
                    risk = c[i] - stop
                    if risk > 0:
                        target = c[i] + self.rr * risk
                        signals.append(Signal(
                            ts=idx[i], symbol=symbol, side=Side.LONG,
                            type=SignalType.ENTRY, strategy=self.name,
                            stop=stop, target=target,
                            reason=f"EMA pullback long @ {c[i]:.2f} adx={adxv[i]:.0f}",
                        ))
                        long_count[d] = long_count.get(d, 0) + 1

            # SHORT mirror.
            if (
                self.allow_short
                and down_trend
                and short_count.get(d, 0) < self.max_entries_per_dir
            ):
                touched = any(
                    hi[i - k] >= ef[i - k] - self.pullback_atr * av[i]
                    for k in range(1, pb + 1)
                )
                held = all(hi[i - k] < es[i - k] for k in range(1, pb + 1))
                resume = c[i] < ef[i] and c[i] < c[i - 1] and c[i - 1] >= ef[i - 1]
                if touched and held and resume:
                    stop = c[i] + self.stop_atr_mult * av[i]
                    risk = stop - c[i]
                    if risk > 0:
                        target = c[i] - self.rr * risk
                        signals.append(Signal(
                            ts=idx[i], symbol=symbol, side=Side.SHORT,
                            type=SignalType.ENTRY, strategy=self.name,
                            stop=stop, target=target,
                            reason=f"EMA pullback short @ {c[i]:.2f} adx={adxv[i]:.0f}",
                        ))
                        short_count[d] = short_count.get(d, 0) + 1

        return signals
