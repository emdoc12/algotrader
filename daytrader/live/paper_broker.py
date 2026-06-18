"""Paper-trading broker for the autonomous day-trading agent.

Executes simulated market orders against the *same live quote the agent
reasoned over* (from :mod:`daytrader.data.quotes`, the shared snapshot/broker
quote source), plus realistic slippage drawn from the backtester's
:class:`CostModel`, tracks cash / positions / equity exactly the way the
backtest engine does, and persists everything through :class:`LiveDB` so the
whole book survives container restarts.

Accounting mirrors ``daytrader.backtest.engine``:

  * Cash holds realized P&L plus short proceeds (minus long cost).
  * A LONG position contributes ``qty * mark`` to equity.
  * A SHORT position contributes ``-qty * mark`` (its proceeds already sit in
    cash, so the net equity effect is ``qty * (entry - mark)``).

Per-cycle quote pinning: the competition loop calls
:meth:`set_cycle_quotes` before each team's trade cycle with the snapshot's
quote map, so the broker fills at the exact prices the agent saw. The pin is
cleared after the cycle, so equity marks and EOD flatten use live quotes.

PAPER mode only -- no real orders are ever sent.
"""
from __future__ import annotations

from typing import Optional

from daytrader.backtest.engine import CostModel
from daytrader.core.types import Side
from daytrader.data import quotes
from daytrader.live.db import LiveDB, _now_iso


