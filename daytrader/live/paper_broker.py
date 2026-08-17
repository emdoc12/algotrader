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

import logging
import os
from typing import Optional

from daytrader.backtest.engine import CostModel
from daytrader.core.types import Side
from daytrader.data import quotes
from daytrader.live.db import LiveDB, _now_iso

log = logging.getLogger(__name__)

def _envf(key: str, default: float) -> float:
    """Read a numeric rail at CALL time, so the dashboard's Settings tab takes
    effect on the next cycle instead of requiring a container restart."""
    try:
        v = os.environ.get(key)
        return float(v) if v not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)


def _envb(key: str, default: bool = True) -> bool:
    v = os.environ.get(key)
    if v in (None, ""):
        return default
    return v not in ("0", "false", "False")


# Risk rails that protect the paper account from oversized LLM orders. These are
# hard broker-level caps (the mission guides desks to size far tighter); an order
# breaching them is rejected with an actionable message the agent can act on.
# Kept as module constants for backwards compatibility; the live code paths read
# the _env* accessors so Settings-tab changes apply without a restart.
MAX_TRADE_RISK_PCT = _envf("MAX_TRADE_RISK_PCT", 1.5)   # entry→stop loss ≤ this % of equity
MAX_GROSS_EXPOSURE = _envf("MAX_GROSS_EXPOSURE", 2.0)   # Σ|position notional| ≤ this × equity
# Σ open risk (entry→stop across ALL positions) ≤ this % of equity. Per-trade
# sizing alone does not bound the account: eight "small" 1.5% trades that all
# fail together is a 12% day. Heat is what actually caps correlated exposure.
MAX_PORTFOLIO_HEAT_PCT = _envf("MAX_PORTFOLIO_HEAT_PCT", 8.0)
# Cooling-off: no NEW positions while equity is this far below its peak.
# Existing positions keep running their stops — this stops digging, not holding.
COOLDOWN_DRAWDOWN_PCT = _envf("COOLDOWN_DRAWDOWN_PCT", 8.0)
# Adding to a position that is UNDERWATER is averaging down. Off by default;
# a strategy that genuinely allows a defined adjustment can pass allow_average_down.
ALLOW_AVERAGE_DOWN = _envb("ALLOW_AVERAGE_DOWN", False)
REQUIRE_STOP = _envb("REQUIRE_STOP", True)

# Server-enforced scale-out: by default, bank AUTO_SCALE_DEFAULT_FRAC of a
# position at +AUTO_SCALE_DEFAULT_R and move the stop to breakeven. Set the frac
# to 0 (globally via AUTO_SCALE_DEFAULT_FRAC, or per-trade) to disable.
AUTO_SCALE_DEFAULT_R = float(os.environ.get("AUTO_SCALE_DEFAULT_R", "1.0"))
AUTO_SCALE_DEFAULT_FRAC = float(os.environ.get("AUTO_SCALE_DEFAULT_FRAC", "0.5"))
# How much realized loss may exceed planned risk before it's flagged (stop-through).
RISK_OVERRUN_MULT = float(os.environ.get("RISK_OVERRUN_MULT", "1.25"))
# How stops are enforced (surfaced to the desks for transparency).
STOP_POLL_SEC = int(os.environ.get("STOP_POLL_SECONDS", "120"))


