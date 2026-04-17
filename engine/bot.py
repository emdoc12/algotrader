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

from ai_strategy import AIStrategy
from config import BotConfig, load_config
from dashboard import Dashboard
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
        self._last_digest_week: int = 0  # ISO week number of last digest sent

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

        # Strategy — AI-powered or indicator-based fallback
        if config.use_ai_strategy and config.anthropic_api_key:
            self.strategy = AIStrategy(
                config=config,
                db=self.db,
                kraken=self.kraken,
                paper_trader=self.paper_trader,
            )
            self._using_ai = True
        else:
            self.strategy = TradingStrategy(
                config=config,
                db=self.db,
                kraken=self.kraken,
                paper_trader=self.paper_trader,
            )
            self._using_ai = False
            if config.use_ai_strategy and not config.anthropic_api_key:
                logger.warning("AI strategy enabled but no ANTHROPIC_API_KEY set — using indicator fallback")

        # Web dashboard
        self.dashboard = Dashboard(
            db=self.db,
            paper_trader=self.paper_trader,
            config=config,
            bot=self,
        )

    def _version(self) -> str:
        """Read version from VERSION file."""
        try:
            version_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "VERSION")
            with open(version_path) as f:
                return f.read().strip()
        except Exception:
            return "2.10.3"

    async def start(self):
        """Start the bot and run until shutdown."""
        self.running = True
        self._print_banner()

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        self.db.log("INFO", f"Bot started in {self.config.mode.upper()} mode")

        # Start web dashboard
        dashboard_port = int(os.getenv("DASHBOARD_PORT", "3737"))
        self._dashboard_runner = await self.dashboard.start(port=dashboard_port)

        # Send Discord startup notification
        if self._using_ai and hasattr(self.strategy, 'discord'):
            await self.strategy.discord.send_startup(mode=self.config.mode, version=self._version())

        # Print initial state
        await self._print_status()

        # Main loop
        scan_count = 0
        while not self._shutdown_event.is_set():
            scan_count += 1
            try:
                result = await self.strategy.run_scan()
                self._log_scan_result(scan_count, result)
                # Feed latest data to dashboard
                if "price" in result:
                    signals = result.get("signals")
                    signals_dict = {}
                    if signals and hasattr(signals, 'ema'):
                        signals_dict = {
                            "recommendation": signals.recommendation,
                            "composite": signals.composite_score,
                            "ema_fast": round(signals.ema.fast_ema, 2),
                            "ema_slow": round(signals.ema.slow_ema, 2),
                            "ema_crossover": signals.ema.crossover,
                            "rsi": round(signals.rsi.rsi, 2),
                            "rsi_signal": signals.rsi.signal,
                            "bb_position": round(signals.bollinger.price_position, 4),
                        }
                    # Add AI decision info if available
                    ai_decision = result.get("ai_decision")
                    if ai_decision:
                        signals_dict["ai_action"] = ai_decision.action
                        signals_dict["ai_symbol"] = getattr(ai_decision, 'symbol', 'BTC/USD')
                        signals_dict["ai_confidence"] = ai_decision.confidence
                        signals_dict["ai_reasoning"] = ai_decision.reasoning
                        signals_dict["ai_outlook"] = ai_decision.market_outlook
                        signals_dict["ai_strategy"] = ai_decision.strategy_used
                        signals_dict["recommendation"] = ai_decision.action
                        signals_dict["composite"] = ai_decision.confidence

                    # Add sentiment if available
                    sentiment = result.get("sentiment")
                    if sentiment and hasattr(sentiment, 'fear_greed_value'):
                        signals_dict["fear_greed"] = sentiment.fear_greed_value
                        signals_dict["fear_greed_label"] = sentiment.fear_greed_label
                        signals_dict["news_sentiment"] = sentiment.news_sentiment_summary

                    # Add market overview if available
                    mkt = result.get("market_overview")
                    if mkt and hasattr(mkt, 'coin_snapshots'):
                        signals_dict["market_momentum"] = mkt.market_momentum
                        signals_dict["sector_rotation"] = mkt.sector_rotation_signal
                        signals_dict["top_movers"] = ", ".join(mkt.top_movers[:3]) if mkt.top_movers else ""
                        signals_dict["coins_scanned"] = len(mkt.coin_snapshots)
                        signals_dict["coin_data"] = [
                            {
                                "symbol": c.symbol,
                                "price": c.price,
                                "change_1h": c.change_1h,
                                "change_24h": c.change_24h,
                                "rsi": c.rsi,
                                "rsi_signal": c.rsi_signal,
                                "momentum": c.momentum_score,
                                "ema_crossover": c.ema_crossover,
                                "bb_position": c.bb_position,
                                "composite_score": c.composite_score,
                                "recommendation": c.recommendation,
                            }
                            for c in mkt.coin_snapshots[:10]
                        ]

                    self.dashboard.update_signals(result["price"], signals_dict)
            except Exception as e:
                logger.error(f"Scan #{scan_count} failed: {e}")
                self.db.log("ERROR", f"Scan failed: {e}")

            # Monday morning weekly digest check
            await self._check_weekly_digest()

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

    async def _check_weekly_digest(self):
        """Send weekly digest on Monday mornings (7-8 AM Eastern)."""
        if not self._using_ai or not hasattr(self.strategy, 'generate_weekly_digest'):
            return

        now = datetime.now(timezone.utc)
        # Convert UTC to Eastern: UTC-5 (EST) or UTC-4 (EDT)
        # Check both windows so it works year-round regardless of DST
        utc_hour = now.hour
        is_monday = now.weekday() == 0
        # 7 AM EDT = 11 UTC, 7 AM EST = 12 UTC — cover both
        is_digest_hour = 11 <= utc_hour < 13
        current_week = now.isocalendar()[1]

        if is_monday and is_digest_hour and current_week != self._last_digest_week:
            try:
                logger.info("Monday morning — generating weekly digest")
                await self.strategy.generate_weekly_digest()
                self._last_digest_week = current_week
                self.db.log("INFO", f"Weekly digest sent (week {current_week})")
            except Exception as e:
                logger.error(f"Weekly digest failed: {e}")
                self.db.log("ERROR", f"Weekly digest failed: {e}")

    def _request_shutdown(self):
        """Signal the bot to shut down gracefully."""
        logger.info("Shutdown requested...")
        self._shutdown_event.set()

    async def _shutdown(self):
        """Clean up resources."""
        logger.info("Shutting down...")
        self.running = False
        if self._dashboard_runner:
            await self._dashboard_runner.cleanup()
        if self._using_ai and hasattr(self.strategy, 'close'):
            await self.strategy.close()
        self.db.log("INFO", "Bot stopped")
        await self.kraken.close()
        self.db.close()
        logger.info("Goodbye!")

    def _print_banner(self):
        """Print startup banner."""
        c = self.config
        mode_label = "PAPER TRADING" if c.mode == "paper" else "LIVE TRADING"
        logger.info("=" * 60)
        ai_label = "AI-Powered (Claude)" if self._using_ai else "Indicator-Based"
        logger.info(f"  AlgoTrader v{self._version()} — Kraken Crypto Bot")
        logger.info(f"  Strategy: {ai_label}")
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
