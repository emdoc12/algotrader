"""
On-Chain Data: BTC network metrics, mempool, stablecoin supply, whale transactions.

Fetches from free public APIs (no auth/keys needed):
- Blockchain.com: network stats, hash rate, tx volume, large transactions
- Mempool.space: mempool size, fee estimates, hashrate
- CoinGecko: stablecoin market caps (USDT, USDC)

Gives Claude visibility into on-chain fundamentals — capital flows,
network health, congestion, and whale activity that precede price moves.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Cache TTL: on-chain data changes slowly
CACHE_TTL = 300.0  # 5 minutes

# Whale threshold in BTC
WHALE_TX_THRESHOLD_BTC = 100


@dataclass
class MempoolStats:
    """Mempool congestion and fee data."""
    mempool_size_bytes: int = 0
    mempool_tx_count: int = 0
    vsize: int = 0  # virtual size in vbytes
    fee_fastest: int = 0  # sat/vB
    fee_half_hour: int = 0
    fee_hour: int = 0
    fee_economy: int = 0
    fee_minimum: int = 0
    congestion_level: str = ""  # "low", "moderate", "high", "extreme"


@dataclass
class StablecoinSignals:
    """Stablecoin supply data as a proxy for capital flows."""
    usdt_market_cap: float = 0.0
    usdc_market_cap: float = 0.0
    total_stablecoin_cap: float = 0.0
    usdt_cap_prev: float = 0.0  # Previous fetch for delta tracking
    usdc_cap_prev: float = 0.0
    usdt_change_pct: float = 0.0
    usdc_change_pct: float = 0.0
    signal: str = ""  # "expanding", "contracting", "stable"


@dataclass
class NetworkMetrics:
    """BTC network health indicators."""
    hash_rate_gh: float = 0.0  # GH/s from blockchain.info stats
    hash_rate_trend: str = ""  # "rising", "falling", "stable"
    hash_rate_7d_values: list = field(default_factory=list)  # recent daily values
    difficulty: float = 0.0
    minutes_between_blocks: float = 0.0
    tx_count_24h: int = 0
    tx_volume_usd_24h: float = 0.0
    avg_block_size_bytes: int = 0
    n_blocks_mined_24h: int = 0
    miners_revenue_usd: float = 0.0
    market_price_usd: float = 0.0
    mempool_unconfirmed_count: int = 0


@dataclass
class WhaleTransaction:
    """A large BTC transaction."""
    hash: str = ""
    total_btc: float = 0.0
    total_usd_approx: float = 0.0
    time: int = 0


@dataclass
class OnChainSnapshot:
    """Full on-chain data snapshot."""
    timestamp: float = 0.0
    mempool: Optional[MempoolStats] = None
    stablecoins: Optional[StablecoinSignals] = None
    network: Optional[NetworkMetrics] = None
    whale_txs: list = field(default_factory=list)  # list[WhaleTransaction]
    fetch_errors: list = field(default_factory=list)


class OnChainDataFetcher:
    """Fetches on-chain data from free public APIs."""

    def __init__(self, http_client: httpx.AsyncClient):
        self._http = http_client
        self._cache: dict[str, tuple[float, object]] = {}  # key -> (timestamp, data)
        self._cache_ttl = CACHE_TTL
        self._last_snapshot: Optional[OnChainSnapshot] = None
        self._last_fetch_time: float = 0.0
        # Track previous stablecoin caps for delta calculation
        self._prev_usdt_cap: float = 0.0
        self._prev_usdc_cap: float = 0.0

    def _get_cached(self, key: str) -> Optional[object]:
        """Return cached value if still fresh, else None."""
        if key in self._cache:
            ts, val = self._cache[key]
            if (time.time() - ts) < self._cache_ttl:
                return val
        return None

    def _set_cached(self, key: str, value: object) -> None:
        """Store a value in cache with current timestamp."""
        self._cache[key] = (time.time(), value)

    async def fetch_all(self) -> OnChainSnapshot:
        """Fetch all on-chain data concurrently. Graceful on partial failure."""
        # Return cached snapshot if still fresh
        if self._last_snapshot and (time.time() - self._last_fetch_time) < self._cache_ttl:
            return self._last_snapshot

        snapshot = OnChainSnapshot(timestamp=time.time())

        tasks = [
            self._fetch_blockchain_stats(),
            self._fetch_mempool_info(),
            self._fetch_fee_estimates(),
            self._fetch_stablecoin_caps(),
            self._fetch_tx_volume(),
            self._fetch_hash_rate_chart(),
            self._fetch_whale_transactions(),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        task_names = [
            "blockchain_stats", "mempool_info", "fee_estimates",
            "stablecoin_caps", "tx_volume", "hash_rate_chart",
            "whale_transactions",
        ]

        blockchain_stats = results[0] if not isinstance(results[0], Exception) else None
        mempool_info = results[1] if not isinstance(results[1], Exception) else None
        fee_estimates = results[2] if not isinstance(results[2], Exception) else None
        stablecoin_data = results[3] if not isinstance(results[3], Exception) else None
        tx_volume_data = results[4] if not isinstance(results[4], Exception) else None
        hash_rate_data = results[5] if not isinstance(results[5], Exception) else None
        whale_txs = results[6] if not isinstance(results[6], Exception) else None

        # Log errors
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                snapshot.fetch_errors.append(f"{task_names[i]}: {r}")
                logger.debug(f"On-chain fetch error ({task_names[i]}): {r}")

        # --- Assemble mempool stats ---
        mempool = MempoolStats()
        if mempool_info:
            mempool.mempool_tx_count = mempool_info.get("count", 0)
            mempool.mempool_size_bytes = mempool_info.get("size", 0)
            mempool.vsize = mempool_info.get("vsize", 0)
        if fee_estimates:
            mempool.fee_fastest = fee_estimates.get("fastestFee", 0)
            mempool.fee_half_hour = fee_estimates.get("halfHourFee", 0)
            mempool.fee_hour = fee_estimates.get("hourFee", 0)
            mempool.fee_economy = fee_estimates.get("economyFee", 0)
            mempool.fee_minimum = fee_estimates.get("minimumFee", 0)
        # Congestion classification based on fastest fee
        if mempool.fee_fastest > 100:
            mempool.congestion_level = "extreme"
        elif mempool.fee_fastest > 50:
            mempool.congestion_level = "high"
        elif mempool.fee_fastest > 20:
            mempool.congestion_level = "moderate"
        else:
            mempool.congestion_level = "low"
        snapshot.mempool = mempool

        # --- Assemble network metrics ---
        network = NetworkMetrics()
        if blockchain_stats:
            network.hash_rate_gh = blockchain_stats.get("hash_rate", 0)
            network.difficulty = blockchain_stats.get("difficulty", 0)
            network.minutes_between_blocks = blockchain_stats.get("minutes_between_blocks", 0)
            network.tx_count_24h = blockchain_stats.get("n_tx", 0)
            network.avg_block_size_bytes = blockchain_stats.get("blocks_size", 0)
            network.n_blocks_mined_24h = blockchain_stats.get("n_blocks_mined", 0)
            network.miners_revenue_usd = blockchain_stats.get("miners_revenue_usd", 0)
            network.market_price_usd = blockchain_stats.get("market_price_usd", 0)
        if mempool_info:
            network.mempool_unconfirmed_count = mempool_info.get("count", 0)
        if tx_volume_data:
            # tx_volume_data is a list of {x: timestamp, y: value} from the chart
            if tx_volume_data:
                # Sum the last ~2 days of values, take the latest day
                network.tx_volume_usd_24h = tx_volume_data[-1].get("y", 0) if tx_volume_data else 0
        # Hash rate trend from chart data
        if hash_rate_data and len(hash_rate_data) >= 2:
            network.hash_rate_7d_values = [v.get("y", 0) for v in hash_rate_data]
            recent_avg = sum(network.hash_rate_7d_values[-3:]) / min(3, len(network.hash_rate_7d_values[-3:]))
            older_avg = sum(network.hash_rate_7d_values[:3]) / min(3, len(network.hash_rate_7d_values[:3]))
            if older_avg > 0:
                change_pct = ((recent_avg - older_avg) / older_avg) * 100
                if change_pct > 2:
                    network.hash_rate_trend = "rising"
                elif change_pct < -2:
                    network.hash_rate_trend = "falling"
                else:
                    network.hash_rate_trend = "stable"
            else:
                network.hash_rate_trend = "unknown"
        else:
            network.hash_rate_trend = "unknown"
        snapshot.network = network

        # --- Assemble stablecoin signals ---
        stablecoins = StablecoinSignals()
        if stablecoin_data:
            tether = stablecoin_data.get("tether", {})
            usdc = stablecoin_data.get("usd-coin", {})
            stablecoins.usdt_market_cap = tether.get("usd_market_cap", 0)
            stablecoins.usdc_market_cap = usdc.get("usd_market_cap", 0)
            stablecoins.total_stablecoin_cap = stablecoins.usdt_market_cap + stablecoins.usdc_market_cap
            # Delta vs previous fetch
            stablecoins.usdt_cap_prev = self._prev_usdt_cap
            stablecoins.usdc_cap_prev = self._prev_usdc_cap
            if self._prev_usdt_cap > 0:
                stablecoins.usdt_change_pct = (
                    (stablecoins.usdt_market_cap - self._prev_usdt_cap) / self._prev_usdt_cap
                ) * 100
            if self._prev_usdc_cap > 0:
                stablecoins.usdc_change_pct = (
                    (stablecoins.usdc_market_cap - self._prev_usdc_cap) / self._prev_usdc_cap
                ) * 100
            # Update previous values for next cycle
            self._prev_usdt_cap = stablecoins.usdt_market_cap
            self._prev_usdc_cap = stablecoins.usdc_market_cap
            # Signal
            total_change = stablecoins.usdt_change_pct + stablecoins.usdc_change_pct
            if total_change > 0.1:
                stablecoins.signal = "expanding (new capital entering)"
            elif total_change < -0.1:
                stablecoins.signal = "contracting (capital leaving)"
            elif self._prev_usdt_cap == 0:
                stablecoins.signal = "first fetch (no delta yet)"
            else:
                stablecoins.signal = "stable"
        snapshot.stablecoins = stablecoins

        # --- Whale transactions ---
        if whale_txs:
            snapshot.whale_txs = whale_txs

        self._last_snapshot = snapshot
        self._last_fetch_time = time.time()
        return snapshot

    # ------------------------------------------------------------------
    # Blockchain.com APIs
    # ------------------------------------------------------------------

    async def _fetch_blockchain_stats(self) -> dict:
        """Fetch BTC network stats from blockchain.info/stats."""
        cached = self._get_cached("blockchain_stats")
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                "https://api.blockchain.info/stats",
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            self._set_cached("blockchain_stats", data)
            return data
        except Exception as e:
            logger.debug(f"Blockchain.info stats fetch failed: {e}")
            raise

    async def _fetch_tx_volume(self) -> list:
        """Fetch estimated transaction volume (USD) from blockchain.info charts."""
        cached = self._get_cached("tx_volume")
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                "https://api.blockchain.info/charts/estimated-transaction-volume-usd",
                params={"timespan": "2days", "format": "json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            values = data.get("values", [])
            self._set_cached("tx_volume", values)
            return values
        except Exception as e:
            logger.debug(f"Blockchain.info tx volume fetch failed: {e}")
            raise

    async def _fetch_hash_rate_chart(self) -> list:
        """Fetch 7-day hash rate chart from blockchain.info."""
        cached = self._get_cached("hash_rate_chart")
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                "https://api.blockchain.info/charts/hash-rate",
                params={"timespan": "7days", "format": "json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            values = data.get("values", [])
            self._set_cached("hash_rate_chart", values)
            return values
        except Exception as e:
            logger.debug(f"Blockchain.info hash rate fetch failed: {e}")
            raise

    async def _fetch_whale_transactions(self) -> list:
        """Detect large BTC transactions via blockchain.info recent blocks.

        We check the latest block's transactions for any moving >100 BTC.
        This avoids the huge unconfirmed-transactions endpoint.
        """
        cached = self._get_cached("whale_txs")
        if cached is not None:
            return cached
        try:
            # Get the latest block hash
            resp = await self._http.get(
                "https://blockchain.info/latestblock",
                params={"format": "json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            latest = resp.json()
            block_hash = latest.get("hash", "")
            if not block_hash:
                return []

            # Get the block's transactions (single block keeps payload reasonable)
            resp2 = await self._http.get(
                f"https://blockchain.info/rawblock/{block_hash}",
                params={"format": "json"},
                timeout=20.0,
            )
            resp2.raise_for_status()
            block_data = resp2.json()

            whale_txs = []
            btc_price = 0.0
            # Try to get a price estimate from cached stats
            stats_cached = self._get_cached("blockchain_stats")
            if stats_cached:
                btc_price = stats_cached.get("market_price_usd", 0)

            for tx in block_data.get("tx", []):
                total_output_sat = sum(
                    out.get("value", 0) for out in tx.get("out", [])
                )
                total_btc = total_output_sat / 1e8
                if total_btc >= WHALE_TX_THRESHOLD_BTC:
                    whale_txs.append(WhaleTransaction(
                        hash=tx.get("hash", "")[:16] + "...",
                        total_btc=round(total_btc, 2),
                        total_usd_approx=round(total_btc * btc_price, 0) if btc_price else 0,
                        time=tx.get("time", 0),
                    ))

            # Sort by size descending, keep top 10
            whale_txs.sort(key=lambda w: w.total_btc, reverse=True)
            whale_txs = whale_txs[:10]
            self._set_cached("whale_txs", whale_txs)
            return whale_txs
        except Exception as e:
            logger.debug(f"Whale transaction fetch failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Mempool.space APIs
    # ------------------------------------------------------------------

    async def _fetch_mempool_info(self) -> dict:
        """Fetch mempool stats from mempool.space."""
        cached = self._get_cached("mempool_info")
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                "https://mempool.space/api/mempool",
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            self._set_cached("mempool_info", data)
            return data
        except Exception as e:
            logger.debug(f"Mempool.space mempool info fetch failed: {e}")
            raise

    async def _fetch_fee_estimates(self) -> dict:
        """Fetch recommended fee rates from mempool.space."""
        cached = self._get_cached("fee_estimates")
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                "https://mempool.space/api/v1/fees/recommended",
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            self._set_cached("fee_estimates", data)
            return data
        except Exception as e:
            logger.debug(f"Mempool.space fee estimates fetch failed: {e}")
            raise

    # ------------------------------------------------------------------
    # CoinGecko API (free, no key)
    # ------------------------------------------------------------------

    async def _fetch_stablecoin_caps(self) -> dict:
        """Fetch USDT and USDC market caps from CoinGecko."""
        cached = self._get_cached("stablecoin_caps")
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "tether,usd-coin",
                    "vs_currencies": "usd",
                    "include_market_cap": "true",
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            self._set_cached("stablecoin_caps", data)
            return data
        except Exception as e:
            logger.debug(f"CoinGecko stablecoin fetch failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Formatting for AI context
    # ------------------------------------------------------------------

    def format_for_context(self, data: OnChainSnapshot) -> str:
        """Format on-chain data for injection into Claude's context window.

        Focuses on actionable signals, not raw numbers.
        """
        if not data:
            return ""

        parts = ["\n## ON-CHAIN DATA (BTC Network + Stablecoins)"]

        # --- Mempool & Fees ---
        if data.mempool:
            m = data.mempool
            parts.append(f"\n### Mempool & Fees — Congestion: {m.congestion_level.upper()}")
            parts.append(
                f"  Unconfirmed TXs: {m.mempool_tx_count:,} | "
                f"Size: {m.vsize / 1_000_000:.1f} MvB"
            )
            parts.append(
                f"  Fees (sat/vB): fastest={m.fee_fastest} | "
                f"30min={m.fee_half_hour} | 1hr={m.fee_hour} | "
                f"economy={m.fee_economy}"
            )
            # Interpretation
            if m.congestion_level == "extreme":
                parts.append("  -> SIGNAL: Extreme congestion — heavy on-chain activity, possible panic or FOMO")
            elif m.congestion_level == "high":
                parts.append("  -> SIGNAL: High fees suggest increased urgency in on-chain movement")
            elif m.congestion_level == "low":
                parts.append("  -> SIGNAL: Low fees — quiet on-chain, no urgency")

        # --- Network Metrics ---
        if data.network:
            n = data.network
            parts.append(f"\n### BTC Network Health")
            if n.market_price_usd > 0:
                parts.append(f"  BTC Price (blockchain.info): ${n.market_price_usd:,.0f}")
            parts.append(
                f"  Hash rate trend: {n.hash_rate_trend} | "
                f"Block interval: {n.minutes_between_blocks:.1f} min (target: 10)"
            )
            if n.tx_count_24h > 0:
                parts.append(f"  24h TXs: {n.tx_count_24h:,}")
            if n.tx_volume_usd_24h > 0:
                parts.append(f"  24h TX volume: ${n.tx_volume_usd_24h:,.0f}")
            if n.n_blocks_mined_24h > 0:
                parts.append(f"  Blocks mined (24h): {n.n_blocks_mined_24h}")
            if n.miners_revenue_usd > 0:
                parts.append(f"  Miner revenue (24h): ${n.miners_revenue_usd:,.0f}")
            # Interpretation
            if n.minutes_between_blocks < 9:
                parts.append("  -> SIGNAL: Blocks faster than 10min — hash rate likely increasing")
            elif n.minutes_between_blocks > 11:
                parts.append("  -> SIGNAL: Blocks slower than 10min — possible hash rate drop or difficulty spike")
            if n.hash_rate_trend == "rising":
                parts.append("  -> SIGNAL: Rising hash rate — miners confident, network strengthening")
            elif n.hash_rate_trend == "falling":
                parts.append("  -> SIGNAL: Falling hash rate — miners may be capitulating or shutting down")

        # --- Stablecoin Supply ---
        if data.stablecoins and data.stablecoins.total_stablecoin_cap > 0:
            s = data.stablecoins
            parts.append(f"\n### Stablecoin Supply — {s.signal}")
            parts.append(
                f"  USDT mcap: ${s.usdt_market_cap / 1e9:.2f}B"
                + (f" ({s.usdt_change_pct:+.3f}%)" if s.usdt_cap_prev > 0 else "")
            )
            parts.append(
                f"  USDC mcap: ${s.usdc_market_cap / 1e9:.2f}B"
                + (f" ({s.usdc_change_pct:+.3f}%)" if s.usdc_cap_prev > 0 else "")
            )
            parts.append(f"  Combined: ${s.total_stablecoin_cap / 1e9:.2f}B")
            # Interpretation
            if "expanding" in s.signal:
                parts.append("  -> SIGNAL: Stablecoin supply expanding — fresh capital entering crypto, bullish")
            elif "contracting" in s.signal:
                parts.append("  -> SIGNAL: Stablecoin supply contracting — capital leaving crypto, bearish")

        # --- Whale Transactions ---
        if data.whale_txs:
            parts.append(f"\n### Whale Transactions (>{WHALE_TX_THRESHOLD_BTC} BTC in latest block)")
            for wtx in data.whale_txs[:5]:  # Show top 5
                usd_str = f" (~${wtx.total_usd_approx:,.0f})" if wtx.total_usd_approx > 0 else ""
                parts.append(f"  {wtx.total_btc:,.2f} BTC{usd_str} | tx: {wtx.hash}")
            total_whale_btc = sum(w.total_btc for w in data.whale_txs)
            parts.append(f"  Total large TXs in block: {len(data.whale_txs)} ({total_whale_btc:,.1f} BTC)")
            if total_whale_btc > 1000:
                parts.append("  -> SIGNAL: Heavy whale movement — watch for exchange deposit/withdrawal patterns")

        # --- Errors ---
        if data.fetch_errors:
            parts.append(f"\n[On-chain data gaps: {', '.join(data.fetch_errors)}]")

        return "\n".join(parts)
