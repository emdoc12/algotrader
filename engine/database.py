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
    btc_quantity: float = 0.0
    total_equity: float = 10000.0
    last_updated: float = 0.0


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
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def record_trade(self, trade: Trade) -> int:
        """Insert a trade record. Returns the trade ID."""
        cursor = self.conn.execute(
            """INSERT INTO trades (timestamp, side, price, quantity, value, fee,
               order_id, mode, strategy, signals, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade.timestamp or time.time(), trade.side, trade.price,
             trade.quantity, trade.value, trade.fee, trade.order_id,
             trade.mode, trade.strategy, trade.signals, trade.status),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_trades(self, limit: int = 50) -> list[Trade]:
        """Get recent trades, newest first."""
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Trade(**dict(row)) for row in rows]

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

    def get_open_position(self) -> Optional[Position]:
        """Get the current open position (we only hold one at a time)."""
        row = self.conn.execute(
            "SELECT * FROM positions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            return Position(**dict(row))
        return None

    def close_position(self, position_id: int):
        """Remove a closed position."""
        self.conn.execute("DELETE FROM positions WHERE id=?", (position_id,))
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
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        """Close database connection."""
        self.conn.close()