class PaperBroker:
    def __init__(
        self,
        db: LiveDB,
        starting_equity: float = 100_000.0,
        cost: CostModel | None = None,
    ):
        self.db = db
        self.starting_equity = float(starting_equity)
        self.cost = cost or CostModel()

        # symbol -> position dict (side as Side, qty/entry_price floats, etc.)
        self._positions: dict[str, dict] = {}
        # Per-cycle pinned quotes (snapshot.market[sym].price). When set, fills
        # use these prices so the broker matches what the agent reasoned over.
        # Cleared between cycles; equity marks and EOD flattens use live quotes.
        self._cycle_quotes: Optional[dict[str, float]] = None

        # ---- restart recovery -------------------------------------------------
        for row in self.db.load_open_positions():
            sym = row["symbol"]
            self._positions[sym] = {
                "symbol": sym,
                "side": Side(row["side"]),
                "qty": float(row["qty"]),
                "entry_price": float(row["entry_price"]),
                "entry_ts": row["entry_ts"],
                "strategy": row["strategy"],
                "stop": row["stop"],
                "target": row["target"],
                "rationale": row["rationale"] or "",
                "horizon": (row["horizon"] if "horizon" in row.keys() and row["horizon"] else "day"),
            }

        last = self.db.last_equity()
        if last is not None and last.get("cash") is not None:
            # Restart-safe: recover cash directly from the last snapshot.
            self._cash = float(last["cash"])
            self.peak_equity = float(last.get("equity") or self._cash)
        else:
            # Cold start: derive cash from starting equity minus the cost of any
            # open positions we just loaded (long cost reduces cash, short
            # proceeds add cash) -- mirroring engine cash accounting.
            self._cash = self.starting_equity
            for pos in self._positions.values():
                notional = pos["entry_price"] * pos["qty"]
                if pos["side"] == Side.LONG:
                    self._cash -= notional
                else:
                    self._cash += notional
            self.peak_equity = self.starting_equity

    # ------------------------------------------------------------------ #
    # pricing                                                             #
    # ------------------------------------------------------------------ #
    def set_cycle_quotes(self, quote_map: Optional[dict[str, float]]) -> None:
        """Pin a per-cycle quote map. Fills served from these prices match
        exactly what the snapshot showed the agent. Pass ``None`` to clear."""
        if quote_map is None:
            self._cycle_quotes = None
        else:
            # Normalize keys to uppercase so callers can pass any case.
            self._cycle_quotes = {str(k).upper(): float(v) for k, v in quote_map.items()
                                  if v is not None}

    def latest_price(self, symbol: str) -> float:
        """Latest live quote, shared with the market-state snapshot.

        Prefers the cycle-pinned quote (so the broker fills at exactly the
        price the agent reasoned over); falls back to a live fetch.
        """
        sym = symbol.upper()
        if self._cycle_quotes is not None:
            pinned = self._cycle_quotes.get(sym)
            if pinned is not None:
                return float(pinned)
        px = quotes.get_quote(sym)
        if px is None:
            raise RuntimeError(f"No price data available for {sym}")
        return float(px)

    # ------------------------------------------------------------------ #
    # cost helpers (mirror engine semantics)                              #
    # ------------------------------------------------------------------ #
    def _entry_fill(self, side: Side, price: float) -> float:
        adj = self.cost.per_side_bps / 1e4
        return price * (1 + adj) if side == Side.LONG else price * (1 - adj)

    def _exit_fill(self, side: Side, price: float) -> float:
        adj = self.cost.per_side_bps / 1e4
        # exiting a long is a sell (worse = lower); exiting a short is a buy.
        return price * (1 - adj) if side == Side.LONG else price * (1 + adj)

    def _commission(self, qty: float) -> float:
        return max(
            self.cost.commission_min, qty * self.cost.commission_per_share
        )

    # ------------------------------------------------------------------ #
    # orders                                                              #
    # ------------------------------------------------------------------ #
    def open(
        self,
        symbol: str,
        side: Side,
        qty: float,
        stop: Optional[float] = None,
        target: Optional[float] = None,
        strategy: str = "agent",
        rationale: str = "",
        horizon: str = "day",
    ) -> dict:
        """Market entry at the latest live price plus slippage.

        ``horizon`` is the intended hold: 'day' (default; flattened at the close),
        'swing' (held for days), or 'long' (held weeks+). Non-day positions
        survive the EOD flatten and ride their stops.
        """
        side = Side(side)
        qty = float(qty)
        horizon = str(horizon).lower() if horizon else "day"
        if horizon not in ("day", "swing", "long"):
            horizon = "day"
        if qty <= 0:
            return self._fail(symbol, side, qty, "qty must be positive")
        if symbol in self._positions:
            return self._fail(symbol, side, qty, "position already open")

        try:
            raw = self.latest_price(symbol)
        except Exception as e:  # noqa: BLE001
            return self._fail(symbol, side, qty, f"price unavailable: {e}")

        fill = self._entry_fill(side, raw)
        notional = fill * qty
        commission = self._commission(qty)
        slip = abs(fill - raw) * qty

        if side == Side.LONG and (notional + commission) > self._cash:
            return self._fail(
                symbol, side, qty,
                f"insufficient cash: need {notional + commission:.2f}, have {self._cash:.2f}",
            )

        # cash accounting: long buys reduce cash; short sells add proceeds.
        if side == Side.LONG:
            self._cash -= notional + commission
        else:
            self._cash += notional - commission

        entry_ts = _now_iso()
        self._positions[symbol] = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": fill,
            "entry_ts": entry_ts,
            "strategy": strategy,
            "stop": stop,
            "target": target,
            "rationale": rationale,
            "horizon": horizon,
            # carried for realized-pnl accounting at close:
            "commission_paid": commission,
            "slippage_paid": slip,
        }
        self.db.upsert_position({
            "symbol": symbol,
            "side": side.value,
            "qty": qty,
            "entry_price": fill,
            "entry_ts": entry_ts,
            "strategy": strategy,
            "stop": stop,
            "target": target,
            "rationale": rationale,
            "horizon": horizon,
        })
        self.db.log_agent(strategy, "open", f"{side.value} {qty} {symbol} @ {fill:.4f} [{horizon}]")
        self._persist_equity()
        return {
            "ok": True,
            "symbol": symbol,
            "side": side.value,
            "qty": qty,
            "fill_price": fill,
            "reason": "",
        }

    def close(self, symbol: str, reason: str = "agent_close") -> dict:
        """Market exit at the latest live price plus slippage; records a trade."""
        pos = self._positions.get(symbol)
        if pos is None:
            return {"ok": False, "symbol": symbol, "reason": "no open position", "pnl": 0.0}

        try:
            raw = self.latest_price(symbol)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "symbol": symbol, "reason": f"price unavailable: {e}", "pnl": 0.0}

        side = pos["side"]
        qty = pos["qty"]
        exit_px = self._exit_fill(side, raw)
        commission = self._commission(qty)
        slip = abs(exit_px - raw) * qty

        # cash accounting: selling a long adds proceeds; covering a short pays.
        if side == Side.LONG:
            self._cash += exit_px * qty - commission
        else:
            self._cash -= exit_px * qty + commission

        direction = 1.0 if side == Side.LONG else -1.0
        gross = direction * (exit_px - pos["entry_price"]) * qty
        total_commission = pos.get("commission_paid", 0.0) + commission
        total_slip = pos.get("slippage_paid", 0.0) + slip
        pnl = gross - total_commission

        trade_id = self.db.record_trade({
            "symbol": symbol,
            "side": side.value,
            "strategy": pos.get("strategy"),
            "entry_ts": pos.get("entry_ts"),
            "entry_price": pos["entry_price"],
            "qty": qty,
            "exit_ts": _now_iso(),
            "exit_price": exit_px,
            "commission": total_commission,
            "slippage_cost": total_slip,
            "pnl": pnl,
            "exit_reason": reason,
            "rationale": pos.get("rationale", ""),
        })
        del self._positions[symbol]
        self.db.delete_position(symbol)
        self.db.log_agent(
            pos.get("strategy") or "agent", "close",
            f"{symbol} @ {exit_px:.4f} pnl={pnl:.2f} ({reason})",
        )
        self._persist_equity()
        return {
            "ok": True,
            "symbol": symbol,
            "side": side.value,
            "qty": qty,
            "exit_price": exit_px,
            "pnl": pnl,
            "trade_id": trade_id,
            "reason": reason,
        }

    def flatten_all(self, reason: str = "eod_flat",
                    horizons: Optional[set] = None) -> list[dict]:
        """Close open positions. If ``horizons`` is given, close only positions
        whose horizon is in that set (e.g. {"day"} at the close leaves swing/long
        holds running); otherwise close everything."""
        results = []
        for symbol in list(self._positions):
            if horizons is not None and self._positions[symbol].get("horizon", "day") not in horizons:
                continue
            results.append(self.close(symbol, reason=reason))
        return results

    # ------------------------------------------------------------------ #
    # state / reporting                                                   #
    # ------------------------------------------------------------------ #
    def _mark(self, symbol: str, fallback: float) -> float:
        try:
            return self.latest_price(symbol)
        except Exception:  # noqa: BLE001 - fall back to entry if data fails
            return fallback

    def positions(self) -> list[dict]:
        """Open positions with unrealized P&L marked at the latest price."""
        out = []
        for sym, pos in self._positions.items():
            mark = self._mark(sym, pos["entry_price"])
            direction = 1.0 if pos["side"] == Side.LONG else -1.0
            unrealized = direction * (mark - pos["entry_price"]) * pos["qty"]
            out.append({
                "symbol": sym,
                "side": pos["side"].value,
                "qty": pos["qty"],
                "entry_price": pos["entry_price"],
                "mark": mark,
                "unrealized_pnl": unrealized,
                "stop": pos.get("stop"),
                "target": pos.get("target"),
                "strategy": pos.get("strategy"),
                "horizon": pos.get("horizon", "day"),
                "rationale": pos.get("rationale", ""),
            })
        return out

    def cash(self) -> float:
        return self._cash

    def equity(self) -> float:
        """Cash plus mark-to-market value of all open positions."""
        eq = self._cash
        for sym, pos in self._positions.items():
            mark = self._mark(sym, pos["entry_price"])
            # _position_value expects an object with .side/.qty; emulate via a
            # tiny shim mirroring engine accounting for long/short.
            if pos["side"] == Side.LONG:
                eq += pos["qty"] * mark
            else:
                eq += -pos["qty"] * mark
        return eq

    def drawdown_pct(self) -> float:
        """Percent drawdown from the in-memory/db peak equity."""
        eq = self.equity()
        if eq > self.peak_equity:
            self.peak_equity = eq
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - eq) / self.peak_equity * 100.0

    def snapshot(self) -> dict:
        eq = self.equity()
        dd = self.drawdown_pct()
        return {
            "cash": self._cash,
            "equity": eq,
            "drawdown_pct": dd,
            "positions": self.positions(),
            "peak_equity": self.peak_equity,
        }

    def performance(self) -> dict:
        """Aggregate stats from recorded round-trip trades."""
        trades = [t for t in self.db.recent_trades(limit=100000) if t.get("pnl") is not None]
        n = len(trades)
        if n == 0:
            return {
                "n_trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "total_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
            }
        pnls = [float(t["pnl"]) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            profit_factor = gross_win / gross_loss
        else:
            profit_factor = float("inf") if gross_win > 0 else 0.0
        return {
            "n_trades": n,
            "win_rate": len(wins) / n,
            "profit_factor": profit_factor,
            "total_pnl": sum(pnls),
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        }

    # ------------------------------------------------------------------ #
    # internals                                                           #
    # ------------------------------------------------------------------ #
    def _persist_equity(self) -> None:
        """Snapshot cash + equity so a restart can recover exact cash."""
        eq = self.equity()
        if eq > self.peak_equity:
            self.peak_equity = eq
        dd = self.drawdown_pct()
        self.db.record_equity(self._cash, eq, len(self._positions), dd)

    def _fail(self, symbol: str, side: Side, qty: float, reason: str) -> dict:
        return {
            "ok": False,
            "symbol": symbol,
            "side": side.value if isinstance(side, Side) else str(side),
            "qty": qty,
            "fill_price": None,
            "reason": reason,
        }
