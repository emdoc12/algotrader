"""
Base strategy class — all strategies inherit from this.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from tastytrade import Session, Account

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """
    Each strategy receives its config dict from the API and is responsible
    for one scan() call. The engine scheduler calls scan() on the interval
    defined in strategy.scanInterval.

    For Kraken-based strategies, the engine injects `kraken` (KrakenSessionManager)
    in addition to session/account. Tastytrade session/account may be None for
    pure-Kraken strategies.
    """

    def __init__(self, config: dict, session: Session | None, account: Account | None, kraken=None):
        self.config = config
        self.session = session
        self.account = account
        self.kraken = kraken        # KrakenSessionManager, injected for kraken platform strategies
        self.strategy_id: int = config["id"]
        self.account_id: int = config["accountId"]
        self.name: str = config["name"]
        self.platform: str = config["platform"]

        import json
        raw = config.get("parameters", "{}")
        self.params: dict = json.loads(raw) if isinstance(raw, str) else raw

        self.max_position_size: float = config.get("maxPositionSize", 1)
        self.max_daily_trades: int = config.get("maxDailyTrades", 5)
        self.max_bp_usage: float = config.get("maxBuyingPowerUsage", 50)

        # Paper trading = dry run (no real orders). Defaults to paper for safety.
        trading_mode = config.get("tradingMode", "paper")
        self.dry_run: bool = (trading_mode != "live")

        self._daily_trade_count = 0
        self._logger = logging.getLogger(f"strategy.{self.name}")

    async def run(self):
        """Called by the scheduler. Checks limits then delegates to scan()."""
        if self._daily_trade_count >= self.max_daily_trades:
            self._logger.info("Daily trade limit (%d) reached — skipping.", self.max_daily_trades)
            return
        try:
            await self.scan()
        except Exception as e:
            self._logger.exception("Unhandled error during scan: %s", e)
            import api_client
            await api_client.post_log(
                "error",
                f"Strategy '{self.name}' scan failed: {e}",
                strategy_id=self.strategy_id,
            )

    @abstractmethod
    async def scan(self):
        """Scan for opportunities and place orders if conditions are met."""
        ...

    def _increment_trades(self):
        self._daily_trade_count += 1

    def reset_daily_count(self):
        self._daily_trade_count = 0
