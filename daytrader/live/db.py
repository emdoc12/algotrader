"""SQLite persistence layer for the live paper-trading agent system.

A thin wrapper over the stdlib ``sqlite3`` module (WAL mode) that stores all
state the autonomous day-trading agent needs to survive container restarts:
round-trip trades, open positions, an agent journal (long-lived memory),
developer requests, equity snapshots, and an agent action log.

Everything is returned as plain ``dict`` rows so callers never have to know
about ``sqlite3.Row``. All queries are parameterized.

DB path comes from the ``DAYTRADER_DB_PATH`` env var, defaulting to
``/home/user/algotrader/cache/daytrader_live.db``.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_DB_PATH = "/home/user/algotrader/cache/daytrader_live.db"


def _now_iso() -> str:
    """Current wall-clock time as an ISO-ish string (UTC-naive local)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class LiveDB:
    """Thin SQLite wrapper. Safe to use from a single-threaded event loop."""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("DAYTRADER_DB_PATH", DEFAULT_DB_PATH)
        # Ensure the parent directory exists (e.g. the cache/ dir).
        parent = Path(self.path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL gives us crash-safe, concurrent-reader persistence.
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._create_tables()

    # ------------------------------------------------------------------ #
    # schema                                                             #
    # ------------------------------------------------------------------ #
    def _create_tables(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT    NOT NULL,
                side          TEXT    NOT NULL,
                strategy      TEXT,
                entry_ts      TEXT,
                entry_price   REAL,
                qty           REAL,
                exit_ts       TEXT,
                exit_price    REAL,
                commission    REAL    DEFAULT 0,
                slippage_cost REAL    DEFAULT 0,
                pnl           REAL,
                exit_reason   TEXT,
                rationale     TEXT
            );

            CREATE TABLE IF NOT EXISTS open_positions (
                symbol      TEXT PRIMARY KEY,
                side        TEXT NOT NULL,
                qty         REAL NOT NULL,
                entry_price REAL NOT NULL,
                entry_ts    TEXT,
                strategy    TEXT,
                stop        REAL,
                target      REAL,
                rationale   TEXT
            );

            CREATE TABLE IF NOT EXISTS journal (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     TEXT NOT NULL,
                author TEXT,
                topic  TEXT,
                note   TEXT
            );

            CREATE TABLE IF NOT EXISTS dev_requests (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     TEXT NOT NULL,
                title  TEXT,
                body   TEXT,
                status TEXT DEFAULT 'open',
                url    TEXT
            );

            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             TEXT NOT NULL,
                cash           REAL,
                equity         REAL,
                open_positions INTEGER,
                drawdown_pct   REAL
            );

            CREATE TABLE IF NOT EXISTS agent_log (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts     TEXT NOT NULL,
                agent  TEXT,
                action TEXT,
                detail TEXT
            );
            """
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # trades                                                             #
    # ------------------------------------------------------------------ #
    def record_trade(self, trade_dict: dict[str, Any]) -> int:
        """Insert a completed round-trip trade. Returns the new row id."""
        cols = (
            "symbol", "side", "strategy", "entry_ts", "entry_price", "qty",
            "exit_ts", "exit_price", "commission", "slippage_cost", "pnl",
            "exit_reason", "rationale",
        )
        params = [trade_dict.get(c) for c in cols]
        cur = self.conn.execute(
            f"INSERT INTO trades ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            params,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_trades(self, limit: int = 100) -> list[dict]:
        """Most recent trades, newest first."""
        cur = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # open positions                                                     #
    # ------------------------------------------------------------------ #
    def upsert_position(self, pos_dict: dict[str, Any]) -> None:
        """Insert or replace an open position keyed by symbol."""
        cols = (
            "symbol", "side", "qty", "entry_price", "entry_ts", "strategy",
            "stop", "target", "rationale",
        )
        params = [pos_dict.get(c) for c in cols]
        self.conn.execute(
            f"INSERT INTO open_positions ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(symbol) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols if c != "symbol"),
            params,
        )
        self.conn.commit()

    def delete_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
        self.conn.commit()

    def load_open_positions(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM open_positions ORDER BY symbol")
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # journal (agent memory)                                             #
    # ------------------------------------------------------------------ #
    def add_journal(self, author: str, topic: str, note: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO journal (ts, author, topic, note) VALUES (?, ?, ?, ?)",
            (_now_iso(), author, topic, note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def recent_journal(self, limit: int = 30) -> list[dict]:
        """Newest journal entries first."""
        cur = self.conn.execute(
            "SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # dev requests                                                        #
    # ------------------------------------------------------------------ #
    def add_dev_request(
        self, title: str, body: str, url: Optional[str] = None, status: str = "open"
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO dev_requests (ts, title, body, status, url) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now_iso(), title, body, status, url),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def open_dev_requests(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM dev_requests WHERE status='open' ORDER BY id DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # equity snapshots                                                    #
    # ------------------------------------------------------------------ #
    def record_equity(
        self, cash: float, equity: float, open_positions: int, drawdown_pct: float
    ) -> None:
        self.conn.execute(
            "INSERT INTO equity_snapshots (ts, cash, equity, open_positions, drawdown_pct) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now_iso(), float(cash), float(equity), int(open_positions), float(drawdown_pct)),
        )
        self.conn.commit()

    def last_equity(self) -> Optional[dict]:
        """Most recent equity snapshot, or None if there are none yet."""
        cur = self.conn.execute(
            "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None

    # ------------------------------------------------------------------ #
    # agent log                                                           #
    # ------------------------------------------------------------------ #
    def log_agent(self, agent: str, action: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO agent_log (ts, agent, action, detail) VALUES (?, ?, ?, ?)",
            (_now_iso(), agent, action, detail),
        )
        self.conn.commit()

    def recent_agent_log(self, limit: int = 100) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM agent_log ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # lifecycle                                                           #
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()
