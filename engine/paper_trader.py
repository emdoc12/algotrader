"""
Paper trading engine.

Simulates order execution against real Kraken prices without risking real money.
Tracks balances, positions, and P&L in SQLite so state survives restarts.
Supports multi-coin trading — holds any number of different crypto assets.
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
    - Supports multiple coins simultaneously
    """

    def __init__(self, db: Database, starting_capital: float = 10000.0,
                 taker_fee_pct: float = 0.26, slippage_pct: float = 0.05):
        self.db = db
        self.taker_fee_pct = taker_fee_pct
        self.slippage_pct = slippage_pct
        self.balance = db.get_paper_balance(default_capital=starting_capital)
        # Load persisted holdings
        self.balance.holdings = db.get_holdings()
        # Migrate legacy btc_quantity into holdings if present
        if self.balance.btc_quantity > 0 and self.balance.holdings.get("BTC", 0) == 0:
            self.balance.holdings["BTC"] = self.balance.btc_quantity
            db.update_holding("BTC", self.balance.btc_quantity)

    def get_balance(self) -> PaperBalance:
        """Get current paper balance."""
        return self.balance

    def update_equity(self, prices: dict):
        """Update total equity based on current prices for all held coins.

        Args:
            prices: dict mapping coin symbol to USD price, e.g. {"BTC": 74500, "ETH": 3200}
        """
        holdings_value = 0.0
        btc_price = prices.get("BTC", 0)
        for symbol, qty in self.balance.holdings.items():
            coin_price = prices.get(symbol, 0)
            holdings_value += qty * coin_price

        self.balance.total_equity = self.balance.cash_usd + holdings_value
        # Keep btc_quantity in sync for backward compat
        self.balance.btc_quantity = self.balance.holdings.get("BTC", 0)
        self.balance.last_updated = time.time()
        self.db.save_paper_balance(self.balance)

        # Record snapshot for performance tracking
        self.db.record_equity_snapshot(
            equity=self.balance.total_equity,
            cash=self.balance.cash_usd,
            btc_value=self.balance.holdings.get("BTC", 0) * btc_price,
            btc_price=btc_price,
        )

    def execute_buy(
        self,
        price: float,
        quantity: float,
        symbol: str = "BTC",
        display_symbol: str = "BTC/USD",
        strategy: str = "",
        signals_json: str = "",
    ) -> Trade:
        """
        Simulate a crypto buy order.

        Args:
            price: Current coin price in USD
            quantity: Amount of coin to buy
            symbol: Coin symbol (BTC, ETH, SOL, etc.)
            display_symbol: Display pair name (BTC/USD, ETH/USD, etc.)
            strategy: Strategy name for logging
            signals_json: JSON snapshot of signals
        """
        # Apply slippage: market buys fill above the quoted price
        fill_price = price * (1.0 + self.slippage_pct / 100.0)
        cost = fill_price * quantity
        fee = cost * (self.taker_fee_pct / 100.0)
        total_cost = cost + fee

        if total_cost > self.balance.cash_usd:
            raise ValueError(
                f"Insufficient funds: need ${total_cost:.2f}, "
                f"have ${self.balance.cash_usd:.2f}"
            )

        # Update balance
        self.balance.cash_usd -= total_cost
        current_qty = self.balance.holdings.get(symbol, 0)
        self.balance.holdings[symbol] = current_qty + quantity
        # Keep btc_quantity in sync
        self.balance.btc_quantity = self.balance.holdings.get("BTC", 0)
        self.balance.last_updated = time.time()
        self.db.save_paper_balance(self.balance)
        self.db.update_holding(symbol, self.balance.holdings[symbol])

        # Record trade at the actual fill price (post-slippage), so FIFO P&L
        # reflects what live execution would have produced.
        trade = Trade(
            timestamp=time.time(),
            side="buy",
            price=fill_price,
            quantity=quantity,
            value=cost,
            fee=fee,
            order_id=f"PAPER-BUY-{symbol}-{int(time.time())}",
            mode="paper",
            strategy=strategy,
            signals=signals_json,
            status="filled",
            symbol=display_symbol,
        )
        trade.id = self.db.record_trade(trade)

        logger.info(
            f"[PAPER] BUY {quantity:.6f} {symbol} @ ${fill_price:,.2f} "
            f"(quote ${price:,.2f}, slip {self.slippage_pct:.2f}%) | "
            f"Cost: ${total_cost:,.2f} (fee: ${fee:.2f}) | "
            f"Cash remaining: ${self.balance.cash_usd:,.2f}"
        )

        return trade

    def execute_sell(
        self,
        price: float,
        quantity: float,
        symbol: str = "BTC",
        display_symbol: str = "BTC/USD",
        strategy: str = "",
        signals_json: str = "",
    ) -> Trade:
        """
        Simulate a crypto sell order.

        Args:
            price: Current coin price in USD
            quantity: Amount of coin to sell
            symbol: Coin symbol (BTC, ETH, SOL, etc.)
            display_symbol: Display pair name (BTC/USD, ETH/USD, etc.)
            strategy: Strategy name for logging
            signals_json: JSON snapshot of signals
        """
        current_qty = self.balance.holdings.get(symbol, 0)
        if quantity > current_qty:
            raise ValueError(
                f"Insufficient {symbol}: want to sell {quantity:.6f}, "
                f"have {current_qty:.6f}"
            )

        # Apply slippage: market sells fill below the quoted price
        fill_price = price * (1.0 - self.slippage_pct / 100.0)
        proceeds = fill_price * quantity
        fee = proceeds * (self.taker_fee_pct / 100.0)
        net_proceeds = proceeds - fee

        # Update balance
        self.balance.cash_usd += net_proceeds
        self.balance.holdings[symbol] = current_qty - quantity
        if self.balance.holdings[symbol] <= 0.000000001:
            self.balance.holdings.pop(symbol, None)
        # Keep btc_quantity in sync
        self.balance.btc_quantity = self.balance.holdings.get("BTC", 0)
        self.balance.last_updated = time.time()
        self.db.save_paper_balance(self.balance)
        self.db.update_holding(symbol, self.balance.holdings.get(symbol, 0))

        # Record trade at the actual fill price (post-slippage)
        trade = Trade(
            timestamp=time.time(),
            side="sell",
            price=fill_price,
            quantity=quantity,
            value=proceeds,
            fee=fee,
            order_id=f"PAPER-SELL-{symbol}-{int(time.time())}",
            mode="paper",
            strategy=strategy,
            signals=signals_json,
            status="filled",
            symbol=display_symbol,
        )
        trade.id = self.db.record_trade(trade)

        logger.info(
            f"[PAPER] SELL {quantity:.6f} {symbol} @ ${fill_price:,.2f} "
            f"(quote ${price:,.2f}, slip {self.slippage_pct:.2f}%) | "
            f"Proceeds: ${net_proceeds:,.2f} (fee: ${fee:.2f}) | "
            f"Cash: ${self.balance.cash_usd:,.2f}"
        )

        return trade
