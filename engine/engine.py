"""
Engine — the main orchestrator.
Polls the AlgoTrader API for enabled strategies, maps them to strategy
classes, and schedules each on its configured scan interval.
Also resets daily trade counts at midnight and handles graceful shutdown.
"""
import asyncio
import logging
import signal
from datetime import datetime, time

import api_client
from config import LOG_LEVEL
from session_manager import SessionManager
from strategies import STRATEGY_MAP
from strategies.base import BaseStrategy

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("engine")


class AlgoEngine:
    def __init__(self):
        self.session_mgr = SessionManager()
        self._tasks: list[asyncio.Task] = []
        self._instances: dict[int, BaseStrategy] = {}
        self._running = False

    async def start(self):
        self._running = True
        await self.session_mgr.connect()

        logger.info("AlgoTrader engine started. DRY_RUN=%s", __import__("config").DRY_RUN)
        await api_client.post_log("info", "AlgoTrader Python engine started.")

        # Start the strategy loader (polls API for enabled strategies)
        loader = asyncio.create_task(self._strategy_loader())
        # Start the midnight reset task
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
        await self.session_mgr.disconnect()
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
                    cls = STRATEGY_MAP.get(strategy_type)
                    if not cls:
                        logger.warning("Unknown strategy type '%s' — skipping.", strategy_type)
                        continue

                    account_data = accounts_by_id.get(s["accountId"])
                    if not account_data:
                        logger.warning("Account %d not found for strategy %d.", s["accountId"], sid)
                        continue

                    account_number = account_data.get("accountNumber")
                    account = self.session_mgr.get_account(account_number)
                    if not account:
                        logger.warning(
                            "Account %s not found in Tastytrade session — "
                            "check account number in Accounts page.",
                            account_number,
                        )
                        await api_client.post_log(
                            "error",
                            f"Account {account_number} not found in Tastytrade session.",
                            strategy_id=sid,
                        )
                        continue

                    instance = cls(s, self.session_mgr.session, account)
                    self._instances[sid] = instance
                    scan_interval = s.get("scanInterval", 300)

                    task = asyncio.create_task(
                        self._run_strategy_loop(instance, scan_interval),
                        name=f"strategy-{sid}",
                    )
                    strategy_tasks[sid] = task
                    logger.info(
                        "Started strategy '%s' (id=%d, type=%s, interval=%ds)",
                        s["name"], sid, strategy_type, scan_interval,
                    )
                    await api_client.post_log(
                        "info",
                        f"Strategy '{s['name']}' started (interval {scan_interval}s, dry_run={__import__('config').DRY_RUN})",
                        strategy_id=sid,
                    )

            except Exception as e:
                logger.exception("Strategy loader error: %s", e)

            await asyncio.sleep(60)  # re-check every minute

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
            # Seconds until next midnight
            tomorrow = datetime.combine(now.date(), time.min)
            from datetime import timedelta
            tomorrow += timedelta(days=1)
            wait = (tomorrow - now).total_seconds()
            await asyncio.sleep(wait)
            for instance in self._instances.values():
                instance.reset_daily_count()
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
