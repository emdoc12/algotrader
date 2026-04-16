import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import * as schema from "@shared/schema";
import path from "path";

// In Docker, DATABASE_URL points to the persistent volume (/app/data/data.db).
// In development it falls back to data.db in the project root.
const dbPath = process.env.DATABASE_URL ?? path.resolve("data.db");
const sqlite = new Database(dbPath);
sqlite.pragma("journal_mode = WAL");

// Auto-create tables on first boot (idempotent — safe to run every startup)
sqlite.exec(`
  CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    username TEXT NOT NULL,
    account_number TEXT NOT NULL,
    session_token TEXT,
    remember_token TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_sandbox INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT 'now'
  );

  CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    platform TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 0,
    account_id INTEGER NOT NULL,
    parameters TEXT NOT NULL DEFAULT '{}',
    scan_interval INTEGER NOT NULL DEFAULT 300,
    last_scan_at TEXT,
    max_position_size REAL NOT NULL DEFAULT 1,
    max_daily_trades INTEGER NOT NULL DEFAULT 5,
    max_buying_power_usage REAL NOT NULL DEFAULT 50,
    created_at TEXT NOT NULL DEFAULT 'now'
  );

  CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    order_id TEXT,
    option_details TEXT,
    pnl REAL,
    notes TEXT,
    executed_at TEXT,
    created_at TEXT NOT NULL DEFAULT 'now'
  );

  CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    quantity REAL NOT NULL,
    average_price REAL NOT NULL,
    current_price REAL,
    market_value REAL,
    unrealized_pnl REAL,
    option_details TEXT,
    updated_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS bot_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL DEFAULT 'info',
    strategy_id INTEGER,
    message TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT 'now'
  );

  CREATE TABLE IF NOT EXISTS watchlist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    notes TEXT
  );
`);

export const db = drizzle(sqlite, { schema });
