"""Pivot Reversal — classic floor-trader pivots, trend-aligned fade-to-center.

Floor-trader (a.k.a. "classic") pivots are computed from the PRIOR day's
completed high/low/close:

    P  = (H + L + C) / 3
    R1 = 2P - L          S1 = 2P - H
    R2 = P + (H - L)     S2 = P - (H - L)

These are intraday support/resistance magnets. The mean-reversion play is to
*fade* tests of the outer levels back toward the central pivot P:

  * Price tags a support level (S1/S2), prints a rejection (a bar that dips to
    the level but closes back above it, up off its low) -> go LONG, target P.
  * Price tags a resistance level (R1/R2), prints a rejection (a bar that pokes
    the level but closes back below it, down off its high) -> go SHORT, target P.

Two filters give the fade an actual edge instead of standing in front of a
freight train:

  * REGIME: only fade *with* the higher-timeframe trend. We only buy support
    dips when the prior daily close is above its EMA (an uptrending name -- the
    dip-buy is trend-aligned) and only short resistance when the prior close is
    below its EMA. Blindly shorting resistance in a bull tape is how fade books
    bleed out; the regime gate removes that.
  * STRETCH: require price to be meaningfully extended from the session VWAP
    (>= `vwap_stretch` ATRs away) before fading. A level tag that is also a
    VWAP stretch is a genuine over-extension primed to revert, not noise.

Stops sit an ATR fraction beyond the tagged level (where the thesis is wrong);
the target is the central pivot P. At most one long and one short per symbol
per day; no new entries late in the session; the engine force-flattens at the
close.

CAUSALITY: today's pivots AND the daily-trend EMA are built only from prior
completed days. We aggregate daily H/L/C by `df.index.normalize()`, take an
EMA over those daily closes, then `.shift(1)` so each calendar day maps to the
PRIOR completed day's values, and broadcast back onto every intraday bar. A bar
at index i only ever consults its own OHLC, the causal session VWAP/ATR up to i,
and these prior-day-derived constants -- never today's full-day extremes or a
future bar.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import atr, ema, vwap_session
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class PivotReversal(Strategy):
    name = "Pivot"

    def __init__(
        self,
        atr_period: int = 14,
        tag_atr: float = 0.15,        # how close to a level counts as a "tag"
        stop_atr_mult: float = 0.7,   # stop sits this many ATR beyond the level
        min_target_atr: float = 0.4,  # skip if P is too close (no edge)
        vwap_stretch: float = 0.9,    # require price >= this many ATR from VWAP
        trend_ema: int = 20,          # daily-close EMA span for the regime gate
        regime: bool = True,          # only fade in the direction of the trend
        no_entry_after: dtime = dtime(15, 0),
        no_entry_before: dtime = dtime(9, 45),  # let the open settle
        allow_short: bool = True,
        use_r2s2: bool = True,        # also fade the outer R2/S2 levels
    ):
        super().__init__(
            atr_period=atr_period, tag_atr=tag_atr, stop_atr_mult=stop_atr_mult,
            min_target_atr=min_target_atr, vwap_stretch=vwap_stretch,
            trend_ema=trend_ema, regime=regime, no_entry_after=no_entry_after,
            no_entry_before=no_entry_before, allow_short=allow_short,
            use_r2s2=use_r2s2,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.atr_period + 5:
            return []
        symbol = df["symbol"].iloc[0]

        # --- prior-day pivots + daily-trend regime (all causal) ---------
        day = df.index.normalize()
        daily_high = df["high"].groupby(day).max()
        daily_low = df["low"].groupby(day).min()
        daily_close = df.groupby(day)["close"].last()
        # shift(1): each calendar day sees only PRIOR completed days.
        ph = daily_high.shift(1)
        pl = daily_low.shift(1)
        pc = daily_close.shift(1)

        P = (ph + pl + pc) / 3.0
        rng = ph - pl
        R1 = 2 * P - pl
        S1 = 2 * P - ph
        R2 = P + rng
        S2 = P - rng

        # Daily-trend regime: EMA of daily closes, shifted so "today" only sees
        # completed days. up_regime[d] = prior close was above its trend EMA.
        daily_ema = ema(daily_close, self.trend_ema).shift(1)
        up_regime = pc > daily_ema

        # Broadcast per-day constants onto every intraday bar.
        Pv = day.map(P).to_numpy()
        R1v = day.map(R1).to_numpy()
        R2v = day.map(R2).to_numpy()
        S1v = day.map(S1).to_numpy()
        S2v = day.map(S2).to_numpy()
        upv = day.map(up_regime).to_numpy()

        a = atr(df, self.atr_period).to_numpy()
        vw = vwap_session(df).to_numpy()

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
            # Stretch from VWAP, in ATR units. Positive => price below VWAP.
            stretch = (vw[i] - close[i]) / av if not np.isnan(vw[i]) else 0.0
            up = bool(upv[i])

            # ---- LONG: fade a support tag back up to P (uptrend only) ----
            long_ok = (up or not self.regime)
            if d not in long_done and long_ok:
                supports = [("S1", S1v[i])]
                if self.use_r2s2:
                    supports.append(("S2", S2v[i]))
                for lbl, lvl in supports:
                    if np.isnan(lvl):
                        continue
                    # Rejection: bar dipped to/below the level but closed back
                    # above it and up off the bar low; price stretched below VWAP.
                    tagged = low[i] <= lvl + tag
                    rejected = close[i] > lvl and close[i] > o[i]
                    stretched = stretch >= self.vwap_stretch
                    target = Pv[i]
                    if (tagged and rejected and stretched
                            and target - close[i] >= self.min_target_atr * av):
                        stop = min(low[i], lvl) - self.stop_atr_mult * av
                        risk = close[i] - stop
                        if risk > 0:
                            signals.append(Signal(
                                ts=idx[i], symbol=symbol, side=Side.LONG,
                                type=SignalType.ENTRY, strategy=self.name,
                                stop=stop, target=target,
                                strength=min(1.0, stretch / (2 * self.vwap_stretch + 1e-9) + 0.5),
                                reason=f"fade {lbl} {lvl:.2f} -> P {target:.2f}",
                            ))
                            long_done.add(d)
                            break

            # ---- SHORT: fade a resistance tag back down to P (downtrend) --
            short_ok = self.allow_short and ((not up) or not self.regime)
            if d not in short_done and short_ok:
                resists = [("R1", R1v[i])]
                if self.use_r2s2:
                    resists.append(("R2", R2v[i]))
                for lbl, lvl in resists:
                    if np.isnan(lvl):
                        continue
                    tagged = high[i] >= lvl - tag
                    rejected = close[i] < lvl and close[i] < o[i]
                    stretched = (-stretch) >= self.vwap_stretch
                    target = Pv[i]
                    if (tagged and rejected and stretched
                            and close[i] - target >= self.min_target_atr * av):
                        stop = max(high[i], lvl) + self.stop_atr_mult * av
                        risk = stop - close[i]
                        if risk > 0:
                            signals.append(Signal(
                                ts=idx[i], symbol=symbol, side=Side.SHORT,
                                type=SignalType.ENTRY, strategy=self.name,
                                stop=stop, target=target,
                                strength=min(1.0, (-stretch) / (2 * self.vwap_stretch + 1e-9) + 0.5),
                                reason=f"fade {lbl} {lvl:.2f} -> P {target:.2f}",
                            ))
                            short_done.add(d)
                            break

        return signals
