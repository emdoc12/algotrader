"""
Whale Monitor: tracks large crypto transactions and exchange flows.

Uses free public APIs (no keys required):
- Blockchain.com for large BTC transactions + address tags
- Blockchair for address labels and blockchain stats

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


# Known exchange deposit/hot wallet addresses (curated list of major exchanges)
# These are well-documented addresses verified across multiple blockchain explorers
KNOWN_EXCHANGE_ADDRESSES = {
    # Binance
    "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo": "binance",
    "3JZq4atUahhuA9rLhXLMhhTo133J9rF97j": "binance",
    "1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s": "binance",
    "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h": "binance",
    # Coinbase
    "3Kzh9qAqVWQhEsfQz7zEQL1EuSx5tyNLNS": "coinbase",
    "3FHNBLobJnbCTFTVakh5TXmEneyf5PT61B": "coinbase",
    "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh": "coinbase",
    "bc1q4c8n5t00jmj8temxdgcc3t32nkg2wjwz24lywv": "coinbase",
    # Kraken
    "3AfSvchLRYUDbqGvMEjUPfqnTfRt4VzZKH": "kraken",
    "bc1qr4dl5wa7kl8yu792dceg9z5knl2gkn220lk7a9": "kraken",
    # Bitfinex
    "3D2oetdNuZUqQHPJmcMDDHYoqkyNVsFk9r": "bitfinex",
    "bc1qgdjqv0av3q56jvd82tkdjpy7gdp9ut8tlqmgrpmv24sq90ecnvqqjwvw97": "bitfinex",
    # Gemini
    "3Bi9H1hzCHWJoFEjc4xzVzEMywi35UYs4A": "gemini",
    # Bitstamp
    "3P3QsMVK89JBNqZQv5zMAKG8FK3kJM4rjt": "bitstamp",
    # OKX
    "3LYJfcfHPXYJreMsASk2jkn69LWEYKzexb": "okx",
    # Huobi/HTX
    "1HckjUpRGcrrRAtFaaCAUaGjsPx9oYmLaZ": "huobi",
    # Bybit
    "bc1qjysjfd9t9aspttpjqzv68k0cc4etn5yrw3c2nt": "bybit",
}

# Address prefixes known to be associated with exchanges (high-volume patterns)
# These are less precise but catch more activity
EXCHANGE_ADDRESS_PREFIXES = {
    "bc1qm34lsc65zpw79lx": "binance",
    "bc1qxy2kgdygjrsqtzq2n": "coinbase",
}


def _identify_address(addr: str) -> tuple[str, str]:
    """Identify if an address belongs to a known exchange.

    Returns (type, exchange_name) where type is 'exchange' or 'unknown'.
    """
    if not addr:
        return "unknown", ""

    # Direct match
    if addr in KNOWN_EXCHANGE_ADDRESSES:
        return "exchange", KNOWN_EXCHANGE_ADDRESSES[addr]

    # Prefix match
    for prefix, exchange in EXCHANGE_ADDRESS_PREFIXES.items():
        if addr.startswith(prefix):
            return "exchange", exchange

    return "unknown", ""


class WhaleMonitor:
    """Monitors whale transactions across crypto markets."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http = http_client or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http_client is None
        self._cache: Optional[WhaleData] = None
        self._cache_time: float = 0.0
        self._cache_ttl: float = 300  # 5 minutes
        # Dynamic label cache: address -> (type, name, expiry)
        self._label_cache: dict[str, tuple[str, str, float]] = {}
        self._label_cache_ttl: float = 3600  # 1 hour

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

    async def _identify_address_with_api(self, addr: str) -> tuple[str, str]:
        """Try to identify an address using the local known list first,
        then Blockchair's address label API as a fallback.
        """
        if not addr:
            return "unknown", ""

        # Step 1: Local known address lookup (instant, no API call)
        addr_type, exchange = _identify_address(addr)
        if addr_type == "exchange":
            return addr_type, exchange

        # Step 2: Check dynamic label cache
        now = time.time()
        if addr in self._label_cache:
            cached_type, cached_name, expiry = self._label_cache[addr]
            if now < expiry:
                return cached_type, cached_name

        # Step 3: Blockchair address label API (free, rate limited)
        # Only look up addresses involved in large transactions to minimize API calls
        try:
            resp = await self._http.get(
                f"https://api.blockchair.com/bitcoin/dashboards/address/{addr}",
                headers={"User-Agent": USER_AGENT},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get(addr, {})
                addr_info = data.get("address", {})
                label = addr_info.get("type", "")

                # Blockchair labels exchanges, pools, etc.
                if label and "exchange" in label.lower():
                    result = ("exchange", label)
                    self._label_cache[addr] = ("exchange", label, now + self._label_cache_ttl)
                    return result

                # Check if balance is very high (likely institutional)
                balance_btc = addr_info.get("balance", 0) / 1e8
                if balance_btc > 1000:
                    result = ("whale", "")
                    self._label_cache[addr] = ("whale", "", now + self._label_cache_ttl)
                    return result
        except Exception:
            pass  # Don't slow down on API failures

        # Cache negative result to avoid re-lookups
        self._label_cache[addr] = ("unknown", "", now + self._label_cache_ttl)
        return "unknown", ""

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

            # Sort by value, process largest first
            scored_txs = []
            for tx in txs:
                total_out = sum(o.get("value", 0) for o in tx.get("out", []))
                btc_value = total_out / 1e8
                if btc_value >= 10:  # Only care about >10 BTC
                    scored_txs.append((btc_value, tx))
            scored_txs.sort(key=lambda x: x[0], reverse=True)

            # Process top 20 largest transactions
            api_lookups_remaining = 5  # Limit Blockchair API calls per scan

            for btc_value, tx in scored_txs[:20]:
                # Get primary input address
                input_addr = ""
                if tx.get("inputs"):
                    input_addr = tx["inputs"][0].get("prev_out", {}).get("addr", "") or ""

                # Get primary output address (largest output)
                output_addr = ""
                if tx.get("out"):
                    largest_out = max(tx["out"], key=lambda o: o.get("value", 0))
                    output_addr = largest_out.get("addr", "") or ""

                # Identify addresses — use local lookup first, API for big transactions
                from_type, from_exchange = _identify_address(input_addr)
                to_type, to_exchange = _identify_address(output_addr)

                # For very large transactions (>100 BTC), try API lookup if local didn't match
                if btc_value > 100 and api_lookups_remaining > 0:
                    if from_type == "unknown" and input_addr:
                        from_type, from_exchange = await self._identify_address_with_api(input_addr)
                        api_lookups_remaining -= 1
                    if to_type == "unknown" and output_addr and api_lookups_remaining > 0:
                        to_type, to_exchange = await self._identify_address_with_api(output_addr)
                        api_lookups_remaining -= 1

                exchange_name = from_exchange or to_exchange

                # Determine signal
                signal = "neutral"
                if to_type == "exchange" and from_type != "exchange":
                    signal = "sell_pressure"
                elif from_type == "exchange" and to_type != "exchange":
                    signal = "accumulation"
                elif from_type == "whale" or to_type == "whale":
                    signal = "whale_movement"
                elif btc_value > 100:
                    signal = "large_movement"

                transactions.append(WhaleTransaction(
                    coin="BTC",
                    amount=btc_value,
                    usd_value=0,  # filled later if needed
                    from_type=from_type,
                    to_type=to_type,
                    exchange=exchange_name,
                    timestamp=tx.get("time", time.time()),
                    signal=signal,
                ))

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
        big_txs = sorted(data.transactions, key=lambda t: t.amount, reverse=True)[:5]
        for tx in big_txs:
            if tx.amount > 0:
                exchange_tag = f" ({tx.exchange})" if tx.exchange else ""
                parts.append(
                    f"  {tx.coin}: {tx.amount:.2f} BTC moved "
                    f"({tx.from_type} → {tx.to_type}){exchange_tag} — {tx.signal}"
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
