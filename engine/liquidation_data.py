"""
Liquidation & Leverage Intelligence: Forward-looking liquidation levels, long/short ratios, and OI analysis.

Fetches from free public APIs (no auth needed):
- Binance Futures: global long/short ratio, top trader long/short ratio, OI history
- Bybit: account long/short ratio

Combines data to estimate:
- Where liquidation clusters sit (above and below current price)
- Whether the market is overleveraged (and in which direction)
- "Liquidation magnets" — price levels the market may be drawn toward
- OI vs price divergence signals (potential squeezes)

Gives Claude a forward-looking view of where forced selling/buying will occur.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Symbols we track
SYMBOLS = {
    "BTC": {"binance": "BTCUSDT", "bybit": "BTCUSDT"},
    "ETH": {"binance": "ETHUSDT", "bybit": "ETHUSDT"},
}

# Common leverage tiers used by retail and semi-pro traders
LEVERAGE_TIERS = [5, 10, 25, 50, 100]

# Rough distribution of OI across leverage tiers (empirical estimate).
# Most OI sits in 5x-25x; 50x and 100x are smaller but their liquidations
# are closer to current price, so they trigger first.
LEVERAGE_OI_WEIGHTS = {
    5: 0.25,
    10: 0.30,
    25: 0.25,
    50: 0.12,
    100: 0.08,
}


@dataclass
class LongShortRatio:
    """Long/short ratio data point from a single source."""
    source: str = ""          # "binance_global", "binance_top", "bybit"
    symbol: str = ""
    long_ratio: float = 0.0   # e.g. 0.55 = 55% longs
    short_ratio: float = 0.0  # e.g. 0.45 = 45% shorts
    ls_ratio: float = 0.0     # long_account / short_account (>1 = more longs)
    timestamp: float = 0.0
    # Trend from historical data points
    trend: str = ""            # "longs_increasing", "longs_decreasing", "stable"
    trend_change_pct: float = 0.0  # How much longs changed over the period


@dataclass
class OIHistory:
    """Open interest history data point."""
    symbol: str = ""
    latest_oi: float = 0.0        # Most recent OI in USD
    oldest_oi: float = 0.0        # Oldest OI in the window
    oi_change_pct: float = 0.0    # Percent change over the window
    oi_trend: str = ""             # "rising", "falling", "flat"
    # Price context (from the OI history endpoint which includes sumOpenInterestValue)
    latest_price: float = 0.0
    oldest_price: float = 0.0
    price_change_pct: float = 0.0
    price_trend: str = ""          # "rising", "falling", "flat"
    # Combined signal
    signal: str = ""               # "strong_trend", "weakening_trend", "squeeze_building", "capitulation"


@dataclass
class LiquidationLevel:
    """Estimated liquidation level at a given leverage."""
    leverage: int = 0
    side: str = ""                 # "long" or "short"
    liquidation_price: float = 0.0
    distance_pct: float = 0.0     # Percentage from current price
    estimated_oi_usd: float = 0.0  # Estimated OI at this level
    weight: float = 0.0           # Relative importance (OI weight)


@dataclass
class LiquidationSnapshot:
    """Complete liquidation and leverage intelligence snapshot."""
    timestamp: float = 0.0
    btc_price: float = 0.0
    eth_price: float = 0.0

    # Long/short ratios (multiple sources per symbol)
    long_short_ratios: dict = field(default_factory=dict)   # {symbol: [LongShortRatio, ...]}
    consensus_ls: dict = field(default_factory=dict)        # {symbol: {"ratio": float, "bias": str}}

    # OI history
    oi_history: dict = field(default_factory=dict)          # {symbol: OIHistory}

    # Liquidation levels
    liquidation_levels: dict = field(default_factory=dict)  # {symbol: [LiquidationLevel, ...]}
    liquidation_magnets: dict = field(default_factory=dict) # {symbol: {"price": float, "side": str, "volume": float}}

    # Leverage health
    leverage_health: dict = field(default_factory=dict)     # {symbol: {"status": str, "detail": str}}

    # Errors
    fetch_errors: list = field(default_factory=list)


class LiquidationDataFetcher:
    """Fetches forward-looking liquidation and leverage intelligence from free APIs."""

    def __init__(self, http_client: httpx.AsyncClient):
        self._http = http_client
        self._last_snapshot: Optional[LiquidationSnapshot] = None
        self._last_fetch_time: float = 0.0
        self._cache_ttl: float = 300.0  # 5-minute cache

    async def fetch_all(self, btc_price: float, eth_price: float = 0.0) -> LiquidationSnapshot:
        """Fetch all liquidation/leverage data.

        Args:
            btc_price: Current BTC price (needed for liquidation level calculation).
            eth_price: Current ETH price. If 0, ETH liquidation levels are skipped.
        """
        # Return cached if fresh enough
        if self._last_snapshot and (time.time() - self._last_fetch_time) < self._cache_ttl:
            return self._last_snapshot

        snapshot = LiquidationSnapshot(
            timestamp=time.time(),
            btc_price=btc_price,
            eth_price=eth_price,
        )

        # Fire all API fetches concurrently
        tasks = [
            self._fetch_binance_global_ls("BTC"),
            self._fetch_binance_global_ls("ETH"),
            self._fetch_binance_top_ls("BTC"),
            self._fetch_binance_top_ls("ETH"),
            self._fetch_bybit_ls("BTC"),
            self._fetch_bybit_ls("ETH"),
            self._fetch_binance_oi_history("BTC"),
            self._fetch_binance_oi_history("ETH"),
        ]
        task_names = [
            "binance_global_ls_BTC", "binance_global_ls_ETH",
            "binance_top_ls_BTC", "binance_top_ls_ETH",
            "bybit_ls_BTC", "bybit_ls_ETH",
            "binance_oi_hist_BTC", "binance_oi_hist_ETH",
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Unpack long/short ratio results
        ls_results = {
            "BTC": [],
            "ETH": [],
        }
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                snapshot.fetch_errors.append(f"{task_names[i]}: {r}")
                logger.debug(f"Liquidation data fetch error ({task_names[i]}): {r}")
                continue

            if i < 6 and isinstance(r, LongShortRatio):
                ls_results[r.symbol].append(r)
            elif i == 6 and isinstance(r, OIHistory):
                snapshot.oi_history["BTC"] = r
            elif i == 7 and isinstance(r, OIHistory):
                snapshot.oi_history["ETH"] = r

        snapshot.long_short_ratios = ls_results

        # Compute consensus long/short ratio per symbol
        for symbol, ratios in ls_results.items():
            if ratios:
                avg_ratio = sum(r.ls_ratio for r in ratios) / len(ratios)
                if avg_ratio > 1.15:
                    bias = "heavily_long"
                elif avg_ratio > 1.03:
                    bias = "slightly_long"
                elif avg_ratio < 0.87:
                    bias = "heavily_short"
                elif avg_ratio < 0.97:
                    bias = "slightly_short"
                else:
                    bias = "balanced"
                snapshot.consensus_ls[symbol] = {"ratio": round(avg_ratio, 4), "bias": bias}

        # Compute liquidation levels
        prices = {"BTC": btc_price, "ETH": eth_price}
        for symbol, price in prices.items():
            if price <= 0:
                continue
            total_oi = 0.0
            oi_hist = snapshot.oi_history.get(symbol)
            if oi_hist:
                total_oi = oi_hist.latest_oi

            levels = self._compute_liquidation_levels(symbol, price, total_oi)
            snapshot.liquidation_levels[symbol] = levels

            # Find the liquidation magnet (highest estimated volume closest to price)
            magnet = self._find_liquidation_magnet(levels, price)
            if magnet:
                snapshot.liquidation_magnets[symbol] = magnet

        # Assess leverage health
        for symbol in ["BTC", "ETH"]:
            snapshot.leverage_health[symbol] = self._assess_leverage_health(
                snapshot.consensus_ls.get(symbol),
                snapshot.oi_history.get(symbol),
            )

        self._last_snapshot = snapshot
        self._last_fetch_time = time.time()
        return snapshot

    # ------------------------------------------------------------------
    # Binance: Global Long/Short Account Ratio
    # ------------------------------------------------------------------

    async def _fetch_binance_global_ls(self, symbol: str) -> LongShortRatio:
        """Fetch global long/short account ratio from Binance Futures."""
        bsym = SYMBOLS[symbol]["binance"]
        try:
            resp = await self._http.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": bsym, "period": "1h", "limit": 12},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse_ls_data(data, symbol, "binance_global")
        except Exception as e:
            logger.debug(f"Binance global L/S fetch failed for {symbol}: {e}")
            raise

    # ------------------------------------------------------------------
    # Binance: Top Trader Long/Short Account Ratio
    # ------------------------------------------------------------------

    async def _fetch_binance_top_ls(self, symbol: str) -> LongShortRatio:
        """Fetch top trader long/short account ratio from Binance Futures."""
        bsym = SYMBOLS[symbol]["binance"]
        try:
            resp = await self._http.get(
                "https://fapi.binance.com/futures/data/topLongShortAccountRatio",
                params={"symbol": bsym, "period": "1h", "limit": 12},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse_ls_data(data, symbol, "binance_top")
        except Exception as e:
            logger.debug(f"Binance top L/S fetch failed for {symbol}: {e}")
            raise

    # ------------------------------------------------------------------
    # Bybit: Long/Short Account Ratio
    # ------------------------------------------------------------------

    async def _fetch_bybit_ls(self, symbol: str) -> LongShortRatio:
        """Fetch long/short account ratio from Bybit."""
        bsym = SYMBOLS[symbol]["bybit"]
        try:
            resp = await self._http.get(
                "https://api.bybit.com/v5/market/account-ratio",
                params={"category": "linear", "symbol": bsym, "period": "1h", "limit": 12},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("result", {}).get("list", [])

            if not items:
                raise ValueError(f"No Bybit L/S data for {symbol}")

            # Bybit returns newest first
            latest = items[0]
            oldest = items[-1]

            buy_ratio = float(latest.get("buyRatio", 0.5))
            sell_ratio = float(latest.get("sellRatio", 0.5))
            ls_ratio = buy_ratio / sell_ratio if sell_ratio > 0 else 1.0

            oldest_buy = float(oldest.get("buyRatio", 0.5))
            change = buy_ratio - oldest_buy

            if change > 0.02:
                trend = "longs_increasing"
            elif change < -0.02:
                trend = "longs_decreasing"
            else:
                trend = "stable"

            return LongShortRatio(
                source="bybit",
                symbol=symbol,
                long_ratio=buy_ratio,
                short_ratio=sell_ratio,
                ls_ratio=round(ls_ratio, 4),
                timestamp=float(latest.get("timestamp", 0)) / 1000,
                trend=trend,
                trend_change_pct=round(change * 100, 2),
            )
        except Exception as e:
            logger.debug(f"Bybit L/S fetch failed for {symbol}: {e}")
            raise

    # ------------------------------------------------------------------
    # Binance: Open Interest History
    # ------------------------------------------------------------------

    async def _fetch_binance_oi_history(self, symbol: str) -> OIHistory:
        """Fetch OI history from Binance Futures (includes price context)."""
        bsym = SYMBOLS[symbol]["binance"]
        try:
            resp = await self._http.get(
                "https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": bsym, "period": "1h", "limit": 12},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

            if not data or not isinstance(data, list):
                raise ValueError(f"No OI history data for {symbol} (response: {str(data)[:100]})")

            # Data comes sorted oldest to newest
            latest = data[-1]
            oldest = data[0]

            # Binance may use "sumOpenInterestValue" (USD) or "sumOpenInterest" (contracts)
            latest_oi = float(latest.get("sumOpenInterestValue") or latest.get("sumOpenInterest", 0))
            oldest_oi = float(oldest.get("sumOpenInterestValue") or oldest.get("sumOpenInterest", 0))
            if latest_oi == 0:
                logger.debug(f"Binance OI fields for {symbol}: {list(latest.keys())}")
            oi_change_pct = ((latest_oi - oldest_oi) / oldest_oi * 100) if oldest_oi > 0 else 0.0

            if oi_change_pct > 3:
                oi_trend = "rising"
            elif oi_change_pct < -3:
                oi_trend = "falling"
            else:
                oi_trend = "flat"

            # Extract price from sumOpenInterest (contracts) and sumOpenInterestValue (USD)
            latest_contracts = float(latest.get("sumOpenInterest", 0))
            oldest_contracts = float(oldest.get("sumOpenInterest", 0))
            latest_price = (latest_oi / latest_contracts) if latest_contracts > 0 else 0.0
            oldest_price = (oldest_oi / oldest_contracts) if oldest_contracts > 0 else 0.0
            price_change_pct = ((latest_price - oldest_price) / oldest_price * 100) if oldest_price > 0 else 0.0

            if price_change_pct > 1:
                price_trend = "rising"
            elif price_change_pct < -1:
                price_trend = "falling"
            else:
                price_trend = "flat"

            # Combined OI + Price signal
            signal = self._compute_oi_price_signal(oi_trend, price_trend)

            return OIHistory(
                symbol=symbol,
                latest_oi=latest_oi,
                oldest_oi=oldest_oi,
                oi_change_pct=round(oi_change_pct, 2),
                oi_trend=oi_trend,
                latest_price=round(latest_price, 2),
                oldest_price=round(oldest_price, 2),
                price_change_pct=round(price_change_pct, 2),
                price_trend=price_trend,
                signal=signal,
            )
        except Exception as e:
            logger.debug(f"Binance OI history fetch failed for {symbol}: {e}")
            raise

    # ------------------------------------------------------------------
    # Helpers: Parsing and Computation
    # ------------------------------------------------------------------

    def _parse_ls_data(self, data: list, symbol: str, source: str) -> LongShortRatio:
        """Parse Binance-style long/short ratio data."""
        if not data:
            raise ValueError(f"No L/S data from {source} for {symbol}")

        # Data comes sorted oldest to newest
        latest = data[-1]
        oldest = data[0]

        long_account = float(latest.get("longAccount", 0.5))
        short_account = float(latest.get("shortAccount", 0.5))
        ls_ratio = float(latest.get("longShortRatio", 1.0))

        oldest_long = float(oldest.get("longAccount", 0.5))
        change = long_account - oldest_long

        if change > 0.02:
            trend = "longs_increasing"
        elif change < -0.02:
            trend = "longs_decreasing"
        else:
            trend = "stable"

        return LongShortRatio(
            source=source,
            symbol=symbol,
            long_ratio=long_account,
            short_ratio=short_account,
            ls_ratio=round(ls_ratio, 4),
            timestamp=float(latest.get("timestamp", 0)) / 1000,
            trend=trend,
            trend_change_pct=round(change * 100, 2),
        )

    @staticmethod
    def _compute_oi_price_signal(oi_trend: str, price_trend: str) -> str:
        """Determine the combined OI + price signal.

        Classic interpretations:
        - OI rising + price rising = strong trend (new money entering, confirming direction)
        - OI rising + price falling = squeeze building (new shorts entering or longs averaging down)
        - OI falling + price rising = short squeeze / weak rally (shorts closing, not new longs)
        - OI falling + price falling = capitulation (longs giving up, liquidations clearing)
        """
        if oi_trend == "rising" and price_trend == "rising":
            return "strong_uptrend"
        elif oi_trend == "rising" and price_trend == "falling":
            return "squeeze_building"
        elif oi_trend == "rising" and price_trend == "flat":
            return "positions_building"
        elif oi_trend == "falling" and price_trend == "rising":
            return "short_squeeze"
        elif oi_trend == "falling" and price_trend == "falling":
            return "capitulation"
        elif oi_trend == "falling" and price_trend == "flat":
            return "positions_unwinding"
        elif oi_trend == "flat" and price_trend == "rising":
            return "mild_bullish"
        elif oi_trend == "flat" and price_trend == "falling":
            return "mild_bearish"
        else:
            return "neutral"

    def _compute_liquidation_levels(
        self, symbol: str, current_price: float, total_oi_usd: float
    ) -> list:
        """Compute estimated liquidation prices for common leverage tiers.

        For a long position at leverage X:
          liquidation_price = entry_price * (1 - 1/X)  (approx, ignoring maintenance margin)

        For a short position at leverage X:
          liquidation_price = entry_price * (1 + 1/X)

        We assume positions were opened near the current price.
        """
        levels = []
        for lev in LEVERAGE_TIERS:
            weight = LEVERAGE_OI_WEIGHTS.get(lev, 0.1)
            estimated_oi = total_oi_usd * weight

            # Long liquidation (below current price)
            long_liq_price = current_price * (1.0 - 1.0 / lev)
            long_distance = -100.0 / lev  # Negative = below current price
            levels.append(LiquidationLevel(
                leverage=lev,
                side="long",
                liquidation_price=round(long_liq_price, 2),
                distance_pct=round(long_distance, 2),
                estimated_oi_usd=round(estimated_oi / 2, 0),  # Split OI 50/50 long/short
                weight=weight,
            ))

            # Short liquidation (above current price)
            short_liq_price = current_price * (1.0 + 1.0 / lev)
            short_distance = 100.0 / lev  # Positive = above current price
            levels.append(LiquidationLevel(
                leverage=lev,
                side="short",
                liquidation_price=round(short_liq_price, 2),
                distance_pct=round(short_distance, 2),
                estimated_oi_usd=round(estimated_oi / 2, 0),
                weight=weight,
            ))

        return levels

    def _find_liquidation_magnet(self, levels: list, current_price: float) -> Optional[dict]:
        """Find the liquidation level most likely to attract price.

        The "magnet" is the level with the highest OI that is closest to the current price.
        We score by: estimated_oi / distance^2 (closer + bigger = stronger magnet).
        """
        if not levels:
            return None

        best = None
        best_score = 0.0

        for level in levels:
            distance = abs(level.liquidation_price - current_price)
            if distance < current_price * 0.002:
                # Too close (<0.2%), skip — likely noise
                continue
            # Score: OI weighted by inverse distance squared
            score = level.estimated_oi_usd / (distance ** 2) if distance > 0 else 0
            if score > best_score:
                best_score = score
                best = level

        if best:
            return {
                "price": best.liquidation_price,
                "side": best.side,
                "leverage": best.leverage,
                "volume": best.estimated_oi_usd,
                "distance_pct": best.distance_pct,
            }
        return None

    def _assess_leverage_health(
        self, consensus: Optional[dict], oi_hist: Optional[OIHistory]
    ) -> dict:
        """Assess overall leverage health for a symbol."""
        status = "unknown"
        details = []

        if consensus:
            ratio = consensus["ratio"]
            bias = consensus["bias"]
            if bias in ("heavily_long",):
                details.append(f"Heavily long-biased (L/S ratio: {ratio:.2f}) — longs vulnerable")
                status = "overleveraged_long"
            elif bias in ("heavily_short",):
                details.append(f"Heavily short-biased (L/S ratio: {ratio:.2f}) — shorts vulnerable")
                status = "overleveraged_short"
            elif bias in ("slightly_long",):
                details.append(f"Slightly long-leaning (L/S ratio: {ratio:.2f})")
                status = "slightly_long"
            elif bias in ("slightly_short",):
                details.append(f"Slightly short-leaning (L/S ratio: {ratio:.2f})")
                status = "slightly_short"
            else:
                details.append(f"Balanced positioning (L/S ratio: {ratio:.2f})")
                status = "healthy"

        if oi_hist:
            signal = oi_hist.signal
            if signal == "squeeze_building":
                details.append(f"OI rising while price falling — squeeze building (OI {oi_hist.oi_change_pct:+.1f}%, price {oi_hist.price_change_pct:+.1f}%)")
                if status == "overleveraged_long":
                    status = "cascade_risk_long"
                elif status == "overleveraged_short":
                    status = "cascade_risk_short"
            elif signal == "strong_uptrend":
                details.append(f"OI rising with price — strong trend, but watch for exhaustion (OI {oi_hist.oi_change_pct:+.1f}%, price {oi_hist.price_change_pct:+.1f}%)")
            elif signal == "capitulation":
                details.append(f"OI and price both falling — capitulation / deleveraging (OI {oi_hist.oi_change_pct:+.1f}%, price {oi_hist.price_change_pct:+.1f}%)")
            elif signal == "short_squeeze":
                details.append(f"OI falling while price rising — short squeeze in progress (OI {oi_hist.oi_change_pct:+.1f}%, price {oi_hist.price_change_pct:+.1f}%)")
            elif signal == "positions_building":
                details.append(f"OI rising, price flat — positions building, breakout imminent (OI {oi_hist.oi_change_pct:+.1f}%)")
            else:
                details.append(f"OI signal: {signal} (OI {oi_hist.oi_change_pct:+.1f}%, price {oi_hist.price_change_pct:+.1f}%)")

        if not details:
            details.append("Insufficient data for leverage assessment")
            status = "unknown"

        return {"status": status, "detail": " | ".join(details)}

    # ------------------------------------------------------------------
    # Formatting for AI context
    # ------------------------------------------------------------------

    def format_for_context(self, data: LiquidationSnapshot) -> str:
        """Format liquidation/leverage data for injection into AI context."""
        if not data:
            return ""

        parts = [
            "\n## LIQUIDATION & LEVERAGE INTELLIGENCE",
            f"BTC price: ${data.btc_price:,.0f}" + (f" | ETH price: ${data.eth_price:,.0f}" if data.eth_price > 0 else ""),
        ]

        # --- Long/Short Ratios ---
        parts.append("\n### Long/Short Ratios")
        parts.append("L/S ratio >1 = more longs, <1 = more shorts")
        for symbol in ["BTC", "ETH"]:
            ratios = data.long_short_ratios.get(symbol, [])
            consensus = data.consensus_ls.get(symbol)
            if not ratios and not consensus:
                continue

            parts.append(f"\n  {symbol}:")
            if consensus:
                parts.append(f"    Consensus: L/S = {consensus['ratio']:.4f} — {consensus['bias']}")

            for r in ratios:
                trend_str = f"{r.trend} ({r.trend_change_pct:+.1f}% over 12h)" if r.trend else ""
                parts.append(
                    f"    {r.source}: L={r.long_ratio:.1%} S={r.short_ratio:.1%} "
                    f"(ratio: {r.ls_ratio:.4f}) {trend_str}"
                )

        # --- OI vs Price Analysis ---
        if data.oi_history:
            parts.append("\n### Open Interest vs Price (12h window)")
            for symbol, oi in data.oi_history.items():
                signal_descriptions = {
                    "strong_uptrend": "STRONG TREND — new money confirming rally",
                    "squeeze_building": "SQUEEZE BUILDING — rising OI into falling price, forced liquidations ahead",
                    "short_squeeze": "SHORT SQUEEZE — shorts closing, fueling rally",
                    "capitulation": "CAPITULATION — positions unwinding, deleveraging",
                    "positions_building": "POSITIONS BUILDING — flat price + rising OI, breakout imminent",
                    "positions_unwinding": "UNWINDING — declining interest, lower volatility ahead",
                    "mild_bullish": "Mildly bullish — price rising on stable OI",
                    "mild_bearish": "Mildly bearish — price falling on stable OI",
                    "neutral": "Neutral — no strong signal",
                }
                signal_desc = signal_descriptions.get(oi.signal, oi.signal)
                parts.append(
                    f"  {symbol}: OI ${oi.latest_oi:,.0f} ({oi.oi_change_pct:+.1f}% {oi.oi_trend}) | "
                    f"Price {oi.price_change_pct:+.1f}% ({oi.price_trend})"
                )
                parts.append(f"    Signal: {signal_desc}")

        # --- Leverage Health ---
        if data.leverage_health:
            parts.append("\n### Leverage Health Assessment")
            for symbol in ["BTC", "ETH"]:
                health = data.leverage_health.get(symbol)
                if not health:
                    continue
                status_icons = {
                    "cascade_risk_long": "DANGER",
                    "cascade_risk_short": "DANGER",
                    "overleveraged_long": "WARNING",
                    "overleveraged_short": "WARNING",
                    "slightly_long": "CAUTION",
                    "slightly_short": "CAUTION",
                    "healthy": "OK",
                    "unknown": "NO DATA",
                }
                icon = status_icons.get(health["status"], "?")
                parts.append(f"  {symbol} [{icon}]: {health['detail']}")

        # --- Liquidation Levels ---
        for symbol in ["BTC", "ETH"]:
            levels = data.liquidation_levels.get(symbol, [])
            if not levels:
                continue

            price = data.btc_price if symbol == "BTC" else data.eth_price
            parts.append(f"\n### {symbol} Liquidation Map (current: ${price:,.0f})")

            # Sort: levels above price (shorts) ascending, levels below price (longs) descending
            above = sorted([l for l in levels if l.side == "short"], key=lambda x: x.liquidation_price)
            below = sorted([l for l in levels if l.side == "long"], key=lambda x: x.liquidation_price, reverse=True)

            parts.append("  SHORT liquidations (above price, ascending):")
            for l in above:
                oi_str = f"${l.estimated_oi_usd:,.0f}" if l.estimated_oi_usd > 0 else "est. N/A"
                parts.append(
                    f"    {l.leverage:>3}x shorts liq @ ${l.liquidation_price:>10,.0f} "
                    f"({l.distance_pct:+.1f}% away) — OI {oi_str}"
                )

            parts.append("  LONG liquidations (below price, descending):")
            for l in below:
                oi_str = f"${l.estimated_oi_usd:,.0f}" if l.estimated_oi_usd > 0 else "est. N/A"
                parts.append(
                    f"    {l.leverage:>3}x longs  liq @ ${l.liquidation_price:>10,.0f} "
                    f"({l.distance_pct:+.1f}% away) — OI {oi_str}"
                )

        # --- Liquidation Magnets ---
        if data.liquidation_magnets:
            parts.append("\n### Liquidation Magnets (price levels market may be drawn to)")
            for symbol in ["BTC", "ETH"]:
                magnet = data.liquidation_magnets.get(symbol)
                if not magnet:
                    continue
                price = data.btc_price if symbol == "BTC" else data.eth_price
                direction = "BELOW" if magnet["price"] < price else "ABOVE"
                side_desc = "longs" if magnet["side"] == "long" else "shorts"
                parts.append(
                    f"  {symbol}: ${magnet['price']:,.0f} ({direction}, {magnet['distance_pct']:+.1f}%) — "
                    f"{magnet['leverage']}x {side_desc} cluster (~${magnet['volume']:,.0f} OI)"
                )

                # Add interpretation
                if magnet["side"] == "long" and magnet["price"] < price:
                    parts.append(
                        f"    Interpretation: {side_desc} overleveraged near ${magnet['price']:,.0f} — "
                        f"price could be drawn down to liquidate them"
                    )
                elif magnet["side"] == "short" and magnet["price"] > price:
                    parts.append(
                        f"    Interpretation: {side_desc} overleveraged near ${magnet['price']:,.0f} — "
                        f"price could be drawn up to squeeze them"
                    )

        # --- Errors ---
        if data.fetch_errors:
            parts.append(f"\n  [Data gaps: {len(data.fetch_errors)} fetch(es) failed]")

        return "\n".join(parts)
