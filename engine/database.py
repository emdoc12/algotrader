"""
SQLite persistence layer for the trading bot.

Stores trades, positions, and paper trading state so nothing is lost on restart.
"""

import json
import sqlite3
import time
import logging
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    """A completed or pending trade record."""
    id: Optional[int] = None
    timestamp: float = 0.0
    side: str = ""          # "buy" or "sell"
    price: float = 0.0
    quantity: float = 0.0
    value: float = 0.0      # price * quantity
    fee: float = 0.0
    order_id: str = ""
    mode: str = "paper"     # "paper" or "live"
    strategy: str = ""
    signals: str = ""       # JSON snapshot of signals at time of trade
    status: str = ""        # "filled", "validated", "pending"
    symbol: str = "BTC/USD" # trading pair


@dataclass
class Position:
    """Current open position."""
    id: Optional[int] = None
    symbol: str = "BTC/USD"
    side: str = "long"
    entry_price: float = 0.0
    quantity: float = 0.0
    entry_time: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class PaperBalance:
    """Paper trading account state."""
    cash_usd: float = 10000.0
    btc_quantity: float = 0.0        # Legacy — kept for backward compat
    total_equity: float = 10000.0
    last_updated: float = 0.0
    holdings: dict = None  # {symbol: quantity} e.g. {"BTC": 0.05, "ETH": 1.2}

    def __post_init__(self):
        if self.holdings is None:
            self.holdings = {}
            if self.btc_quantity > 0:
                self.holdings["BTC"] = self.btc_quantity


