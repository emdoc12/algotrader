"""Paper options trading: open, mark, manage and settle multi-leg positions.

The share book and the options book are deliberately separate. An option is not
a share with a different price — it has a multiplier, an expiration, collateral
that is not the premium, and an ending that can hand you stock you did not ask
for. Bolting that onto the equity position model would have produced a system
that *looked* like it traded options while pricing them as shares, which is the
one outcome worse than not supporting them.

What this module guarantees:

  * **Defined risk only.** Every structure's worst case is computed from its
    payoff (see :mod:`daytrader.core.options`) before the order is accepted. A
    naked short call is rejected, by arithmetic rather than by pattern-matching
    the strategy's name.
  * **Collateral is really held.** A cash-secured put ties up the strike, a
    vertical ties up its width. It comes out of buying power for as long as the
    position is open, so a desk cannot sell ten puts it could never be assigned on.
  * **Expiration actually happens.** ITM shorts are assigned into real share
    positions, longs are exercised, OTM contracts expire worthless. That is what
    makes the wheel a strategy rather than a story: sell the put, get assigned,
    own the shares, sell the call.
  * **P&L lands in the same books as everything else.** A closed structure is
    written to the ``trades`` table, so the leaderboard, performance stats and
    per-strategy breakdown include options without any of them knowing.

Cash convention throughout: a credit is positive cash, a debit is negative.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

from daytrader.core.options import (
    OptionLeg, OptionError, contract, describe, net_cash, parse_occ,
    risk_measure, structure_risk,
)

log = logging.getLogger(__name__)

# Per-contract commission, both ways, mirroring a retail options schedule.
OPTION_COMMISSION = float(os.environ.get("OPTION_COMMISSION_PER_CONTRACT", "0.65"))
# Cap on how much of the account one options structure may tie up.
MAX_OPTION_COLLATERAL_PCT = float(os.environ.get("MAX_OPTION_COLLATERAL_PCT", "25"))
# Per-trade risk cap for OPTIONS specifically. Deliberately looser than the
# share cap: a share stop can be honored, whereas an assigned put hands you a
# position you then manage, and the collateral cap already bounds how much of
# the account can be committed. At 1.5% the wheel is arithmetically unrunnable
# on any underlying above about $40 in a $50k account.
MAX_OPTION_RISK_PCT = float(os.environ.get("MAX_OPTION_RISK_PCT", "5.0"))
# Adverse move used to size structures whose downside is the underlying itself.
OPTION_STRESS_MOVE_PCT = float(os.environ.get("OPTION_STRESS_MOVE_PCT", "20"))


class OptionOrderError(ValueError):
    """A rejected options order, with a message the desk can act on."""


def _now_iso() -> str:
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _today() -> date:
    try:
        from daytrader.live.competition import _today_et
        y, m, d = (int(x) for x in str(_today_et())[:10].split("-"))
        return date(y, m, d)
    except Exception:  # noqa: BLE001
        return date.today()


# --------------------------------------------------------------------------- #
# leg construction from agent input                                           #
# --------------------------------------------------------------------------- #
def build_legs(underlying: str, raw_legs: list[dict], quote_fn=None) -> list[OptionLeg]:
    """Turn agent-supplied leg dicts into priced :class:`OptionLeg` objects.

    Each dict needs ``expiration``, ``strike``, ``right`` and ``qty`` (signed, or
    unsigned with ``action`` of buy/sell). ``price`` may be supplied; otherwise
    ``quote_fn(contract)`` is asked for a live mark. Refusing to invent a price
    is the point — a structure priced from a guess would produce a P&L series
    that means nothing.
    """
    if not raw_legs:
        raise OptionOrderError("no legs given")
    if len(raw_legs) > 4:
        raise OptionOrderError("at most 4 legs per structure")
    out: list[OptionLeg] = []
    for i, raw in enumerate(raw_legs):
        raw = dict(raw or {})
        try:
            if raw.get("occ"):
                c = parse_occ(raw["occ"])
            else:
                c = contract(raw.get("underlying") or underlying,
                             raw.get("expiration"), raw.get("strike"),
                             raw.get("right") or raw.get("type"))
        except OptionError as e:
            raise OptionOrderError(f"leg {i + 1}: {e}") from e

        qty = raw.get("qty", raw.get("contracts", 1))
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            raise OptionOrderError(f"leg {i + 1}: qty must be a number") from None
        action = str(raw.get("action") or raw.get("side") or "").lower()
        if action in ("sell", "short", "sell_to_open", "stc", "sto"):
            qty = -abs(qty)
        elif action in ("buy", "long", "buy_to_open", "bto", "btc"):
            qty = abs(qty)
        if qty == 0:
            raise OptionOrderError(f"leg {i + 1}: qty cannot be zero")

        price = raw.get("price")
        if price is None and quote_fn is not None:
            price = quote_fn(c)
        if price is None:
            raise OptionOrderError(
                f"leg {i + 1} ({c}): no market price available. Pull the chain with "
                "get_option_chain first and pass the leg prices you can actually "
                "trade at — a structure priced from a guess produces a P&L that "
                "means nothing.")
        try:
            out.append(OptionLeg(c, qty, float(price)))
        except OptionError as e:
            raise OptionOrderError(f"leg {i + 1}: {e}") from e
    return out


# --------------------------------------------------------------------------- #
# the book                                                                    #
# --------------------------------------------------------------------------- #
class OptionsBook:
    """Manages a desk's open option structures against its paper account.

    Holds a reference to the broker rather than subclassing it, so the equity
    book keeps working exactly as it did and options are strictly additive.
    """

    def __init__(self, broker):
        self.broker = broker
        self.db = broker.db

    # -- pricing ----------------------------------------------------------- #
    def _chain_price(self, c) -> float | None:
        """Mid price for one contract from the live chain, or None."""
        try:
            from daytrader.live import tastytrade_data as tt
            if not tt.is_configured():
                return None
            dte = c.dte(_today())
            chain = tt.get_option_chain(
                c.underlying, max_expirations=6,
                strikes_around_atr=40, min_dte=max(0, dte - 2), max_dte=dte + 2,
                strike_pct_window=35.0)
            block = chain.get(str(c.expiration))
            if not block:
                return None
            slot = (block.get("strikes") or {}).get(str(float(c.strike))) \
                or (block.get("strikes") or {}).get(str(c.strike))
            if not slot:
                return None
            leg = slot.get("call" if c.is_call() else "put") or {}
            bid, ask = leg.get("bid"), leg.get("ask")
            if bid is not None and ask is not None and ask > 0:
                return (float(bid) + float(ask)) / 2.0
            return float(ask or bid) if (ask or bid) else None
        except Exception as e:  # noqa: BLE001
            log.info("options: chain price for %s failed (%s)", c, e)
            return None

    def mark_leg(self, c, fallback: float | None = None) -> float | None:
        """Current per-share value of a contract.

        When no quote is available the fallback is the leg's LAST TRADED price,
        not its intrinsic value. Intrinsic is only the right answer at
        expiration: using it while a contract still has weeks of life left
        marks every out-of-the-money short to zero, which books the entire
        credit as profit the instant the position opens. That is not a
        conservative estimate, it is manufactured P&L — a credit received is
        not earned until it decays.

        So: quote first; intrinsic only once the contract has expired; otherwise
        hold the position flat at what it was worth when last priced.
        """
        px = self._chain_price(c)
        if px is not None:
            return px
        if c.dte(_today()) <= 0:
            spot = self._spot(c.underlying)
            if spot is not None:
                return c.intrinsic(spot)
        return fallback

    def covering_shares(self, underlying: str, exclude_id: int | None = None) -> tuple[float, float]:
        """Shares available to cover short calls in ``underlying``: (qty, basis).

        Shares already pledged against another open short call are netted out,
        so the same 100 shares cannot cover two calls — which would quietly make
        the second one naked.
        """
        from daytrader.core.types import Side
        pos = self.broker._positions.get(underlying)
        if pos is None or pos.get("side") != Side.LONG:
            return 0.0, 0.0
        held = float(pos["qty"])
        pledged = 0.0
        for row in self.db.open_option_positions():
            if row["underlying"] != underlying or row["id"] == exclude_id:
                continue
            try:
                for l in self._legs_of(row):
                    if l.is_short and l.contract.is_call():
                        pledged += abs(l.qty) * l.contract.multiplier
            except Exception:  # noqa: BLE001
                continue
        return max(0.0, held - pledged), float(pos["entry_price"])

    def _spot(self, underlying: str) -> float | None:
        try:
            return float(self.broker.latest_price(underlying))
        except Exception:  # noqa: BLE001
            return None

    # -- open -------------------------------------------------------------- #
    def open_structure(self, underlying: str, raw_legs: list[dict],
                       strategy: str = "options", rationale: str = "",
                       profit_target_pct: float | None = 50.0,
                       dte_exit: int | None = 21) -> dict:
        """Open a multi-leg options position. Returns a result dict, never raises."""
        underlying = str(underlying or "").upper().strip()
        try:
            legs = build_legs(underlying, raw_legs,
                              quote_fn=lambda c: self.mark_leg(c))
        except OptionOrderError as e:
            return {"ok": False, "error_code": "bad_legs", "reason": str(e)}

        underlyings = {l.contract.underlying for l in legs}
        if len(underlyings) > 1:
            return {"ok": False, "error_code": "mixed_underlyings",
                    "reason": f"all legs must share one underlying, got {sorted(underlyings)}"}
        underlying = underlyings.pop()

        today = _today()
        expired = [str(l.contract) for l in legs if l.contract.dte(today) < 0]
        if expired:
            return {"ok": False, "error_code": "expired_contract",
                    "reason": f"already expired: {', '.join(expired)}"}

        # Shares the desk already owns cover short calls written against them.
        # Without this a covered call — the second half of every wheel — is
        # indistinguishable from a naked short call and gets rejected.
        # Only short calls NOT already capped by a long call need share cover.
        # An iron condor's short call is covered by its own long wing, and
        # demanding shares for it would reject the structure outright.
        short_calls = sum(abs(l.qty) for l in legs if l.is_short and l.contract.is_call())
        long_calls = sum(abs(l.qty) for l in legs if not l.is_short and l.contract.is_call())
        uncovered = max(0.0, short_calls - long_calls) * 100.0
        shares, share_basis = (0.0, 0.0)
        if uncovered:
            avail, basis = self.covering_shares(underlying)
            shares = min(avail, uncovered)
            share_basis = basis
            if shares < uncovered:
                return {"ok": False, "error_code": "uncovered_short_call",
                        "reason": (f"this writes {uncovered / 100:g} uncovered call(s) against "
                                   f"{avail / 100:g} uncommitted round lot(s) of {underlying}. "
                                   "A short call needs either 100 shares per contract or a "
                                   "long call above it — otherwise the loss is unbounded. "
                                   "Buy the shares first (that is the wheel), or make it a "
                                   "vertical by buying a higher strike.")}

        info = describe(legs, shares, share_basis)
        if not info["defined_risk"]:
            return {"ok": False, "error_code": "undefined_risk",
                    "reason": (f"{info['structure']} has UNBOUNDED loss and is not "
                               "permitted. Every options position must be defined "
                               "risk — buy a further-out strike to cap it."),
                    "structure": info}

        max_loss = float(info["max_loss"] or 0.0)
        coll = float(info["collateral"])
        eq = self.broker.equity()
        commission = OPTION_COMMISSION * sum(abs(l.qty) for l in legs)
        spot = self._spot(underlying)
        rm = risk_measure(legs, spot, OPTION_STRESS_MOVE_PCT, shares, share_basis)
        risk = float(rm["risk"])
        info["risk_measure"] = rm

        # ---- risk rails, on the same terms as a share trade ---------------- #
        if eq > 0:
            per_trade = _envf("MAX_OPTION_RISK_PCT", MAX_OPTION_RISK_PCT) / 100.0 * eq
            if risk > per_trade:
                return {"ok": False, "error_code": "risk_cap",
                        "reason": (f"risk ${risk:,.0f} ({rm['basis']}: {rm['note']}) exceeds "
                                   f"the {_envf('MAX_OPTION_RISK_PCT', MAX_OPTION_RISK_PCT):.1f}% "
                                   f"per-trade options cap (${per_trade:,.0f}). Trade fewer "
                                   "contracts, pick a lower-priced underlying, or narrow the "
                                   "spread — for options the RISK is the structure's loss, "
                                   "never the premium collected."),
                        "risk": risk, "max_loss": max_loss, "structure": info}
            heat_cap_pct = _envf("MAX_PORTFOLIO_HEAT_PCT", 8.0)
            if heat_cap_pct > 0:
                heat = self.broker.portfolio_heat()   # already includes open options
                if (heat + risk) > heat_cap_pct / 100.0 * eq:
                    return {"ok": False, "error_code": "portfolio_heat",
                            "reason": (f"open risk is ${heat:,.0f} and this structure adds "
                                       f"${risk:,.0f}, past the {heat_cap_pct:.1f}% heat "
                                       f"cap (${heat_cap_pct / 100.0 * eq:,.0f})."),
                            "structure": info}
            coll_cap = MAX_OPTION_COLLATERAL_PCT / 100.0 * eq
            if coll > coll_cap:
                return {"ok": False, "error_code": "collateral_cap",
                        "reason": (f"this ties up ${coll:,.0f} of collateral, over the "
                                   f"{MAX_OPTION_COLLATERAL_PCT:.0f}% per-structure cap "
                                   f"(${coll_cap:,.0f}). Note collateral is NOT the premium: "
                                   "a cash-secured put holds the whole strike."),
                        "collateral": coll, "structure": info}

        # ---- cooling-off / daily limit apply to options too ---------------- #
        block = self._blocked_by_account_rails(eq)
        if block:
            return block

        opened_cash = net_cash(legs)
        if shares:
            # The shares are the collateral; they are already paid for and are
            # sitting in the equity book. Charging cash again would double-count.
            coll = 0.0
            info["collateral"] = 0.0
        available = self.buying_power()
        need = coll + commission - max(0.0, opened_cash)
        if need > available:
            return {"ok": False, "error_code": "insufficient_buying_power",
                    "reason": (f"{info['structure']} needs ${coll:,.0f} collateral "
                               f"(+${commission:.2f} commission) and ${available:,.0f} is "
                               f"available. Collateral is what the position TIES UP, not "
                               f"the ${abs(opened_cash):,.0f} of premium."),
                    "collateral": coll, "buying_power": round(available, 2)}

        # ---- book it -------------------------------------------------------- #
        self.broker._cash += opened_cash - commission
        # Did the engine independently see a live price for any leg? If not, the
        # structure was priced entirely from what the desk supplied, which is
        # allowed — refusing the whole lane because the stream is down would be
        # worse — but it must be recorded rather than silently assumed live.
        priced_live = any(self._chain_price(l.contract) is not None for l in legs[:1])
        rec = {
            "underlying": underlying,
            "structure": info["structure"],
            "legs": json.dumps([l.to_dict() for l in legs]),
            "opened_ts": _now_iso(),
            "open_cash": round(opened_cash, 2),
            "collateral": round(coll, 2),
            "max_loss": round(risk, 2),
            "max_profit": info["max_profit"],
            "expiration": str(min(l.contract.expiration for l in legs)),
            "strategy": strategy,
            "rationale": str(rationale or "")[:1000],
            "open_commission": round(commission, 2),
            "profit_target_pct": profit_target_pct,
            "dte_exit": dte_exit,
            "status": "open",
        }
        pid = self.db.add_option_position(rec)
        try:
            self.db.log_agent(strategy, "open_option",
                              f"{info['structure']} {underlying} #{pid} "
                              f"net {'credit' if opened_cash > 0 else 'debit'} "
                              f"${abs(opened_cash):,.2f}")
            self.broker._persist_equity()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "id": pid, "underlying": underlying,
                "structure": info["structure"],
                "net_credit": round(opened_cash, 2) if opened_cash > 0 else 0.0,
                "net_debit": round(-opened_cash, 2) if opened_cash < 0 else 0.0,
                "collateral": round(coll, 2), "risk": round(risk, 2),
                "risk_basis": rm["basis"], "risk_note": rm["note"],
                "max_loss": round(max_loss, 2),
                "max_profit": info["max_profit"], "breakevens": info["breakevens"],
                "commission": round(commission, 2),
                "expiration": rec["expiration"],
                "legs": [str(l.contract) + f" x{l.qty:+g} @ {l.price:.2f}" for l in legs],
                "managed": self._management_note(profit_target_pct, dte_exit),
                **({} if priced_live else {"quote_warning": (
                    "OPENED ON UNVERIFIED PRICES — no live chain quote was available to "
                    "cross-check your leg prices, so the fill used exactly what you passed. "
                    "The structure and its risk are computed correctly from those numbers, "
                    "but if they came from a stale/historical chain the real fill would "
                    "differ. Mark-to-market will hold this position flat until live quotes "
                    "return rather than invent a value.")})}

    def _blocked_by_account_rails(self, eq: float) -> dict | None:
        """Cooling-off and daily-loss checks, shared with the share book."""
        try:
            day_cap = _envf("DAILY_LOSS_LIMIT_PCT", 3.0)
            if day_cap > 0 and eq > 0:
                day_pnl = self.broker.session_realized_pnl()
                if day_pnl <= -(day_cap / 100.0 * eq):
                    return {"ok": False, "error_code": "daily_loss_limit",
                            "reason": (f"today's realized P&L is ${day_pnl:,.0f}, at the "
                                       f"{day_cap:.1f}% daily loss limit. No new positions "
                                       "of any kind today.")}
            cool = _envf("COOLDOWN_DRAWDOWN_PCT", 8.0)
            dd = self.broker.drawdown_pct()
            if cool > 0 and dd >= cool:
                return {"ok": False, "error_code": "cooling_off",
                        "reason": (f"equity is {dd:.1f}% below its peak, past the "
                                   f"{cool:.1f}% cooling-off threshold. No new positions "
                                   "until equity recovers.")}
        except Exception:  # noqa: BLE001
            return None
        return None

    @staticmethod
    def _management_note(profit_target_pct, dte_exit) -> str:
        bits = []
        if profit_target_pct:
            bits.append(f"auto-close at {profit_target_pct:g}% of max profit")
        if dte_exit is not None:
            bits.append(f"auto-close at {int(dte_exit)} DTE")
        return " and ".join(bits) if bits else "no automatic management — you must close it"

    # -- state -------------------------------------------------------------- #
    def _legs_of(self, row: dict) -> list[OptionLeg]:
        legs = []
        for d in json.loads(row["legs"]):
            c = parse_occ(d["occ"]) if d.get("occ") else contract(
                d["underlying"], d["expiration"], d["strike"], d["right"])
            legs.append(OptionLeg(c, float(d["qty"]), float(d.get("price") or 0.0)))
        return legs

    def market_value(self, legs: list[OptionLeg]) -> float:
        """Signed mark-to-market value of the structure (longs +, shorts −)."""
        total = 0.0
        for l in legs:
            px = self.mark_leg(l.contract, fallback=l.price)
            total += l.qty * float(px or 0.0) * l.contract.multiplier
        return total

    def collateral_held(self) -> float:
        return sum(float(r.get("collateral") or 0.0) for r in self.db.open_option_positions())

    def open_risk(self) -> float:
        """Σ sized risk across open structures — options' contribution to heat.

        This is the same measure the order was approved against (see
        :func:`daytrader.core.options.risk_measure`), so a position cannot pass
        the gate on one definition of risk and then be carried under another.
        """
        return sum(float(r.get("max_loss") or 0.0) for r in self.db.open_option_positions())

    def unrealized(self) -> float:
        """Mark-to-market P&L across open structures.

        Cash already moved by the opening credit/debit, so the position's
        contribution to equity is its current market value: a short structure
        carries a negative market value (buying it back costs money), which is
        exactly how the credit received stops being profit until it decays.
        """
        total = 0.0
        for row in self.db.open_option_positions():
            try:
                total += self.market_value(self._legs_of(row))
            except Exception:  # noqa: BLE001
                continue
        return total

    def buying_power(self) -> float:
        """Cash free of futures margin AND options collateral.

        The broker already nets both out, so this must not subtract collateral a
        second time — doing so would halve the account's apparent capacity every
        time a structure was open.
        """
        return self.broker.buying_power()

    def positions(self) -> list[dict]:
        out = []
        today = _today()
        for row in self.db.open_option_positions():
            try:
                legs = self._legs_of(row)
            except Exception:  # noqa: BLE001
                continue
            mv = self.market_value(legs)
            open_cash = float(row.get("open_cash") or 0.0)
            # P&L if closed now: what you took in, less what it costs to close.
            pnl = open_cash + mv
            max_profit = row.get("max_profit")
            pct = (100.0 * pnl / float(max_profit)) if max_profit else None
            dte = min(l.contract.dte(today) for l in legs)
            out.append({
                "id": row["id"], "underlying": row["underlying"],
                "structure": row["structure"], "strategy": row.get("strategy"),
                "opened_ts": row.get("opened_ts"),
                "expiration": row.get("expiration"), "dte": dte,
                "net_credit_received": round(open_cash, 2) if open_cash > 0 else 0.0,
                "net_debit_paid": round(-open_cash, 2) if open_cash < 0 else 0.0,
                "cost_to_close": round(-mv, 2),
                "unrealized_pnl": round(pnl, 2),
                "pct_of_max_profit": round(pct, 1) if pct is not None else None,
                "max_loss": row.get("max_loss"), "max_profit": max_profit,
                "collateral": row.get("collateral"),
                "legs": [f"{l.contract} x{l.qty:+g}" for l in legs],
                "rationale": row.get("rationale"),
            })
        return out

    # -- close -------------------------------------------------------------- #
    def close_structure(self, pid: int, reason: str = "agent_close") -> dict:
        row = self.db.get_option_position(pid)
        if row is None or row.get("status") != "open":
            return {"ok": False, "error_code": "no_such_position",
                    "reason": f"no OPEN option position #{pid}"}
        legs = self._legs_of(row)
        mv = self.market_value(legs)
        commission = OPTION_COMMISSION * sum(abs(l.qty) for l in legs)
        open_cash = float(row.get("open_cash") or 0.0)
        # Closing means reversing the position: you receive its market value.
        close_cash = mv
        # Both sides of the commission belong to the trade. Charging only the
        # closing leg overstates every result by the opening fee.
        total_comm = commission + float(row.get("open_commission") or 0.0)
        pnl = open_cash + close_cash - total_comm
        self.broker._cash += close_cash - commission
        self.db.close_option_position(pid, close_cash, pnl, reason)
        self._record_trade(row, legs, open_cash, close_cash, pnl, total_comm, reason)
        try:
            self.broker._persist_equity()
            self.db.log_agent(row.get("strategy") or "options", "close_option",
                              f"#{pid} {row['structure']} pnl ${pnl:,.2f} ({reason})")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "id": pid, "structure": row["structure"],
                "underlying": row["underlying"],
                "cost_to_close": round(-close_cash, 2),
                "pnl": round(pnl, 2), "reason": reason}

    def _record_trade(self, row, legs, open_cash, close_cash, pnl, commission, reason):
        """Write the round trip into the shared trades table.

        Options land in the same ledger as shares and futures on purpose: the
        leaderboard, profit factor and per-strategy breakdown then include them
        without any of those needing to know options exist.
        """
        try:
            qty = max(abs(l.qty) for l in legs)
            self.db.record_trade({
                "symbol": f"{row['underlying']} {row['structure']}",
                "side": "short" if open_cash > 0 else "long",
                "strategy": row.get("strategy") or "options",
                "entry_ts": row.get("opened_ts"),
                "entry_price": round(abs(open_cash) / max(qty, 1) / 100.0, 4),
                "qty": qty,
                "exit_ts": _now_iso(),
                "exit_price": round(abs(close_cash) / max(qty, 1) / 100.0, 4),
                "commission": round(commission, 2),
                "slippage_cost": 0.0,
                "pnl": round(pnl, 2),
                "exit_reason": reason,
                "rationale": row.get("rationale") or "",
            })
        except Exception as e:  # noqa: BLE001
            log.warning("options: could not record trade for #%s (%s)", row.get("id"), e)

    # -- lifecycle ---------------------------------------------------------- #
    def manage(self) -> list[dict]:
        """Per-cycle management: profit targets, DTE exits, and expiration.

        Called by the runner alongside the share book's ``manage_positions``, so
        an options desk is managed even on cycles where no agent runs. Without
        this, a 50%-profit rule is a note in a journal rather than a rule.
        """
        actions = []
        today = _today()
        for row in list(self.db.open_option_positions()):
            try:
                legs = self._legs_of(row)
            except Exception:  # noqa: BLE001
                continue
            dte = min(l.contract.dte(today) for l in legs)

            if dte < 0:
                actions.append(self.settle_expiration(row["id"]))
                continue

            mv = self.market_value(legs)
            pnl = float(row.get("open_cash") or 0.0) + mv
            target = row.get("profit_target_pct")
            max_profit = row.get("max_profit")
            if target and max_profit and float(max_profit) > 0:
                if pnl >= float(target) / 100.0 * float(max_profit):
                    actions.append(self.close_structure(
                        row["id"], f"profit_target_{float(target):g}pct"))
                    continue
            dte_exit = row.get("dte_exit")
            if dte_exit is not None and dte <= int(dte_exit):
                # Gamma risk rises sharply into the last weeks; the standard
                # management rule closes there rather than holding for the last
                # few dollars of a credit.
                actions.append(self.close_structure(row["id"], f"dte_exit_{int(dte_exit)}"))
        return [a for a in actions if a]

    def settle_expiration(self, pid: int) -> dict:
        """Settle an expired structure: assign shorts, exercise longs, expire the rest.

        This is the part that makes the wheel real. A short put that finishes ITM
        does not simply pay its loss — it delivers 100 shares per contract at the
        strike, and the desk now owns stock it must manage (and can sell calls
        against). Cash-settling that away would quietly turn the wheel into a
        premium-selling simulator.
        """
        row = self.db.get_option_position(pid)
        if row is None or row.get("status") != "open":
            return {"ok": False, "reason": f"no open option position #{pid}"}
        legs = self._legs_of(row)
        underlying = row["underlying"]
        spot = self._spot(underlying)
        if spot is None:
            return {"ok": False, "reason": f"cannot settle #{pid}: no {underlying} price"}

        cash_delta = 0.0
        shares_delta = 0.0
        basis_cost = 0.0
        basis_shares = 0.0
        events = []
        for l in legs:
            c = l.contract
            itm = c.intrinsic(spot) > 0
            if not itm:
                events.append(f"{c} x{l.qty:+g} expired worthless")
                continue
            # Assigned/exercised: shares move at the strike.
            sign = 1.0 if c.is_call() else -1.0        # calls deliver shares long
            shares = sign * l.qty * c.multiplier       # +buy, -sell
            cash_delta -= shares * c.strike
            shares_delta += shares
            if shares > 0:
                # Acquisition price is the STRIKE — that is what was paid. Using
                # the market price would understate the cost basis of assigned
                # stock and hide the loss until it was sold.
                basis_cost += shares * c.strike
                basis_shares += shares
            what = "exercised" if l.qty > 0 else "ASSIGNED"
            events.append(f"{c} x{l.qty:+g} {what} at {c.strike:g} "
                          f"({shares:+g} shares, ${-shares * c.strike:+,.0f} cash)")

        self.broker._cash += cash_delta
        # An assignment is a TRANSFER at the strike, not a loss on the option.
        # The option's realized P&L is the premium it collected (or the debit it
        # paid); the shares arrive at the strike as their cost basis and carry
        # their own P&L in the equity book from there. Booking the share move as
        # an option loss as well would count the same dollars twice — which it
        # did, reporting -$811 of trades against a -$14 account move.
        open_cash = float(row.get("open_cash") or 0.0)
        pnl = open_cash - float(row.get("open_commission") or 0.0)
        self.db.close_option_position(pid, cash_delta, pnl, "expiration")
        self._record_trade(row, legs, open_cash, 0.0, pnl,
                           float(row.get("open_commission") or 0.0), "expiration")

        if shares_delta:
            acq = (basis_cost / basis_shares) if basis_shares else spot
            self._deliver_shares(underlying, shares_delta, acq,
                                 row.get("strategy") or "options",
                                 strike_price=(abs(cash_delta) / abs(shares_delta)))
        try:
            self.broker._persist_equity()
            self.db.log_agent(row.get("strategy") or "options", "expire_option",
                              f"#{pid} {row['structure']} @ {spot:.2f}: " + "; ".join(events))
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "id": pid, "settled": True, "underlying": underlying,
                "spot_at_expiration": round(spot, 2), "events": events,
                "shares_delivered": shares_delta, "cash_delta": round(cash_delta, 2),
                "pnl": round(pnl, 2)}

    def _deliver_shares(self, symbol: str, shares: float, acq_price: float,
                        strategy: str, strike_price: float | None = None) -> None:
        """Move assigned/exercised shares into the ordinary equity book.

        They become a normal position — markable, closable, and eligible to have
        covered calls written against them — because that is what they are. The
        cash has already moved at the strike, so this only records the holding.
        """
        from daytrader.core.types import Side
        pos = self.broker._positions.get(symbol)
        if pos is None:
            if shares > 0:
                self.broker._positions[symbol] = {
                    "symbol": symbol, "side": Side.LONG, "qty": shares,
                    "entry_price": acq_price, "entry_ts": _now_iso(),
                    "strategy": strategy, "stop": None, "target": None,
                    "rationale": "assigned/exercised from an option at expiration",
                    "horizon": "long", "trail_atr_mult": None, "trail_pct": None,
                    "with_trend": None, "init_stop": None, "planned_risk": None,
                    "auto_scale_r": None, "auto_scale_frac": 0.0, "scaled": False,
                    "adx_decay_exit": None, "adx_peak": None, "adx_neg_bars": 0,
                    "max_adds": None, "adds_used": 0, "entry_ctx": None,
                    "commission_paid": 0.0, "slippage_paid": 0.0,
                }
                self.broker._persist_position(self.broker._positions[symbol])
            return
        # Existing holding: shares add, or a short call called them away.
        new_qty = float(pos["qty"]) + shares if pos["side"] == Side.LONG else float(pos["qty"]) - shares
        if shares < 0 and pos["side"] == Side.LONG:
            # Shares called away by a short call. This is a SALE at the strike
            # and must be recorded as one, otherwise the difference between the
            # cost basis and the strike silently vanishes from the books.
            sold = min(abs(shares), float(pos["qty"]))
            px = float(strike_price if strike_price else acq_price)
            realized = (px - float(pos["entry_price"])) * sold
            try:
                self.db.record_trade({
                    "symbol": symbol, "side": "long",
                    "strategy": strategy, "entry_ts": pos.get("entry_ts"),
                    "entry_price": pos["entry_price"], "qty": sold,
                    "exit_ts": _now_iso(), "exit_price": px,
                    "commission": 0.0, "slippage_cost": 0.0,
                    "pnl": round(realized, 2), "exit_reason": "called_away",
                    "rationale": "shares delivered against a short call at expiration",
                })
            except Exception as e:  # noqa: BLE001
                log.warning("options: could not record called-away trade (%s)", e)
        if new_qty <= 1e-9:
            self.broker._positions.pop(symbol, None)
            try:
                self.db.delete_position(symbol)
            except Exception:  # noqa: BLE001
                pass
            return
        if pos["side"] == Side.LONG and shares > 0:
            old = float(pos["qty"])
            pos["entry_price"] = (pos["entry_price"] * old + acq_price * shares) / new_qty
        pos["qty"] = new_qty
        self.broker._persist_position(pos)


def _envf(key: str, default: float) -> float:
    try:
        v = os.environ.get(key)
        return float(v) if v not in (None, "") else float(default)
    except (TypeError, ValueError):
        return float(default)
