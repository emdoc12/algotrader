"""
Session Manager — handles Tastytrade authentication and token refresh.
Maintains a single live session shared across all strategies.
"""
import asyncio
import logging
from datetime import datetime, timezone
from tastytrade import Session, Account
from config import TT_USERNAME, TT_PASSWORD, TT_IS_SANDBOX

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages a single Tastytrade session with automatic token refresh.
    Use as an async context manager or call connect() / disconnect() manually.
    """

    def __init__(self):
        self.session: Session | None = None
        self.accounts: list[Account] = []
        self._refresh_task: asyncio.Task | None = None

    async def connect(self):
        """Authenticate and start background token refresh."""
        logger.info("Connecting to Tastytrade (sandbox=%s)...", TT_IS_SANDBOX)
        self.session = Session(
            TT_USERNAME,
            TT_PASSWORD,
            is_test=TT_IS_SANDBOX,
            remember_me=True,          # keeps a 24h remember token
        )
        self.accounts = Account.get(self.session)
        logger.info(
            "Connected. Accounts: %s",
            [a.account_number for a in self.accounts],
        )
        self._refresh_task = asyncio.create_task(self._auto_refresh())

    async def disconnect(self):
        if self._refresh_task:
            self._refresh_task.cancel()
        self.session = None
        logger.info("Disconnected from Tastytrade.")

    async def _auto_refresh(self):
        """Refresh the session token a minute before expiry."""
        while True:
            try:
                if self.session:
                    expiry = self.session.session_expiration
                    now = datetime.now(tz=timezone.utc)
                    # expiry is a datetime; refresh 60s before it expires
                    secs_left = (expiry - now).total_seconds()
                    sleep_secs = max(secs_left - 60, 30)
                    logger.debug("Session refresh in %.0f seconds", sleep_secs)
                    await asyncio.sleep(sleep_secs)
                    self.session.refresh()
                    logger.info("Session token refreshed.")
                else:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Session refresh failed: %s. Retrying in 30s.", e)
                await asyncio.sleep(30)

    def get_account(self, account_number: str) -> Account | None:
        """Return an Account by its account number string."""
        return next(
            (a for a in self.accounts if a.account_number == account_number),
            None,
        )

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.disconnect()
