"""VWAP mean-reversion (fade extreme extensions back to session VWAP).

Intraday price tends to oscillate around the volume-weighted average price
during balanced (non-trending) sessions. When price stretches far beyond a
session-VWAP standard-deviation band *without* a strong directional trend,
it often snaps back toward VWAP. This strategy fades those extensions:

  * Compute session VWAP and +/- n_std intraday bands.
  * Go LONG when the prior bar pokes below the lower band (stretched cheap) and
    the current bar curls back up while still below VWAP (room to run); go SHORT
    when the prior bar pokes above the upper band and the current bar curls down
    while still above VWAP.
  * Only trade a genuinely *balanced* regime. Two filters do this:
      - ADX below a tight ceiling (no directional trend in force), and
      - price has crossed VWAP at least `min_crosses` times in the trailing
        window -- i.e. the tape is oscillating around VWAP, not running away
        from it. Empirically, fading extensions on Mag7 names is a money-loser
        unless this "two-sided / mean-reverting tape" condition holds: the most
        extreme extensions tend to be the strongest momentum (which keep going),
        so we deliberately fade *moderate* extensions in quiet, balanced tape.
  * Target a fraction of the distance back to VWAP (a partial revert, which
    fills far more reliably than a full snap-back); protective stop an ATR
    multiple beyond entry. Flat by EOD (engine force-flattens).
  * At most a couple of long / short entries per symbol per day, gated away from
    the open and the close.

Causal: every level used at bar i is built from data up to and including i
(session VWAP/bands, ATR and ADX are cumulative/causal; the VWAP-cross count
looks only at the trailing window ending at i).
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import adx, atr, session_vwap_bands
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class VwapReversion(Strategy):
    name = "VWAP-MR"

    def __init__(
        self,
        n_std: float = 2.0,
        atr_period: int = 14,
        stop_atr_mult: float = 1.5,
        target_frac: float = 0.6,
        min_rr: float = 0.4,
        adx_period: int = 14,
        adx_max: float = 18.0,
        min_atr_frac: float = 0.0005,
        cross_lookback: int = 12,
        min_crosses: int = 2,
        no_entry_before: dtime = dtime(10, 0),
        no_entry_after: dtime = dtime(15, 30),
        max_per_dir: int = 2,
        allow_short: bool = True,
    ):
        super().__init__(
            n_std=n_std, atr_period=atr_period, stop_atr_mult=stop_atr_mult,
            target_frac=target_frac, min_rr=min_rr, adx_period=adx_period,
            adx_max=adx_max, min_atr_frac=min_atr_frac,
            cross_lookback=cross_lookback, min_crosses=min_crosses,
            no_entry_before=no_entry_before, no_entry_after=no_entry_after,
            max_per_dir=max_per_dir, allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.atr_period + 5:
            return []
        symbol = df["symbol"].iloc[0]

        vw, upper, lower = session_vwap_bands(df, self.n_std)
        a = atr(df, self.atr_period)
        adx_ = adx(df, self.adx_period)

        idx = df.index
        day = df.index.normalize()
        close = df["close"].values
        low = df["low"].values
        high = df["high"].values
        vwv = vw.values
        upv = upper.values
        lov = lower.values
        av = a.values
        adv = adx_.values

        # Count VWAP crossings in the trailing window as a "balanced tape" proxy.
        # cross[i] == 1 when price flipped sides of VWAP between bar i-1 and i.
        # This is causal: the rolling sum at i uses only bars <= i.
        above = (close > vwv).astype(float)
        cross = np.abs(np.diff(above, prepend=above[0]))

        signals: list[Signal] = []
        long_count: dict = {}
        short_count: dict = {}

        for i in range(1, len(df)):
            t = idx[i].time()
            if t < self.no_entry_before or t >= self.no_entry_after:
                continue
            if (np.isnan(vwv[i]) or np.isnan(upv[i]) or np.isnan(lov[i])
                    or np.isnan(av[i]) or av[i] <= 0):
                continue
            # Skip dead tape: require ATR to be a sane fraction of price.
            if av[i] / close[i] < self.min_atr_frac:
                continue
            # Mean reversion only in a non-trending regime.
            if adv[i] > self.adx_max:
                continue
            # Require an oscillating, two-sided tape around VWAP.
            lb = max(0, i - self.cross_lookback)
            if cross[lb:i + 1].sum() < self.min_crosses:
                continue

            d = day[i]

            # LONG: prior bar dipped below the lower band, current bar curls
            # back up (close > prior close) and is still below VWAP (room to run).
            if (long_count.get(d, 0) < self.max_per_dir
                    and low[i - 1] < lov[i - 1]
                    and close[i] > close[i - 1]
                    and close[i] < vwv[i]):
                stop = close[i] - self.stop_atr_mult * av[i]
                risk = close[i] - stop
                if risk > 0:
                    target = close[i] + self.target_frac * (vwv[i] - close[i])
                    reward = target - close[i]
                    if reward >= self.min_rr * risk:
                        signals.append(Signal(
                            ts=idx[i], symbol=symbol, side=Side.LONG,
                            type=SignalType.ENTRY, strategy=self.name,
                            stop=stop, target=target,
                            reason=f"VWAP-MR long: below lower band, revert to {vwv[i]:.2f}",
                        ))
                        long_count[d] = long_count.get(d, 0) + 1

            # SHORT: prior bar poked above the upper band, current bar curls
            # back down and is still above VWAP.
            if (self.allow_short
                    and short_count.get(d, 0) < self.max_per_dir
                    and high[i - 1] > upv[i - 1]
                    and close[i] < close[i - 1]
                    and close[i] > vwv[i]):
                stop = close[i] + self.stop_atr_mult * av[i]
                risk = stop - close[i]
                if risk > 0:
                    target = close[i] - self.target_frac * (close[i] - vwv[i])
                    reward = close[i] - target
                    if reward >= self.min_rr * risk:
                        signals.append(Signal(
                            ts=idx[i], symbol=symbol, side=Side.SHORT,
                            type=SignalType.ENTRY, strategy=self.name,
                            stop=stop, target=target,
                            reason=f"VWAP-MR short: above upper band, revert to {vwv[i]:.2f}",
                        ))
                        short_count[d] = short_count.get(d, 0) + 1

        return signals
