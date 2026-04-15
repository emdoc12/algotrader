"""
Engine — the main orchestrator.
Polls the AlgoTrader API for enabled strategies, maps them to strategy
classes, and schedules each on its configured scan interval.
Also resets daily trade counts at midnight and handles graceful shutdown.

Supports two platforms:
  - tastytrade / tasty_crypto : uses Tastytrade SDK (Session + Account)
  - kraken                    : uses KrakenSessionManager (24/7 spot crypto)
"""
import asyncio
import logging
import signal
from datetime import datetime, time, timedelta

import api_client
from config import LOG_LEVEL, TASTYTRADE_ENABLED, KRAKEN_ENABLED
from session_manager import SessionManager
from strategies import STRATEGY_MAP, STREAMING_STRATEGY_TYPES
from strategies.base import BaseStrategy

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("engine")

# Platforms that use Kraken instead of Tastytrade
KRAKEN_PLATFORMS = {"kraken"}
TASTYTRADE_PLATFORMS = {"tastytrade", "tasty_crypto"}


class AlgoEngine:
    def __init__(self):
        self.session_mgr = SessionManager() if TASTYTRADE_ENABLED else None
        self._kraken = None
        self._tasks: list[asyncio.Task] = []
        self._instances: dict[int, BaseStrategy] = {}
        self._scanner_instances: dict[int, object] = {}   # BullflowScanner instances
        self._running = False

    async def start(self):
        self._running = True

        # Connect Tastytrade
        if TASTYTRADE_ENABLED and self.session_mgr:
            await self.session_mgr.connect()
            logger.info("Tastytrade session ready.")
        else:
            logger.warning("TT_USERNAME/TT_PASSWORD not set — Tastytrade disabled.")

        # Connect Kraken
        if KRAKEN_ENABLED:
            from kraken_session_manager import KrakenSessionManager
            self._kraken = KrakenSessionManager()
            await self._kraken.connect()
            logger.info("Kraken session ready.")
        else:
            logger.warning("KRAKEN_API_KEY/KRAKEN_API_SECRET not set — Kraken disabled.")

        import config
        logger.info("AlgoTrader engine started. DRY_RUN=%s", config.DRY_RUN)
        await api_client.post_log("info", f"AlgoTrader engine started (DRY_RUN={config.DRY_RUN}).")

        loader = asyncio.create_task(self._strategy_loader())
        resetter = asyncio.create_task(self._midnight_resetter())
        self._tasks = [loader, resetter]

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        for scanner in self._scanner_instances.values():
            await scanner.stop()
        if self.session_mgr:
            await self.session_mgr.disconnect()
        if self._kraken:
            await self._kraken.disconnect()
        logger.info("Engine stopped.")
        await api_client.post_log("info", "AlgoTrader Python engine stopped.")

    async def _strategy_loader(self):
        """
        Every 60 seconds, fetches the current enabled strategies from the API.
        Starts new strategy tasks, cancels tasks for disabled/removed strategies.
        """
        strategy_tasks: dict[int, asyncio.Task] = {}

        while self._running:
            try:
                enabled = await api_client.get_strategies()
                enabled_ids = {s["id"] for s in enabled}
                accounts_data = await api_client.get_accounts()
                accounts_by_id = {a["id"]: a for a in accounts_data}

                # Cancel tasks for strategies no longer enabled
                for sid in list(strategy_tasks):
                    if sid not in enabled_ids:
                        logger.info("Strategy %d disabled — stopping.", sid)
                        strategy_tasks[sid].cancel()
                        del strategy_tasks[sid]
                        self._instances.pop(sid, None)

                # Start tasks for newly enabled strategies
                for s in enabled:
                    sid = s["id"]
                    if sid in strategy_tasks:
                        continue  # already running

                    strategy_type = s.get("type", "")
                    platform = s.get("platform", "tastytrade")

                    # ── Streaming strategies (Bullflow scanner) ───────────────
                    if strategy_type in STREAMING_STRATEGY_TYPES:
                        await self._start_scanner(s, strategy_tasks)
                        continue

                    cls = STRATEGY_MAP.get(strategy_type)
                    if not cls:
                        logger.warning("Unknown strategy type '%s' — skipping.", strategy_type)
                        continue

                    # Route to correct broker
                    if platform in KRAKEN_PLATFORMS:
                        if not self._kraken:
                            logger.warning(
                                "Strategy '%s' requires Kraken but KRAKEN_API_KEY is not configured.", s["name"]
                            )
                            await api_client.post_log(
                                "error",
                                f"Strategy '{s['name']}' requires Kraken — add KRAKEN_API_KEY to .env",
                                strategy_id=sid,
                            )
                            continue
                        instance = cls(s, session=None, account=None, kraken=self._kraken)

                    else:
                        # Tastytrade / Tasty Crypto
                        if not self.session_mgr:
                            logger.warning(
                                "Strategy '%s' requires Tastytrade but TT_USERNAME is not configured.", s["name"]
                            )
                            continue

                        account_data = accounts_by_id.get(s["accountId"])
                        if not account_data:
                            logger.warning("Account %d not found for strategy %d.", s["accountId"], sid)
                            continue

                        account_number = account_data.get("accountNumber")
                        account = self.session_mgr.get_account(account_number)
                        if not account:
                            logger.warning(
                                "Account %s not found in Tastytrade session — check Accounts page.",
                                account_number,
                            )
                            await api_client.post_log(
                                "error",
                                f"Account {account_number} not found in Tastytrade session.",
                                strategy_id=sid,
                            )
                            continue
                        instance = cls(s, session=self.session_mgr.session, account=account)

                    self._instances[sid] = instance
                    scan_interval = s.get("scanInterval", 300)

                    task = asyncio.create_task(
                        self._run_strategy_loop(instance, scan_interval),
                        name=f"strategy-{sid}",
                    )
                    strategy_tasks[sid] = task

                    import config
                    logger.info(
                        "Started strategy '%s' (id=%d, type=%s, platform=%s, interval=%ds)",
                        s["name"], sid, strategy_type, platform, scan_interval,
                    )
                    await api_client.post_log(
                        "info",
                        f"Strategy '{s['name']}' started on {platform} "
                        f"(interval {scan_interval}s, dry_run={config.DRY_RUN})",
                        strategy_id=sid,
                    )

            except Exception as e:
                logger.exception("Strategy loader error: %s", e)

            await asyncio.sleep(60)

    async def _start_scanner(self, s: dict, strategy_tasks: dict):
        """Instantiate and launch a BullflowScanner as a persistent asyncio task."""
        from bullflow_scanner import BullflowScanner
        from config import BULLFLOW_API_KEY

        sid = s["id"]

        if not BULLFLOW_API_KEY:
            logger.warning("Strategy '%s' requires BULLFLOW_API_KEY — not set in .env.", s["name"])
            await api_client.post_log(
                "error",
                f"Strategy '{s['name']}' requires BULLFLOW_API_KEY — add it to .env",
                strategy_id=sid,
            )
            return

        # Inject Tastytrade session if available (for live execution)
        session = self.session_mgr.session if self.session_mgr else None
        account = None
        if self.session_mgr and s.get("accountId"):
            accounts_data = await api_client.get_accounts()
            accounts_by_id = {a["id"]: a for a in accounts_data}
            account_data = accounts_by_id.get(s["accountId"])
            if account_data:
                account = self.session_mgr.get_account(account_data.get("accountNumber"))

        scanner = BullflowScanner(s, session=session, account=account)
        self._scanner_instances[sid] = scanner

        task = asyncio.create_task(scanner.run(), name=f"scanner-{sid}")
        strategy_tasks[sid] = task

        import config
        logger.info(
            "Started Bullflow scanner '%s' (id=%d, dry_run=%s)",
            s["name"], sid, config.DRY_RUN,
        )
        await api_client.post_log(
            "info",
            f"Bullflow scanner '{s['name']}' launched (dry_run={config.DRY_RUN})",
            strategy_id=sid,
        )

    async def _run_strategy_loop(self, strategy: BaseStrategy, interval: int):
        """Runs a strategy's scan() on a fixed interval until cancelled."""
        logger.debug("Strategy loop started for '%s' (every %ds)", strategy.name, interval)
        while True:
            await strategy.run()
            await asyncio.sleep(interval)

    async def _midnight_resetter(self):
        """Resets all strategy daily trade counters at midnight."""
        while self._running:
            now = datetime.now()
            tomorrow = datetime.combine(now.date(), time.min) + timedelta(days=1)
            wait = (tomorrow - now).total_seconds()
            await asyncio.sleep(wait)
            for instance in self._instances.values():
                instance.reset_daily_count()
            for scanner in self._scanner_instances.values():
                scanner.reset_daily_count()
            logger.info("Daily trade counts reset.")
            await api_client.post_log("info", "Daily trade limits reset at midnight.")


async def main():
    engine = AlgoEngine()
    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("Shutdown signal received.")
        asyncio.create_task(engine.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown)

    await engine.start()


if __name__ == "__main__":
    asyncio.run(main())
