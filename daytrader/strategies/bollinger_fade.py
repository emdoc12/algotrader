"""Bollinger Band fade (mean reversion to the middle band).

Fade stretched closes back to the band midline:

  * Long when a bar closes below the lower band (price is statistically
    stretched to the downside); target the middle band (the rolling mean),
    protective stop an ATR below the entry.
  * Short the mirror image: close above the upper band, target the midline,
    stop an ATR above.
  * Trend filter: do NOT fade strong trends. We skip when ADX is elevated
    (directional move underway) or when the bands are expanding hard
    (band width well above its recent average => volatility breakout, not a
    fade). Fading those is how mean-reversion books blow up.
  * Overtrading guard: at most one long and one short fade per symbol per day.

The midline target is recomputed at entry and frozen onto the signal, so the
fill logic stays causal. Indicators are causal; engine fills at next bar open.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import adx, atr, bollinger
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class BollingerFade(Strategy):
    name = "BB-Fade"

    def __init__(
        self,
        bb_window: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
        stop_atr_mult: float = 1.5,
        adx_period: int = 14,
        max_adx: float = 35.0,
        width_window: int = 50,
        max_width_ratio: float = 1.8,
        no_entry_after: dtime = dtime(15, 0),
        allow_short: bool = True,
    ):
        super().__init__(
            bb_window=bb_window, bb_std=bb_std, atr_period=atr_period,
            stop_atr_mult=stop_atr_mult, adx_period=adx_period, max_adx=max_adx,
            width_window=width_window, max_width_ratio=max_width_ratio,
            no_entry_after=no_entry_after, allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < max(self.bb_window, self.width_window) + 5:
            return []
        symbol = df["symbol"].iloc[0]

        close = df["close"]
        mid, upper, lower, width = bollinger(close, self.bb_window, self.bb_std)
        a = atr(df, self.atr_period)
        adx_s = adx(df, self.adx_period)
        # Rolling average band width: width "expanding hard" => current width is
        # much larger than its recent norm (a volatility breakout, skip the fade).
        width_avg = width.rolling(self.width_window).mean()

        cv = close.values
        midv = mid.values
        upv = upper.values
        lowv = lower.values
        wv = width.values
        wavg = width_avg.values
        av = a.values
        adxv = adx_s.values
        idx = df.index
        day = df.index.normalize()

        signals: list[Signal] = []
        long_done: set = set()
        short_done: set = set()

        for i in range(len(df)):
            t = idx[i].time()
            if t >= self.no_entry_after:
                continue
            d = day[i]
            price = cv[i]
            m = midv[i]
            up = upv[i]
            lo = lowv[i]
            ai = av[i]

            if np.isnan(m) or np.isnan(ai) or ai <= 0:
                continue
            # Trend / expansion filters.
            if not np.isnan(adxv[i]) and adxv[i] > self.max_adx:
                continue
            if not np.isnan(wavg[i]) and wavg[i] > 0 and wv[i] > self.max_width_ratio * wavg[i]:
                continue

            # ---- LONG fade: close below lower band, target the midline.
            if (not np.isnan(lo) and d not in long_done
                    and price < lo and m > price):
                stop = price - self.stop_atr_mult * ai
                target = m  # revert to the mean
                if stop < price and target > price:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.LONG, type=SignalType.ENTRY,
                        strategy=self.name, stop=stop, target=target,
                        strength=min(1.0, (lo - price) / ai + 0.5),
                        reason=f"BB fade long: px {price:.2f} < lower {lo:.2f}",
                    ))
                    long_done.add(d)

            # ---- SHORT fade: close above upper band, target the midline.
            if (self.allow_short and not np.isnan(up) and d not in short_done
                    and price > up and m < price):
                stop = price + self.stop_atr_mult * ai
                target = m
                if stop > price and target < price:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.SHORT, type=SignalType.ENTRY,
                        strategy=self.name, stop=stop, target=target,
                        strength=min(1.0, (price - up) / ai + 0.5),
                        reason=f"BB fade short: px {price:.2f} > upper {up:.2f}",
                    ))
                    short_done.add(d)

        return signals