def _load_json(raw):
    """Decode a JSON column, tolerating NULL / already-decoded / malformed values."""
    if raw is None or isinstance(raw, dict):
        return raw or None
    try:
        import json
        v = json.loads(raw)
        return v if isinstance(v, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _clean_adx_decay(cfg) -> Optional[dict]:
    """Normalize an ``adx_decay_exit`` config, or None if it asks for nothing.

    Mirrors the backtest engine's contract so a config validated in
    ``backtest_strategy`` behaves identically live.
    """
    if not isinstance(cfg, dict):
        return None
    out: dict = {}
    drop = cfg.get("adx_drop_from_peak")
    bars = cfg.get("negative_slope_bars")
    try:
        if drop is not None and float(drop) > 0:
            out["adx_drop_from_peak"] = float(drop)
    except (TypeError, ValueError):
        pass
    try:
        if bars is not None and int(bars) > 0:
            out["negative_slope_bars"] = int(bars)
    except (TypeError, ValueError):
        pass
    return out or None


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
        # SPY's direction ("up"/"down") for the current cycle, so entries can be
        # tagged with_trend / counter_trend at fill time.
        self._cycle_spy_direction: Optional[str] = None

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
                "trail_atr_mult": (row["trail_atr_mult"] if "trail_atr_mult" in row.keys() else None),
                "trail_pct": (row["trail_pct"] if "trail_pct" in row.keys() else None),
                "with_trend": (row["with_trend"] if "with_trend" in row.keys() else None),
                "init_stop": (row["init_stop"] if "init_stop" in row.keys() else row["stop"]),
                "planned_risk": (row["planned_risk"] if "planned_risk" in row.keys() else None),
                "auto_scale_r": (row["auto_scale_r"] if "auto_scale_r" in row.keys() else None),
                "auto_scale_frac": (row["auto_scale_frac"] if "auto_scale_frac" in row.keys() else None),
                "scaled": bool(row["scaled"]) if "scaled" in row.keys() and row["scaled"] else False,
                "adx_decay_exit": _load_json(row["adx_decay_exit"]) if "adx_decay_exit" in row.keys() else None,
                "adx_peak": (row["adx_peak"] if "adx_peak" in row.keys() else None),
                "adx_neg_bars": int(row["adx_neg_bars"] or 0) if "adx_neg_bars" in row.keys() else 0,
                "max_adds": (row["max_adds"] if "max_adds" in row.keys() else None),
                "adds_used": int(row["adds_used"] or 0) if "adds_used" in row.keys() else 0,
                "entry_ctx": _load_json(row["entry_ctx"]) if "entry_ctx" in row.keys() else None,
            }

        last = self.db.last_equity()
        if last is not None and last.get("cash") is not None:
            # Restart-safe: recover cash directly from the last snapshot.
            self._cash = float(last["cash"])
            # Drawdown peak must be the historical MAX, not just the last
            # snapshot, or a restart silently resets max-drawdown to zero.
            hist_peak = self.db.max_equity()
            self.peak_equity = max(float(last.get("equity") or self._cash),
                                   hist_peak if hist_peak is not None else 0.0)
        else:
            # Cold start: derive cash from starting equity minus the cost of any
            # open positions we just loaded (long cost reduces cash, short
            # proceeds add cash) -- mirroring engine cash accounting.
            self._cash = self.starting_equity
            for sym, pos in self._positions.items():
                mult = self._mult(sym)
                if mult != 1.0:
                    continue          # futures: margin pledged, cash untouched
                notional = pos["entry_price"] * pos["qty"]
                if pos["side"] == Side.LONG:
                    self._cash -= notional
                else:
                    self._cash += notional
            self.peak_equity = self.starting_equity

    # ------------------------------------------------------------------ #
    # pricing                                                             #
    # ------------------------------------------------------------------ #
    def set_cycle_context(self, spy_direction: Optional[str] = None,
                          breadth: Optional[dict] = None,
                          sectors: Optional[list] = None) -> None:
        """Per-cycle market context, stamped onto entries at fill time.

        Recording the TAPE at entry — not just the chart — is what lets
        ``get_performance_breakdown`` later answer "do these shorts only work on
        broad-down days?". Without it the question is unanswerable after the
        fact, because breadth is gone by the time the trade closes.
        """
        self._cycle_spy_direction = spy_direction
        self._cycle_breadth = breadth or None
        self._cycle_sectors = sectors or None

    def _entry_context(self, symbol: str) -> dict:
        """Breadth + this symbol's sector cluster, as of the current cycle."""
        from daytrader.core.breadth import bucket
        b = getattr(self, "_cycle_breadth", None) or {}
        pct = b.get("breadth_pct")
        out = {
            "breadth_pct": float(pct) if pct is not None else None,
            "breadth_advancers": b.get("advancers"),
            "breadth_total": b.get("total"),
            "breadth_bucket": b.get("breadth_bucket") or bucket(pct),
            "breadth_change_20m": b.get("breadth_change_20m"),
            "sector": None, "sector_avg_adx": None, "sector_pct_down": None,
        }
        try:
            from daytrader.live.market_state import _SECTORS
            from daytrader.core.breadth import sector_of
            sec = sector_of(symbol, _SECTORS)
            out["sector"] = sec
            for row in (getattr(self, "_cycle_sectors", None) or []):
                if row.get("sector") == sec:
                    out["sector_avg_adx"] = row.get("avg_adx")
                    # sector_clusters reports rsi/adx aggregates; pct EMA-down
                    # comes from the same cluster row when present.
                    out["sector_pct_down"] = row.get("pct_ema_down")
                    break
        except Exception:  # noqa: BLE001
            pass
        return out

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

    def _commission(self, qty: float, symbol: str | None = None) -> float:
        """Per-side commission. Futures bill per CONTRACT, equities per share."""
        if symbol is not None:
            from daytrader.core.contracts import commission as _fut_comm
            c = _fut_comm(symbol, qty)
            if c is not None:
                return c
        return max(self.cost.commission_min, qty * self.cost.commission_per_share)

    @staticmethod
    def _mult(symbol) -> float:
        """Dollars per 1.00 of price — 1.0 for equities (the share model)."""
        from daytrader.core.contracts import multiplier
        return multiplier(symbol)

    def margin_held(self) -> float:
        """Buying power tied up by open futures positions.

        Derived from the live position book rather than tracked incrementally,
        so a restart cannot desynchronize it.
        """
        from daytrader.core.contracts import initial_margin
        return sum(initial_margin(s, p["qty"]) for s, p in self._positions.items())

    def buying_power(self) -> float:
        """Cash not already pledged as futures margin or options collateral."""
        return self._cash - self.margin_held() - self.options.collateral_held()

    def portfolio_heat(self) -> float:
        """Σ open risk across every position, in dollars.

        Shares and futures contribute entry→stop; options contribute their
        computed MAX LOSS, which is the honest equivalent — the premium is not
        the risk.
        """
        total = self.options.open_risk()
        for sym, p in self._positions.items():
            stop = p.get("stop")
            if stop is None:
                continue
            total += abs(float(p["entry_price"]) - float(stop)) * float(p["qty"]) * self._mult(sym)
        return total

    def heat_pct(self) -> float:
        eq = self.equity()
        return (100.0 * self.portfolio_heat() / eq) if eq > 0 else 0.0

    def session_realized_pnl(self) -> float:
        """Realized P&L booked TODAY (ET), from closed round trips."""
        from daytrader.live import analytics as _an
        from daytrader.live.competition import _today_et
        try:
            today = _today_et()
        except Exception:  # noqa: BLE001
            return 0.0
        total = 0.0
        for tr in self.db.recent_trades(limit=500):
            ex = _an._to_et(tr.get("exit_ts"))
            if ex is None or str(ex.date()) != str(today):
                continue
            if tr.get("pnl") is not None:
                total += float(tr["pnl"])
        return total

    def risk_state(self) -> dict:
        """What the rails currently permit — surfaced so a desk sizes to the
        budget that actually remains instead of discovering it via a rejection."""
        eq = self.equity()
        heat = self.portfolio_heat()
        heat_cap = _envf("MAX_PORTFOLIO_HEAT_PCT", 8.0) / 100.0 * eq
        dd = self.drawdown_pct()
        cool = _envf("COOLDOWN_DRAWDOWN_PCT", 8.0)
        day_pnl = self.session_realized_pnl()
        day_cap = _envf("DAILY_LOSS_LIMIT_PCT", 3.0)
        return {
            "equity": round(eq, 2),
            "session_realized_pnl": round(day_pnl, 2),
            "daily_loss_limit_pct": day_cap,
            "daily_loss_remaining": round(max(0.0, day_cap / 100.0 * eq + day_pnl), 2),
            "daily_loss_limit_hit": bool(day_cap > 0 and eq > 0
                                         and day_pnl <= -(day_cap / 100.0 * eq)),
            "open_risk": round(heat, 2),
            "open_risk_pct": round(self.heat_pct(), 2),
            "heat_cap_pct": _envf("MAX_PORTFOLIO_HEAT_PCT", 8.0),
            "risk_budget_remaining": round(max(0.0, heat_cap - heat), 2),
            "max_trade_risk_pct": _envf("MAX_TRADE_RISK_PCT", 1.5),
            "drawdown_pct": round(dd, 2),
            "cooldown_at_drawdown_pct": cool,
            "in_cooldown": bool(dd >= cool),
            "options_open_risk": round(self.options.open_risk(), 2),
            "options_collateral_held": round(self.options.collateral_held(), 2),
            "buying_power": round(self.buying_power(), 2),
            "note": ("risk_budget_remaining is the dollars of NEW entry→stop risk you "
                     "may still add. In cooldown, new positions are blocked until "
                     "equity recovers; existing positions keep running their stops."),
        }

    # ------------------------------------------------------------------ #
    # orders                                                              #
    # ------------------------------------------------------------------ #
    def _persist_position(self, pos: dict) -> None:
        """Write the in-memory position to the DB (used on open and on every
        trailing-stop ratchet)."""
        self.db.upsert_position({
            "symbol": pos["symbol"],
            "side": pos["side"].value if hasattr(pos["side"], "value") else pos["side"],
            "qty": pos["qty"],
            "entry_price": pos["entry_price"],
            "entry_ts": pos.get("entry_ts"),
            "strategy": pos.get("strategy"),
            "stop": pos.get("stop"),
            "target": pos.get("target"),
            "rationale": pos.get("rationale", ""),
            "horizon": pos.get("horizon", "day"),
            "trail_atr_mult": pos.get("trail_atr_mult"),
            "trail_pct": pos.get("trail_pct"),
            "with_trend": pos.get("with_trend"),
            "init_stop": pos.get("init_stop"),
            "planned_risk": pos.get("planned_risk"),
            "auto_scale_r": pos.get("auto_scale_r"),
            "auto_scale_frac": pos.get("auto_scale_frac"),
            "scaled": 1 if pos.get("scaled") else 0,
            "adx_decay_exit": (__import__("json").dumps(pos["adx_decay_exit"])
                               if pos.get("adx_decay_exit") else None),
            "adx_peak": pos.get("adx_peak"),
            "adx_neg_bars": int(pos.get("adx_neg_bars") or 0),
            "max_adds": pos.get("max_adds"),
            "adds_used": int(pos.get("adds_used") or 0),
            "entry_ctx": (__import__("json").dumps(pos["entry_ctx"])
                          if pos.get("entry_ctx") else None),
        })

    def _audit(self, pos: dict, qty_closed: float, pnl: float):
        """(with_trend, planned_risk for this qty, risk_overrun flag). Planned
        risk is recomputed from the INITIAL stop so partials/scale-outs prorate
        cleanly; overrun flags a stop-through (realized loss >> planned)."""
        init_stop = pos.get("init_stop")
        if init_stop is None:
            init_stop = pos.get("stop")
        planned = abs(pos["entry_price"] - init_stop) * qty_closed if init_stop is not None else None
        overrun = 1 if (planned and pnl < 0 and (-pnl) > RISK_OVERRUN_MULT * planned) else 0
        return pos.get("with_trend"), (round(planned, 2) if planned is not None else None), overrun

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
        trail_atr_mult: Optional[float] = None,
        trail_pct: Optional[float] = None,
        auto_scale_r: Optional[float] = None,
        auto_scale_frac: Optional[float] = None,
        adx_decay_exit: Optional[dict] = None,
        max_adds: Optional[int] = None,
    ) -> dict:
        """Market entry at the latest live price plus slippage.

        ``horizon`` is the intended hold: 'day' (default; flattened at the close),
        'swing' (held for days), or 'long' (held weeks+). Non-day positions
        survive the EOD flatten and ride their stops.

        ``trail_atr_mult`` / ``trail_pct`` enable a server-side trailing stop that
        :meth:`manage_positions` ratchets in the favorable direction each cycle.

        ``auto_scale_r`` / ``auto_scale_frac`` enable server-enforced scale-out:
        when the mark reaches +auto_scale_r R (R = entry→initial-stop), the system
        banks auto_scale_frac of the position and moves the stop to breakeven.
        Defaults come from AUTO_SCALE_DEFAULT_*; set frac to 0 to disable.

        ``adx_decay_exit`` enables the same intra-trade regime-deterioration exit
        the backtest engine implements, e.g.
        ``{"adx_drop_from_peak": 5.0, "negative_slope_bars": 3}``:
        :meth:`manage_positions` force-closes the position once its ADX has
        fallen that far from its post-entry peak, or its ADX slope has been
        negative for that many consecutive cycles.
        """
        side = Side(side)
        qty = float(qty)
        horizon = str(horizon).lower() if horizon else "day"
        if horizon not in ("day", "swing", "long"):
            horizon = "day"
        trail_atr_mult = float(trail_atr_mult) if trail_atr_mult else None
        trail_pct = float(trail_pct) if trail_pct else None
        auto_scale_r = float(auto_scale_r) if auto_scale_r is not None else _envf("AUTO_SCALE_DEFAULT_R", 1.0)
        auto_scale_frac = float(auto_scale_frac) if auto_scale_frac is not None else _envf("AUTO_SCALE_DEFAULT_FRAC", 0.5)
        adx_decay_exit = _clean_adx_decay(adx_decay_exit)
        if qty <= 0:
            return self._fail(symbol, side, qty, "qty must be positive")
        # Defense in depth: staged orders and deployed strategies reach open()
        # without passing through the tool layer's check.
        from daytrader.live.tools import unsupported_instrument
        bad = unsupported_instrument(symbol)
        if bad:
            return self._fail(symbol, side, qty, bad)
        if symbol in self._positions:
            held = self._positions[symbol]
            same = held["side"] == side
            return self._fail(symbol, side, qty, (
                f"duplicate_symbol_position: {symbol} already has an open "
                f"{held['side'].value} position ({held['qty']:g} @ "
                f"{held['entry_price']:.2f}). "
                + ("Use add_to_position to scale in — it blends into one position "
                   "with a volume-weighted entry, so you keep the runner and the "
                   "trailing ratchet."
                   if same else
                   "This order is the OPPOSITE side, which is a reduce or a flip — "
                   "use take_partial or close_position explicitly.")))

        try:
            raw = self.latest_price(symbol)
        except Exception as e:  # noqa: BLE001
            return self._fail(symbol, side, qty, f"price unavailable: {e}")

        from daytrader.core.contracts import initial_margin, spec_for
        spec = spec_for(symbol)          # None for equities/ETFs (share model)
        fill = self._entry_fill(side, raw)
        mult = self._mult(symbol)
        notional = fill * qty * mult
        commission = self._commission(qty, symbol)
        slip = abs(fill - raw) * qty * mult

        # ---- risk rails (reject oversized / unsafe orders) ------------------
        if _envb("REQUIRE_STOP", True) and stop is None:
            return self._fail(symbol, side, qty,
                              "a protective stop is required on every entry")
        if stop is not None:
            if side == Side.LONG and stop >= fill:
                return self._fail(symbol, side, qty,
                                  f"long stop {stop:.2f} must be BELOW entry {fill:.2f}")
            if side == Side.SHORT and stop <= fill:
                return self._fail(symbol, side, qty,
                                  f"short stop {stop:.2f} must be ABOVE entry {fill:.2f}")
        if target is not None:
            if side == Side.LONG and target <= fill:
                return self._fail(symbol, side, qty,
                                  f"long target {target:.2f} must be ABOVE entry {fill:.2f}")
            if side == Side.SHORT and target >= fill:
                return self._fail(symbol, side, qty,
                                  f"short target {target:.2f} must be BELOW entry {fill:.2f}")
        eq = self.equity()
        # Daily loss limit. Bounds the DAY, which the peak-to-trough cooling-off
        # rule does not: an account can lose 3% today, recover, and lose 3% again
        # tomorrow without ever tripping a drawdown threshold.
        day_cap = _envf("DAILY_LOSS_LIMIT_PCT", 3.0)
        if day_cap > 0 and eq > 0:
            day_pnl = self.session_realized_pnl()
            if day_pnl <= -(day_cap / 100.0 * eq):
                return self._fail(
                    symbol, side, qty,
                    f"daily_loss_limit: today's realized P&L is ${day_pnl:,.0f}, at or past "
                    f"the {day_cap:.1f}% daily loss limit (${day_cap / 100.0 * eq:,.0f}). "
                    "You are done INITIATING for the day — manage what is open and write up "
                    "in the journal what went wrong. New entries resume tomorrow.")
        # Cooling-off. A desk deep in drawdown is the one most likely to try to
        # trade its way out; this blocks NEW risk while letting existing
        # positions keep running their stops.
        cool = _envf("COOLDOWN_DRAWDOWN_PCT", 8.0)
        dd = self.drawdown_pct()
        if cool > 0 and dd >= cool:
            return self._fail(
                symbol, side, qty,
                f"cooling_off: equity is {dd:.1f}% below its peak, at or beyond the "
                f"{cool:.1f}% cooling-off threshold. No NEW positions until equity "
                "recovers; open positions keep running their stops. Review what is "
                "not working before adding risk.")
        if stop is not None and eq > 0:
            risk_pct = _envf("MAX_TRADE_RISK_PCT", 1.5)
            risk_amt = abs(fill - stop) * qty * mult
            cap = risk_pct / 100.0 * eq
            if risk_amt > cap:
                return self._fail(
                    symbol, side, qty,
                    f"trade risk ${risk_amt:,.0f} exceeds the {risk_pct:.1f}% cap "
                    f"(${cap:,.0f}); reduce qty or tighten the stop")
            # Portfolio heat. Per-trade sizing does not bound the account:
            # eight "small" 1.5% trades that fail together is a 12% day.
            heat_pct_cap = _envf("MAX_PORTFOLIO_HEAT_PCT", 8.0)
            if heat_pct_cap > 0:
                heat = self.portfolio_heat()
                heat_cap = heat_pct_cap / 100.0 * eq
                if (heat + risk_amt) > heat_cap:
                    return self._fail(
                        symbol, side, qty,
                        f"portfolio_heat: open risk is already ${heat:,.0f} "
                        f"({100.0 * heat / eq:.1f}% of equity) and this order adds "
                        f"${risk_amt:,.0f}, exceeding the {heat_pct_cap:.1f}% heat cap "
                        f"(${heat_cap:,.0f}). ${max(0.0, heat_cap - heat):,.0f} of new "
                        "risk remains — size to that, close something, or tighten stops. "
                        "Call get_risk_state to see the budget before sizing.")
        if eq > 0:
            from daytrader.live.tastytrade_margin import equity_buying_power_multiple
            mirroring = equity_buying_power_multiple() > 1.0
            # The gross line must not silently undercut a mirrored buying-power
            # multiple — a 4x day-trading line means nothing behind a 2x cap.
            gross_cap = max(_envf("MAX_GROSS_EXPOSURE", 2.0), equity_buying_power_multiple())
            # Under mirrored broker terms futures are governed by MARGIN, not
            # notional — that is how tastytrade actually works, and a notional
            # cap mis-governs them badly (one MES is $37.6k of notional against
            # ~$100 of stop risk). Off by default, so the standing competition
            # keeps its original notional rail.
            gross = sum(abs(p["qty"]) * self._mark(s, p["entry_price"]) * self._mult(s)
                        for s, p in self._positions.items()
                        if not (mirroring and spec_for(s) is not None))
            counts_toward_gross = not (mirroring and spec is not None)
            projected = gross + (notional if counts_toward_gross else 0.0)
            if projected > gross_cap * eq:
                return self._fail(
                    symbol, side, qty,
                    f"gross exposure ${projected:,.0f} would exceed "
                    f"{gross_cap:.1f}x equity (${gross_cap * eq:,.0f}); reduce size")

        # ---- funding: margin for futures, cash for equities -----------------
        margin_req = initial_margin(symbol, qty)
        if spec is not None:
            # A futures position is not bought — margin is PLEDGED against it.
            # Cash moves only by commission; the notional never leaves the
            # account, which is exactly why the share model got this so wrong.
            avail = self.buying_power()
            if (margin_req + commission) > avail:
                return self._fail(
                    symbol, side, qty,
                    f"insufficient buying power for {qty:g} {spec.name}: needs "
                    f"${margin_req + commission:,.0f} margin, ${avail:,.0f} available "
                    f"(${self._cash:,.0f} cash less ${self.margin_held():,.0f} already "
                    f"pledged). Notional would be ${notional:,.0f}.")
            self._cash -= commission
        elif side == Side.LONG:
            # Equity buying power. 1.0x is the cash model (the long-standing
            # default); mirroring the owner's Reg-T account raises it to ~2x, or
            # ~4x on day-trading buying power.
            from daytrader.live.tastytrade_margin import equity_buying_power_multiple
            bp_mult = equity_buying_power_multiple()
            if bp_mult > 1.0:
                # A margin buy is a LOAN, not free funding: cash still pays the
                # full notional and may go negative (a debit balance), exactly
                # as it does at a real broker. Only the LIMIT changes — from
                # "cash on hand" to "a multiple of equity". Keeping the cash
                # mechanics symmetric is what stops the close from crediting
                # proceeds that were never paid, i.e. minting money.
                held = sum(p["qty"] * self._mark(s, p["entry_price"])
                           for s, p in self._positions.items()
                           if p["side"] == Side.LONG and self._mult(s) == 1.0)
                limit = bp_mult * eq
                if (held + notional + commission) > limit:
                    return self._fail(
                        symbol, side, qty,
                        f"exceeds equity buying power: ${held + notional:,.0f} of long "
                        f"exposure vs a ${limit:,.0f} line ({bp_mult:.1f}x equity)")
            elif (notional + commission) > self._cash:
                return self._fail(
                    symbol, side, qty,
                    f"insufficient cash: need {notional + commission:.2f}, have {self._cash:.2f}",
                )
            self._cash -= notional + commission
        else:
            self._cash += notional - commission

        entry_ts = _now_iso()
        # Judged on EFFECTIVE market direction, so a long in an inverse ETF
        # (SQQQ/SOXS/…) on a down tape is correctly tagged with_trend.
        from daytrader.live.analytics import with_trend_label
        with_trend = with_trend_label(symbol, side, self._cycle_spy_direction)
        planned_risk = abs(fill - stop) * qty if stop is not None else None
        entry_ctx = self._entry_context(symbol)
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
            "trail_atr_mult": trail_atr_mult,
            "trail_pct": trail_pct,
            "with_trend": with_trend,
            "init_stop": stop,
            "planned_risk": planned_risk,
            "auto_scale_r": auto_scale_r,
            "auto_scale_frac": auto_scale_frac,
            "scaled": False,
            "adx_decay_exit": adx_decay_exit,
            "adx_peak": None,      # highest ADX seen since entry
            "adx_neg_bars": 0,     # consecutive cycles with a negative ADX slope
            "max_adds": int(max_adds) if max_adds is not None else None,
            "adds_used": 0,
            "entry_ctx": entry_ctx,
            # carried for realized-pnl accounting at close:
            "commission_paid": commission,
            "slippage_paid": slip,
        }
        self._persist_position(self._positions[symbol])
        trail = f" trail={trail_atr_mult}xATR" if trail_atr_mult else (f" trail={trail_pct}%" if trail_pct else "")
        self.db.log_agent(strategy, "open", f"{side.value} {qty} {symbol} @ {fill:.4f} [{horizon}]{trail}")
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
        mult = self._mult(symbol)
        commission = self._commission(qty, symbol)
        slip = abs(exit_px - raw) * qty * mult
        direction = 1.0 if side == Side.LONG else -1.0
        gross = direction * (exit_px - pos["entry_price"]) * qty * mult

        if mult != 1.0:
            # Futures settle in cash: the margin is released (it was pledged,
            # never spent) and only the realized P&L moves the balance.
            self._cash += gross - commission
        elif side == Side.LONG:
            self._cash += exit_px * qty - commission
        else:
            self._cash -= exit_px * qty + commission
        total_commission = pos.get("commission_paid", 0.0) + commission
        total_slip = pos.get("slippage_paid", 0.0) + slip
        pnl = gross - total_commission
        wt, planned_risk, overrun = self._audit(pos, qty, pnl)

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
            "with_trend": wt,
            "planned_risk": planned_risk,
            "risk_overrun": overrun,
            # Entry-time tape context, so the breakdown can later ask whether
            # this setup only works on a broad-down day.
            **(pos.get("entry_ctx") or {}),
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

    def reduce_position(self, symbol: str, fraction: float,
                        reason: str = "partial_take") -> dict:
        """Close a FRACTION (0<f<1) of an open position — a partial take-profit —
        leaving a runner. f>=1 closes it fully. Books the closed portion as a
        trade and prorates carried entry costs onto it."""
        pos = self._positions.get(symbol)
        if pos is None:
            return {"ok": False, "symbol": symbol, "reason": "no open position", "pnl": 0.0}
        try:
            fraction = float(fraction)
        except (TypeError, ValueError):
            return {"ok": False, "symbol": symbol, "reason": "fraction must be a number"}
        if fraction >= 1:
            return self.close(symbol, reason=reason)
        if fraction <= 0:
            return {"ok": False, "symbol": symbol, "reason": "fraction must be in (0,1)"}
        try:
            raw = self.latest_price(symbol)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "symbol": symbol, "reason": f"price unavailable: {e}", "pnl": 0.0}

        side = pos["side"]
        qty_total = pos["qty"]
        qty_close = qty_total * fraction
        exit_px = self._exit_fill(side, raw)
        mult = self._mult(symbol)
        commission = self._commission(qty_close, symbol)
        slip = abs(exit_px - raw) * qty_close * mult
        direction = 1.0 if side == Side.LONG else -1.0
        gross = direction * (exit_px - pos["entry_price"]) * qty_close * mult
        if mult != 1.0:
            self._cash += gross - commission      # futures: cash-settled
        elif side == Side.LONG:
            self._cash += exit_px * qty_close - commission
        else:
            self._cash -= exit_px * qty_close + commission
        # Prorate the entry commission/slippage carried on the position.
        share = qty_close / qty_total
        entry_comm = pos.get("commission_paid", 0.0) * share
        entry_slip = pos.get("slippage_paid", 0.0) * share
        pnl = gross - (entry_comm + commission)
        wt, planned_risk, overrun = self._audit(pos, qty_close, pnl)
        trade_id = self.db.record_trade({
            "symbol": symbol, "side": side.value, "strategy": pos.get("strategy"),
            "entry_ts": pos.get("entry_ts"), "entry_price": pos["entry_price"],
            "qty": qty_close, "exit_ts": _now_iso(), "exit_price": exit_px,
            "commission": entry_comm + commission, "slippage_cost": entry_slip + slip,
            "pnl": pnl, "exit_reason": reason, "rationale": pos.get("rationale", ""),
            "with_trend": wt, "planned_risk": planned_risk, "risk_overrun": overrun,
            **(pos.get("entry_ctx") or {}),
        })
        # Shrink the remaining position and its carried costs.
        pos["qty"] = qty_total - qty_close
        pos["commission_paid"] = pos.get("commission_paid", 0.0) - entry_comm
        pos["slippage_paid"] = pos.get("slippage_paid", 0.0) - entry_slip
        self._persist_position(pos)
        self.db.log_agent(pos.get("strategy") or "agent", "partial_close",
                          f"{symbol} {fraction:.0%} @ {exit_px:.4f} pnl={pnl:.2f} ({reason})")
        self._persist_equity()
        return {"ok": True, "symbol": symbol, "closed_qty": round(qty_close, 6),
                "remaining_qty": round(pos["qty"], 6), "exit_price": exit_px,
                "pnl": round(pnl, 2), "trade_id": trade_id}

    def add_to_position(self, symbol: str, qty: float, stop: Optional[float] = None,
                        target: Optional[float] = None,
                        auto_scale_r: Optional[float] = None,
                        auto_scale_frac: Optional[float] = None,
                        allow_average_down: bool = False,
                        rationale: str = "") -> dict:
        """Scale INTO an existing position, blending into one averaged position.

        Building in tranches is the whole point: initiate on confirmation, add on
        the first pullback that holds structure. Closing and re-entering at a
        larger size is not a substitute — it surrenders the runner, pays two
        extra sets of spread and slippage, and resets the trailing ratchet.

        The blend is a volume-weighted average entry with summed quantity, so the
        position stays a single row with a single stop/target. Risk is re-checked
        at the POSITION level against the blended entry, because that — not the
        tranche — is what is actually at risk.

        Adds are SAME-DIRECTION only. An opposite-direction order against an open
        position is a reduce or a flip, which must stay explicit (take_partial /
        close_position), never an implicit side effect of an add.
        """
        symbol = str(symbol).upper()
        pos = self._positions.get(symbol)
        if pos is None:
            return {"ok": False, "symbol": symbol, "error_code": "no_open_position",
                    "reason": (f"no open {symbol} position to add to — use place_trade "
                               "to initiate one")}
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            return {"ok": False, "symbol": symbol, "error_code": "bad_qty",
                    "reason": "qty must be a number"}
        if qty <= 0:
            return {"ok": False, "symbol": symbol, "error_code": "bad_qty",
                    "reason": "qty must be positive; to reduce use take_partial"}

        side = pos["side"]
        max_adds = pos.get("max_adds")
        adds_used = int(pos.get("adds_used") or 0)
        if max_adds is not None and adds_used >= int(max_adds):
            return {"ok": False, "symbol": symbol, "error_code": "max_adds_reached",
                    "reason": (f"{symbol} has used all {int(max_adds)} planned adds "
                               f"(max_adds set at initiation); close or re-plan")}

        try:
            raw = self.latest_price(symbol)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "symbol": symbol, "error_code": "price_unavailable",
                    "reason": f"price unavailable: {e}"}

        # No averaging down. Adding to a position that is already underwater
        # improves the average but increases the loss — it is the single most
        # reliable way to turn a small mistake into an account-level one.
        # Scaling INTO strength is still allowed, which is the point of tranching.
        if not (allow_average_down or ALLOW_AVERAGE_DOWN):
            underwater = ((side == Side.LONG and raw < float(pos["entry_price"]))
                          or (side == Side.SHORT and raw > float(pos["entry_price"])))
            if underwater:
                return {"ok": False, "symbol": symbol, "error_code": "averaging_down",
                        "reason": (f"{symbol} is underwater (mark {raw:.2f} vs entry "
                                   f"{float(pos['entry_price']):.2f}) — adding here is "
                                   "averaging down, which is blocked. Add on strength, or "
                                   "pass allow_average_down=true only if your DECLARED "
                                   "strategy defines this adjustment.")}

        from daytrader.core.contracts import initial_margin, spec_for
        spec = spec_for(symbol)
        mult = self._mult(symbol)
        fill = self._entry_fill(side, raw)
        add_notional = fill * qty * mult
        commission = self._commission(qty, symbol)
        slip = abs(fill - raw) * qty * mult

        old_qty = float(pos["qty"])
        new_qty = old_qty + qty
        blended = (float(pos["entry_price"]) * old_qty + fill * qty) / new_qty
        eff_stop = float(stop) if stop is not None else pos.get("stop")

        # ---- risk rails, evaluated on the BLENDED position ------------------
        if eff_stop is not None:
            if side == Side.LONG and eff_stop >= blended:
                return {"ok": False, "symbol": symbol, "error_code": "bad_stop",
                        "reason": (f"long stop {eff_stop:.2f} must be BELOW the blended "
                                   f"entry {blended:.2f} (adding raised your average)")}
            if side == Side.SHORT and eff_stop <= blended:
                return {"ok": False, "symbol": symbol, "error_code": "bad_stop",
                        "reason": (f"short stop {eff_stop:.2f} must be ABOVE the blended "
                                   f"entry {blended:.2f} (adding lowered your average)")}
        eq = self.equity()
        total_risk = (abs(blended - eff_stop) * new_qty * mult) if eff_stop is not None else None
        if eff_stop is not None and eq > 0:
            cap = _envf("MAX_TRADE_RISK_PCT", 1.5) / 100.0 * eq
            if total_risk > cap:
                return {"ok": False, "symbol": symbol, "error_code": "risk_cap",
                        "reason": (f"blended position risk ${total_risk:,.0f} "
                                   f"({new_qty:g} @ {blended:.2f} → stop {eff_stop:.2f}) "
                                   f"exceeds the {_envf('MAX_TRADE_RISK_PCT', 1.5):.1f}% cap "
                                   f"(${cap:,.0f}); add less or tighten the stop"),
                        "blended_entry": round(blended, 4), "total_qty": new_qty,
                        "total_planned_risk": round(total_risk, 2)}
            # Heat, measured on the portfolio AFTER the blend: the existing
            # position's contribution is replaced, not added to.
            heat_pct_cap = _envf("MAX_PORTFOLIO_HEAT_PCT", 8.0)
            if heat_pct_cap > 0:
                old_risk = (abs(float(pos["entry_price"]) - float(pos["stop"])) * old_qty * mult
                            if pos.get("stop") is not None else 0.0)
                projected = self.portfolio_heat() - old_risk + total_risk
                heat_cap = heat_pct_cap / 100.0 * eq
                if projected > heat_cap:
                    return {"ok": False, "symbol": symbol, "error_code": "portfolio_heat",
                            "reason": (f"this add would put total open risk at ${projected:,.0f} "
                                       f"({100.0 * projected / eq:.1f}% of equity), past the "
                                       f"{heat_pct_cap:.1f}% heat cap (${heat_cap:,.0f}). "
                                       "Add less, tighten stops, or close another position."),
                            "projected_open_risk": round(projected, 2),
                            "heat_cap": round(heat_cap, 2)}

        # ---- funding ---------------------------------------------------------
        if spec is not None:
            need = initial_margin(symbol, qty) + commission
            avail = self.buying_power()
            if need > avail:
                return {"ok": False, "symbol": symbol, "error_code": "insufficient_margin",
                        "reason": (f"add needs ${need:,.0f} margin, ${avail:,.0f} available")}
            self._cash -= commission
        elif side == Side.LONG:
            from daytrader.live.tastytrade_margin import equity_buying_power_multiple
            bp_mult = equity_buying_power_multiple()
            if bp_mult <= 1.0 and (add_notional + commission) > self._cash:
                return {"ok": False, "symbol": symbol, "error_code": "insufficient_cash",
                        "reason": (f"add needs ${add_notional + commission:,.2f}, "
                                   f"${self._cash:,.2f} cash available")}
            self._cash -= add_notional + commission
        else:
            self._cash += add_notional - commission

        # ---- blend -----------------------------------------------------------
        pos["qty"] = new_qty
        pos["entry_price"] = round(blended, 6)
        if stop is not None:
            pos["stop"] = float(stop)
        if target is not None:
            pos["target"] = float(target)
        # R is measured from the BLENDED entry to the stop in force after the add,
        # so every R-based mechanism (auto-scale, breakeven) reflects the position
        # that actually exists rather than the first tranche.
        pos["init_stop"] = pos.get("stop")
        pos["planned_risk"] = round(total_risk, 2) if total_risk is not None else None
        pos["adds_used"] = adds_used + 1
        pos["commission_paid"] = float(pos.get("commission_paid", 0.0)) + commission
        pos["slippage_paid"] = float(pos.get("slippage_paid", 0.0)) + slip
        if auto_scale_r is not None:
            pos["auto_scale_r"] = float(auto_scale_r)
        if auto_scale_frac is not None:
            pos["auto_scale_frac"] = float(auto_scale_frac)
        # Re-arm the scale-out against the new blended level. A position that
        # already scaled once is materially different after an add, and leaving
        # it disarmed would silently drop the protection. Pass auto_scale_frac=0
        # to keep a core hold unmanaged.
        rearmed = bool(pos.get("scaled")) and float(pos.get("auto_scale_frac") or 0) > 0
        if rearmed:
            pos["scaled"] = False
        self._persist_position(pos)
        self._persist_equity()
        self.db.log_agent(pos.get("strategy") or "agent", "add_to_position",
                          f"{symbol} +{qty:g} @ {fill:.4f} -> {new_qty:g} @ {blended:.4f}")

        out = {
            "ok": True, "symbol": symbol, "side": side.value,
            "added_qty": qty, "add_fill_price": round(fill, 4),
            "total_qty": new_qty,
            "blended_entry": round(blended, 4),
            "stop": pos.get("stop"), "target": pos.get("target"),
            "total_planned_risk": round(total_risk, 2) if total_risk is not None else None,
            "risk_pct_of_equity": round(100.0 * total_risk / eq, 3) if (total_risk and eq > 0) else None,
            "adds_used": pos["adds_used"], "max_adds": max_adds,
            "commission": round(commission, 4),
            "auto_scale_r": pos.get("auto_scale_r"),
            "auto_scale_frac": pos.get("auto_scale_frac"),
            "note": (f"Blended into ONE position: {new_qty:g} @ {blended:.4f}. R is now "
                     f"measured from the blended entry."
                     + (" Scale-out re-armed at the new level." if rearmed else "")
                     + (" auto_scale_frac=0 → no server-side scaling on this hold."
                        if float(pos.get("auto_scale_frac") or 0) == 0 else "")),
        }
        if rationale:
            out["rationale"] = rationale
        return out

    def modify_position(self, symbol: str, stop: Optional[float] = None,
                        target: Optional[float] = None) -> dict:
        """Modify an open position's protective stop and/or target."""
        pos = self._positions.get(symbol)
        if pos is None:
            return {"ok": False, "symbol": symbol, "reason": "no open position"}
        changed = {}
        if stop is not None:
            pos["stop"] = float(stop)
            changed["stop"] = pos["stop"]
        if target is not None:
            pos["target"] = float(target)
            changed["target"] = pos["target"]
        if not changed:
            return {"ok": False, "symbol": symbol, "reason": "provide a stop and/or target to modify"}
        self._persist_position(pos)
        self.db.log_agent(pos.get("strategy") or "agent", "modify_stops", f"{symbol} {changed}")
        return {"ok": True, "symbol": symbol, **changed}

    def move_stop_to_breakeven(self, symbol: str) -> dict:
        """Set the protective stop to the entry price (lock in a no-loss runner)."""
        pos = self._positions.get(symbol)
        if pos is None:
            return {"ok": False, "symbol": symbol, "reason": "no open position"}
        be = round(float(pos["entry_price"]), 4)
        pos["stop"] = be
        self._persist_position(pos)
        self.db.log_agent(pos.get("strategy") or "agent", "breakeven", f"{symbol} stop->{be:.4f}")
        return {"ok": True, "symbol": symbol, "stop": be, "note": "stop moved to entry (breakeven)"}

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

    def manage_positions(self, quote_map: Optional[dict] = None,
                         atr_map: Optional[dict] = None,
                         adx_map: Optional[dict] = None) -> list[dict]:
        """Server-side bracket management, run once per trade cycle.

        For each open position: (1) ratchet a trailing stop in the favorable
        direction (by ``trail_atr_mult`` * ATR, or ``trail_pct`` of price), then
        (2) auto-close if the current mark has hit the stop or the target, and
        (3) force-close on ADX decay when the trade opted into ``adx_decay_exit``.
        This lets winners run on a trailing stop and protects the open gain
        without the agent having to babysit every cycle.

        ``adx_map`` is {symbol: {"adx14": float, "adx_slope": float}} for this
        cycle — only needed for positions carrying an ``adx_decay_exit``.

        Granularity is the trade cycle (not intrabar), so fills are at the
        current mark when a level is breached — honest about between-cycle gap
        risk. Returns a list of {symbol, action, ...} events.
        """
        quote_map = {str(k).upper(): v for k, v in (quote_map or {}).items() if v is not None}
        atr_map = {str(k).upper(): v for k, v in (atr_map or {}).items() if v is not None}
        adx_map = {str(k).upper(): v for k, v in (adx_map or {}).items() if v is not None}
        events: list[dict] = []
        for sym in list(self._positions):
            pos = self._positions.get(sym)
            if pos is None:
                continue
            mark = quote_map.get(sym)
            if mark is None:
                try:
                    mark = self.latest_price(sym)
                except Exception:  # noqa: BLE001
                    continue
            mark = float(mark)
            side = pos["side"]

            # 0) server-enforced scale-out at +Nx R: bank a fraction and move the
            #    stop to breakeven once the trade reaches its reward multiple.
            frac = pos.get("auto_scale_frac")
            r_mult = pos.get("auto_scale_r")
            init_stop = pos.get("init_stop") if pos.get("init_stop") is not None else pos.get("stop")
            if (not pos.get("scaled") and frac and frac > 0 and r_mult and r_mult > 0
                    and init_stop is not None):
                risk_ps = abs(pos["entry_price"] - init_stop)
                if risk_ps > 0:
                    tgt = (pos["entry_price"] + r_mult * risk_ps if side == Side.LONG
                           else pos["entry_price"] - r_mult * risk_ps)
                    reached = mark >= tgt if side == Side.LONG else mark <= tgt
                    if reached:
                        res = self.reduce_position(sym, frac, reason=f"auto_scale_{r_mult:g}R")
                        # position may still exist (partial); mark it + breakeven
                        if sym in self._positions:
                            self._positions[sym]["scaled"] = True
                            self.move_stop_to_breakeven(sym)
                        events.append({"symbol": sym, "action": "auto_scale",
                                       "closed": res.get("closed_qty"), "pnl": res.get("pnl")})
                        pos = self._positions.get(sym)
                        if pos is None:
                            continue

            # 1) ratchet trailing stop (only ever tightens toward price)
            trail_dist = None
            if pos.get("trail_atr_mult") and atr_map.get(sym):
                trail_dist = float(pos["trail_atr_mult"]) * float(atr_map[sym])
            elif pos.get("trail_pct"):
                trail_dist = mark * float(pos["trail_pct"]) / 100.0
            if trail_dist and trail_dist > 0:
                cur = pos.get("stop")
                if side == Side.LONG:
                    new_stop = mark - trail_dist
                    if cur is None or new_stop > cur:
                        pos["stop"] = round(new_stop, 4)
                        self._persist_position(pos)
                        events.append({"symbol": sym, "action": "trail_stop", "stop": pos["stop"]})
                else:
                    new_stop = mark + trail_dist
                    if cur is None or new_stop < cur:
                        pos["stop"] = round(new_stop, 4)
                        self._persist_position(pos)
                        events.append({"symbol": sym, "action": "trail_stop", "stop": pos["stop"]})

            # 2) auto-execute stop / target at the current mark
            stop, target = pos.get("stop"), pos.get("target")
            hit = None
            if side == Side.LONG:
                if stop is not None and mark <= stop:
                    hit = "stop"
                elif target is not None and mark >= target:
                    hit = "target"
            else:
                if stop is not None and mark >= stop:
                    hit = "stop"
                elif target is not None and mark <= target:
                    hit = "target"
            if hit:
                res = self.close(sym, reason=f"auto_{hit}")
                events.append({"symbol": sym, "action": hit, "pnl": res.get("pnl")})
                continue

            # 3) ADX-decay exit — the regime deteriorated under the trade. Same
            #    contract as the backtest engine's adx_decay_exit, so a config
            #    validated in a backtest behaves identically live.
            decay = self._check_adx_decay(sym, pos, adx_map.get(sym))
            if decay:
                res = self.close(sym, reason="auto_adx_decay")
                events.append({"symbol": sym, "action": "adx_decay",
                               "reason": decay, "pnl": res.get("pnl")})

        # 4) Options: profit targets, DTE exits and expiration settlement.
        #    Hooked in here rather than at each runner call site so every path
        #    that manages the share book manages the options book too — an
        #    expiring short put must be settled whether or not an agent ran.
        try:
            for ev in self.options.manage():
                if ev and ev.get("ok"):
                    events.append({"symbol": ev.get("underlying"),
                                   "action": "option_" + str(
                                       ev.get("reason") or ("expiration" if ev.get("settled")
                                                            else "close")),
                                   "option_id": ev.get("id"), "pnl": ev.get("pnl")})
        except Exception as e:  # noqa: BLE001 - never let options break the share book
            log.warning("options management failed: %s", e)
        return events

    def _check_adx_decay(self, sym: str, pos: dict, info) -> Optional[str]:
        """Update this position's ADX peak / negative-slope streak and report why
        an ``adx_decay_exit`` fired (or None). Tracking state is persisted so a
        restart mid-trade doesn't reset the peak and silently disarm the exit."""
        cfg = pos.get("adx_decay_exit")
        if not cfg or not isinstance(info, dict):
            return None
        adx = info.get("adx14")
        slope = info.get("adx_slope")
        if adx is None:
            return None
        try:
            adx = float(adx)
        except (TypeError, ValueError):
            return None

        peak = pos.get("adx_peak")
        peak = adx if peak is None else max(float(peak), adx)
        pos["adx_peak"] = peak
        try:
            neg = (int(pos.get("adx_neg_bars") or 0) + 1) if (
                slope is not None and float(slope) < 0) else 0
        except (TypeError, ValueError):
            neg = 0
        pos["adx_neg_bars"] = neg
        self._persist_position(pos)

        drop_cfg = cfg.get("adx_drop_from_peak")
        if drop_cfg is not None and (peak - adx) >= float(drop_cfg):
            return (f"ADX {adx:.1f} fell {peak - adx:.1f} from post-entry peak "
                    f"{peak:.1f} (limit {float(drop_cfg):.1f})")
        bars_cfg = cfg.get("negative_slope_bars")
        if bars_cfg is not None and neg >= int(bars_cfg):
            return f"ADX slope negative {neg} consecutive cycles (limit {int(bars_cfg)})"
        return None

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
            unrealized = direction * (mark - pos["entry_price"]) * pos["qty"] * self._mult(sym)
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
                "trail_atr_mult": pos.get("trail_atr_mult"),
                "trail_pct": pos.get("trail_pct"),
                "with_trend": pos.get("with_trend"),
                "planned_risk": pos.get("planned_risk"),
                "scaled": bool(pos.get("scaled")),
                "auto_scale_r": pos.get("auto_scale_r"),
                "auto_scale_frac": pos.get("auto_scale_frac"),
                "adx_decay_exit": pos.get("adx_decay_exit"),
                "adx_peak": pos.get("adx_peak"),
                "adx_neg_bars": pos.get("adx_neg_bars") or 0,
                "max_adds": pos.get("max_adds"),
                "adds_used": int(pos.get("adds_used") or 0),
                "rationale": pos.get("rationale", ""),
            })
        return out

    def capital_base(self) -> float:
        """Starting equity plus net owner deposits — the denominator for return.

        A deposit changes what you were GIVEN, not what you EARNED. Measuring
        return against the original stake after an injection would book the
        transfer as profit, which is the one thing it definitely is not.
        """
        try:
            return self.starting_equity + self.db.capital_contributed()
        except Exception:  # noqa: BLE001
            return self.starting_equity

    def deposit(self, amount: float, reason: str = "owner deposit") -> dict:
        """Add owner capital. Explicitly NOT P&L.

        Cash rises, and so do the drawdown peak and the day's risk anchor by the
        same amount — otherwise the injection would read as a profitable day and
        would quietly hand the desk a fresh daily-loss budget.
        """
        amount = float(amount)
        if amount == 0:
            return {"ok": False, "reason": "amount must be non-zero"}
        before_eq = self.equity()
        self._cash += amount
        self.peak_equity = max(0.0, self.peak_equity + amount)
        self.db.add_capital_event(amount, reason)
        self._persist_equity()
        after_eq = self.equity()
        return {"ok": True, "amount": amount, "reason": reason,
                "equity_before": round(before_eq, 2), "equity_after": round(after_eq, 2),
                "capital_base": round(self.capital_base(), 2),
                "note": ("Recorded as a capital event, not a trade. Return % is measured "
                         "against the new capital base, so this shows as $0 P&L.")}

    def cash(self) -> float:
        return self._cash

    def equity(self) -> float:
        """Cash plus mark-to-market value of all open positions."""
        eq = self._cash
        for sym, pos in self._positions.items():
            mark = self._mark(sym, pos["entry_price"])
            mult = self._mult(sym)
            if mult != 1.0:
                # Futures: cash was never debited by the notional (margin is
                # pledged, not spent), so the position contributes its
                # unrealized P&L. Adding market value here would double-count
                # the whole contract value into equity.
                direction = 1.0 if pos["side"] == Side.LONG else -1.0
                eq += direction * (mark - pos["entry_price"]) * pos["qty"] * mult
            elif pos["side"] == Side.LONG:
                eq += pos["qty"] * mark
            else:
                eq += -pos["qty"] * mark
        # Options: cash already moved by the opening credit/debit, so an open
        # structure contributes its market value. A short structure marks
        # NEGATIVE (buying it back costs money), which is precisely why a credit
        # received is not profit until it decays.
        eq += self.options.unrealized()
        return eq

    @property
    def options(self):
        """The desk's options book (lazily built, one per broker)."""
        book = getattr(self, "_options_book", None)
        if book is None:
            from daytrader.live.options_book import OptionsBook
            book = OptionsBook(self)
            self._options_book = book
        return book

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
        held = self.margin_held()
        out = {
            "cash": self._cash,
            "equity": eq,
            "drawdown_pct": dd,
            "positions": self.positions(),
            "peak_equity": self.peak_equity,
        }
        try:
            from daytrader.live.tastytrade_margin import describe as _mdesc
            terms = _mdesc()
            if terms.get("source") == "tastytrade":
                out["margin_terms"] = terms
        except Exception:  # noqa: BLE001
            pass
        if held > 0:
            out["margin_held"] = round(held, 2)
            out["buying_power"] = round(self._cash - held, 2)
            out["margin_note"] = (
                "Futures margin is PLEDGED, not spent: cash still shows the full balance "
                "but only buying_power can fund new positions. A futures position's "
                "equity contribution is its unrealized P&L, not its notional.")
        return out

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
        # Log the rejection. Without this a desk that TRIES to trade every cycle
        # and is refused by a rail is indistinguishable from a desk that chose to
        # stand aside — both simply show no positions, which is why six idle
        # desks looked like they were "doing god knows what".
        try:
            self.db.log_agent("broker", "rejected",
                              f"{side.value if isinstance(side, Side) else side} "
                              f"{qty:g} {symbol}: {reason}"[:500])
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": False,
            "symbol": symbol,
            "side": side.value if isinstance(side, Side) else str(side),
            "qty": qty,
            "fill_price": None,
            "reason": reason,
        }
