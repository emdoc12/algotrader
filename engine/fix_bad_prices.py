#!/usr/bin/env python3
"""
Migration script: Fix trades recorded with wrong prices.

Bug: Non-BTC coins (DOT, ETH, SOL, etc.) were matched against the Kraken
pair name ("DOTUSD") but the market scanner stores the friendly name ("DOT").
The lookup always failed and fell back to the BTC price (~$74k), so all
altcoin trades were recorded at the BTC price instead of the coin's real price.

This script:
  1. Identifies all non-BTC trades where the price looks like BTC (> $1000)
  2. Calculates the net cash damage from those trades
  3. Deletes the bad trades
  4. Closes any open positions for affected coins
  5. Resets holdings for affected coins
  6. Restores the cash balance to what it would be without the bad trades

Run inside the container:
    docker exec -it <container> python3 engine/fix_bad_prices.py

Or with a volume mount:
    python3 fix_bad_prices.py --db /path/to/bot_data.db
"""

import argparse
import os
import sqlite3
import sys
import time


# Price thresholds per coin — any trade above this is clearly using BTC price
# These are generous upper bounds; real prices are far below
MAX_REASONABLE_PRICE = {
    "DOT/USD": 100,
    "ETH/USD": 10000,     # ETH could be high, but not $74k
    "SOL/USD": 1000,
    "DOGE/USD": 10,
    "ADA/USD": 50,
    "AVAX/USD": 500,
    "LINK/USD": 500,
    "POL/USD": 50,
    "XRP/USD": 50,
}

# Default: anything non-BTC above $5000 is suspicious
DEFAULT_MAX_PRICE = 5000


