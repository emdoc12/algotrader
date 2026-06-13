"""RSI(2) intraday mean reversion (Larry Connors style).

The Connors RSI(2) idea: in an uptrend, very short-term oversoldness tends to
snap back. We adapt it to a 5-minute intraday series:

  * Trend filter: only buy dips when price is above a slow EMA (the intraday
    "regime"), only short rips when below it. This keeps us trading *with* the
    larger drift and avoids catching falling knives.
  * Entry: a 2-period RSI computed on the bar closes dips below an oversold
    threshold (long) or pops above an overbought threshold (short).
  * Exit: RSI mean-reverts back through a neutral level (~55), OR a fixed
    profit target is hit, OR an ATR-based protective stop, OR EOD (engine).
  * Overtrading guard: at most one long and one short entry per symbol per day.

All indicators are causal; every signal at bar i uses only data through i.
The engine fills at the next bar's open.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import atr, ema, rsi
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class Rsi2Reversion(Strategy):
    name = "RSI2"

    def __init__(
        self,
        rsi_period: int = 2,
        trend_ema: int = 200,
        oversold: float = 5.0,
        overbought: float = 95.0,
        exit_rsi: float = 60.0,
        atr_period: int = 14,
        stop_atr_mult: float = 1.0,
        target_atr_mult: float = 1.0,
        no_entry_after: dtime = dtime(15, 0),
        allow_short: bool = True,
    ):
        super().__init__(
            rsi_period=rsi_period, trend_ema=trend_ema,
            oversold=oversold, overbought=overbought, exit_rsi=exit_rsi,
            atr_period=atr_period, stop_atr_mult=stop_atr_mult,
            target_atr_mult=target_atr_mult, no_entry_after=no_entry_after,
            allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.atr_period + 5:
            return []
        symbol = df["symbol"].iloc[0]

        close = df["close"]
        r = rsi(close, self.rsi_period)
        trend = ema(close, self.trend_ema)
        a = atr(df, self.atr_period)

        cv = close.values
        rv = r.values
        tv = trend.values
        av = a.values
        idx = df.index
        day = df.index.normalize()

        signals: list[Signal] = []
        # Track which day/direction already entered, and whether we're "in" a
        # synthetic position so we know when to emit our indicator EXIT.
        long_done: set = set()
        short_done: set = set()
        in_long = False
        in_short = False

        for i in range(len(df)):
            t = idx[i].time()
            d = day[i]
            price = cv[i]
            ri = rv[i]
            ti = tv[i]
            ai = av[i]

            # ---- indicator-based exits (emitted while we believe a position is open)
            if in_long and ri >= self.exit_rsi:
                signals.append(Signal(
                    ts=idx[i], symbol=symbol, side=Side.LONG, type=SignalType.EXIT,
                    strategy=self.name, reason=f"RSI back to {ri:.0f}",
                ))
                in_long = False
            if in_short and ri <= (100.0 - self.exit_rsi):
                signals.append(Signal(
                    ts=idx[i], symbol=symbol, side=Side.SHORT, type=SignalType.EXIT,
                    strategy=self.name, reason=f"RSI back to {ri:.0f}",
                ))
                in_short = False

            if t >= self.no_entry_after:
                continue
            if np.isnan(ti) or np.isnan(ai) or ai <= 0:
                continue

            # ---- LONG: oversold dip in an uptrend
            if (not in_long and d not in long_done
                    and ri < self.oversold and price > ti):
                stop = price - self.stop_atr_mult * ai
                target = price + self.target_atr_mult * ai
                if stop < price:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.LONG, type=SignalType.ENTRY,
                        strategy=self.name, stop=stop, target=target,
                        strength=min(1.0, (self.oversold - ri) / self.oversold + 0.5),
                        reason=f"RSI2 {ri:.0f} oversold, px>{ti:.2f} EMA",
                    ))
                    long_done.add(d)
                    in_long = True

            # ---- SHORT: overbought pop in a downtrend
            if (self.allow_short and not in_short and d not in short_done
                    and ri > self.overbought and price < ti):
                stop = price + self.stop_atr_mult * ai
                target = price - self.target_atr_mult * ai
                if stop > price:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.SHORT, type=SignalType.ENTRY,
                        strategy=self.name, stop=stop, target=target,
                        strength=min(1.0, (ri - self.overbought) / (100.0 - self.overbought) + 0.5),
                        reason=f"RSI2 {ri:.0f} overbought, px<{ti:.2f} EMA",
                    ))
                    short_done.add(d)
                    in_short = True

        return signals