class Database:
    """SQLite database for bot persistence."""

    def __init__(self, db_path: str = "bot_data.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        """Create tables if they don't exist."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                value REAL NOT NULL,
                fee REAL NOT NULL DEFAULT 0,
                order_id TEXT DEFAULT '',
                mode TEXT DEFAULT 'paper',
                strategy TEXT DEFAULT '',
                signals TEXT DEFAULT '',
                status TEXT DEFAULT 'filled'
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL DEFAULT 'BTC/USD',
                side TEXT NOT NULL DEFAULT 'long',
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                entry_time REAL NOT NULL,
                stop_loss REAL NOT NULL DEFAULT 0,
                take_profit REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS paper_balance (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                cash_usd REAL NOT NULL,
                btc_quantity REAL NOT NULL DEFAULT 0,
                total_equity REAL NOT NULL,
                last_updated REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                data TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                equity REAL NOT NULL,
                cash REAL NOT NULL,
                btc_value REAL NOT NULL,
                btc_price REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                weekly_profit_target REAL DEFAULT 0,
                monthly_profit_target REAL DEFAULT 0,
                weekly_btc_target REAL DEFAULT 0,
                monthly_btc_target REAL DEFAULT 0,
                notes TEXT DEFAULT '',
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                role TEXT NOT NULL,
                message TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS holdings (
                symbol TEXT PRIMARY KEY,
                quantity REAL NOT NULL DEFAULT 0
            );
        """)
        # Add symbol column to trades if not present (migration)
        try:
            self.conn.execute("SELECT symbol FROM trades LIMIT 1")
        except sqlite3.OperationalError:
            self.conn.execute("ALTER TABLE trades ADD COLUMN symbol TEXT DEFAULT 'BTC/USD'")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def record_trade(self, trade: Trade) -> int:
        """Insert a trade record. Returns the trade ID."""
        cursor = self.conn.execute(
            """INSERT INTO trades (timestamp, side, price, quantity, value, fee,
               order_id, mode, strategy, signals, status, symbol)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade.timestamp or time.time(), trade.side, trade.price,
             trade.quantity, trade.value, trade.fee, trade.order_id,
             trade.mode, trade.strategy, trade.signals, trade.status,
             trade.symbol),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_trades(self, limit: int = 50) -> list[Trade]:
        """Get recent trades, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Trade(**dict(row)) for row in rows]

    def get_trades_with_pnl(self, limit: int = 0, since_ts: float = 0) -> list[dict]:
        """Get trades with P&L computed for sells by matching against buys (FIFO).

        Returns list of dicts: each trade dict plus 'pnl_dollar', 'pnl_pct', 'running_pnl'.
        Buys show pnl_dollar=None. Sells show profit/loss vs matched buy cost basis.
        """
        query = "SELECT * FROM trades ORDER BY timestamp ASC"
        rows = self.conn.execute(query).fetchall()
        all_trades = [dict(row) for row in rows]

        # FIFO matching: track buy lots as (price, qty, fee_per_unit)
        buy_lots: list[dict] = []
        results: list[dict] = []
        running_pnl = 0.0

        for t in all_trades:
            t["pnl_dollar"] = None
            t["pnl_pct"] = None

            if t["side"] == "buy":
                buy_lots.append({
                    "price": t["price"],
                    "qty": t["quantity"],
                    "fee_per_unit": t["fee"] / t["quantity"] if t["quantity"] > 0 else 0,
                })
            elif t["side"] == "sell" and buy_lots:
                # Match sell against oldest buys (FIFO)
                sell_qty = t["quantity"]
                sell_revenue = t["price"] * sell_qty
                sell_fee = t["fee"]
                cost_basis = 0.0
                buy_fees = 0.0
                qty_matched = 0.0

                while sell_qty > 0 and buy_lots:
                    lot = buy_lots[0]
                    match_qty = min(sell_qty, lot["qty"])
                    cost_basis += lot["price"] * match_qty
                    buy_fees += lot["fee_per_unit"] * match_qty
                    lot["qty"] -= match_qty
                    sell_qty -= match_qty
                    qty_matched += match_qty
                    if lot["qty"] <= 0.00000001:
                        buy_lots.pop(0)

                total_fees = buy_fees + sell_fee
                pnl = sell_revenue - cost_basis - total_fees
                pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0.0
                running_pnl += pnl
                t["pnl_dollar"] = round(pnl, 2)
                t["pnl_pct"] = round(pnl_pct, 2)

            t["running_pnl"] = round(running_pnl, 2)
            results.append(t)

        # Apply filters
        if since_ts > 0:
            results = [r for r in results if r["timestamp"] >= since_ts]

        # Reverse to newest first
        results.reverse()

        if limit > 0:
            results = results[:limit]

        return results

    def get_trade_stats(self) -> dict:
        """Get aggregate trade statistics."""
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN side = 'buy' THEN 1 ELSE 0 END) as buys,
                SUM(CASE WHEN side = 'sell' THEN 1 ELSE 0 END) as sells,
                SUM(fee) as total_fees,
                SUM(CASE WHEN side = 'sell' THEN value ELSE -value END) as net_flow
            FROM trades
        """).fetchone()
        return dict(row) if row else {}

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def save_position(self, position: Position) -> int:
        """Save or update an open position."""
        if position.id:
            self.conn.execute(
                """UPDATE positions SET entry_price=?, quantity=?, stop_loss=?,
                   take_profit=?, unrealized_pnl=? WHERE id=?""",
                (position.entry_price, position.quantity, position.stop_loss,
                 position.take_profit, position.unrealized_pnl, position.id),
            )
        else:
            cursor = self.conn.execute(
                """INSERT INTO positions (symbol, side, entry_price, quantity,
                   entry_time, stop_loss, take_profit, unrealized_pnl)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (position.symbol, position.side, position.entry_price,
                 position.quantity, position.entry_time or time.time(),
                 position.stop_loss, position.take_profit, position.unrealized_pnl),
            )
            position.id = cursor.lastrowid
        self.conn.commit()
        return position.id

    def get_open_position(self, symbol: str = None) -> Optional[Position]:
        """Get an open position, optionally filtered by symbol."""
        if symbol:
            row = self.conn.execute(
                "SELECT * FROM positions WHERE symbol=? ORDER BY id DESC LIMIT 1", (symbol,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM positions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row:
            return Position(**dict(row))
        return None

    def get_open_positions(self) -> list[Position]:
        """Get ALL open positions (one per coin)."""
        rows = self.conn.execute(
            "SELECT * FROM positions ORDER BY entry_time ASC"
        ).fetchall()
        return [Position(**dict(row)) for row in rows]

    def close_position(self, position_id: int):
        """Remove a closed position."""
        self.conn.execute("DELETE FROM positions WHERE id=?", (position_id,))
        self.conn.commit()

    # ------------------------------------------------------------------
    # Holdings (multi-coin paper balances)
    # ------------------------------------------------------------------

    def get_holdings(self) -> dict:
        """Get all coin holdings. Returns {symbol: quantity}."""
        rows = self.conn.execute("SELECT symbol, quantity FROM holdings").fetchall()
        return {row["symbol"]: row["quantity"] for row in rows}

    def update_holding(self, symbol: str, quantity: float):
        """Set holding quantity for a coin. Removes if quantity <= 0."""
        if quantity <= 0.000000001:
            self.conn.execute("DELETE FROM holdings WHERE symbol=?", (symbol,))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO holdings (symbol, quantity) VALUES (?, ?)",
                (symbol, quantity),
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Paper balance
    # ------------------------------------------------------------------

    def get_paper_balance(self, default_capital: float = 10000.0) -> PaperBalance:
        """Get paper trading balance, creating default if none exists."""
        row = self.conn.execute("SELECT * FROM paper_balance WHERE id=1").fetchone()
        if row:
            return PaperBalance(**{k: row[k] for k in ["cash_usd", "btc_quantity", "total_equity", "last_updated"]})

        # Initialize
        balance = PaperBalance(
            cash_usd=default_capital,
            btc_quantity=0.0,
            total_equity=default_capital,
            last_updated=time.time(),
        )
        self.save_paper_balance(balance)
        return balance

    def save_paper_balance(self, balance: PaperBalance):
        """Save paper trading balance."""
        self.conn.execute(
            """INSERT OR REPLACE INTO paper_balance (id, cash_usd, btc_quantity, total_equity, last_updated)
               VALUES (1, ?, ?, ?, ?)""",
            (balance.cash_usd, balance.btc_quantity, balance.total_equity, balance.last_updated),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Performance tracking
    # ------------------------------------------------------------------

    def record_equity_snapshot(self, equity: float, cash: float, btc_value: float, btc_price: float):
        """Record a point-in-time equity snapshot for charting."""
        self.conn.execute(
            "INSERT INTO performance (timestamp, equity, cash, btc_value, btc_price) VALUES (?, ?, ?, ?, ?)",
            (time.time(), equity, cash, btc_value, btc_price),
        )
        self.conn.commit()

    def get_equity_history(self, limit: int = 1000) -> list[dict]:
        """Get equity history for charting."""
        rows = self.conn.execute(
            "SELECT * FROM performance ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log(self, level: str, message: str, data: str = ""):
        """Write a log entry to the database."""
        self.conn.execute(
            "INSERT INTO bot_log (timestamp, level, message, data) VALUES (?, ?, ?, ?)",
            (time.time(), level, message, data),
        )
        self.conn.commit()

    def get_logs(self, limit: int = 100, level: Optional[str] = None) -> list[dict]:
        """Get recent log entries."""
        if level:
            rows = self.conn.execute(
                "SELECT * FROM bot_log WHERE level=? ORDER BY timestamp DESC LIMIT ?",
                (level, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM bot_log ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Goals
    # ------------------------------------------------------------------

    def get_goals(self) -> dict:
        """Get current profit goals."""
        row = self.conn.execute("SELECT * FROM goals WHERE id=1").fetchone()
        if row:
            return dict(row)
        return {
            "weekly_profit_target": 0,
            "monthly_profit_target": 0,
            "weekly_btc_target": 0,
            "monthly_btc_target": 0,
            "notes": "",
        }

    def save_goals(self, weekly_profit: float = 0, monthly_profit: float = 0,
                   weekly_btc: float = 0, monthly_btc: float = 0, notes: str = ""):
        """Save profit goals."""
        self.conn.execute(
            """INSERT OR REPLACE INTO goals
               (id, weekly_profit_target, monthly_profit_target, weekly_btc_target, monthly_btc_target, notes, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?)""",
            (weekly_profit, monthly_profit, weekly_btc, monthly_btc, notes, time.time()),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # Chat history
    # ------------------------------------------------------------------

    def add_chat_message(self, role: str, message: str):
        """Add a chat message (role: 'user' or 'assistant')."""
        self.conn.execute(
            "INSERT INTO chat_history (timestamp, role, message) VALUES (?, ?, ?)",
            (time.time(), role, message),
        )
        self.conn.commit()

    def get_chat_history(self, limit: int = 50) -> list[dict]:
        """Get recent chat messages, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM chat_history ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_chat_history(self):
        """Clear all chat messages."""
        self.conn.execute("DELETE FROM chat_history")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Weekly/Monthly P&L calculations
    # ------------------------------------------------------------------

    def get_period_pnl(self, seconds_ago: int) -> dict:
        """Calculate P&L for a time period (e.g., 7*86400 for weekly)."""
        cutoff = time.time() - seconds_ago
        rows = self.conn.execute(
            "SELECT side, value, fee FROM trades WHERE timestamp >= ?", (cutoff,)
        ).fetchall()

        buys = sum(r["value"] + r["fee"] for r in rows if r["side"] == "buy")
        sells = sum(r["value"] - r["fee"] for r in rows if r["side"] == "sell")
        trade_count = len(rows)

        return {
            "trade_count": trade_count,
            "total_bought": buys,
            "total_sold": sells,
            "realized_pnl": sells - buys if sells > 0 else 0,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        """Close database connection."""
        self.conn.close()