def fix_bad_trades(db_path: str, dry_run: bool = False):
    """Find and fix trades recorded with wrong (BTC) prices."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    print(f"\n{'=' * 60}")
    print(f"  Bad Price Migration Tool")
    print(f"  Database: {db_path}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE — WILL MODIFY DATABASE'}")
    print(f"{'=' * 60}\n")

    # Find all non-BTC trades
    rows = conn.execute(
        "SELECT * FROM trades WHERE symbol != 'BTC/USD' ORDER BY timestamp ASC"
    ).fetchall()

    if not rows:
        print("No non-BTC trades found. Nothing to fix.")
        conn.close()
        return

    print(f"Found {len(rows)} non-BTC trades total.\n")

    # Identify bad trades
    bad_trades = []
    good_trades = []
    for row in rows:
        r = dict(row)
        symbol = r["symbol"]
        max_price = MAX_REASONABLE_PRICE.get(symbol, DEFAULT_MAX_PRICE)

        if r["price"] > max_price:
            bad_trades.append(r)
        else:
            good_trades.append(r)

    if not bad_trades:
        print("No trades with obviously wrong prices found. All looks good!")
        conn.close()
        return

    print(f"Found {len(bad_trades)} trades with wrong (BTC) prices:")
    print(f"{'─' * 80}")

    # Summarize by coin
    coins_affected = {}
    total_cash_damage = 0.0

    for t in bad_trades:
        sym = t["symbol"]
        if sym not in coins_affected:
            coins_affected[sym] = {
                "buys": 0, "sells": 0,
                "cash_spent_on_buys": 0.0,
                "cash_received_from_sells": 0.0,
                "total_fees": 0.0,
                "trades": [],
            }

        info = coins_affected[sym]
        info["trades"].append(t)
        info["total_fees"] += t["fee"]

        if t["side"] == "buy":
            info["buys"] += 1
            # Cash went DOWN by value + fee
            info["cash_spent_on_buys"] += t["value"] + t["fee"]
        elif t["side"] == "sell":
            info["sells"] += 1
            # Cash went UP by value - fee
            info["cash_received_from_sells"] += t["value"] - t["fee"]

    for sym, info in coins_affected.items():
        net_cash = info["cash_received_from_sells"] - info["cash_spent_on_buys"]
        info["net_cash_impact"] = net_cash
        total_cash_damage += net_cash

        print(f"\n  {sym}:")
        print(f"    Bad trades: {info['buys']} buys, {info['sells']} sells")
        print(f"    Cash spent on buys:       ${info['cash_spent_on_buys']:>12,.2f}")
        print(f"    Cash received from sells: ${info['cash_received_from_sells']:>12,.2f}")
        print(f"    Net cash impact:          ${net_cash:>12,.2f}")
        print(f"    Total fees paid:          ${info['total_fees']:>12,.2f}")

        # Show a few example trades
        for t in info["trades"][:3]:
            ts = time.strftime('%m/%d %H:%M:%S', time.localtime(t["timestamp"]))
            print(f"      [{ts}] {t['side'].upper()} {t['quantity']:.6f} @ ${t['price']:,.2f} "
                  f"(value: ${t['value']:,.2f}, fee: ${t['fee']:.2f})")
        if len(info["trades"]) > 3:
            print(f"      ... and {len(info['trades']) - 3} more")

    print(f"\n{'─' * 80}")
    print(f"  TOTAL CASH DAMAGE: ${total_cash_damage:,.2f}")
    print(f"  (negative = money lost from account due to bad trades)")
    print(f"{'─' * 80}")

    # Get current balance
    bal_row = conn.execute("SELECT * FROM paper_balance WHERE id = 1").fetchone()
    if bal_row:
        current_cash = bal_row["cash_usd"]
        current_equity = bal_row["total_equity"]
        print(f"\n  Current cash:   ${current_cash:,.2f}")
        print(f"  Current equity: ${current_equity:,.2f}")
        restored_cash = current_cash - total_cash_damage  # subtracting a negative = adding
        print(f"  Restored cash:  ${restored_cash:,.2f}")

    if dry_run:
        print(f"\n  DRY RUN — no changes made. Run without --dry-run to apply fixes.")
        conn.close()
        return

    # Confirm
    print(f"\n  This will:")
    print(f"    1. Delete {len(bad_trades)} bad trades from the database")
    print(f"    2. Close all positions for affected coins")
    print(f"    3. Reset holdings for affected coins to 0")
    print(f"    4. Restore ${abs(total_cash_damage):,.2f} to cash balance")

    # Apply fixes
    print(f"\n  Applying fixes...")

    # 1. Delete bad trades
    bad_ids = [t["id"] for t in bad_trades]
    for tid in bad_ids:
        conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
    print(f"    Deleted {len(bad_ids)} bad trades")

    # 2. Close positions for affected coins
    for sym in coins_affected:
        conn.execute("DELETE FROM positions WHERE symbol = ?", (sym,))
    print(f"    Closed positions for: {', '.join(coins_affected.keys())}")

    # 3. Reset holdings for affected coins
    for sym in coins_affected:
        # Extract base coin from display symbol (e.g., "DOT/USD" -> "DOT")
        base = sym.split("/")[0]
        conn.execute("DELETE FROM holdings WHERE symbol = ?", (base,))
    print(f"    Reset holdings for: {', '.join(s.split('/')[0] for s in coins_affected)}")

    # 4. Restore cash balance
    if bal_row:
        conn.execute(
            "UPDATE paper_balance SET cash_usd = ?, total_equity = ?, last_updated = ? WHERE id = 1",
            (restored_cash, restored_cash, time.time()),
        )
        print(f"    Cash restored: ${current_cash:,.2f} → ${restored_cash:,.2f}")

    # 5. Log the migration
    conn.execute(
        "INSERT INTO bot_log (timestamp, level, message, data) VALUES (?, ?, ?, ?)",
        (time.time(), "INFO",
         f"Migration: Removed {len(bad_trades)} trades with wrong prices. "
         f"Restored ${abs(total_cash_damage):,.2f} to cash. Affected coins: {', '.join(coins_affected.keys())}",
         ""),
    )

    conn.commit()
    conn.close()

    print(f"\n  ✓ Migration complete!")
    print(f"    Restart the bot to pick up the corrected state.\n")


def main():
    parser = argparse.ArgumentParser(description="Fix trades recorded with wrong (BTC) prices")
    parser.add_argument("--db", default="/app/data/bot_data.db",
                        help="Path to bot_data.db (default: /app/data/bot_data.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fixed without making changes")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: Database not found at {args.db}")
        sys.exit(1)

    fix_bad_trades(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
