"""
Kraken API client wrapper.

Handles all communication with Kraken:
- Public market data (ticker, OHLCV history) — no API key needed
- Private trading (place/cancel orders, balances) — requires API key
- Proper async support via asyncio.to_thread for the sync SDK
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Ticker:
    bid: Decimal
    ask: Decimal
    last: Decimal
    mid: Decimal
    volume_24h: Decimal
    timestamp: float  # unix epoch


@dataclass
class OHLCV:
    """Single candlestick bar."""
    timestamp: float   # unix epoch (open time)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OrderResult:
    order_id: str
    status: str        # "pending", "filled", "validated" (paper)
    description: str


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class KrakenClient:
    """
    Async-friendly Kraken client.

    Uses the public REST API directly for market data (more reliable than the SDK
    for OHLCV), and the python-kraken-sdk for authenticated trading endpoints.
    """

    BASE_URL = "https://api.kraken.com"

    def __init__(self, api_key: str = "", api_secret: str = "", symbol: str = "XBTUSD"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbol = symbol
        self._http = httpx.AsyncClient(timeout=30.0)
        self._spot_client = None  # lazy-loaded SDK client for private endpoints

    # ------------------------------------------------------------------
    # Public endpoints (no auth)
    # ------------------------------------------------------------------

    async def get_ticker(self) -> Ticker:
        """Fetch current BTC/USD ticker from Kraken public API."""
        url = f"{self.BASE_URL}/0/public/Ticker"
        resp = await self._http.get(url, params={"pair": self.symbol})
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            raise RuntimeError(f"Kraken API error: {data['error']}")

        # Kraken returns data keyed by their internal pair name
        pair_data = list(data["result"].values())[0]

        bid = Decimal(pair_data["b"][0])
        ask = Decimal(pair_data["a"][0])
        last = Decimal(pair_data["c"][0])
        mid = (bid + ask) / 2
        volume = Decimal(pair_data["v"][1])  # 24h volume

        return Ticker(
            bid=bid, ask=ask, last=last, mid=mid,
            volume_24h=volume, timestamp=time.time()
        )

    async def get_ohlcv(self, interval: int = 15, count: int = 100) -> list[OHLCV]:
        """
        Fetch OHLCV candles from Kraken public API.

        Args:
            interval: Candle interval in minutes (1, 5, 15, 30, 60, 240, 1440)
            count: Number of candles to return (Kraken returns up to 720)

        Returns:
            List of OHLCV bars, oldest first.
        """
        url = f"{self.BASE_URL}/0/public/OHLC"

        # Calculate 'since' timestamp to get enough bars
        # Each bar covers `interval` minutes, so we go back count * interval minutes
        since = int(time.time()) - (count * interval * 60) - (interval * 60 * 10)

        resp = await self._http.get(url, params={
            "pair": self.symbol,
            "interval": interval,
            "since": since,
        })
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            raise RuntimeError(f"Kraken OHLC error: {data['error']}")

        # Result is keyed by pair name, plus a "last" timestamp
        pair_key = [k for k in data["result"] if k != "last"][0]
        raw_bars = data["result"][pair_key]

        bars = []
        for bar in raw_bars:
            # Kraken OHLC format: [time, open, high, low, close, vwap, volume, count]
            bars.append(OHLCV(
                timestamp=float(bar[0]),
                open=float(bar[1]),
                high=float(bar[2]),
                low=float(bar[3]),
                close=float(bar[4]),
                volume=float(bar[6]),
            ))

        # Return only the last `count` bars
        return bars[-count:]

    async def get_order_book(self, pair: str = "", depth: int = 10) -> dict:
        """
        Fetch order book depth from Kraken public API.

        Returns dict with:
            bids: list of [price, volume] (highest first)
            asks: list of [price, volume] (lowest first)
            bid_wall: largest bid volume and its price
            ask_wall: largest ask volume and its price
            spread: ask - bid as dollar amount
            spread_pct: spread as percentage of mid price
            bid_depth_usd: total USD value on bid side
            ask_depth_usd: total USD value on ask side
            imbalance: (bid_depth - ask_depth) / (bid_depth + ask_depth), -1 to +1
        """
        url = f"{self.BASE_URL}/0/public/Depth"
        target_pair = pair or self.symbol
        resp = await self._http.get(url, params={"pair": target_pair, "count": depth})
        resp.raise_for_status()
        data = resp.json()

        if data.get("error"):
            raise RuntimeError(f"Kraken Depth error: {data['error']}")

        pair_data = list(data["result"].values())[0]
        raw_bids = pair_data.get("bids", [])
        raw_asks = pair_data.get("asks", [])

        bids = [[float(b[0]), float(b[1])] for b in raw_bids]
        asks = [[float(a[0]), float(a[1])] for a in raw_asks]

        # Compute depth metrics
        bid_depth_usd = sum(p * v for p, v in bids)
        ask_depth_usd = sum(p * v for p, v in asks)
        total_depth = bid_depth_usd + ask_depth_usd

        bid_wall = max(bids, key=lambda x: x[1]) if bids else [0, 0]
        ask_wall = max(asks, key=lambda x: x[1]) if asks else [0, 0]

        best_bid = bids[0][0] if bids else 0
        best_ask = asks[0][0] if asks else 0
        mid = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else 1
        spread = best_ask - best_bid
        spread_pct = (spread / mid) * 100

        imbalance = (bid_depth_usd - ask_depth_usd) / total_depth if total_depth > 0 else 0

        return {
            "bids": bids[:depth],
            "asks": asks[:depth],
            "bid_wall_price": bid_wall[0],
            "bid_wall_volume": bid_wall[1],
            "ask_wall_price": ask_wall[0],
            "ask_wall_volume": ask_wall[1],
            "spread": round(spread, 4),
            "spread_pct": round(spread_pct, 4),
            "bid_depth_usd": round(bid_depth_usd, 2),
            "ask_depth_usd": round(ask_depth_usd, 2),
            "imbalance": round(imbalance, 4),
        }

    # ------------------------------------------------------------------
    # Private endpoints (requires API key)
    # ------------------------------------------------------------------

    def _get_spot_client(self):
        """Lazy-load the Kraken SDK spot client for authenticated requests."""
        if self._spot_client is None:
            if not self.api_key or not self.api_secret:
                raise RuntimeError(
                    "Kraken API key and secret are required for private endpoints. "
                    "Set KRAKEN_API_KEY and KRAKEN_API_SECRET in your .env file."
                )
            from kraken.spot import Market, Trade, User
            self._trade_client = Trade(key=self.api_key, secret=self.api_secret)
            self._user_client = User(key=self.api_key, secret=self.api_secret)
            self._spot_client = True
        return True

    async def get_balance(self) -> dict[str, Decimal]:
        """Get account balances. Returns dict like {'ZUSD': Decimal('5000'), 'XXBT': Decimal('0.1')}."""
        self._get_spot_client()
        result = await asyncio.to_thread(self._user_client.get_account_balance)
        return {k: Decimal(str(v)) for k, v in result.items()}

    async def get_open_orders(self) -> list[dict]:
        """Get all open orders."""
        self._get_spot_client()
        result = await asyncio.to_thread(self._user_client.get_open_orders)
        orders = result.get("open", {})
        return [{"id": k, **v} for k, v in orders.items()]

    async def place_market_order(
        self,
        side: str,           # "buy" or "sell"
        volume: Decimal,
        validate: bool = False,  # True = dry run (Kraken validates but doesn't execute)
    ) -> OrderResult:
        """Place a market order."""
        self._get_spot_client()
        params = {
            "ordertype": "market",
            "type": side,
            "pair": self.symbol,
            "volume": str(volume),
            "validate": validate,
        }
        logger.info(f"Placing {'VALIDATED ' if validate else ''}market {side} order: {volume} {self.symbol}")
        result = await asyncio.to_thread(self._trade_client.create_order, **params)

        if validate:
            return OrderResult(
                order_id="VALIDATED",
                status="validated",
                description=result.get("descr", {}).get("order", str(result)),
            )

        txids = result.get("txid", [])
        order_id = txids[0] if txids else "UNKNOWN"
        return OrderResult(
            order_id=order_id,
            status="pending",
            description=result.get("descr", {}).get("order", str(result)),
        )

    async def place_limit_order(
        self,
        side: str,
        volume: Decimal,
        price: Decimal,
        validate: bool = False,
    ) -> OrderResult:
        """Place a limit order."""
        self._get_spot_client()
        params = {
            "ordertype": "limit",
            "type": side,
            "pair": self.symbol,
            "volume": str(volume),
            "price": str(price),
            "validate": validate,
        }
        logger.info(f"Placing {'VALIDATED ' if validate else ''}limit {side} order: {volume} @ {price}")
        result = await asyncio.to_thread(self._trade_client.create_order, **params)

        if validate:
            return OrderResult(
                order_id="VALIDATED",
                status="validated",
                description=result.get("descr", {}).get("order", str(result)),
            )

        txids = result.get("txid", [])
        order_id = txids[0] if txids else "UNKNOWN"
        return OrderResult(
            order_id=order_id,
            status="pending",
            description=result.get("descr", {}).get("order", str(result)),
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self):
        """Close HTTP client."""
        await self._http.aclose()
