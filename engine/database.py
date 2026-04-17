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

            CREATE INDEX IF NOT EXISTS idx_performance_ts ON performance(timestamp);

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

            CREATE TABLE IF NOT EXISTS strategy_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                category TEXT NOT NULL DEFAULT 'observation',
                coin TEXT DEFAULT '',
                strategy TEXT DEFAULT '',
                lesson TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                source TEXT DEFAULT 'trade'
            );

            CREATE TABLE IF NOT EXISTS operator_directives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                directive TEXT NOT NULL,
                source TEXT DEFAULT 'chat',
                active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS research_notebook (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                topic TEXT NOT NULL DEFAULT 'general',
                coins TEXT DEFAULT '',
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                source TEXT DEFAULT 'ai',
                still_relevant INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS ai_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                coin TEXT NOT NULL,
                condition TEXT NOT NULL,
                threshold REAL NOT NULL,
                reason TEXT DEFAULT '',
                action_plan TEXT DEFAULT '',
                created_at REAL NOT NULL,
                triggered_at REAL DEFAULT 0,
                active INTEGER DEFAULT 1
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

    def close_all_positions_for_symbol(self, symbol: str):
        """Remove ALL position rows for a symbol (cleanup after full sell)."""
        self.conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
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
        # Prune old data periodically (every ~100 inserts)
        count = self.conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
        if count > 10000 and count % 100 == 0:
            self._prune_performance_history()

    def _prune_performance_history(self):
        """Thin out old performance rows to prevent unbounded table growth.

        Keeps:
        - All rows from last 7 days (full resolution)
        - One row per hour for 7-30 days old
        - One row per day for 30+ days old
        """
        now = time.time()
        week_ago = now - 7 * 86400
        month_ago = now - 30 * 86400

        # Thin 7-30 day range: keep one per hour
        self.conn.execute("""
            DELETE FROM performance
            WHERE timestamp < ? AND timestamp >= ?
            AND id NOT IN (
                SELECT MIN(id) FROM performance
                WHERE timestamp < ? AND timestamp >= ?
                GROUP BY CAST(timestamp / 3600 AS INTEGER)
            )
        """, (week_ago, month_ago, week_ago, month_ago))

        # Thin 30+ day range: keep one per day
        self.conn.execute("""
            DELETE FROM performance
            WHERE timestamp < ?
            AND id NOT IN (
                SELECT MIN(id) FROM performance
                WHERE timestamp < ?
                GROUP BY CAST(timestamp / 86400 AS INTEGER)
            )
        """, (month_ago, month_ago))

        self.conn.commit()
        remaining = self.conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
        logger.debug(f"Performance table pruned, {remaining} rows remaining")

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
    # Operator directives — persistent instructions from chat
    # ------------------------------------------------------------------

    def add_directive(self, directive: str, source: str = "chat") -> int:
        """Save a standing instruction from the operator."""
        cursor = self.conn.execute(
            "INSERT INTO operator_directives (timestamp, directive, source, active) VALUES (?, ?, ?, 1)",
            (time.time(), directive, source),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_active_directives(self) -> list[dict]:
        """Get all active operator directives."""
        rows = self.conn.execute(
            "SELECT * FROM operator_directives WHERE active = 1 ORDER BY timestamp ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def deactivate_directive(self, directive_id: int):
        """Deactivate a directive (operator changed their mind)."""
        self.conn.execute(
            "UPDATE operator_directives SET active = 0 WHERE id = ?", (directive_id,)
        )
        self.conn.commit()

    def clear_directives(self):
        """Deactivate all directives."""
        self.conn.execute("UPDATE operator_directives SET active = 0")
        self.conn.commit()

    # ------------------------------------------------------------------
    # Strategy journal — persistent AI memory
    # ------------------------------------------------------------------

    def add_journal_entry(self, lesson: str, category: str = "observation",
                          coin: str = "", strategy: str = "",
                          confidence: float = 0.5, source: str = "trade") -> int:
        """Write a lesson/observation to the strategy journal."""
        cursor = self.conn.execute(
            """INSERT INTO strategy_journal
               (timestamp, category, coin, strategy, lesson, confidence, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), category, coin, strategy, lesson, confidence, source),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_journal_entries(self, limit: int = 20, category: str = "",
                            coin: str = "") -> list[dict]:
        """Get recent journal entries, newest first."""
        query = "SELECT * FROM strategy_journal"
        params = []
        conditions = []
        if category:
            conditions.append("category = ?")
            params.append(category)
        if coin:
            conditions.append("coin = ?")
            params.append(coin)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_journal_summary(self, limit: int = 15) -> list[dict]:
        """Get the most recent high-confidence journal entries for AI context."""
        rows = self.conn.execute(
            """SELECT * FROM strategy_journal
               WHERE confidence >= 0.5
               ORDER BY confidence DESC, timestamp DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Research notebook — dedicated long-form AI memory
    # ------------------------------------------------------------------

    def add_research_note(self, title: str, body: str, topic: str = "general",
                          coins: str = "", source: str = "ai") -> int:
        """Write a research note to the notebook."""
        cursor = self.conn.execute(
            """INSERT INTO research_notebook
               (timestamp, topic, coins, title, body, source, still_relevant)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (time.time(), topic, coins, title, body, source),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_research_notes(self, topic: str = "", coins: str = "",
                           only_relevant: bool = True) -> list[dict]:
        """Get all research notes, newest first. No limit — Claude sees everything."""
        query = "SELECT * FROM research_notebook"
        params = []
        conditions = []
        if only_relevant:
            conditions.append("still_relevant = 1")
        if topic:
            conditions.append("topic = ?")
            params.append(topic)
        if coins:
            conditions.append("coins LIKE ?")
            params.append(f"%{coins}%")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def mark_research_note_stale(self, note_id: int):
        """Mark a research note as no longer relevant (soft delete)."""
        self.conn.execute(
            "UPDATE research_notebook SET still_relevant = 0 WHERE id = ?",
            (note_id,),
        )
        self.conn.commit()

    def get_research_note_count(self) -> int:
        """Count active research notes."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM research_notebook WHERE still_relevant = 1"
        ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Performance stats for AI context
    # ------------------------------------------------------------------

    def get_performance_stats(self) -> dict:
        """Compute full performance statistics from trade history.

        Returns dict with: win_rate, avg_win, avg_loss, profit_factor,
        max_drawdown, max_drawdown_pct, total_pnl, total_fees,
        by_coin (per-coin stats), by_strategy (per-strategy stats).
        """
        trades = self.get_trades_with_pnl()  # newest-first
        trades.reverse()  # oldest-first for drawdown calc

        sells = [t for t in trades if t["side"] == "sell" and t["pnl_dollar"] is not None]
        winners = [t for t in sells if t["pnl_dollar"] > 0]
        losers = [t for t in sells if t["pnl_dollar"] <= 0]

        total_sells = len(sells)
        win_rate = (len(winners) / total_sells * 100) if total_sells else 0
        avg_win = (sum(t["pnl_dollar"] for t in winners) / len(winners)) if winners else 0
        avg_loss = (sum(t["pnl_dollar"] for t in losers) / len(losers)) if losers else 0
        gross_profit = sum(t["pnl_dollar"] for t in winners)
        gross_loss = abs(sum(t["pnl_dollar"] for t in losers)) if losers else 0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0)
        total_pnl = sum(t["pnl_dollar"] for t in sells)
        total_fees = sum(t.get("fee", 0) for t in trades)

        # Max drawdown from running P&L curve
        peak_pnl = 0.0
        max_dd = 0.0
        running = 0.0
        for t in sells:
            running += t["pnl_dollar"]
            if running > peak_pnl:
                peak_pnl = running
            dd = peak_pnl - running
            if dd > max_dd:
                max_dd = dd

        # Per-coin breakdown
        by_coin = {}
        for t in sells:
            sym = t.get("symbol", "BTC/USD")
            if sym not in by_coin:
                by_coin[sym] = {"wins": 0, "losses": 0, "pnl": 0.0}
            if t["pnl_dollar"] > 0:
                by_coin[sym]["wins"] += 1
            else:
                by_coin[sym]["losses"] += 1
            by_coin[sym]["pnl"] += t["pnl_dollar"]
        for sym in by_coin:
            total = by_coin[sym]["wins"] + by_coin[sym]["losses"]
            by_coin[sym]["win_rate"] = (by_coin[sym]["wins"] / total * 100) if total else 0
            by_coin[sym]["pnl"] = round(by_coin[sym]["pnl"], 2)

        # Per-strategy breakdown
        by_strategy = {}
        for t in sells:
            strat = t.get("strategy", "unknown") or "unknown"
            # Clean up "ai_" prefix for readability
            strat = strat.replace("ai_", "")
            if strat not in by_strategy:
                by_strategy[strat] = {"wins": 0, "losses": 0, "pnl": 0.0}
            if t["pnl_dollar"] > 0:
                by_strategy[strat]["wins"] += 1
            else:
                by_strategy[strat]["losses"] += 1
            by_strategy[strat]["pnl"] += t["pnl_dollar"]
        for strat in by_strategy:
            total = by_strategy[strat]["wins"] + by_strategy[strat]["losses"]
            by_strategy[strat]["win_rate"] = (by_strategy[strat]["wins"] / total * 100) if total else 0
            by_strategy[strat]["pnl"] = round(by_strategy[strat]["pnl"], 2)

        return {
            "total_trades": len(trades),
            "total_sells": total_sells,
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "max_drawdown": round(max_dd, 2),
            "by_coin": by_coin,
            "by_strategy": by_strategy,
        }

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
