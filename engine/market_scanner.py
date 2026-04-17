"""
Market Scanner: fetches live crypto data from Kraken public API
and computes cross-market analysis for AI-driven trading decisions.
Full technical indicators (EMA, RSI, Bollinger Bands) for all coins.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from indicators import generate_signals, Signals

logger = logging.getLogger(__name__)

KRAKEN_BASE = "https://api.kraken.com/0/public"


@dataclass
class CoinSnapshot:
    symbol: str
    price: float
    change_1h: float
    change_24h: float
    volume_24h: float
    volume_change_pct: float
    rsi: Optional[float]
    momentum_score: float  # -1 to +1
    relative_strength: float  # vs BTC
    # Full technical indicators (computed from OHLCV)
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    ema_crossover: Optional[str] = None  # "bullish" / "bearish" / "neutral"
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    bb_position: Optional[float] = None  # 0.0 = at lower, 1.0 = at upper
    bb_bandwidth: Optional[float] = None
    rsi_signal: Optional[str] = None  # "oversold" / "overbought" / "neutral"
    composite_score: Optional[float] = None  # -1.0 to +1.0
    recommendation: Optional[str] = None  # "STRONG_BUY" / "BUY" / "HOLD" / "SELL" / "STRONG_SELL"


@dataclass
class GlobalMarketData:
    """Global crypto market data from CoinGecko."""
    btc_dominance: float = 0.0           # e.g. 54.3 (percent)
    eth_dominance: float = 0.0
    total_market_cap_usd: float = 0.0
    total_volume_24h_usd: float = 0.0
    market_cap_change_24h_pct: float = 0.0
    active_coins: int = 0


@dataclass
class MarketOverview:
    coin_snapshots: List[CoinSnapshot]
    btc_dominance_trend: str
    market_momentum: str  # "risk_on" / "risk_off" / "neutral"
    sector_rotation_signal: str
    top_movers: List[str]
    correlations: Dict[str, List[str]]
    timestamp: float
    global_data: Optional[GlobalMarketData] = None


def _compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Compute RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-(period):]
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / period if gains else 0.0
    avg_loss = sum(losses) / period if losses else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


class MarketScanner:
    """Scans Kraken for multi-coin market data and builds a cross-market overview."""

    WATCHLIST: List[str] = [
        "XBTUSD",
        "ETHUSD",
        "SOLUSD",
        "DOGEUSD",
        "ADAUSD",
        "AVAXUSD",
        "LINKUSD",
        "DOTUSD",
        "POLUSD",
        "XRPUSD",
    ]

    # Friendly names for display
    SYMBOL_MAP: Dict[str, str] = {
        "XBTUSD": "BTC",
        "ETHUSD": "ETH",
        "SOLUSD": "SOL",
        "DOGEUSD": "DOGE",
        "ADAUSD": "ADA",
        "AVAXUSD": "AVAX",
        "LINKUSD": "LINK",
        "DOTUSD": "DOT",
        "POLUSD": "POL",
        "XRPUSD": "XRP",
    }

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def scan_all(self) -> MarketOverview:
        """Concurrently fetch ticker + OHLCV for every coin + global data, return full overview."""
        tasks = [self._fetch_coin_data(pair) for pair in self.WATCHLIST]
        tasks.append(self._fetch_global_market_data())
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Last result is global market data
        global_data_result = results[-1]
        global_data = global_data_result if isinstance(global_data_result, GlobalMarketData) else None
        results = results[:-1]  # coin results only

        snapshots: List[CoinSnapshot] = []
        for pair, result in zip(self.WATCHLIST, results):
            if isinstance(result, BaseException):
                logger.warning("Failed to fetch %s: %s", pair, result)
                continue
            if result is not None:
                snapshots.append(result)

        if not snapshots:
            logger.error("No coin data could be fetched.")
            return MarketOverview(
                coin_snapshots=[],
                btc_dominance_trend="unknown",
                market_momentum="neutral",
                sector_rotation_signal="no data",
                top_movers=[],
                correlations={},
                timestamp=time.time(),
            )

        # Post-processing that requires the full set of snapshots
        snapshots = self._compute_relative_strength(snapshots)
        correlations = self._compute_correlations(snapshots)
        rotation = self._detect_rotation(snapshots)
        momentum = self._overall_momentum(snapshots)
        btc_trend = self._btc_dominance_trend(snapshots)
        top_movers = self._top_movers(snapshots)

        return MarketOverview(
            coin_snapshots=snapshots,
            btc_dominance_trend=btc_trend,
            market_momentum=momentum,
            sector_rotation_signal=rotation,
            top_movers=top_movers,
            correlations=correlations,
            timestamp=time.time(),
            global_data=global_data,
        )

    def format_for_ai(self, overview: MarketOverview) -> str:
        """Return a plain-text summary suitable for injection into an AI prompt."""
        lines: List[str] = []
        lines.append("=== MARKET SCANNER OVERVIEW ===")
        lines.append("")
        lines.append(
            "Market momentum : " + overview.market_momentum
        )
        lines.append(
            "BTC dominance   : " + overview.btc_dominance_trend
        )
        lines.append(
            "Sector rotation : " + overview.sector_rotation_signal
        )
        if overview.top_movers:
            lines.append(
                "Top movers      : " + ", ".join(overview.top_movers)
            )
        lines.append("")
        lines.append("--- Per-coin snapshots (full technicals) ---")

        for snap in overview.coin_snapshots:
            rsi_str = f"{snap.rsi:.1f}" if snap.rsi is not None else "n/a"
            rsi_sig = f" ({snap.rsi_signal})" if snap.rsi_signal else ""
            lines.append(
                f"\n  {snap.symbol} — ${snap.price:,.4f}  "
                f"1h={snap.change_1h:+.2f}%  24h={snap.change_24h:+.2f}%  "
                f"mom={snap.momentum_score:+.3f}  relstr={snap.relative_strength:+.3f}"
            )
            lines.append(
                f"    Vol 24h: {snap.volume_24h:,.0f}  Vol change: {snap.volume_change_pct:+.1f}%"
            )
            lines.append(
                f"    RSI: {rsi_str}{rsi_sig}"
            )
            if snap.ema_fast is not None:
                ema_dir = "▲" if snap.ema_crossover == "bullish" else "▼" if snap.ema_crossover == "bearish" else "—"
                lines.append(
                    f"    EMA(9/21): ${snap.ema_fast:,.4f} / ${snap.ema_slow:,.4f}  Crossover: {snap.ema_crossover} {ema_dir}"
                )
            if snap.bb_upper is not None:
                lines.append(
                    f"    Bollinger: ${snap.bb_lower:,.4f} — ${snap.bb_middle:,.4f} — ${snap.bb_upper:,.4f}  "
                    f"Position: {snap.bb_position:.1%}  BW: {snap.bb_bandwidth:.4f}"
                )
            if snap.composite_score is not None:
                lines.append(
                    f"    Composite: {snap.composite_score:+.3f}  Signal: {snap.recommendation}"
                )

        if overview.correlations:
            lines.append("")
            lines.append("--- Correlation signals ---")
            for label, members in overview.correlations.items():
                lines.append(f"  {label}: {', '.join(members)}")

        # Global market data
        if overview.global_data:
            g = overview.global_data
            lines.append("")
            lines.append("--- Global crypto market ---")
            lines.append(f"  BTC dominance: {g.btc_dominance:.1f}%  |  ETH dominance: {g.eth_dominance:.1f}%")
            lines.append(f"  Total market cap: ${g.total_market_cap_usd/1e9:,.1f}B  (24h: {g.market_cap_change_24h_pct:+.2f}%)")
            lines.append(f"  Total 24h volume: ${g.total_volume_24h_usd/1e9:,.1f}B")
            if g.btc_dominance > 55:
                lines.append(f"  ⚠ BTC dominance HIGH ({g.btc_dominance:.1f}%) — alts may underperform")
            elif g.btc_dominance < 45:
                lines.append(f"  ⚠ BTC dominance LOW ({g.btc_dominance:.1f}%) — alt season potential")

        lines.append("")
        lines.append("=== END MARKET SCANNER ===")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def _fetch_coin_data(self, pair: str) -> Optional[CoinSnapshot]:
        """Fetch ticker + OHLCV for a single pair and compute derived metrics."""
        symbol = self.SYMBOL_MAP.get(pair, pair)
        ticker_data, ohlcv_data = await asyncio.gather(
            self._get_ticker(pair),
            self._get_ohlcv(pair),
            return_exceptions=True,
        )

        if isinstance(ticker_data, BaseException):
            logger.warning("Ticker fetch failed for %s: %s", pair, ticker_data)
            return None
        if isinstance(ohlcv_data, BaseException):
            logger.warning("OHLCV fetch failed for %s: %s", pair, ohlcv_data)
            return None
        if ticker_data is None or ohlcv_data is None:
            return None

        # --- Parse ticker ---
        price = float(ticker_data.get("c", [0])[0])
        volume_24h = float(ticker_data.get("v", [0, 0])[1])
        open_24h = float(ticker_data.get("o", 0))

        change_24h = 0.0
        if open_24h > 0:
            change_24h = ((price - open_24h) / open_24h) * 100.0

        # --- Parse OHLCV candles ---
        closes = [float(c[4]) for c in ohlcv_data]

        # 1h change: roughly 4 x 15-min candles
        change_1h = 0.0
        if len(closes) >= 5:
            ref = closes[-5]
            if ref > 0:
                change_1h = ((closes[-1] - ref) / ref) * 100.0

        # Volume change: compare last 4 bars to prior 4 bars
        volumes = [float(c[6]) for c in ohlcv_data]
        volume_change_pct = 0.0
        if len(volumes) >= 8:
            recent_vol = sum(volumes[-4:])
            prior_vol = sum(volumes[-8:-4])
            if prior_vol > 0:
                volume_change_pct = ((recent_vol - prior_vol) / prior_vol) * 100.0

        # RSI (basic)
        rsi = _compute_rsi(closes, period=14)

        # Full technical indicators from generate_signals
        signals: Optional[Signals] = None
        try:
            if len(closes) >= 21:  # need at least ema_slow_period bars
                signals = generate_signals(closes)
        except Exception as e:
            logger.debug(f"Full indicators failed for {pair}: {e}")

        # Momentum score
        momentum_score = self._calc_momentum(
            change_1h, change_24h, volume_change_pct, rsi
        )

        snap = CoinSnapshot(
            symbol=symbol,
            price=price,
            change_1h=round(change_1h, 4),
            change_24h=round(change_24h, 4),
            volume_24h=volume_24h,
            volume_change_pct=round(volume_change_pct, 2),
            rsi=round(rsi, 2) if rsi is not None else None,
            momentum_score=round(momentum_score, 4),
            relative_strength=0.0,  # filled in later
        )

        # Attach full technicals if computed
        if signals:
            snap.ema_fast = round(signals.ema.fast_ema, 4)
            snap.ema_slow = round(signals.ema.slow_ema, 4)
            snap.ema_crossover = signals.ema.crossover
            snap.bb_upper = round(signals.bollinger.upper, 4)
            snap.bb_middle = round(signals.bollinger.middle, 4)
            snap.bb_lower = round(signals.bollinger.lower, 4)
            snap.bb_position = round(signals.bollinger.price_position, 4)
            snap.bb_bandwidth = round(signals.bollinger.bandwidth, 6)
            snap.rsi_signal = signals.rsi.signal
            snap.rsi = round(signals.rsi.rsi, 2)  # use the full RSI calc
            snap.composite_score = round(signals.composite_score, 4)
            snap.recommendation = signals.recommendation

        return snap

    async def _get_ticker(self, pair: str) -> Optional[dict]:
        url = f"{KRAKEN_BASE}/Ticker"
        resp = await self._client.get(url, params={"pair": pair}, timeout=10.0)
        resp.raise_for_status()
        body = resp.json()
        errors = body.get("error", [])
        if errors:
            logger.warning("Kraken ticker errors for %s: %s", pair, errors)
            return None
        result = body.get("result", {})
        # Kraken may return the pair under a slightly different key
        for key in result:
            return result[key]
        return None

    async def _get_ohlcv(self, pair: str) -> Optional[List[list]]:
        url = f"{KRAKEN_BASE}/OHLC"
        resp = await self._client.get(
            url, params={"pair": pair, "interval": 15}, timeout=10.0
        )
        resp.raise_for_status()
        body = resp.json()
        errors = body.get("error", [])
        if errors:
            logger.warning("Kraken OHLC errors for %s: %s", pair, errors)
            return None
        result = body.get("result", {})
        # The result dict has the pair key + a "last" key; grab the list
        for key, value in result.items():
            if isinstance(value, list):
                # Return last 100 bars
                return value[-100:]
        return None

    async def _fetch_global_market_data(self) -> Optional[GlobalMarketData]:
        """Fetch global crypto market data (BTC dominance, total market cap) from CoinGecko."""
        try:
            resp = await self._client.get(
                "https://api.coingecko.com/api/v3/global",
                headers={"User-Agent": "AlgoTrader/2.8"},
                timeout=10.0,
            )
            if resp.status_code != 200:
                return None

            data = resp.json().get("data", {})
            market_cap_pct = data.get("market_cap_percentage", {})

            return GlobalMarketData(
                btc_dominance=round(market_cap_pct.get("btc", 0), 2),
                eth_dominance=round(market_cap_pct.get("eth", 0), 2),
                total_market_cap_usd=data.get("total_market_cap", {}).get("usd", 0),
                total_volume_24h_usd=data.get("total_volume", {}).get("usd", 0),
                market_cap_change_24h_pct=round(data.get("market_cap_change_percentage_24h_usd", 0), 2),
                active_coins=data.get("active_cryptocurrencies", 0),
            )
        except Exception as e:
            logger.warning(f"CoinGecko global data fetch failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Analysis helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_momentum(
        change_1h: float,
        change_24h: float,
        volume_change_pct: float,
        rsi: Optional[float],
    ) -> float:
        """
        Weighted momentum score in [-1, +1].
        Weights: 1h 0.3, 24h 0.3, volume 0.2, RSI signal 0.2.
        """
        # Normalise price changes: cap at +/-10% -> +/-1
        norm_1h = _clamp(change_1h / 10.0)
        norm_24h = _clamp(change_24h / 10.0)

        # Normalise volume change: cap at +/-100% -> +/-1
        norm_vol = _clamp(volume_change_pct / 100.0)

        # RSI signal: oversold (<30) is bullish (+1), overbought (>70) is bearish (-1)
        rsi_signal = 0.0
        if rsi is not None:
            if rsi < 30:
                rsi_signal = _clamp((30 - rsi) / 20.0)
            elif rsi > 70:
                rsi_signal = _clamp(-((rsi - 70) / 20.0))

        score = (
            0.3 * norm_1h
            + 0.3 * norm_24h
            + 0.2 * norm_vol
            + 0.2 * rsi_signal
        )
        return _clamp(score)

    @staticmethod
    def _compute_relative_strength(
        snapshots: List[CoinSnapshot],
    ) -> List[CoinSnapshot]:
        """Set each coin's relative_strength vs BTC's 24h change."""
        btc_change = 0.0
        for s in snapshots:
            if s.symbol == "BTC":
                btc_change = s.change_24h
                break

        for s in snapshots:
            if s.symbol == "BTC":
                s.relative_strength = 0.0
            else:
                diff = s.change_24h - btc_change
                s.relative_strength = round(_clamp(diff / 10.0), 4)
        return snapshots

    @staticmethod
    def _compute_correlations(
        snapshots: List[CoinSnapshot],
    ) -> Dict[str, List[str]]:
        """
        Simple correlation buckets based on 24h change direction
        relative to BTC.
        """
        btc_dir = 0.0
        for s in snapshots:
            if s.symbol == "BTC":
                btc_dir = s.change_24h
                break

        moving_with_btc: List[str] = []
        diverging_from_btc: List[str] = []

        for s in snapshots:
            if s.symbol == "BTC":
                continue
            # Same direction and within 2x magnitude -> correlated
            same_sign = (s.change_24h >= 0) == (btc_dir >= 0)
            if same_sign and abs(s.change_24h) > 0:
                moving_with_btc.append(s.symbol)
            else:
                diverging_from_btc.append(s.symbol)

        result: Dict[str, List[str]] = {}
        if moving_with_btc:
            result["moving_with_BTC"] = moving_with_btc
        if diverging_from_btc:
            result["diverging_from_BTC"] = diverging_from_btc
        return result

    @staticmethod
    def _detect_rotation(snapshots: List[CoinSnapshot]) -> str:
        """Detect whether money is rotating from BTC to alts or vice-versa."""
        btc_snap = None
        alt_changes: List[float] = []
        for s in snapshots:
            if s.symbol == "BTC":
                btc_snap = s
            else:
                alt_changes.append(s.change_24h)

        if btc_snap is None or not alt_changes:
            return "insufficient data"

        avg_alt = sum(alt_changes) / len(alt_changes)
        btc_chg = btc_snap.change_24h

        diff = avg_alt - btc_chg
        if diff > 1.5:
            return "money flowing from BTC to alts"
        elif diff < -1.5:
            return "flight to BTC"
        return "no clear rotation"

    @staticmethod
    def _overall_momentum(snapshots: List[CoinSnapshot]) -> str:
        """Aggregate momentum across all coins."""
        if not snapshots:
            return "neutral"
        avg = sum(s.momentum_score for s in snapshots) / len(snapshots)
        if avg > 0.15:
            return "risk_on"
        elif avg < -0.15:
            return "risk_off"
        return "neutral"

    @staticmethod
    def _btc_dominance_trend(snapshots: List[CoinSnapshot]) -> str:
        """Infer BTC dominance trend from relative strength of alts."""
        alt_rs = [s.relative_strength for s in snapshots if s.symbol != "BTC"]
        if not alt_rs:
            return "unknown"
        avg_rs = sum(alt_rs) / len(alt_rs)
        if avg_rs > 0.05:
            return "declining (alts outperforming)"
        elif avg_rs < -0.05:
            return "increasing (BTC outperforming)"
        return "stable"

    @staticmethod
    def _top_movers(snapshots: List[CoinSnapshot], count: int = 3) -> List[str]:
        """Return top movers by absolute 24h change."""
        ranked = sorted(snapshots, key=lambda s: abs(s.change_24h), reverse=True)
        movers: List[str] = []
        for s in ranked[:count]:
            direction = "up" if s.change_24h >= 0 else "down"
            movers.append(f"{s.symbol} ({direction} {abs(s.change_24h):.2f}%)")
        return movers
