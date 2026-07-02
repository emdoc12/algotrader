"""Event-driven backtest engine with realistic intraday fills.

Design goals (the things that make a backtest trustworthy):

  * No look-ahead. A signal decided on bar *t* (using its close) is executed
    at the OPEN of bar *t+1*. Indicators are causal by construction.
  * Realistic costs. Every fill pays half-spread + slippage; commissions are
    configurable. Stops are market orders and suffer gap-through-stop fills.
  * Honest intrabar logic. If a bar's range touches both stop and target, we
    assume the stop filled first (the conservative assumption).
  * True day trading. Positions are force-flattened at the session close; an
    optional daily loss limit halts trading for the rest of the day.
  * Portfolio aware. One shared equity account across SPY + Mag7 so drawdown
    and position sizing reflect the whole book, not one symbol in isolation.

The engine is granularity-agnostic: feed it 5m, 15m, or 1h frames.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime
from typing import Callable, Optional

import numpy as np
import pandas as pd

from daytrader.core.types import Fill, Position, Side, Signal, SignalType, Trade


@dataclass
class CostModel:
    """Transaction-cost assumptions, expressed in basis points of notional.

    Defaults are conservative-but-realistic for highly liquid SPY/Mag7 names.
    Use `CostModel.pessimistic()` to stress-test under the example dashboard's
    much harsher 0.4% slippage assumption.
    """
    slippage_bps: float = 2.0       # market-impact + timing slip per side
    half_spread_bps: float = 1.0    # half the bid/ask spread per side
    commission_per_share: float = 0.0   # most brokers are commission-free on equities
    commission_min: float = 0.0     # per-order minimum
    gap_through_stop: bool = True   # fill stops at the gapped open if worse

    @classmethod
    def pessimistic(cls) -> "CostModel":
        return cls(slippage_bps=40.0, half_spread_bps=5.0, commission_per_share=0.005)

    @classmethod
    def zero(cls) -> "CostModel":
        return cls(slippage_bps=0.0, half_spread_bps=0.0, commission_per_share=0.0)

    @property
    def per_side_bps(self) -> float:
        return self.slippage_bps + self.half_spread_bps


@dataclass
class EngineConfig:
    starting_equity: float = 100_000.0
    cost: CostModel = field(default_factory=CostModel)
    eod_flat: bool = True                 # force-close all positions at session end
    session_close: dtime = dtime(15, 55)  # last bar to hold into; flatten here
    daily_loss_limit_pct: float = 2.0     # halt trading for the day past this loss
    max_concurrent_positions: int = 4     # cap simultaneous open positions
    allow_short: bool = True
    breakeven_at_r: float = 0.0           # move stop to entry after +N*R (0=off)
    trail_atr_mult: float = 0.0           # trail stop at close -/+ mult*ATR (0=off)


# A sizing function: (equity, signal, entry_price, atr) -> share quantity (>=0)
SizingFn = Callable[[float, Signal, float, float], float]


def _default_sizer(equity: float, signal: Signal, price: float, atr: float) -> float:
    """Risk a fixed 0.5% of equity per trade against the stop distance."""
    risk_dollars = equity * 0.005
    if signal.stop and price:
        per_share_risk = abs(price - signal.stop)
        if per_share_risk > 0:
            return max(0.0, risk_dollars / per_share_risk)
    # fall back to a small notional if no stop provided
    return (equity * 0.05) / price if price else 0.0


class BacktestEngine:
    def __init__(self, config: EngineConfig | None = None, sizer: SizingFn | None = None):
        self.cfg = config or EngineConfig()
        self.sizer = sizer or _default_sizer
        self.reset()

    def reset(self):
        self.cash = self.cfg.starting_equity
        self.positions: dict[str, Position] = {}
        self.trades: list[Trade] = []
        self.fills: list[Fill] = []
        self.equity_curve: list[tuple] = []
        self._day_start_equity = self.cfg.starting_equity
        self._day = None
        self._halted_day = False

    # ---- fill helpers -------------------------------------------------
    def _apply_entry_cost(self, side: Side, price: float) -> float:
        adj = self.cfg.cost.per_side_bps / 1e4
        return price * (1 + adj) if side == Side.LONG else price * (1 - adj)

    def _apply_exit_cost(self, side: Side, price: float) -> float:
        adj = self.cfg.cost.per_side_bps / 1e4
        # exiting a long is a sell (price down), exiting a short is a buy (price up)
        return price * (1 - adj) if side == Side.LONG else price * (1 + adj)

    def _commission(self, qty: float) -> float:
        return max(self.cfg.cost.commission_min, qty * self.cfg.cost.commission_per_share)

    # ---- portfolio bookkeeping ---------------------------------------
    def _equity(self, marks: dict[str, float]) -> float:
        # Cash holds realized P&L plus short proceeds / minus long cost.
        # Open positions contribute their mark-to-market value.
        eq = self.cash
        for sym, pos in self.positions.items():
            mark = marks.get(sym, pos.entry_price)
            eq += _position_value(pos, mark)
        return eq

    # ---- core run -----------------------------------------------------
    def run(
        self,
        data: dict[str, pd.DataFrame],
        signals: list[Signal],
    ) -> tuple[list[Trade], pd.Series]:
        """Run the simulation.

        Args:
            data: {symbol: OHLCV DataFrame} indexed by Eastern timestamps,
                  RTH-filtered, sorted ascending.
            signals: ENTRY/EXIT signals decided at a bar's close. Executed at
                     the next bar's open for that symbol.
        Returns:
            (trades, equity_curve Series indexed by timestamp).
        """
        self.reset()

        # Precompute ATR per symbol for sizing (causal).
        from daytrader.core.indicators import atr as atr_ind
        atr_map = {s: atr_ind(df, 14) for s, df in data.items()}

        # Map each symbol's timestamp -> integer position for next-bar lookup.
        index_of: dict[str, dict[pd.Timestamp, int]] = {}
        bars_list: dict[str, list] = {}
        for s, df in data.items():
            index_of[s] = {ts: i for i, ts in enumerate(df.index)}
            bars_list[s] = list(df.itertuples(index=True))

        # Schedule entries/exits at the NEXT bar open after the decision bar.
        scheduled: dict[tuple, list[Signal]] = {}
        for sig in signals:
            df = data.get(sig.symbol)
            if df is None or sig.ts not in index_of[sig.symbol]:
                continue
            i = index_of[sig.symbol][sig.ts]
            if i + 1 >= len(df):
                continue  # no next bar to fill on
            exec_ts = df.index[i + 1]
            scheduled.setdefault((exec_ts, sig.symbol), []).append(sig)

        # Build a unified, ordered event timeline across all symbols.
        timeline: list[tuple] = []
        for s, df in data.items():
            for ts in df.index:
                timeline.append((ts, s))
        timeline.sort(key=lambda x: (x[0], x[1]))

        # Pre-index OHLC rows for O(1) access during the loop.
        rows: dict[tuple, tuple] = {}
        last_bar_of_day: dict[tuple, bool] = {}
        for s, df in data.items():
            days = df.index.normalize()
            for i, ts in enumerate(df.index):
                rows[(ts, s)] = (
                    float(df["open"].iloc[i]),
                    float(df["high"].iloc[i]),
                    float(df["low"].iloc[i]),
                    float(df["close"].iloc[i]),
                )
                is_last = (i == len(df) - 1) or (days[i] != days[i + 1])
                last_bar_of_day[(ts, s)] = bool(is_last)

        marks: dict[str, float] = {}

        for ts, sym in timeline:
            o, h, l, c = rows[(ts, sym)]
            marks[sym] = c
            day = ts.normalize()

            # New trading day: reset halt + daily anchor.
            if self._day != day:
                self._day = day
                self._day_start_equity = self._equity(marks)
                self._halted_day = False

            # 1) Manage an existing position in this symbol (stops/targets/EOD).
            atr_now = float(atr_map[sym].get(ts, np.nan)) if ts in atr_map[sym].index else np.nan
            self._manage_position(ts, sym, o, h, l, c, atr_now)

            # 2) Daily loss-limit check (halts new entries, flattens book).
            cur_eq = self._equity(marks)
            day_pnl_pct = (cur_eq - self._day_start_equity) / self._day_start_equity * 100.0
            if not self._halted_day and day_pnl_pct <= -self.cfg.daily_loss_limit_pct:
                self._halted_day = True
                self._flatten_all(ts, marks, reason="daily_loss_limit")

            # 3) End-of-day flat.
            if self.cfg.eod_flat and last_bar_of_day[(ts, sym)] and sym in self.positions:
                self._close_position(ts, sym, c, reason="eod_flat")

            # 4) Process scheduled orders for this (ts, sym). Skip ENTRY fills on
            # the last bar of a day — there is no later bar to manage or exit
            # them, so they would be held overnight, which this day-trading
            # engine must never allow (that would let a strategy harvest gaps
            # that live paper trading, flat at the close, can never realize).
            for sig in scheduled.get((ts, sym), []):
                if last_bar_of_day[(ts, sym)] and sig.type == SignalType.ENTRY:
                    continue
                self._handle_signal(ts, sig, o, atr_map, marks)

            # 5) Mark-to-market equity snapshot.
            self.equity_curve.append((ts, self._equity(marks)))

        # Close anything still open at the very end.
        if self.positions:
            last_ts = timeline[-1][0]
            for sym in list(self.positions):
                self._close_position(last_ts, sym, marks.get(sym, self.positions[sym].entry_price),
                                     reason="end_of_data")

        eq = pd.Series(
            [e for _, e in self.equity_curve],
            index=pd.DatetimeIndex([t for t, _ in self.equity_curve]),
            name="equity",
        )
        eq = eq[~eq.index.duplicated(keep="last")]
        return self.trades, eq

    # ---- signal handling ----------------------------------------------
    def _handle_signal(self, ts, sig: Signal, open_px: float, atr_map, marks):
        if sig.type == SignalType.EXIT:
            if sig.symbol in self.positions:
                self._close_position(ts, sig.symbol, open_px, reason=sig.reason or "signal_exit")
            return
        # ENTRY
        if self._halted_day:
            return
        if sig.symbol in self.positions:
            return  # one position per symbol
        if len(self.positions) >= self.cfg.max_concurrent_positions:
            return
        if sig.side == Side.SHORT and not self.cfg.allow_short:
            return

        atr_val = float(atr_map[sig.symbol].get(ts, np.nan)) if ts in atr_map[sig.symbol].index else np.nan
        if np.isnan(atr_val):
            atr_val = open_px * 0.005
        equity = self._equity(marks)
        qty = self.sizer(equity, sig, open_px, atr_val)
        qty = float(np.floor(qty))
        if qty <= 0:
            return

        fill_px = self._apply_entry_cost(sig.side, open_px)
        notional = fill_px * qty
        # cap leverage: don't spend more than available buying power (long only uses cash)
        if sig.side == Side.LONG and notional > self.cash:
            qty = float(np.floor(self.cash / fill_px))
            if qty <= 0:
                return
            notional = fill_px * qty
        commission = self._commission(qty)
        slip = abs(fill_px - open_px) * qty

        # cash accounting: long buys reduce cash; short sells add cash (proceeds)
        if sig.side == Side.LONG:
            self.cash -= notional + commission
        else:
            self.cash += notional - commission

        self.positions[sig.symbol] = Position(
            symbol=sig.symbol, side=sig.side, qty=qty, entry_price=fill_px,
            entry_ts=ts, strategy=sig.strategy, stop=sig.stop, target=sig.target,
            init_stop=sig.stop, commission_paid=commission, slippage_paid=slip,
        )
        self.fills.append(Fill(ts, sig.symbol, sig.side, qty, fill_px, commission, slip,
                               sig.strategy, sig.reason))

    def _manage_position(self, ts, sym, o, h, l, c, atr_now: float = float("nan")):
        pos = self.positions.get(sym)
        if pos is None:
            return
        # update MAE/MFE on open P&L using the bar extremes
        if pos.side == Side.LONG:
            pos.mfe = max(pos.mfe, (h - pos.entry_price) * pos.qty)
            pos.mae = min(pos.mae, (l - pos.entry_price) * pos.qty)
        else:
            pos.mfe = max(pos.mfe, (pos.entry_price - l) * pos.qty)
            pos.mae = min(pos.mae, (pos.entry_price - h) * pos.qty)

        # Dynamic stop adjustments BEFORE testing for a stop hit this bar.
        self._adjust_stop(pos, h, l, c, atr_now)

        stop, target = pos.stop, pos.target
        hit_stop = hit_target = False
        stop_px = target_px = None

        if pos.side == Side.LONG:
            if stop is not None and l <= stop:
                hit_stop = True
                stop_px = min(o, stop) if (self.cfg.cost.gap_through_stop and o < stop) else stop
            if target is not None and h >= target:
                hit_target = True
                target_px = target
        else:  # SHORT
            if stop is not None and h >= stop:
                hit_stop = True
                stop_px = max(o, stop) if (self.cfg.cost.gap_through_stop and o > stop) else stop
            if target is not None and l <= target:
                hit_target = True
                target_px = target

        if hit_stop:  # conservative: stop takes priority if both touched
            self._close_position(ts, sym, stop_px, reason="stop", market=True)
        elif hit_target:
            self._close_position(ts, sym, target_px, reason="target", market=False)

    def _adjust_stop(self, pos: Position, h, l, c, atr_now: float):
        """Apply breakeven and/or ATR-trailing stop logic (both optional).

        Stops only ever move in the favorable direction (never loosened).
        """
        be_r = self.cfg.breakeven_at_r
        trail = self.cfg.trail_atr_mult

        if be_r > 0 and not pos.breakeven_done and pos.init_stop is not None:
            init_risk = abs(pos.entry_price - pos.init_stop)
            if init_risk > 0:
                if pos.side == Side.LONG and (h - pos.entry_price) >= be_r * init_risk:
                    new_stop = pos.entry_price
                    pos.stop = max(pos.stop, new_stop) if pos.stop is not None else new_stop
                    pos.breakeven_done = True
                elif pos.side == Side.SHORT and (pos.entry_price - l) >= be_r * init_risk:
                    new_stop = pos.entry_price
                    pos.stop = min(pos.stop, new_stop) if pos.stop is not None else new_stop
                    pos.breakeven_done = True

        if trail > 0 and not np.isnan(atr_now) and atr_now > 0:
            if pos.side == Side.LONG:
                trail_stop = c - trail * atr_now
                pos.stop = max(pos.stop, trail_stop) if pos.stop is not None else trail_stop
            else:
                trail_stop = c + trail * atr_now
                pos.stop = min(pos.stop, trail_stop) if pos.stop is not None else trail_stop

    def _close_position(self, ts, sym, raw_price: float, reason: str, market: bool = True):
        pos = self.positions.pop(sym, None)
        if pos is None:
            return
        # Market exits (stops/EOD) pay slippage; limit targets fill at the limit.
        exit_px = self._apply_exit_cost(pos.side, raw_price) if market else raw_price
        commission = self._commission(pos.qty)
        slip = abs(exit_px - raw_price) * pos.qty

        if pos.side == Side.LONG:
            self.cash += exit_px * pos.qty - commission
        else:
            self.cash -= exit_px * pos.qty + commission  # buy to cover

        trade = Trade(
            symbol=sym, side=pos.side, strategy=pos.strategy,
            entry_ts=pos.entry_ts, entry_price=pos.entry_price, qty=pos.qty,
            exit_ts=ts, exit_price=exit_px,
            commission=pos.commission_paid + commission,
            slippage_cost=pos.slippage_paid + slip,
            exit_reason=reason, mae=pos.mae, mfe=pos.mfe,
        )
        self.trades.append(trade)
        self.fills.append(Fill(ts, sym, pos.side, pos.qty, exit_px, commission, slip,
                               pos.strategy, reason))

    def _flatten_all(self, ts, marks, reason: str):
        for sym in list(self.positions):
            self._close_position(ts, sym, marks.get(sym, self.positions[sym].entry_price),
                                 reason=reason)


def _position_value(pos: Position, mark: float) -> float:
    """Mark-to-market contribution of an open position to equity.

    Long: cash already paid for the shares, so they are worth qty*mark now.
    Short: proceeds (qty*entry) are already sitting in cash; the open liability
    of buying the shares back is -qty*mark. Net effect on equity is therefore
    qty*(entry - mark), exactly the short's unrealized P&L.
    """
    if pos.side == Side.LONG:
        return pos.qty * mark
    return -pos.qty * mark
