"""
Deterministic risk-sizing layer.

Sits between Claude's trade tags and execution. Claude can ask for a position
of any size; this module clamps to whichever limit binds first:

  - max_position_pct       — single position value as % of equity
  - max_per_coin_pct       — total exposure to a coin (across multiple positions)
  - max_risk_per_trade_pct — dollars at risk (entry - stop) * qty as % of equity
  - max_total_exposure_pct — total crypto holdings as % of equity (keeps dry powder)
  - daily_loss_limit_pct   — if breached, cooldown blocks all new buys

The point: Claude is free to think about *what* to trade; the system enforces
*how much*. Same clamps used by AI path, advanced order path, and any future
indicator strategy.
"""

from dataclasses import dataclass, field


@dataclass
class RiskLimits:
    """Hard sizing limits enforced before every order placement."""
    max_position_pct: float = 25.0          # single position cap (% equity)
    max_per_coin_pct: float = 35.0          # combined exposure per coin (% equity)
    max_risk_per_trade_pct: float = 1.5     # stop-distance dollars (% equity)
    max_total_exposure_pct: float = 80.0    # total holdings (% equity) — dry-powder floor
    daily_loss_limit_pct: float = 4.0       # daily realized+unrealized drawdown trigger
    drawdown_size_multiplier: float = 0.5   # when drawdown breaker active, multiply size by this


@dataclass
class ClampResult:
    """What the risk manager decided about a buy request."""
    requested_qty: float
    final_qty: float
    blocked: bool = False
    reasons: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return abs(self.final_qty - self.requested_qty) > 1e-9


def clamp_buy_size(
    symbol: str,
    requested_qty: float,
    price: float,
    stop_price: float,
    equity: float,
    available_cash: float,
    coin_exposure_usd: float,
    total_exposure_usd: float,
    limits: RiskLimits,
    drawdown_active: bool = False,
    cooldown_active: bool = False,
) -> ClampResult:
    """Clamp a buy request to the binding risk limit.

    Args:
        symbol: display symbol like "BTC/USD" (informational only)
        requested_qty: what Claude asked for (in coin units)
        price: current price per coin
        stop_price: stop-loss price (used for risk-dollar sizing). 0 = no stop given.
        equity: total account equity in USD
        available_cash: cash net of any reserved (pending) buy orders
        coin_exposure_usd: current USD value held in this coin
        total_exposure_usd: current USD value across all crypto holdings
        limits: hard limits to enforce
        drawdown_active: drawdown breaker on? (halves size)
        cooldown_active: daily loss limit hit? (blocks entirely)

    Returns:
        ClampResult with final_qty, blocked flag, and human-readable reasons.
    """
    result = ClampResult(requested_qty=requested_qty, final_qty=requested_qty)

    if cooldown_active:
        result.blocked = True
        result.final_qty = 0.0
        result.reasons.append("daily_loss_cooldown")
        return result

    if requested_qty <= 0 or price <= 0 or equity <= 0:
        result.blocked = True
        result.final_qty = 0.0
        result.reasons.append("invalid_inputs")
        return result

    qty = requested_qty

    # 1. Single-position cap
    max_position_value = equity * (limits.max_position_pct / 100.0)
    pos_qty_cap = max_position_value / price
    if qty > pos_qty_cap:
        result.reasons.append(f"max_position_pct={limits.max_position_pct:.1f}%")
        qty = pos_qty_cap

    # 2. Per-coin total exposure cap (this buy + existing holdings)
    max_coin_value = equity * (limits.max_per_coin_pct / 100.0)
    headroom_value = max_coin_value - coin_exposure_usd
    if headroom_value <= 0:
        result.blocked = True
        result.final_qty = 0.0
        result.reasons.append(f"per_coin_cap_full={limits.max_per_coin_pct:.1f}%")
        return result
    coin_qty_cap = headroom_value / price
    if qty > coin_qty_cap:
        result.reasons.append(f"max_per_coin_pct={limits.max_per_coin_pct:.1f}%")
        qty = coin_qty_cap

    # 3. Risk-dollar cap (stop-distance based)
    if stop_price > 0 and stop_price < price:
        stop_dist = price - stop_price
        max_risk_dollars = equity * (limits.max_risk_per_trade_pct / 100.0)
        risk_qty_cap = max_risk_dollars / stop_dist
        if qty > risk_qty_cap:
            result.reasons.append(
                f"max_risk_per_trade={limits.max_risk_per_trade_pct:.2f}% "
                f"(stop ${stop_dist:.2f} away)"
            )
            qty = risk_qty_cap

    # 4. Total exposure cap (dry-powder floor)
    max_total_value = equity * (limits.max_total_exposure_pct / 100.0)
    total_headroom = max_total_value - total_exposure_usd
    if total_headroom <= 0:
        result.blocked = True
        result.final_qty = 0.0
        result.reasons.append(f"total_exposure_cap_full={limits.max_total_exposure_pct:.1f}%")
        return result
    total_qty_cap = total_headroom / price
    if qty > total_qty_cap:
        result.reasons.append(f"max_total_exposure_pct={limits.max_total_exposure_pct:.1f}%")
        qty = total_qty_cap

    # 5. Drawdown circuit breaker — halve final size
    if drawdown_active:
        result.reasons.append(
            f"drawdown_breaker x{limits.drawdown_size_multiplier:.2f}"
        )
        qty = qty * limits.drawdown_size_multiplier

    # 6. Available cash cap (always last — we never over-spend)
    cash_qty_cap = (available_cash * 0.9974) / price  # reserve 0.26% for taker fee
    if qty > cash_qty_cap:
        result.reasons.append("available_cash")
        qty = max(0.0, cash_qty_cap)

    result.final_qty = qty
    if qty <= 0:
        result.blocked = True
        result.reasons.append("clamped_to_zero")

    return result


def usd_to_qty(
    usd_amount: float,
    price: float,
    fee_pct: float = 0.26,
) -> float:
    """Convert a USD budget into coin quantity, reserving fee from the budget."""
    if price <= 0 or usd_amount <= 0:
        return 0.0
    spendable = usd_amount / (1.0 + fee_pct / 100.0)
    return spendable / price


def risk_usd_to_qty(
    risk_usd: float,
    price: float,
    stop_price: float,
) -> float:
    """Convert risk dollars to qty given a stop. risk_usd / (entry - stop)."""
    if price <= 0 or stop_price <= 0 or stop_price >= price:
        return 0.0
    stop_dist = price - stop_price
    if stop_dist <= 0:
        return 0.0
    return risk_usd / stop_dist
