"""Core data types shared across the day-trading system.

These dataclasses are the contract every module (data loader, strategies,
backtester, risk manager, reporting) agrees on. Keep them dependency-free
so any component can import them without pulling in heavy libraries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class SignalType(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar for one symbol at one timestamp (timezone: US/Eastern)."""
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    """A trade intention emitted by a strategy.

    The backtester turns signals into orders, applies risk sizing, and
    simulates realistic fills. A strategy never touches cash or positions
    directly -- it only expresses intent.
    """
    ts: datetime
    symbol: str
    side: Side
    type: SignalType
    strategy: str
    # Suggested risk anchors (price terms). The risk manager may override size.
    stop: Optional[float] = None          # protective stop price
    target: Optional[float] = None        # profit target price
    limit: Optional[float] = None         # limit price; None => market
    strength: float = 1.0                 # 0..1 conviction, used for sizing
    reason: str = ""                      # human-readable rationale
    meta: dict = field(default_factory=dict)


@dataclass
class Fill:
    """A simulated execution after slippage/spread/commission."""
    ts: datetime
    symbol: str
    side: Side
    qty: float
    price: float          # effective fill price including slippage + half-spread
    commission: float
    slippage_cost: float  # dollars lost to slippage+spread vs mid
    strategy: str
    reason: str = ""


@dataclass
class Trade:
    """A round-trip position from entry fill to exit fill."""
    symbol: str
    side: Side
    strategy: str
    entry_ts: datetime
    entry_price: float
    qty: float
    exit_ts: Optional[datetime] = None
    exit_price: Optional[float] = None
    commission: float = 0.0
    slippage_cost: float = 0.0
    exit_reason: str = ""
    mae: float = 0.0   # max adverse excursion ($), most negative open P&L
    mfe: float = 0.0   # max favorable excursion ($), most positive open P&L

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def gross_pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        direction = 1.0 if self.side == Side.LONG else -1.0
        return direction * (self.exit_price - self.entry_price) * self.qty

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.commission

    @property
    def return_pct(self) -> float:
        if self.exit_price is None or self.entry_price == 0:
            return 0.0
        direction = 1.0 if self.side == Side.LONG else -1.0
        return direction * (self.exit_price - self.entry_price) / self.entry_price

    @property
    def hold_minutes(self) -> float:
        if self.exit_ts is None:
            return 0.0
        return (self.exit_ts - self.entry_ts).total_seconds() / 60.0


@dataclass
class Position:
    """An open position tracked by the broker simulator."""
    symbol: str
    side: Side
    qty: float
    entry_price: float
    entry_ts: datetime
    strategy: str
    stop: Optional[float] = None
    target: Optional[float] = None
    init_stop: Optional[float] = None     # original stop, for R-multiple math
    breakeven_done: bool = False          # breakeven stop already applied
    commission_paid: float = 0.0
    slippage_paid: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0

    def unrealized(self, price: float) -> float:
        direction = 1.0 if self.side == Side.LONG else -1.0
        return direction * (price - self.entry_price) * self.qty
