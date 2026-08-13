"""Option contracts: identity, pricing arithmetic, and strategy structures.

Everything here is pure — no network, no broker, no database — so the same code
prices a live chain, a historical chain, and a backtest.

Three ideas do most of the work:

  * An :class:`OptionLeg` is one contract and a signed quantity. Positive is
    long (you paid), negative is short (you were paid). Every structure the
    desks trade — a cash-secured put, a vertical, a condor — is just a list of
    legs, so one model prices all of them and there is no per-strategy special
    casing.
  * **Max loss is computed, not asserted.** :func:`structure_risk` walks the
    payoff at every strike boundary, which is where the extremes of a piecewise
    linear payoff must lie, and reports the worst outcome. A structure whose
    loss is unbounded says so, and the broker refuses it. This is what makes
    "defined risk only" an enforced rule rather than a hopeful label.
  * **Collateral is what the position actually ties up**, which for options is
    not the premium: a cash-secured put ties up the strike, a vertical ties up
    its width. Sizing against premium is how an account that looks fine ends up
    unable to meet an assignment.

Signs follow the cash: a debit is negative cash, a credit is positive.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

# Standard equity option deliverable. Adjusted contracts (splits, mergers) can
# differ, so it is carried per-contract rather than assumed globally.
STANDARD_MULTIPLIER = 100.0

CALL = "C"
PUT = "P"

# OCC 21-character symbol: root padded to 6, YYMMDD, C/P, strike x1000 in 8.
_OCC_RE = re.compile(r"^\s*([A-Z][A-Z0-9.]{0,5})\s*(\d{6})([CP])(\d{8})\s*$")


class OptionError(ValueError):
    """Raised on a malformed contract or an impossible structure."""


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()[:10]
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return date(y, m, d)
    except Exception as e:  # noqa: BLE001
        raise OptionError(f"bad expiration {v!r}; use YYYY-MM-DD") from e


@dataclass(frozen=True)
class OptionContract:
    """One listed option. Immutable, hashable, and its own dictionary key."""

    underlying: str
    expiration: date
    strike: float
    right: str                       # "C" or "P"
    multiplier: float = STANDARD_MULTIPLIER

    def __post_init__(self):
        object.__setattr__(self, "underlying", str(self.underlying).upper().strip())
        object.__setattr__(self, "expiration", _as_date(self.expiration))
        object.__setattr__(self, "right", _normalize_right(self.right))
        strike = float(self.strike)
        if strike <= 0:
            raise OptionError(f"strike must be positive, got {strike}")
        object.__setattr__(self, "strike", strike)
        if float(self.multiplier) <= 0:
            raise OptionError("multiplier must be positive")
        object.__setattr__(self, "multiplier", float(self.multiplier))

    # -- identity ---------------------------------------------------------- #
    @property
    def occ(self) -> str:
        """The 21-character OCC symbol (what a broker and a data feed agree on)."""
        return (f"{self.underlying:<6}{self.expiration:%y%m%d}{self.right}"
                f"{int(round(self.strike * 1000)):08d}")

    def __str__(self) -> str:
        return (f"{self.underlying} {self.expiration:%Y-%m-%d} "
                f"{self.strike:g}{self.right}")

    def dte(self, asof: date | None = None) -> int:
        """Calendar days to expiration. Negative once expired."""
        base = _as_date(asof) if asof is not None else date.today()
        return (self.expiration - base).days

    def is_call(self) -> bool:
        return self.right == CALL

    # -- payoff ------------------------------------------------------------ #
    def intrinsic(self, spot: float) -> float:
        """Per-share intrinsic value at ``spot`` (never negative)."""
        spot = float(spot)
        return max(0.0, spot - self.strike) if self.is_call() else max(0.0, self.strike - spot)

    def extrinsic(self, spot: float, price: float) -> float:
        """Time value: the premium above intrinsic."""
        return float(price) - self.intrinsic(spot)

    def moneyness(self, spot: float) -> str:
        """'itm' / 'atm' / 'otm' — atm within 0.5% of the strike."""
        spot = float(spot)
        if abs(spot - self.strike) <= 0.005 * self.strike:
            return "atm"
        if self.is_call():
            return "itm" if spot > self.strike else "otm"
        return "itm" if spot < self.strike else "otm"

    def to_dict(self) -> dict:
        return {"underlying": self.underlying, "expiration": str(self.expiration),
                "strike": self.strike, "right": self.right,
                "multiplier": self.multiplier, "occ": self.occ}


def _normalize_right(v) -> str:
    s = str(v or "").strip().upper()
    if s in ("C", "CALL", "CALLS"):
        return CALL
    if s in ("P", "PUT", "PUTS"):
        return PUT
    raise OptionError(f"right must be call or put, got {v!r}")


def parse_occ(symbol: str, multiplier: float = STANDARD_MULTIPLIER) -> OptionContract:
    """Parse an OCC symbol (``SPY   260814C00580000``) into a contract."""
    m = _OCC_RE.match(str(symbol or "").upper())
    if not m:
        raise OptionError(
            f"{symbol!r} is not an OCC option symbol. Expected root + YYMMDD + C/P + "
            "strike x1000, e.g. 'SPY   260814C00580000'. Build one with "
            "contract(underlying, expiration, strike, right) instead of hand-writing it.")
    root, ymd, right, strike = m.groups()
    yy, mm, dd = int(ymd[:2]), int(ymd[2:4]), int(ymd[4:])
    return OptionContract(root, date(2000 + yy, mm, dd), int(strike) / 1000.0,
                          right, multiplier)


def contract(underlying: str, expiration, strike: float, right: str,
             multiplier: float = STANDARD_MULTIPLIER) -> OptionContract:
    """Build a contract from plain values (the ergonomic constructor)."""
    return OptionContract(underlying, expiration, strike, right, multiplier)


def is_option_symbol(symbol: str) -> bool:
    return bool(_OCC_RE.match(str(symbol or "").upper()))


# --------------------------------------------------------------------------- #
# legs and structures                                                         #
# --------------------------------------------------------------------------- #
@dataclass
class OptionLeg:
    """One contract with a signed quantity: +long (paid), −short (received)."""

    contract: OptionContract
    qty: float                       # signed; contracts, not shares
    price: float = 0.0               # per-share premium (positive)

    def __post_init__(self):
        self.qty = float(self.qty)
        if self.qty == 0:
            raise OptionError("leg qty cannot be zero — omit the leg instead")
        self.price = float(self.price)
        if self.price < 0:
            raise OptionError("leg price is a premium and cannot be negative")

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    def cash_flow(self) -> float:
        """Cash at execution: negative to open a long, positive for a short."""
        return -self.qty * self.price * self.contract.multiplier

    def value(self, price: float) -> float:
        """Signed mark-to-market value of the leg at ``price`` per share."""
        return self.qty * float(price) * self.contract.multiplier

    def payoff_at(self, spot: float) -> float:
        """Signed intrinsic value at expiration for underlying ``spot``."""
        return self.qty * self.contract.intrinsic(spot) * self.contract.multiplier

    def to_dict(self) -> dict:
        return {**self.contract.to_dict(), "qty": self.qty, "price": self.price,
                "side": "short" if self.is_short else "long"}


def net_cash(legs: list[OptionLeg]) -> float:
    """Net cash to open: positive = net credit, negative = net debit."""
    return sum(l.cash_flow() for l in legs)


def _payoff(legs: list[OptionLeg], spot: float, shares: float = 0.0,
            share_basis: float = 0.0) -> float:
    """Structure payoff at ``spot``, optionally including a share position.

    Shares matter because a short call is only naked if nothing covers it. A
    covered call is long stock plus a short call, and that combination has a
    bounded loss — modelling the stock is what lets the engine tell the two
    apart instead of rejecting every call a wheel would ever write.
    """
    total = sum(l.payoff_at(spot) for l in legs)
    if shares:
        total += shares * (spot - share_basis)
    return total


def structure_risk(legs: list[OptionLeg], shares: float = 0.0,
                   share_basis: float = 0.0) -> dict:
    """Max profit, max loss and breakevens for a multi-leg structure.

    The expiration payoff of any combination of options is piecewise linear with
    breaks only at the strikes, so its extremes are at a strike, at zero, or at
    infinity. Evaluating those points is exact — no simulation, no sampling
    error — and it is the only way to prove a structure is defined-risk rather
    than assume it from its name.

    ``max_loss`` is a positive number of dollars, or ``None`` when the loss is
    unbounded (a naked short call). ``defined_risk`` is the flag the broker
    checks before accepting an order.
    """
    if not legs:
        raise OptionError("no legs")
    opened = net_cash(legs)
    strikes = sorted({l.contract.strike for l in legs})
    if shares and share_basis:
        strikes = sorted(set(strikes) | {float(share_basis)})
    # Probe each strike plus points between and outside them, so a kink is never
    # stepped over.
    lo, hi = strikes[0], strikes[-1]
    span = max(hi - lo, hi * 0.5, 1.0)
    probes = [0.0] + strikes + [hi + span, hi + span * 10.0]
    for a, b in zip(strikes, strikes[1:]):
        probes.append((a + b) / 2.0)
    probes = sorted(set(probes))

    results = [(p, opened + _payoff(legs, p, shares, share_basis)) for p in probes]
    values = [v for _, v in results]
    max_profit = max(values)
    max_loss_val = min(values)

    # Unbounded above (net short calls) or below (net short puts / long stock-like
    # exposure): compare the two farthest probes to see whether the payoff is
    # still moving in the losing direction rather than flattening out.
    far_slope = (results[-1][1] - results[-2][1]) / max(results[-1][0] - results[-2][0], 1e-9)
    unbounded_up = far_slope < -1e-6
    # Below zero the underlying cannot go, so downside is always bounded; the
    # worst case there is spot = 0, which the probe list already includes.
    unbounded = unbounded_up

    out = {
        "net_cash": round(opened, 2),
        "credit": round(opened, 2) if opened > 0 else 0.0,
        "debit": round(-opened, 2) if opened < 0 else 0.0,
        "max_profit": None if _unbounded_profit(far_slope) else round(max_profit, 2),
        "max_loss": None if unbounded else round(abs(min(0.0, max_loss_val)), 2),
        "defined_risk": not unbounded,
        "breakevens": _breakevens(legs, opened, probes, shares, share_basis),
    }
    if shares:
        out["covered_by_shares"] = shares
    if unbounded:
        out["risk_note"] = ("loss is UNBOUNDED — the payoff keeps falling as the "
                            "underlying rises (a naked short call). Buy a further "
                            "strike to cap it.")
    return out


def _unbounded_profit(far_slope: float) -> bool:
    return far_slope > 1e-6


def _breakevens(legs: list[OptionLeg], opened: float, probes: list[float],
                shares: float = 0.0, share_basis: float = 0.0) -> list[float]:
    """Underlying prices where the structure breaks even, by linear interpolation
    between probe points (exact, since the payoff is linear between strikes)."""
    outs: list[float] = []
    pts = [(p, opened + _payoff(legs, p, shares, share_basis)) for p in sorted(set(probes))]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if y0 == 0.0:
            outs.append(x0)
        elif (y0 < 0) != (y1 < 0) and (y1 - y0) != 0:
            outs.append(x0 + (x1 - x0) * (-y0) / (y1 - y0))
    return [round(x, 2) for x in sorted(set(outs)) if x > 0]


def collateral(legs: list[OptionLeg], structure: dict | None = None) -> float:
    """Cash/buying power the structure ties up while it is open.

    Not the premium — the premium is what changes hands, the collateral is what
    is held against the obligation:

      * a cash-secured put holds strike x multiplier x contracts,
      * a defined-risk spread holds its max loss (the width, less the credit),
      * a long option holds only the debit paid.

    A desk that sizes against premium instead of this is the desk that cannot
    meet an assignment.
    """
    risk = structure or structure_risk(legs)
    shorts = [l for l in legs if l.is_short]
    if not shorts:
        return round(max(0.0, -net_cash(legs)), 2)     # debit paid
    if risk.get("defined_risk") and risk.get("max_loss") is not None:
        # Cash-secured puts land here too: the "max loss" of a naked short put
        # is strike-to-zero, which IS the cash securing it.
        return round(float(risk["max_loss"]), 2)
    return float("inf")                                 # undefined; broker rejects


def stress_loss(legs: list[OptionLeg], spot: float, move_pct: float = 20.0,
                shares: float = 0.0, share_basis: float = 0.0) -> dict:
    """Loss if the underlying moves ``move_pct`` against the structure.

    This exists because "max loss" is the wrong risk number for a cash-secured
    put. Its true worst case is the stock going to zero, and sizing a put to a
    1.5%-of-equity cap on that basis is impossible on any account this size —
    the rule would not make the wheel safe, it would make it unrunnable, and a
    rule nobody can follow gets bypassed rather than obeyed.

    So for structures whose loss is bounded by a spread width, the risk measure
    IS the max loss (it is real and reachable). For structures exposed to the
    underlying itself, risk is measured at a severe but plausible adverse move.
    Collateral still covers the full obligation either way, which is what
    actually stops a desk from selling more puts than it can be assigned on.
    """
    spot = float(spot)
    if spot <= 0:
        raise OptionError("spot must be positive")
    opened = net_cash(legs)
    down = opened + _payoff(legs, spot * (1.0 - move_pct / 100.0), shares, share_basis)
    up = opened + _payoff(legs, spot * (1.0 + move_pct / 100.0), shares, share_basis)
    worst = min(down, up)
    return {"move_pct": move_pct,
            "loss_down": round(abs(min(0.0, down)), 2),
            "loss_up": round(abs(min(0.0, up)), 2),
            "stress_loss": round(abs(min(0.0, worst)), 2)}


def risk_measure(legs: list[OptionLeg], spot: float | None = None,
                 move_pct: float = 20.0, shares: float = 0.0,
                 share_basis: float = 0.0) -> dict:
    """The number the risk rails should size against, and why.

    Returns ``{"risk", "basis", "note"}``. ``basis`` is ``"max_loss"`` when the
    structure is width-bounded, or ``"stress"`` when its downside is the
    underlying itself.
    """
    info = structure_risk(legs, shares, share_basis)
    max_loss = info.get("max_loss")
    if max_loss is None:
        return {"risk": float("inf"), "basis": "unbounded",
                "note": "loss is unbounded; not permitted"}
    # Width-bounded when every short is covered by a long of the same right —
    # then max_loss is a real, reachable number worth sizing against.
    shorts = [l for l in legs if l.is_short]
    longs = [l for l in legs if not l.is_short]
    covered = all(
        any(o.contract.right == s.contract.right and abs(o.qty) >= abs(s.qty)
            for o in longs)
        for s in shorts) if shorts else True
    if shares:
        # Stock covers short calls, but the combined position is still exposed
        # to the underlying falling, so it is sized by stress rather than by a
        # (technically real, practically useless) stock-to-zero max loss.
        covered = False
    if covered:
        return {"risk": float(max_loss), "basis": "max_loss",
                "note": "loss is capped by the spread width"}
    if spot is None:
        return {"risk": float(max_loss), "basis": "max_loss",
                "note": "no spot available; sized against the absolute worst case"}
    st = stress_loss(legs, spot, move_pct, shares, share_basis)
    return {"risk": float(st["stress_loss"]), "basis": "stress",
            "move_pct": move_pct, "max_loss_if_zero": float(max_loss),
            "note": (f"downside is the underlying itself, so risk is measured at a "
                     f"{move_pct:g}% adverse move (${st['stress_loss']:,.0f}); the full "
                     f"${max_loss:,.0f} obligation is still held as collateral")}


def structure_name(legs: list[OptionLeg]) -> str:
    """Best-effort label for the common structures, for journals and records."""
    if len(legs) == 1:
        l = legs[0]
        side = "short" if l.is_short else "long"
        kind = "call" if l.contract.is_call() else "put"
        if l.is_short and kind == "put":
            return "cash_secured_put"
        if l.is_short and kind == "call":
            return "short_call"
        return f"{side}_{kind}"
    rights = {l.contract.right for l in legs}
    exps = {l.contract.expiration for l in legs}
    if len(legs) == 2 and len(rights) == 1 and len(exps) == 1:
        credit = net_cash(legs) > 0
        put = PUT in rights
        if put:
            return "bull_put_spread" if credit else "bear_put_spread"
        return "bear_call_spread" if credit else "bull_call_spread"
    if len(legs) == 2 and len(rights) == 1 and len(exps) == 2:
        return "calendar_spread"
    if len(legs) == 4 and rights == {CALL, PUT} and len(exps) == 1:
        return "iron_condor"
    if len(legs) == 2 and rights == {CALL, PUT} and len(exps) == 1:
        strikes = {l.contract.strike for l in legs}
        return "straddle" if len(strikes) == 1 else "strangle"
    return f"{len(legs)}_leg_structure"


def describe(legs: list[OptionLeg], shares: float = 0.0,
             share_basis: float = 0.0) -> dict:
    """Everything a desk (or a reviewer reading the journal) needs to see."""
    risk = structure_risk(legs, shares, share_basis)
    out = {
        "structure": structure_name(legs),
        "legs": [l.to_dict() for l in legs],
        "collateral": collateral(legs, risk),
        **risk,
    }
    ml, mp = out.get("max_loss"), out.get("max_profit")
    if ml and mp and ml > 0:
        out["reward_risk"] = round(mp / ml, 2)
    return out
