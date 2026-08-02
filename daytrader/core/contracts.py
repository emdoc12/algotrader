"""Futures contract specifications.

Everything else in the system prices a position as ``price * qty`` — the share
model. That identity is false for futures: a contract is a claim on
``multiplier`` units of the underlying, so one E-mini S&P at 7520 controls
$376,000, not $7,520. Without this layer notional, P&L, exposure limits and the
risk cap are all wrong by the multiplier (50x for ES, 1000x for CL), and the
error is silent — quotes and bars flow fine and orders fill.

Margins are **exchange minimums and they move**. The values here are realistic
2026 figures, not a live feed; brokers routinely require more, and exchanges
raise them in volatility. Every one is overridable:
``ES_INITIAL_MARGIN=18000`` (per contract, USD).

Symbols follow the loader's Yahoo convention (``ES=F``), since that is what the
data layer resolves.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    name: str
    multiplier: float          # dollars per 1.00 of price
    tick_size: float           # minimum price increment
    initial_margin: float      # USD per contract to open
    maintenance_margin: float  # USD per contract to hold
    commission_per_contract: float = 1.25   # per side; ~$2.50 round turn is typical
    underlying: str = ""       # ETF proxy, for context in messages

    @property
    def tick_value(self) -> float:
        """Dollars per minimum tick."""
        return self.multiplier * self.tick_size

    def notional(self, price: float, qty: float = 1.0) -> float:
        return float(price) * float(qty) * self.multiplier


def _m(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except (TypeError, ValueError):
        return float(default)


def _spec(sym, name, mult, tick, init, maint, comm=1.25, under="") -> ContractSpec:
    root = sym.replace("=F", "")
    return ContractSpec(
        symbol=sym, name=name, multiplier=mult, tick_size=tick,
        initial_margin=_m(f"{root}_INITIAL_MARGIN", init),
        maintenance_margin=_m(f"{root}_MAINTENANCE_MARGIN", maint),
        commission_per_contract=_m(f"{root}_COMMISSION", comm),
        underlying=under,
    )


# Index, energy, metals and rates futures with liquid, retail-accessible
# contracts. Micros are listed alongside their full-size parent because they
# are the only ones that size sensibly against a $25k account.
_SPECS: dict[str, ContractSpec] = {s.symbol: s for s in [
    # -- equity index --
    _spec("ES=F",  "E-mini S&P 500",      50.0,  0.25, 16000, 14500, 1.25, "SPY"),
    _spec("MES=F", "Micro E-mini S&P 500", 5.0,  0.25,  1600,  1450, 0.52, "SPY"),
    _spec("NQ=F",  "E-mini Nasdaq 100",   20.0,  0.25, 28000, 25000, 1.25, "QQQ"),
    _spec("MNQ=F", "Micro E-mini Nasdaq",  2.0,  0.25,  2800,  2500, 0.52, "QQQ"),
    _spec("RTY=F", "E-mini Russell 2000", 50.0,  0.10, 16000, 14500, 1.25, "IWM"),
    _spec("M2K=F", "Micro Russell 2000",   5.0,  0.10,  1600,  1450, 0.52, "IWM"),
    _spec("YM=F",  "E-mini Dow",           5.0,  1.00,  9000,  8200, 1.25, "DIA"),
    _spec("MYM=F", "Micro E-mini Dow",     0.5,  1.00,   900,   820, 0.52, "DIA"),
    # -- energy --
    _spec("CL=F",  "Crude Oil",         1000.0,  0.01,  6000,  5500, 1.50, "USO"),
    _spec("MCL=F", "Micro Crude Oil",    100.0,  0.01,   600,   550, 0.52, "USO"),
    _spec("NG=F",  "Natural Gas",       10000.0, 0.001, 4500,  4100, 1.50, "UNG"),
    # -- metals --
    _spec("GC=F",  "Gold",                100.0, 0.10, 12000, 11000, 1.50, "GLD"),
    _spec("MGC=F", "Micro Gold",           10.0, 0.10,  1200,  1100, 0.52, "GLD"),
    _spec("SI=F",  "Silver",             5000.0, 0.005,14000, 12500, 1.50, "SLV"),
    _spec("SIL=F", "Micro Silver",       1000.0, 0.005, 2800,  2500, 0.52, "SLV"),
    # -- rates --
    _spec("ZB=F",  "30-Year T-Bond",     1000.0, 0.03125, 4200, 3800, 1.25, "TLT"),
    _spec("ZN=F",  "10-Year T-Note",     1000.0, 0.015625, 2200, 2000, 1.25, "IEF"),
]}


def is_futures(symbol) -> bool:
    return str(symbol or "").strip().upper().endswith("=F")


def spec_for(symbol) -> ContractSpec | None:
    """Contract spec, or None for a non-futures (share-model) symbol."""
    return _SPECS.get(str(symbol or "").strip().upper())


def _broker_override(symbol) -> dict:
    """Contract reference data from the owner's broker, when mirroring is on."""
    try:
        from daytrader.live.tastytrade_margin import profile
        root = str(symbol or "").strip().upper().replace("=F", "")
        return (profile().get("contracts") or {}).get(root) or {}
    except Exception:  # noqa: BLE001 - never let an optional feed break pricing
        return {}


def multiplier(symbol) -> float:
    """Dollars per 1.00 of price. 1.0 for equities/ETFs — the share model.

    Prefers the broker's own ``notional_multiplier`` when tastytrade mirroring
    is enabled, since that is authoritative for what the account would actually
    be filled at; falls back to the static table.
    """
    s = spec_for(symbol)
    if s is None:
        return 1.0
    m = _broker_override(symbol).get("multiplier")
    try:
        return float(m) if m else s.multiplier
    except (TypeError, ValueError):
        return s.multiplier


def initial_margin(symbol, qty: float = 1.0) -> float:
    """Buying power consumed by ``qty`` contracts. 0.0 for non-futures.

    Scaled by the owner's futures intraday relief when mirroring is enabled.
    """
    s = spec_for(symbol)
    if s is None:
        return 0.0
    base = s.initial_margin * abs(float(qty))
    try:
        from daytrader.live.tastytrade_margin import futures_margin_scale
        return base * futures_margin_scale()
    except Exception:  # noqa: BLE001
        return base


def maintenance_margin(symbol, qty: float = 1.0) -> float:
    s = spec_for(symbol)
    return (s.maintenance_margin * abs(float(qty))) if s else 0.0


def commission(symbol, qty: float = 1.0) -> float | None:
    """Per-side commission for futures; None means 'use the equity cost model'."""
    s = spec_for(symbol)
    return (s.commission_per_contract * abs(float(qty))) if s else None


def supported_symbols() -> list[str]:
    return sorted(_SPECS)


def describe(symbol) -> dict | None:
    s = spec_for(symbol)
    if s is None:
        return None
    return {
        "symbol": s.symbol, "name": s.name, "multiplier": s.multiplier,
        "tick_size": s.tick_size, "tick_value": round(s.tick_value, 4),
        "initial_margin": s.initial_margin, "maintenance_margin": s.maintenance_margin,
        "commission_per_contract": s.commission_per_contract,
        "etf_proxy": s.underlying,
    }
