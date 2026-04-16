"""
Paper trading engine.

Simulates order execution against real Kraken prices without risking real money.
Tracks balances, positions, and P&L in SQLite so state survives restarts.
"""

import logging
import time
from decimal import Decimal

from database import Database, Trade, Position, PaperBalance

logger = logging.getLogger(__name__)


class PaperTrader:
    """
    Simulates trading with fake money using real market prices.

    - Fills market orders instantly at current price + slippage
    - Applies realistic Kraken fee schedule
    - Persists all state to SQLite
    """

    def __init__(self, db: Database, starting_capital: float = 10000.0,
                 taker_fee_pct: float = 0.26):
        self.db = db
        self.taker_fee_pct = taker_fee_pct
        self.balance = db.get_paper_balance(default_capital=starting_capital)

    def get_balance(self) -> PaperBalance:
        """Get current paper balance."""
        return self.balance

    def update_equity(self, btc_price: float):
        """Update total equity based on current BTC price."""
        btc_value = self.balance.btc_quantity * btc_price
        self.balance.total_equity = self.balance.cash_usd + btc_value
        self.balance.last_updated = time.time()
        self.db.save_paper_balance(self.balance)

        # Record snapshot for performance tracking
        self.db.record_equity_snapshot(
            equity=self.balance.total_equity,
            cash=self.balance.cash_usd,
            btc_value=btc_value,
            btc_price=btc_price,
        )

    def execute_buy(
        self,
        price: float,
        quantity: float,
        strategy: str = "",
        signals_json: str = "",
    ) -> Trade:
        """
        Simulate a BTC buy order.

        Args:
            price: Current BTC price
            quantity: Amount of BTC to buy
            strategy: Strategy name for logging
            signals_json: JSON snapshot of signals

        Returns:
            Trade record
        """
        cost = price * quantity
        fee = cost * (self.taker_fee_pct / 100.0)
        total_cost = cost + fee

        if total_cost > self.balance.cash_usd:
            raise ValueError(
                f"Insufficient funds: need ${total_cost:.2f}, "
                f"have ${self.balance.cash_usd:.2f}"
            )

        # Update balance
        self.balance.cash_usd -= total_cost
        self.balance.btc_quantity += quantity
        self.balance.last_updated = time.time()
        self.db.save_paper_balance(self.balance)

        # Record trade
        trade = Trade(
            timestamp=time.time(),
            side="buy",
            price=price,
            quantity=quantity,
            value=cost,
            fee=fee,
            order_id=f"PAPER-BUY-{int(time.time())}",
            mode="paper",
            strategy=strategy,
            signals=signals_json,
            status="filled",
        )
        trade.id = self.db.record_trade(trade)

        logger.info(
            f"[PAPER] BUY {quantity:.6f} BTC @ ${price:,.2f} | "
            f"Cost: ${total_cost:,.2f} (fee: ${fee:.2f}) | "
            f"Cash remaining: ${self.balance.cash_usd:,.2f}"
        )

        return trade

    def execute_sell(
        self,
        price: float,
        quantity: float,
        strategy: str = "",
        signals_json: str = "",
    ) -> Trade:
        """
        Simulate a BTC sell order.

        Args:
            price: Current BTC price
            quantity: Amount of BTC to sell
            strategy: Strategy name for logging
            signals_json: JSON snapshot of signals

        Returns:
            Trade record
        """
        if quantity > self.balance.btc_quantity:
            raise ValueError(
                f"Insufficient BTC: want to sell {quantity:.6f}, "
                f"have {self.balance.btc_quantity:.6f}"
            )

        proceeds = price * quantity
        fee = proceeds * (self.taker_fee_pct / 100.0)
        net_proceeds = proceeds - fee

        # Update balance
        self.balance.cash_usd += net_proceeds
        self.balance.btc_quantity -= quantity
        self.balance.last_updated = time.time()
        self.db.save_paper_balance(self.balance)

        # Record trade
        trade = Trade(
            timestamp=time.time(),
            side="sell",
            price=price,
            quantity=quantity,
            value=proceeds,
            fee=fee,
            order_id=f"PAPER-SELL-{int(time.time())}",
            mode="paper",
            strategy=strategy,
            signals=signals_json,
            status="filled",
        )
        trade.id = self.db.record_trade(trade)

        logger.info(
            f"[PAPER] SELL {quantity:.6f} BTC @ ${price:,.2f} | "
            f"Proceeds: ${net_proceeds:,.2f} (fee: ${fee:.2f}) | "
            f"Cash: ${self.balance.cash_usd:,.2f}"
        )

        return trade
