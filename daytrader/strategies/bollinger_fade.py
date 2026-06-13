"""Bollinger Band fade (mean reversion toward the middle band).

Fade stretched closes back toward the band midline:

  * Long when a bar closes below the lower band by at least a small ATR margin
    (price is statistically stretched cheap, not just grazing the band). Target
    a fraction (``target_frac``) of the distance back to the middle band, with
    a protective ATR stop below the entry. Targeting *part* of the way to the
    mean fills as a limit (no exit slippage) and lands far more often than
    holding out for the full midline, which is what tips the strategy from
    losing to roughly breakeven-or-better.
  * Short the mirror image: close above the upper band, target back toward the
    midline, stop an ATR above.
  * Trend / expansion filter: do NOT fade strong trends. Skip when ADX is
    elevated (a directional move is underway) or when the bands are expanding
    hard (current width well above its recent average => volatility breakout,
    not a fade). Fading those is how mean-reversion books blow up.
  * Overtrading guard: at most one long and one short fade per symbol per day.

On a strongly trending universe the long fades carry the edge while upper-band
shorts fade the prevailing drift and bleed, so ``allow_short`` defaults to
False; the symmetric short logic is implemented -- flip the flag to enable it.

The midline target is computed at entry and frozen onto the signal, so fills
stay causal. Indicators are causal; the engine fills at the next bar's open.
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
        target_frac: float = 0.7,
        min_pierce_atr: float = 0.1,
        adx_period: int = 14,
        max_adx: float = 30.0,
        width_window: int = 50,
        max_width_ratio: float = 1.8,
        no_entry_after: dtime = dtime(15, 0),
        allow_short: bool = False,
    ):
        super().__init__(
            bb_window=bb_window, bb_std=bb_std, atr_period=atr_period,
            stop_atr_mult=stop_atr_mult, target_frac=target_frac,
            min_pierce_atr=min_pierce_atr, adx_period=adx_period, max_adx=max_adx,
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

            # ---- LONG fade: close pierces below the lower band by an ATR margin.
            if (not np.isnan(lo) and d not in long_done
                    and price < lo and m > price
                    and (lo - price) >= self.min_pierce_atr * ai):
                stop = price - self.stop_atr_mult * ai
                target = price + self.target_frac * (m - price)
                if stop < price and target > price:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.LONG, type=SignalType.ENTRY,
                        strategy=self.name, stop=stop, target=target,
                        strength=min(1.0, (lo - price) / ai + 0.5),
                        reason=f"BB fade long: px {price:.2f} < lower {lo:.2f}",
                    ))
                    long_done.add(d)

            # ---- SHORT fade: close pierces above the upper band by an ATR margin.
            if (self.allow_short and not np.isnan(up) and d not in short_done
                    and price > up and m < price
                    and (price - up) >= self.min_pierce_atr * ai):
                stop = price + self.stop_atr_mult * ai
                target = price - self.target_frac * (price - m)
                if stop > price and target < price:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.SHORT, type=SignalType.ENTRY,
                        strategy=self.name, stop=stop, target=target,
                        strength=min(1.0, (price - up) / ai + 0.5),
                        reason=f"BB fade short: px {price:.2f} > upper {up:.2f}",
                    ))
                    short_done.add(d)

        return signals
