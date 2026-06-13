"""Risk management: position sizing and portfolio guardrails.

The engine owns execution-time guardrails (daily loss limit, max concurrent
positions, EOD flat). This module supplies the *sizing* policy — how many
shares to take per signal — and factory helpers to assemble a sizer with the
risk budget you want. Keeping sizing here (not in strategies) means every
strategy is sized on one consistent, conservative risk framework, which is the
single biggest lever on max drawdown.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from daytrader.core.types import Signal


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.4    # % of equity risked from entry to stop
    max_position_pct: float = 25.0     # cap notional per position (% of equity)
    min_shares: float = 1.0
    atr_stop_mult: float = 1.5         # used when a signal omits a stop
    use_strength: bool = True          # scale size by signal.strength (0..1)
    vol_target: bool = False           # scale inversely to recent volatility
    vol_target_daily_pct: float = 1.0  # target per-trade vol when vol_target on


def make_sizer(cfg: RiskConfig):
    """Build a sizing function compatible with BacktestEngine.

    Signature: (equity, signal, entry_price, atr) -> share quantity.
    Sizes so that hitting the stop loses ~risk_per_trade_pct of equity, then
    caps notional at max_position_pct. Optionally scales by signal strength.
    """
    def sizer(equity: float, signal: Signal, price: float, atr: float) -> float:
        if price <= 0:
            return 0.0
        risk_budget = equity * (cfg.risk_per_trade_pct / 100.0)

        if cfg.use_strength and signal.strength:
            risk_budget *= max(0.0, min(1.0, signal.strength))

        # Per-share risk: prefer the signal's stop; else an ATR multiple.
        if signal.stop is not None and abs(price - signal.stop) > 1e-9:
            per_share_risk = abs(price - signal.stop)
        else:
            per_share_risk = max(atr * cfg.atr_stop_mult, price * 0.002)

        qty = risk_budget / per_share_risk

        if cfg.vol_target and atr > 0:
            # Dampen size when ATR (as % of price) is unusually high.
            atr_pct = atr / price * 100.0
            if atr_pct > 0:
                qty *= min(1.0, cfg.vol_target_daily_pct / atr_pct)

        # Notional cap.
        max_notional = equity * (cfg.max_position_pct / 100.0)
        qty = min(qty, max_notional / price)

        return max(0.0, qty) if qty >= cfg.min_shares else 0.0

    return sizer
