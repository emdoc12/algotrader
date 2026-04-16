"""
Main bot loop — runs 24/7, scanning for signals on a configurable interval.

Features:
- Graceful shutdown on SIGINT/SIGTERM
- Auto-reconnect on transient API errors
- Periodic equity snapshots for performance tracking
- Structured logging to console + SQLite
"""

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

# Ensure engine/ directory is on the import path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import BotConfig, load_config
from database import Database
from kraken_client import KrakenClient
from paper_trader import PaperTrader
from strategy import TradingStrategy

logger = logging.getLogger("algotrader")


def setup_logging(level: str = "INFO"):
    """Configure structured logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(log_level)

    # Root logger
    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(console)

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class AlgoTraderBot:
    """
    The main trading bot that orchestrates everything.

    Lifecycle:
        1. Initialize config, database, Kraken client
        2. If paper mode, initialize paper trader
        3. Run scan loop on interval
        4. On shutdown, close connections gracefully
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.running = False
        self._shutdown_event = asyncio.Event()

        # Initialize components
        self.db = Database(config.db_path)
        self.kraken = KrakenClient(
            api_key=config.kraken.api_key,
            api_secret=config.kraken.api_secret,
            symbol=config.kraken.symbol,
        )

        # Paper trader (only in paper mode)
        self.paper_trader = None
        if config.mode == "paper":
            self.paper_trader = PaperTrader(
                db=self.db,
                starting_capital=config.paper.starting_capital,
                taker_fee_pct=config.paper.taker_fee_pct,
            )

        # Strategy
        self.strategy = TradingStrategy(
            config=config,
            db=self.db,
            kraken=self.kraken,
            paper_trader=self.paper_trader,
        )

    async def start(self):
        """Start the bot and run until shutdown."""
        self.running = True
        self._print_banner()

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        self.db.log("INFO", f"Bot started in {self.config.mode.upper()} mode")

        # Print initial state
        await self._print_status()

        # Main loop
        scan_count = 0
        while not self._shutdown_event.is_set():
            scan_count += 1
            try:
                result = await self.strategy.run_scan()
                self._log_scan_result(scan_count, result)
            except Exception as e:
                logger.error(f"Scan #{scan_count} failed: {e}")
                self.db.log("ERROR", f"Scan failed: {e}")

            # Wait for next scan or shutdown
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.config.strategy.scan_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass  # Normal — timeout means it's time for the next scan

        # Shutdown
        await self._shutdown()

    def _request_shutdown(self):
        """Signal the bot to shut down gracefully."""
        logger.info("Shutdown requested...")
        self._shutdown_event.set()

    async def _shutdown(self):
        """Clean up resources."""
        logger.info("Shutting down...")
        self.running = False
        self.db.log("INFO", "Bot stopped")
        await self.kraken.close()
        self.db.close()
        logger.info("Goodbye!")

    def _print_banner(self):
        """Print startup banner."""
        c = self.config
        mode_label = "PAPER TRADING" if c.mode == "paper" else "LIVE TRADING"
        logger.info("=" * 60)
        logger.info(f"  AlgoTrader v2.0.0 — Kraken BTC Bot")
        logger.info(f"  Mode: {mode_label}")
        logger.info(f"  Symbol: {c.kraken.display_symbol}")
        logger.info(f"  Candle interval: {c.candle_interval}m")
        logger.info(f"  Scan interval: {c.strategy.scan_interval_seconds}s")
        logger.info(f"  EMA: {c.strategy.ema_fast_period}/{c.strategy.ema_slow_period}")
        logger.info(f"  RSI period: {c.strategy.rsi_period}")
        logger.info(f"  Stop-loss: {c.strategy.stop_loss_pct}% | Take-profit: {c.strategy.take_profit_pct}%")
        if c.mode == "paper":
            balance = self.paper_trader.get_balance()
            logger.info(f"  Starting capital: ${balance.cash_usd:,.2f}")
        logger.info("=" * 60)

    async def _print_status(self):
        """Print current market and account status."""
        try:
            ticker = await self.kraken.get_ticker()
            logger.info(
                f"BTC/USD: ${float(ticker.last):,.2f} | "
                f"Bid: ${float(ticker.bid):,.2f} | "
                f"Ask: ${float(ticker.ask):,.2f} | "
                f"24h Vol: {float(ticker.volume_24h):,.2f}"
            )
        except Exception as e:
            logger.warning(f"Could not fetch initial ticker: {e}")

        position = self.db.get_open_position()
        if position:
            logger.info(
                f"Open position: {position.quantity:.6f} BTC @ ${position.entry_price:,.2f} | "
                f"SL: ${position.stop_loss:,.2f} | TP: ${position.take_profit:,.2f}"
            )
        else:
            logger.info("No open position")

    def _log_scan_result(self, scan_num: int, result: dict):
        """Log the result of a scan cycle."""
        action = result.get("action", "none")
        price = result.get("price", 0)
        rec = result.get("recommendation", "?")
        score = result.get("composite_score", 0)

        if "error" in result:
            logger.warning(f"Scan #{scan_num}: Error — {result['error']}")
            return

        # Build status line
        pos_info = ""
        if result.get("has_position"):
            pnl_pct = result.get("unrealized_pnl_pct", 0)
            pos_info = f" | Position P&L: {pnl_pct:+.2f}%"

        balance_info = ""
        if self.paper_trader:
            bal = self.paper_trader.get_balance()
            balance_info = f" | Equity: ${bal.total_equity:,.2f}"

        logger.info(
            f"Scan #{scan_num}: ${price:,.2f} | "
            f"Signal: {rec} ({score:+.3f}) | "
            f"Action: {action}{pos_info}{balance_info}"
        )


def main():
    """Entry point."""
    config = load_config()
    setup_logging(config.log_level)
    bot = AlgoTraderBot(config)

    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        logger.info("Interrupted")


if __name__ == "__main__":
    main()
