"""
Whale Monitor: tracks large crypto transactions and exchange flows.

Uses free public APIs (no keys required):
- Blockchain.com for large BTC transactions
- Blockchair for multi-chain large transactions
- Whale Alert public feed (rate-limited)

Provides signals when whales are moving coins to/from exchanges,
which often precedes large price moves.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class WhaleTransaction:
    """A single large transaction."""
    coin: str = ""
    amount: float = 0.0
    usd_value: float = 0.0
    from_type: str = ""    # "exchange", "unknown", "whale"
    to_type: str = ""      # "exchange", "unknown", "whale"
    exchange: str = ""     # name if exchange involved
    timestamp: float = 0.0
    signal: str = ""       # "sell_pressure", "buy_pressure", "accumulation", "distribution"


@dataclass
class WhaleData:
    """Aggregated whale activity."""
    transactions: list[WhaleTransaction] = field(default_factory=list)
    exchange_inflow_btc: float = 0.0    # BTC moving TO exchanges (sell pressure)
    exchange_outflow_btc: float = 0.0   # BTC moving FROM exchanges (accumulation)
    net_flow: float = 0.0               # negative = outflow (bullish), positive = inflow (bearish)
    large_tx_count: int = 0
    alert_level: str = "normal"         # "normal", "elevated", "high"
    summary: str = ""
    timestamp: float = 0.0


# Known exchange addresses (partial list — enough to detect major flows)
EXCHANGE_KEYWORDS = [
    "coinbase", "binance", "kraken", "bitfinex", "huobi", "okex", "okx",
    "gemini", "bitstamp", "kucoin", "bybit", "ftx", "crypto.com",
    "exchange", "hot wallet",
]


class WhaleMonitor:
    """Monitors whale transactions across crypto markets."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http = http_client or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http_client is None
        self._cache: Optional[WhaleData] = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 300  # 5 minutes

    async def get_whale_activity(self) -> WhaleData:
        """Fetch and aggregate whale transaction data."""
        # Check cache
        if self._cache and (time.time() - self._cache_time < self._cache_ttl):
            return self._cache

        result = WhaleData(timestamp=time.time())
        transactions = []

        # Try multiple sources
        try:
            btc_txs = await self._fetch_blockchain_large_txs()
            transactions.extend(btc_txs)
        except Exception as e:
            logger.debug(f"Blockchain.com fetch failed: {e}")

        try:
            blockchair_txs = await self._fetch_blockchair_stats()
            transactions.extend(blockchair_txs)
        except Exception as e:
            logger.debug(f"Blockchair fetch failed: {e}")

        result.transactions = transactions
        result.large_tx_count = len(transactions)

        # Aggregate exchange flows
        for tx in transactions:
            if tx.to_type == "exchange":
                result.exchange_inflow_btc += tx.amount
            elif tx.from_type == "exchange":
                result.exchange_outflow_btc += tx.amount

        result.net_flow = result.exchange_inflow_btc - result.exchange_outflow_btc

        # Determine alert level
        if result.net_flow > 1000:  # >1000 BTC net inflow
            result.alert_level = "high"
        elif result.net_flow > 500 or result.large_tx_count > 10:
            result.alert_level = "elevated"
        else:
            result.alert_level = "normal"

        # Build summary
        result.summary = self._build_summary(result)

        self._cache = result
        self._cache_time = time.time()

        logger.info(
            f"Whale monitor: {result.large_tx_count} large txs | "
            f"Net flow: {result.net_flow:+.2f} BTC | Alert: {result.alert_level}"
        )

        return result

    async def _fetch_blockchain_large_txs(self) -> list[WhaleTransaction]:
        """Fetch recent large BTC transactions from Blockchain.com."""
        transactions = []
        try:
            # Blockchain.com latest unconfirmed transactions (large ones)
            resp = await self._http.get(
                "https://blockchain.info/unconfirmed-transactions?format=json",
                headers={"User-Agent": USER_AGENT},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return transactions

            data = resp.json()
            txs = data.get("txs", [])

            for tx in txs:
                # Calculate total output value
                total_out = sum(o.get("value", 0) for o in tx.get("out", []))
                btc_value = total_out / 1e8  # satoshis to BTC

                # Only care about large transactions (>10 BTC)
                if btc_value < 10:
                    continue

                # Simple heuristic: check if inputs/outputs look like exchanges
                from_exchange = False
                to_exchange = False
                exchange_name = ""

                for inp in tx.get("inputs", []):
                    addr = inp.get("prev_out", {}).get("addr", "").lower()
                    for kw in EXCHANGE_KEYWORDS:
                        if kw in addr:
                            from_exchange = True
                            exchange_name = kw
                            break

                for out in tx.get("out", []):
                    addr = out.get("addr", "").lower() if out.get("addr") else ""
                    for kw in EXCHANGE_KEYWORDS:
                        if kw in addr:
                            to_exchange = True
                            exchange_name = exchange_name or kw
                            break

                signal = "neutral"
                if to_exchange and not from_exchange:
                    signal = "sell_pressure"
                elif from_exchange and not to_exchange:
                    signal = "accumulation"
                elif btc_value > 100:
                    signal = "large_movement"

                transactions.append(WhaleTransaction(
                    coin="BTC",
                    amount=btc_value,
                    usd_value=0,  # filled later if needed
                    from_type="exchange" if from_exchange else "unknown",
                    to_type="exchange" if to_exchange else "unknown",
                    exchange=exchange_name,
                    timestamp=tx.get("time", time.time()),
                    signal=signal,
                ))

                if len(transactions) >= 20:
                    break

        except Exception as e:
            logger.debug(f"Blockchain.com parsing error: {e}")

        return transactions

    async def _fetch_blockchair_stats(self) -> list[WhaleTransaction]:
        """Fetch blockchain stats from Blockchair (free, no key needed)."""
        transactions = []
        try:
            resp = await self._http.get(
                "https://api.blockchair.com/bitcoin/stats",
                headers={"User-Agent": USER_AGENT},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return transactions

            data = resp.json().get("data", {})

            # Blockchair stats give us aggregate mempool info
            mempool_txs = data.get("mempool_transactions", 0)
            mempool_size = data.get("mempool_size", 0)
            avg_tx_fee = data.get("average_transaction_fee_24h", 0)
            suggested_fee = data.get("suggested_transaction_fee_per_byte_sat", 0)

            # High mempool activity + high fees = lots of movement
            if mempool_txs > 50000 or suggested_fee > 50:
                transactions.append(WhaleTransaction(
                    coin="BTC",
                    amount=0,
                    from_type="network",
                    to_type="network",
                    timestamp=time.time(),
                    signal=f"high_network_activity (mempool: {mempool_txs} txs, fee: {suggested_fee} sat/byte)",
                ))

        except Exception as e:
            logger.debug(f"Blockchair parsing error: {e}")

        return transactions

    def _build_summary(self, data: WhaleData) -> str:
        """Build human-readable summary for AI context."""
        parts = []

        if data.large_tx_count == 0:
            return "No significant whale activity detected."

        parts.append(f"Whale activity: {data.large_tx_count} large transactions detected")

        if data.exchange_inflow_btc > 0:
            parts.append(f"Exchange inflow: {data.exchange_inflow_btc:.2f} BTC (potential sell pressure)")
        if data.exchange_outflow_btc > 0:
            parts.append(f"Exchange outflow: {data.exchange_outflow_btc:.2f} BTC (accumulation)")

        if data.net_flow > 0:
            parts.append(f"Net flow: +{data.net_flow:.2f} BTC to exchanges (BEARISH signal)")
        elif data.net_flow < 0:
            parts.append(f"Net flow: {data.net_flow:.2f} BTC from exchanges (BULLISH signal)")

        parts.append(f"Alert level: {data.alert_level}")

        # Notable transactions
        big_txs = sorted(data.transactions, key=lambda t: t.amount, reverse=True)[:3]
        for tx in big_txs:
            if tx.amount > 0:
                parts.append(
                    f"  {tx.coin}: {tx.amount:.2f} moved "
                    f"({tx.from_type} → {tx.to_type}) — {tx.signal}"
                )

        return " | ".join(parts)

    def format_for_context(self, data: WhaleData) -> str:
        """Format whale data for inclusion in AI trading context."""
        if not data.transactions and data.alert_level == "normal":
            return ""
        return f"\n## WHALE ACTIVITY\n{data.summary}"

    async def close(self):
        """Clean up."""
        if self._owns_http:
            await self._http.aclose()
