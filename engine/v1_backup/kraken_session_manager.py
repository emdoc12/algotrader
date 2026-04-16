"""
Kraken Session Manager
-----------------------
Wraps the python-kraken-sdk for authenticated spot trading.
Provides:
  - get_ticker(symbol)     -> dict with bid/ask/last
  - get_balance()          -> dict of asset balances
  - get_open_positions()   -> list of open positions (from open orders + balance)
  - place_order(...)       -> create a limit or market order
  - cancel_order(txid)     -> cancel an open order

Kraken spot symbol format: "BTC/USD", "ETH/USD", "SOL/USD"
(python-kraken-sdk accepts both "XBTUSD" legacy and "BTC/USD" modern format)
"""
import asyncio
import logging
from decimal import Decimal
from kraken.spot import Market, Trade, User
from config import KRAKEN_API_KEY, KRAKEN_API_SECRET, DRY_RUN

logger = logging.getLogger(__name__)


class KrakenSessionManager:
    """Manages a Kraken Spot REST session."""

    def __init__(self):
        self._market = Market()
        self._trade = Trade(key=KRAKEN_API_KEY, secret=KRAKEN_API_SECRET)
        self._user = User(key=KRAKEN_API_KEY, secret=KRAKEN_API_SECRET)

    async def connect(self):
        """Verify credentials by fetching account balance."""
        try:
            balance = await asyncio.to_thread(self._user.get_account_balance)
            logger.info("Kraken connected. Assets held: %s", list(balance.keys()))
        except Exception as e:
            logger.error("Kraken connection failed: %s", e)
            raise

    async def disconnect(self):
        logger.info("Kraken session closed.")

    # ── Market Data ─────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> dict:
        """
        Returns {"bid": float, "ask": float, "last": float, "mid": float}
        symbol: e.g. "BTC/USD"
        """
        kraken_pair = _to_kraken_pair(symbol)
        result = await asyncio.to_thread(
            self._market.get_ticker, pair=kraken_pair
        )
        # result is a dict keyed by Kraken's internal pair name
        data = next(iter(result.values()))
        bid = float(data["b"][0])
        ask = float(data["a"][0])
        last = float(data["c"][0])
        return {
            "bid": bid,
            "ask": ask,
            "last": last,
            "mid": (bid + ask) / 2,
        }

    async def get_ohlc(self, symbol: str, interval: int = 1440) -> list[dict]:
        """
        Returns list of OHLC dicts (time, open, high, low, close, volume).
        interval: minutes — 1440 = daily candles (good for MA calculation)
        """
        kraken_pair = _to_kraken_pair(symbol)
        result = await asyncio.to_thread(
            self._market.get_ohlc, pair=kraken_pair, interval=interval
        )
        raw = next(iter(v for k, v in result.items() if k != "last"))
        return [
            {
                "time": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[6]),
            }
            for row in raw
        ]

    # ── Account ──────────────────────────────────────────────

    async def get_balance(self) -> dict[str, float]:
        """Returns dict of asset -> available balance, e.g. {"BTC": 0.5, "USD": 5000.0}"""
        raw = await asyncio.to_thread(self._user.get_account_balance)
        return {asset: float(amount) for asset, amount in raw.items() if float(amount) > 0}

    async def get_open_orders(self) -> list[dict]:
        result = await asyncio.to_thread(self._user.get_open_orders)
        orders = result.get("open", {})
        return [{"txid": txid, **info} for txid, info in orders.items()]

    # ── Order Placement ──────────────────────────────────────

    async def place_limit_order(
        self,
        symbol: str,
        side: str,          # "buy" or "sell"
        quantity: Decimal,
        price: Decimal,
        validate: bool = False,
    ) -> dict:
        """
        Place a limit order on Kraken spot.
        validate=True does a dry-run (Kraken validates but doesn't submit).
        Returns Kraken's response dict.
        """
        kraken_pair = _to_kraken_pair(symbol)
        effective_validate = validate or DRY_RUN

        logger.info(
            "[%s] KRAKEN LIMIT %s %.6f %s @ $%.4f",
            "DRY" if effective_validate else "LIVE",
            side.upper(),
            float(quantity),
            symbol,
            float(price),
        )

        result = await asyncio.to_thread(
            self._trade.create_order,
            ordertype="limit",
            side=side,
            volume=str(quantity),
            pair=kraken_pair,
            price=str(price),
            validate=effective_validate,
        )
        return result

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        validate: bool = False,
    ) -> dict:
        """Place a market order on Kraken spot."""
        kraken_pair = _to_kraken_pair(symbol)
        effective_validate = validate or DRY_RUN

        logger.info(
            "[%s] KRAKEN MARKET %s %.6f %s",
            "DRY" if effective_validate else "LIVE",
            side.upper(),
            float(quantity),
            symbol,
        )

        result = await asyncio.to_thread(
            self._trade.create_order,
            ordertype="market",
            side=side,
            volume=str(quantity),
            pair=kraken_pair,
            validate=effective_validate,
        )
        return result

    async def cancel_order(self, txid: str) -> dict:
        result = await asyncio.to_thread(self._trade.cancel_order, txid=txid)
        return result


# ── Helpers ──────────────────────────────────────────────────

def _to_kraken_pair(symbol: str) -> str:
    """
    Convert human-readable symbol to Kraken REST pair format.
    "BTC/USD" -> "XBTUSD"  (Kraken's legacy name for BTC)
    "ETH/USD" -> "ETHUSD"
    "SOL/USD" -> "SOLUSD"
    """
    _overrides = {
        "BTC/USD": "XBTUSD",
        "BTC/EUR": "XBTEUR",
        "ETH/USD": "ETHUSD",
        "ETH/EUR": "ETHEUR",
    }
    if symbol in _overrides:
        return _overrides[symbol]
    # Generic: "SOL/USD" -> "SOLUSD"
    return symbol.replace("/", "")
