"""Gap strategy — opening gap vs. the prior close, momentum-aware.

Each morning the first RTH bar opens some distance from the PRIOR day's close.
That gap, measured in ATR, drives two opposite plays. Critically, in liquid
momentum names (SPY + Mag7) the dominant edge is to go *with* a gap that keeps
running, not to fade it -- naive gap-fades stand in front of momentum and bleed.
So this strategy leads with gap-and-go and keeps a conservative, regime-gated
fade as a secondary path.

  * GAP-AND-GO (primary): a gap (>= `go_min_atr`) whose first bars extend in the
    gap's direction tends to continue. Go WITH it, but only when it agrees with
    the higher-timeframe trend regime (prior daily close vs. its EMA):
      - Gap UP   + early bars pushing higher + uptrend  -> LONG.
      - Gap DOWN + early bars pushing lower  + downtrend -> SHORT.
    Stop is an ATR multiple beyond the gap base (open / bar extreme); target is
    an RR multiple of that risk.

  * GAP-FILL FADE (secondary, optional via `allow_fade`): a MODERATE gap
    (`fade_min_atr`..`fade_max_atr`) that is fighting the trend -- a gap UP in a
    downtrending name, or a gap DOWN in an uptrending name -- is statistically
    prone to fill. Fade it back to the prior close once the early bars roll over
    toward that close.

Both decisions are made on the close of an early bar (within the first
`confirm_bars`); the engine fills at the next bar's open. One trade per symbol
per day; no entries after `no_entry_after` (the gap edge lives in the morning).

CAUSALITY: prior-day inputs are yesterday's completed close and an EMA over
PRIOR daily closes, both obtained via `groupby(normalize())` aggregates and
`.shift(1)`. Today's open is the FIRST bar's open; momentum confirmation uses
only bars at/after the open up to the current bar i -- never a later bar, never
today's full-day high/low/close, never today's own close as a "prior" close.
"""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

from daytrader.core.indicators import atr, ema
from daytrader.core.types import Side, Signal, SignalType
from daytrader.strategies.base import Strategy


class GapFade(Strategy):
    name = "Gap"

    def __init__(
        self,
        atr_period: int = 14,
        go_min_atr: float = 0.5,     # gap >= this (in ATR) is a gap-and-go candidate
        go_rr: float = 2.0,          # reward:risk target for gap-and-go
        confirm_bars: int = 3,       # decide within the first N bars after open
        stop_atr_mult: float = 0.8,  # stop this many ATR beyond the gap base
        trend_ema: int = 20,         # daily-close EMA span for the regime gate
        regime: bool = True,         # require the gap to agree with the trend
        allow_fade: bool = True,     # enable the secondary gap-fill fade path
        fade_min_atr: float = 0.5,   # moderate-gap fade window (lower bound)
        fade_max_atr: float = 1.2,   # moderate-gap fade window (upper bound)
        no_entry_after: dtime = dtime(11, 0),  # gap edge lives in the morning
        allow_short: bool = True,
    ):
        super().__init__(
            atr_period=atr_period, go_min_atr=go_min_atr, go_rr=go_rr,
            confirm_bars=confirm_bars, stop_atr_mult=stop_atr_mult,
            trend_ema=trend_ema, regime=regime, allow_fade=allow_fade,
            fade_min_atr=fade_min_atr, fade_max_atr=fade_max_atr,
            no_entry_after=no_entry_after, allow_short=allow_short,
        )

    def generate(self, df: pd.DataFrame) -> list[Signal]:
        if len(df) < self.atr_period + 5:
            return []
        symbol = df["symbol"].iloc[0]

        # --- prior-day close + daily-trend regime (causal) -------------
        day = df.index.normalize()
        daily_close = df.groupby(day)["close"].last()
        pc = daily_close.shift(1)        # each day -> prior day's close
        pcv = day.map(pc).to_numpy()

        daily_ema = ema(daily_close, self.trend_ema).shift(1)
        up_regime = pc > daily_ema       # prior close above its trend EMA
        upv = day.map(up_regime).to_numpy()

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
            # Decide within the first `confirm_bars` bars (need >=1 bar of action
            # to confirm momentum, so skip the raw 09:30 open bar itself).
            k = bar_in_day[i]
            if k < 1 or k >= self.confirm_bars:
                continue

            av = a[i]
            if np.isnan(av) or av <= 0 or np.isnan(pcv[i]):
                continue

            today_open = tov[i]
            gap = today_open - pcv[i]
            gap_atr = abs(gap) / av
            if gap_atr < min(self.go_min_atr, self.fade_min_atr):
                continue

            cl = close[i]
            up = bool(upv[i])

            # ---- GAP-AND-GO LONG: gap up, extending, uptrend ----------
            if (gap > 0 and gap_atr >= self.go_min_atr
                    and cl > today_open and cl > o[i]
                    and (up or not self.regime)):
                stop = min(low[i], today_open) - self.stop_atr_mult * av
                risk = cl - stop
                if risk > 0:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.LONG,
                        type=SignalType.ENTRY, strategy=self.name,
                        stop=stop, target=cl + self.go_rr * risk,
                        strength=min(1.0, gap_atr / 2.0),
                        reason=f"gap-and-go up {gap_atr:.1f}ATR",
                    ))
                    done.add(d)
                    continue

            # ---- GAP-AND-GO SHORT: gap down, extending, downtrend -----
            if (gap < 0 and gap_atr >= self.go_min_atr
                    and cl < today_open and cl < o[i]
                    and self.allow_short and ((not up) or not self.regime)):
                stop = max(high[i], today_open) + self.stop_atr_mult * av
                risk = stop - cl
                if risk > 0:
                    signals.append(Signal(
                        ts=idx[i], symbol=symbol, side=Side.SHORT,
                        type=SignalType.ENTRY, strategy=self.name,
                        stop=stop, target=cl - self.go_rr * risk,
                        strength=min(1.0, gap_atr / 2.0),
                        reason=f"gap-and-go down {gap_atr:.1f}ATR",
                    ))
                    done.add(d)
                    continue

            # ---- GAP-FILL FADE (secondary): moderate, counter-trend ---
            # Only fade gaps that fight the prevailing trend -- those revert;
            # gaps that agree with the trend are handled by gap-and-go above.
            if not self.allow_fade:
                continue
            if not (self.fade_min_atr <= gap_atr <= self.fade_max_atr):
                continue
            target = pcv[i]  # prior close = the gap-fill objective

            # Gap UP fighting a DOWN trend -> short back toward prior close.
            if gap > 0 and self.allow_short and ((not up) or not self.regime):
                if cl < today_open and cl > target:  # early bars rolling over
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
            # Gap DOWN fighting an UP trend -> long back toward prior close.
            elif gap < 0 and (up or not self.regime):
                if cl > today_open and cl < target:  # early bars bouncing
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
