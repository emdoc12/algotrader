"""MACD trend continuation.

A momentum-continuation setup that uses the MACD histogram zero-cross as the
trigger, gated by a long trend filter and an ADX regime filter so it only
fires when a real trend exists:

  * Trend filter: a slow EMA (default 100). Longs only when price is above it;
    shorts only when below.
  * Regime filter: ADX above a floor => a trend is present (skip range days).
  * Trigger: the MACD histogram crossing ABOVE zero (macd_line crossing above
    its signal line) is a fresh bullish impulse; crossing BELOW zero is bearish.
    We require the cross on the current bar (hist[i] > 0 and hist[i-1] <= 0) and
    that the MACD line itself sits on the trade's side of zero, so we trade
    impulses in the direction of momentum rather than counter-trend snaps.
  * Risk: ATR-based stop and a fixed reward:risk target do the exiting. An
    optional MACD-cross exit (off by default) can also book trades when the
    histogram crosses back through zero against us; on 5-minute bars that exit
    whipsaws and bleeds, so the default lets the stop/target run. Engine
    flattens at EOD.
  * Discipline: at most one entry per symbol/day/direction, gated away from the
    noisy first 30 minutes and the last hour.

Indicators (macd, adx, atr, ema) are causal; a signal at bar i uses only data
through bar i and the engine fills at the next bar's open.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import adx, atr, ema, macd
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class MacdTrend(Strategy):
    name = "MACD"

    def __init__(
        self,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        trend_ema: int = 100,
        adx_period: int = 14,
        adx_min: float = 25.0,
        atr_period: int = 14,
        stop_atr_mult: float = 1.5,
        rr: float = 1.5,
        min_atr_frac: float = 0.0006,
        max_entries_per_dir: int = 1,
        no_entry_before: dtime = dtime(10, 0),
        no_entry_after: dtime = dtime(15, 0),
        macd_cross_exit: bool = False,
        allow_short: bool = True,
    ):
        super().__init__(
            macd_fast=macd_fast, macd_slow=macd_slow, macd_signal=macd_signal,
            trend_ema=trend_ema, adx_period=adx_period, adx_min=adx_min,
            atr_period=atr_period, stop_atr_mult=stop_atr_mult, rr=rr,
            min_atr_frac=min_atr_frac,
            max_entries_per_dir=max_entries_per_dir,
            no_entry_before=no_entry_before, no_entry_after=no_entry_after,
            macd_cross_exit=macd_cross_exit, allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        n = len(df)
        warmup = max(self.trend_ema, self.macd_slow + self.macd_signal, self.adx_period) + 5
        if n < warmup:
            return []
        symbol = df["symbol"].iloc[0]

        close = df["close"]
        macd_line, _signal_line, hist = macd(
            close, self.macd_fast, self.macd_slow, self.macd_signal
        )
        trend = ema(close, self.trend_ema)
        adx_s = adx(df, self.adx_period)
        a = atr(df, self.atr_period)

        c = close.values
        ml = macd_line.values
        hv = hist.values
        tv = trend.values
        adxv = adx_s.values
        av = a.values
        idx = df.index
        day = df.index.normalize()

        signals: list[Signal] = []
        long_count: dict = {}
        short_count: dict = {}
        # Track the side we believe is open so we emit one matching EXIT per
        # entry. We clear it on a counter-cross exit and at each new day (the
        # engine force-flattens at EOD, so positions never span days).
        open_dir: Side | None = None
        open_day = None

        for i in range(warmup, n):
            if np.isnan(av[i]) or av[i] <= 0 or np.isnan(tv[i]) or np.isnan(hv[i - 1]):
                continue
            d = day[i]
            t = idx[i].time()

            if open_day != d:
                open_dir = None
                open_day = d

            bull_cross = hv[i] > 0 and hv[i - 1] <= 0
            bear_cross = hv[i] < 0 and hv[i - 1] >= 0

            # MACD-cross exits: book the trade when momentum flips against the
            # held side, regardless of the entry-cutoff time.
            if self.macd_cross_exit and open_dir is not None:
                if open_dir == Side.LONG and bear_cross:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.LONG,
                        type=SignalType.EXIT, strategy=self.name,
                        reason="MACD bear cross exit",
                    ))
                    open_dir = None
                elif open_dir == Side.SHORT and bull_cross:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.SHORT,
                        type=SignalType.EXIT, strategy=self.name,
                        reason="MACD bull cross exit",
                    ))
                    open_dir = None

            if t < self.no_entry_before or t >= self.no_entry_after:
                continue
            if adxv[i] < self.adx_min:
                continue
            # Require enough range for a 2R target to be reachable.
            if av[i] / c[i] < self.min_atr_frac:
                continue

            # LONG: bullish histogram cross above zero, MACD line above zero
            # (genuine upside momentum), and price above the trend EMA.
            if (
                bull_cross
                and ml[i] > 0
                and c[i] > tv[i]
                and long_count.get(d, 0) < self.max_entries_per_dir
            ):
                stop = c[i] - self.stop_atr_mult * av[i]
                risk = c[i] - stop
                if risk > 0:
                    target = c[i] + self.rr * risk
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.LONG,
                        type=SignalType.ENTRY, strategy=self.name,
                        stop=stop, target=target,
                        reason=f"MACD bull cross @ {c[i]:.2f} adx={adxv[i]:.0f}",
                    ))
                    long_count[d] = long_count.get(d, 0) + 1
                    open_dir = Side.LONG

            # SHORT mirror.
            elif (
                self.allow_short
                and bear_cross
                and ml[i] < 0
                and c[i] < tv[i]
                and short_count.get(d, 0) < self.max_entries_per_dir
            ):
                stop = c[i] + self.stop_atr_mult * av[i]
                risk = stop - c[i]
                if risk > 0:
                    target = c[i] - self.rr * risk
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.SHORT,
                        type=SignalType.ENTRY, strategy=self.name,
                        stop=stop, target=target,
                        reason=f"MACD bear cross @ {c[i]:.2f} adx={adxv[i]:.0f}",
                    ))
                    short_count[d] = short_count.get(d, 0) + 1
                    open_dir = Side.SHORT

        return signals
