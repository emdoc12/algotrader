"""
Derivatives Market Data: Funding rates, open interest, liquidations, and options.

Fetches from free public APIs (no auth needed):
- Binance Futures: funding rates, open interest, liquidations
- Bybit: funding rates, open interest
- OKX: funding rates, open interest
- Deribit: options data, put/call ratio

Gives Claude a view into what leveraged traders are doing — essential
for spotting squeezes, crowded trades, and sentiment shifts.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Map our coin symbols to exchange-specific contract names
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT", "ADA": "ADAUSDT", "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT", "DOT": "DOTUSDT", "XRP": "XRPUSDT",
    "POL": "MATICUSDT",  # Polygon still listed as MATIC on Binance futures
}

BYBIT_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "DOGE": "DOGEUSDT", "ADA": "ADAUSDT", "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT", "DOT": "DOTUSDT", "XRP": "XRPUSDT",
    "POL": "MATICUSDT",
}

OKX_SYMBOLS = {
    "BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "SOL": "SOL-USDT-SWAP",
    "DOGE": "DOGE-USDT-SWAP", "ADA": "ADA-USDT-SWAP", "AVAX": "AVAX-USDT-SWAP",
    "LINK": "LINK-USDT-SWAP", "DOT": "DOT-USDT-SWAP", "XRP": "XRP-USDT-SWAP",
    "POL": "MATIC-USDT-SWAP",
}

# Deribit only has BTC and ETH options
DERIBIT_CURRENCIES = ["BTC", "ETH"]


@dataclass
class FundingRate:
    """Funding rate from a single exchange."""
    exchange: str = ""
    symbol: str = ""
    rate: float = 0.0           # Current funding rate (e.g., 0.0001 = 0.01%)
    annualized: float = 0.0     # Annualized rate
    next_funding_time: float = 0.0


@dataclass
class CoinDerivatives:
    """Aggregated derivatives data for one coin."""
    symbol: str = ""
    # Funding rates across exchanges
    funding_rates: list = field(default_factory=list)  # list[FundingRate]
    avg_funding_rate: float = 0.0
    funding_sentiment: str = ""  # "bullish" (negative = shorts pay), "bearish" (positive = longs pay), "neutral"
    # Open interest
    binance_oi_usd: float = 0.0
    bybit_oi_usd: float = 0.0
    okx_oi_usd: float = 0.0
    total_oi_usd: float = 0.0
    # Liquidations (Binance only — most liquid)
    long_liquidations_1h: float = 0.0
    short_liquidations_1h: float = 0.0
    liquidation_bias: str = ""  # "longs_rekt", "shorts_rekt", "balanced"


@dataclass
class OptionsSnapshot:
    """Options market data from Deribit."""
    currency: str = ""          # BTC or ETH
    put_volume: float = 0.0
    call_volume: float = 0.0
    put_call_ratio: float = 0.0  # >1 = more puts (bearish hedging), <1 = more calls (bullish)
    put_oi: float = 0.0
    call_oi: float = 0.0
    put_call_oi_ratio: float = 0.0
    max_pain: float = 0.0       # Strike price where most options expire worthless
    sentiment: str = ""          # "bearish_hedging", "bullish_bets", "neutral"


@dataclass
class DerivativesSnapshot:
    """Full derivatives market snapshot."""
    timestamp: float = 0.0
    coins: dict = field(default_factory=dict)       # {symbol: CoinDerivatives}
    options: dict = field(default_factory=dict)      # {currency: OptionsSnapshot}
    market_leverage_sentiment: str = ""              # Overall: "overleveraged_long", "overleveraged_short", "balanced"
    fetch_errors: list = field(default_factory=list)


class DerivativesDataFetcher:
    """Fetches derivatives market data from free public APIs."""

    def __init__(self, http_client: httpx.AsyncClient):
        self._http = http_client
        self._last_snapshot: Optional[DerivativesSnapshot] = None
        self._last_fetch_time: float = 0.0
        self._cache_ttl: float = 120.0  # Cache for 2 minutes (funding rates don't change fast)

    async def fetch_all(self, coins: list[str] = None) -> DerivativesSnapshot:
        """Fetch all derivatives data for the given coins.

        Args:
            coins: List of coin symbols like ["BTC", "ETH", "SOL"]. Defaults to all.
        """
        # Return cached if fresh enough
        if self._last_snapshot and (time.time() - self._last_fetch_time) < self._cache_ttl:
            return self._last_snapshot

        if coins is None:
            coins = list(BINANCE_SYMBOLS.keys())

        snapshot = DerivativesSnapshot(timestamp=time.time())

        # Fire all fetches concurrently
        tasks = [
            self._fetch_binance_funding(coins),
            self._fetch_bybit_funding(coins),
            self._fetch_okx_funding(coins),
            self._fetch_binance_oi(coins),
            self._fetch_bybit_oi(coins),
            self._fetch_okx_oi(coins),
            self._fetch_binance_liquidations(coins),
            self._fetch_deribit_options(),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        binance_funding = results[0] if not isinstance(results[0], Exception) else {}
        bybit_funding = results[1] if not isinstance(results[1], Exception) else {}
        okx_funding = results[2] if not isinstance(results[2], Exception) else {}
        binance_oi = results[3] if not isinstance(results[3], Exception) else {}
        bybit_oi = results[4] if not isinstance(results[4], Exception) else {}
        okx_oi = results[5] if not isinstance(results[5], Exception) else {}
        binance_liqs = results[6] if not isinstance(results[6], Exception) else {}
        deribit_options = results[7] if not isinstance(results[7], Exception) else {}

        # Log errors
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                task_names = ["binance_funding", "bybit_funding", "okx_funding",
                              "binance_oi", "bybit_oi", "okx_oi",
                              "binance_liquidations", "deribit_options"]
                snapshot.fetch_errors.append(f"{task_names[i]}: {r}")
                logger.debug(f"Derivatives fetch error ({task_names[i]}): {r}")

        # Assemble per-coin data
        for coin in coins:
            cd = CoinDerivatives(symbol=coin)

            # Funding rates
            rates = []
            if coin in binance_funding:
                rates.append(binance_funding[coin])
            if coin in bybit_funding:
                rates.append(bybit_funding[coin])
            if coin in okx_funding:
                rates.append(okx_funding[coin])
            cd.funding_rates = rates

            if rates:
                cd.avg_funding_rate = sum(r.rate for r in rates) / len(rates)
                # Positive funding = longs pay shorts (bearish pressure)
                # Negative funding = shorts pay longs (bullish pressure)
                if cd.avg_funding_rate > 0.0005:
                    cd.funding_sentiment = "bearish (longs overleveraged)"
                elif cd.avg_funding_rate < -0.0005:
                    cd.funding_sentiment = "bullish (shorts overleveraged)"
                elif cd.avg_funding_rate > 0.0001:
                    cd.funding_sentiment = "slightly bearish"
                elif cd.avg_funding_rate < -0.0001:
                    cd.funding_sentiment = "slightly bullish"
                else:
                    cd.funding_sentiment = "neutral"

            # Open interest
            cd.binance_oi_usd = binance_oi.get(coin, 0)
            cd.bybit_oi_usd = bybit_oi.get(coin, 0)
            cd.okx_oi_usd = okx_oi.get(coin, 0)
            cd.total_oi_usd = cd.binance_oi_usd + cd.bybit_oi_usd + cd.okx_oi_usd

            # Liquidations
            liqs = binance_liqs.get(coin, {})
            cd.long_liquidations_1h = liqs.get("long", 0)
            cd.short_liquidations_1h = liqs.get("short", 0)
            total_liqs = cd.long_liquidations_1h + cd.short_liquidations_1h
            if total_liqs > 0:
                if cd.long_liquidations_1h > cd.short_liquidations_1h * 2:
                    cd.liquidation_bias = "longs_rekt"
                elif cd.short_liquidations_1h > cd.long_liquidations_1h * 2:
                    cd.liquidation_bias = "shorts_rekt"
                else:
                    cd.liquidation_bias = "balanced"

            snapshot.coins[coin] = cd

        # Options data
        for currency, opt in deribit_options.items():
            snapshot.options[currency] = opt

        # Overall leverage sentiment
        total_positive = sum(1 for c in snapshot.coins.values() if c.avg_funding_rate > 0.0002)
        total_negative = sum(1 for c in snapshot.coins.values() if c.avg_funding_rate < -0.0002)
        total_coins = len(snapshot.coins)
        if total_positive > total_coins * 0.6:
            snapshot.market_leverage_sentiment = "overleveraged_long"
        elif total_negative > total_coins * 0.6:
            snapshot.market_leverage_sentiment = "overleveraged_short"
        else:
            snapshot.market_leverage_sentiment = "balanced"

        self._last_snapshot = snapshot
        self._last_fetch_time = time.time()
        return snapshot

    # ------------------------------------------------------------------
    # Binance Futures
    # ------------------------------------------------------------------

    async def _fetch_binance_funding(self, coins: list[str]) -> dict:
        """Fetch current funding rates from Binance Futures."""
        result = {}
        try:
            resp = await self._http.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

            # Build lookup by symbol
            by_symbol = {item["symbol"]: item for item in data}

            for coin in coins:
                bsym = BINANCE_SYMBOLS.get(coin)
                if bsym and bsym in by_symbol:
                    item = by_symbol[bsym]
                    rate = float(item.get("lastFundingRate", 0))
                    result[coin] = FundingRate(
                        exchange="Binance",
                        symbol=coin,
                        rate=rate,
                        annualized=rate * 3 * 365 * 100,  # 3 funding periods per day
                        next_funding_time=float(item.get("nextFundingTime", 0)) / 1000,
                    )
        except Exception as e:
            logger.debug(f"Binance funding fetch failed: {e}")
            raise
        return result

    async def _fetch_binance_oi(self, coins: list[str]) -> dict:
        """Fetch open interest from Binance Futures."""
        result = {}
        tasks = []
        for coin in coins:
            bsym = BINANCE_SYMBOLS.get(coin)
            if bsym:
                tasks.append(self._fetch_single_binance_oi(coin, bsym))
        if tasks:
            oi_results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in oi_results:
                if isinstance(r, dict):
                    result.update(r)
        return result

    async def _fetch_single_binance_oi(self, coin: str, bsym: str) -> dict:
        """Fetch OI for a single Binance symbol."""
        try:
            resp = await self._http.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": bsym},
                timeout=8.0,
            )
            resp.raise_for_status()
            data = resp.json()
            oi_qty = float(data.get("openInterest", 0))
            # We'd need the price to convert to USD — estimate from mark price
            resp2 = await self._http.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol": bsym},
                timeout=8.0,
            )
            resp2.raise_for_status()
            mark = float(resp2.json().get("markPrice", 0))
            return {coin: oi_qty * mark}
        except Exception:
            return {}

    async def _fetch_binance_liquidations(self, coins: list[str]) -> dict:
        """Fetch recent liquidations from Binance Futures (last hour)."""
        result = {}
        cutoff = (time.time() - 3600) * 1000  # 1 hour ago in ms

        for coin in coins[:5]:  # Limit to top 5 to avoid rate limits
            bsym = BINANCE_SYMBOLS.get(coin)
            if not bsym:
                continue
            try:
                resp = await self._http.get(
                    "https://fapi.binance.com/fapi/v1/allForceOrders",
                    params={"symbol": bsym, "limit": 100},
                    timeout=8.0,
                )
                resp.raise_for_status()
                orders = resp.json()

                long_liq = 0.0
                short_liq = 0.0
                for order in orders:
                    if float(order.get("time", 0)) < cutoff:
                        continue
                    qty = float(order.get("origQty", 0))
                    price = float(order.get("averagePrice", 0) or order.get("price", 0))
                    value = qty * price
                    if order.get("side") == "SELL":
                        # Liquidated longs sell to close
                        long_liq += value
                    else:
                        # Liquidated shorts buy to close
                        short_liq += value

                result[coin] = {"long": long_liq, "short": short_liq}
            except Exception as e:
                logger.debug(f"Binance liquidation fetch failed for {coin}: {e}")

        return result

    # ------------------------------------------------------------------
    # Bybit
    # ------------------------------------------------------------------

    async def _fetch_bybit_funding(self, coins: list[str]) -> dict:
        """Fetch funding rates from Bybit."""
        result = {}
        try:
            resp = await self._http.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            tickers = data.get("result", {}).get("list", [])

            by_symbol = {t["symbol"]: t for t in tickers}

            for coin in coins:
                bsym = BYBIT_SYMBOLS.get(coin)
                if bsym and bsym in by_symbol:
                    t = by_symbol[bsym]
                    rate = float(t.get("fundingRate", 0))
                    result[coin] = FundingRate(
                        exchange="Bybit",
                        symbol=coin,
                        rate=rate,
                        annualized=rate * 3 * 365 * 100,
                    )
        except Exception as e:
            logger.debug(f"Bybit funding fetch failed: {e}")
            raise
        return result

    async def _fetch_bybit_oi(self, coins: list[str]) -> dict:
        """Fetch open interest from Bybit."""
        result = {}
        try:
            resp = await self._http.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            tickers = data.get("result", {}).get("list", [])

            by_symbol = {t["symbol"]: t for t in tickers}

            for coin in coins:
                bsym = BYBIT_SYMBOLS.get(coin)
                if bsym and bsym in by_symbol:
                    t = by_symbol[bsym]
                    oi_value = float(t.get("openInterestValue", 0))
                    result[coin] = oi_value
        except Exception as e:
            logger.debug(f"Bybit OI fetch failed: {e}")
            raise
        return result

    # ------------------------------------------------------------------
    # OKX
    # ------------------------------------------------------------------

    async def _fetch_okx_funding(self, coins: list[str]) -> dict:
        """Fetch funding rates from OKX."""
        result = {}
        for coin in coins:
            inst_id = OKX_SYMBOLS.get(coin)
            if not inst_id:
                continue
            try:
                resp = await self._http.get(
                    "https://www.okx.com/api/v5/public/funding-rate",
                    params={"instId": inst_id},
                    timeout=8.0,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("data", [])
                if items:
                    rate = float(items[0].get("fundingRate", 0))
                    result[coin] = FundingRate(
                        exchange="OKX",
                        symbol=coin,
                        rate=rate,
                        annualized=rate * 3 * 365 * 100,
                    )
            except Exception as e:
                logger.debug(f"OKX funding fetch failed for {coin}: {e}")
        return result

    async def _fetch_okx_oi(self, coins: list[str]) -> dict:
        """Fetch open interest from OKX."""
        result = {}
        for coin in coins:
            inst_id = OKX_SYMBOLS.get(coin)
            if not inst_id:
                continue
            try:
                resp = await self._http.get(
                    "https://www.okx.com/api/v5/public/open-interest",
                    params={"instType": "SWAP", "instId": inst_id},
                    timeout=8.0,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("data", [])
                if items:
                    # OKX returns OI in contracts — need to convert
                    oi_contracts = float(items[0].get("oi", 0))
                    # Get mark price for USD conversion
                    try:
                        resp2 = await self._http.get(
                            "https://www.okx.com/api/v5/public/mark-price",
                            params={"instType": "SWAP", "instId": inst_id},
                            timeout=8.0,
                        )
                        resp2.raise_for_status()
                        mark_data = resp2.json().get("data", [])
                        mark_price = float(mark_data[0].get("markPx", 0)) if mark_data else 0
                    except Exception:
                        mark_price = 0

                    if mark_price > 0:
                        # Contract size varies by coin — approximate
                        ct_val = float(items[0].get("oiCcy", oi_contracts))
                        result[coin] = ct_val * mark_price
                    else:
                        result[coin] = 0
            except Exception as e:
                logger.debug(f"OKX OI fetch failed for {coin}: {e}")
        return result

    # ------------------------------------------------------------------
    # Deribit (Options)
    # ------------------------------------------------------------------

    async def _fetch_deribit_options(self) -> dict:
        """Fetch options data from Deribit (BTC and ETH only)."""
        result = {}
        for currency in DERIBIT_CURRENCIES:
            try:
                resp = await self._http.get(
                    "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
                    params={"currency": currency, "kind": "option"},
                    timeout=10.0,
                )
                resp.raise_for_status()
                data = resp.json()
                instruments = data.get("result", [])

                put_volume = 0.0
                call_volume = 0.0
                put_oi = 0.0
                call_oi = 0.0

                for inst in instruments:
                    name = inst.get("instrument_name", "")
                    vol = float(inst.get("volume", 0))
                    oi = float(inst.get("open_interest", 0))

                    if "-P" in name:
                        put_volume += vol
                        put_oi += oi
                    elif "-C" in name:
                        call_volume += vol
                        call_oi += oi

                total_vol = put_volume + call_volume
                total_oi = put_oi + call_oi
                pc_ratio = (put_volume / call_volume) if call_volume > 0 else 0
                pc_oi_ratio = (put_oi / call_oi) if call_oi > 0 else 0

                # Determine sentiment
                if pc_ratio > 1.2:
                    sentiment = "bearish_hedging"
                elif pc_ratio < 0.7:
                    sentiment = "bullish_bets"
                else:
                    sentiment = "neutral"

                result[currency] = OptionsSnapshot(
                    currency=currency,
                    put_volume=put_volume,
                    call_volume=call_volume,
                    put_call_ratio=round(pc_ratio, 3),
                    put_oi=put_oi,
                    call_oi=call_oi,
                    put_call_oi_ratio=round(pc_oi_ratio, 3),
                    sentiment=sentiment,
                )
            except Exception as e:
                logger.debug(f"Deribit options fetch failed for {currency}: {e}")
                raise
        return result

    # ------------------------------------------------------------------
    # Formatting for AI context
    # ------------------------------------------------------------------

    def format_for_context(self, snapshot: DerivativesSnapshot) -> str:
        """Format derivatives data for injection into AI context."""
        if not snapshot or not snapshot.coins:
            return ""

        parts = [
            "\n## DERIVATIVES & LEVERAGE DATA (Futures + Options)",
            f"Overall leverage sentiment: {snapshot.market_leverage_sentiment}",
        ]

        # Funding rates table
        parts.append("\n### Funding Rates (8h rate | annualized)")
        parts.append("Positive = longs pay shorts (bearish), Negative = shorts pay longs (bullish)")
        for coin, cd in sorted(snapshot.coins.items(), key=lambda x: abs(x[1].avg_funding_rate), reverse=True):
            if not cd.funding_rates:
                continue
            rate_strs = []
            for fr in cd.funding_rates:
                rate_strs.append(f"{fr.exchange}:{fr.rate:+.4%}")
            avg_ann = cd.avg_funding_rate * 3 * 365 * 100
            parts.append(
                f"  {coin}: avg {cd.avg_funding_rate:+.4%} ({avg_ann:+.1f}%/yr) "
                f"[{', '.join(rate_strs)}] — {cd.funding_sentiment}"
            )

        # Open interest
        coins_with_oi = [(c, d) for c, d in snapshot.coins.items() if d.total_oi_usd > 0]
        if coins_with_oi:
            parts.append("\n### Open Interest (USD)")
            for coin, cd in sorted(coins_with_oi, key=lambda x: x[1].total_oi_usd, reverse=True):
                parts.append(
                    f"  {coin}: ${cd.total_oi_usd:,.0f} total "
                    f"(Binance: ${cd.binance_oi_usd:,.0f}, Bybit: ${cd.bybit_oi_usd:,.0f}, OKX: ${cd.okx_oi_usd:,.0f})"
                )

        # Liquidations
        coins_with_liqs = [(c, d) for c, d in snapshot.coins.items()
                           if d.long_liquidations_1h > 0 or d.short_liquidations_1h > 0]
        if coins_with_liqs:
            parts.append("\n### Liquidations (last 1h)")
            for coin, cd in sorted(coins_with_liqs,
                                   key=lambda x: x[1].long_liquidations_1h + x[1].short_liquidations_1h,
                                   reverse=True):
                total = cd.long_liquidations_1h + cd.short_liquidations_1h
                parts.append(
                    f"  {coin}: ${total:,.0f} total | "
                    f"Longs rekt: ${cd.long_liquidations_1h:,.0f} | Shorts rekt: ${cd.short_liquidations_1h:,.0f} "
                    f"— {cd.liquidation_bias}"
                )

        # Options
        if snapshot.options:
            parts.append("\n### Options Market (Deribit)")
            parts.append("Put/Call ratio >1 = hedging/bearish, <1 = bullish bets")
            for currency, opt in snapshot.options.items():
                parts.append(
                    f"  {currency}: P/C ratio: {opt.put_call_ratio:.3f} (volume) / "
                    f"{opt.put_call_oi_ratio:.3f} (OI) — {opt.sentiment}"
                )
                parts.append(
                    f"    Puts: {opt.put_volume:.1f} vol, {opt.put_oi:.1f} OI | "
                    f"Calls: {opt.call_volume:.1f} vol, {opt.call_oi:.1f} OI"
                )

        return "\n".join(parts)
