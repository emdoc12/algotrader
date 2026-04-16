"""
Market sentiment data fetcher.

Gathers external signals for the AI to reason about:
- Crypto Fear & Greed Index (alternative.me — free, no API key)
- Recent BTC news headlines (CryptoPanic — free tier, no API key needed for public feed)
- On-chain summary from Kraken OHLCV data (volume trends, price momentum)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SentimentData:
    """Combined sentiment snapshot."""
    # Fear & Greed
    fear_greed_value: int = 50          # 0 = extreme fear, 100 = extreme greed
    fear_greed_label: str = "Neutral"   # "Extreme Fear", "Fear", "Neutral", "Greed", "Extreme Greed"
    fear_greed_yesterday: int = 50
    fear_greed_week_ago: int = 50

    # News
    news_headlines: list[str] = field(default_factory=list)  # Latest 5-10 headlines
    news_sentiment_summary: str = ""     # "mostly_bullish", "mixed", "mostly_bearish"

    # Volume analysis (derived from OHLCV)
    volume_trend: str = "stable"         # "increasing", "decreasing", "stable"
    volume_24h_change_pct: float = 0.0
    price_momentum_1h: float = 0.0       # percent change last hour
    price_momentum_24h: float = 0.0      # percent change last 24h

    # Metadata
    timestamp: float = 0.0
    errors: list[str] = field(default_factory=list)


class SentimentFetcher:
    """Fetches market sentiment from multiple free sources."""

    def __init__(self):
        self._http = httpx.AsyncClient(timeout=15.0)

    async def fetch_all(self, ohlcv_bars: list = None) -> SentimentData:
        """
        Fetch all sentiment data concurrently.

        Args:
            ohlcv_bars: Recent OHLCV bars for volume/momentum analysis
        """
        data = SentimentData(timestamp=time.time())

        # Fetch Fear & Greed and News concurrently
        results = await asyncio.gather(
            self._fetch_fear_greed(),
            self._fetch_news(),
            return_exceptions=True,
        )

        # Fear & Greed
        if isinstance(results[0], Exception):
            data.errors.append(f"Fear & Greed: {results[0]}")
            logger.warning(f"Failed to fetch Fear & Greed: {results[0]}")
        elif results[0]:
            fg = results[0]
            data.fear_greed_value = fg["value"]
            data.fear_greed_label = fg["label"]
            data.fear_greed_yesterday = fg.get("yesterday", 50)
            data.fear_greed_week_ago = fg.get("week_ago", 50)

        # News
        if isinstance(results[1], Exception):
            data.errors.append(f"News: {results[1]}")
            logger.warning(f"Failed to fetch news: {results[1]}")
        elif results[1]:
            data.news_headlines = results[1]["headlines"]
            data.news_sentiment_summary = results[1]["sentiment"]

        # Volume & momentum from OHLCV
        if ohlcv_bars and len(ohlcv_bars) >= 10:
            data.volume_trend, data.volume_24h_change_pct = self._analyze_volume(ohlcv_bars)
            data.price_momentum_1h = self._calc_momentum(ohlcv_bars, periods=4)   # 4 x 15min = 1h
            data.price_momentum_24h = self._calc_momentum(ohlcv_bars, periods=96)  # 96 x 15min = 24h

        return data

    async def _fetch_fear_greed(self) -> Optional[dict]:
        """Fetch Crypto Fear & Greed Index from alternative.me."""
        url = "https://api.alternative.me/fng/?limit=7&format=json"
        resp = await self._http.get(url)
        resp.raise_for_status()
        result = resp.json()

        entries = result.get("data", [])
        if not entries:
            return None

        current = entries[0]
        yesterday = entries[1] if len(entries) > 1 else current
        week_ago = entries[6] if len(entries) > 6 else current

        return {
            "value": int(current["value"]),
            "label": current["value_classification"],
            "yesterday": int(yesterday["value"]),
            "week_ago": int(week_ago["value"]),
        }

    async def _fetch_news(self) -> Optional[dict]:
        """Fetch recent BTC news from CryptoPanic public feed."""
        url = "https://cryptopanic.com/api/free/v1/posts/?currencies=BTC&kind=news&public=true"
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            result = resp.json()
        except Exception:
            # CryptoPanic might block without auth — fall back to empty
            return {"headlines": [], "sentiment": "unknown"}

        posts = result.get("results", [])[:10]
        headlines = [p.get("title", "") for p in posts if p.get("title")]

        # Simple sentiment heuristic from headline keywords
        bullish_words = {"surge", "rally", "soar", "bull", "pump", "high", "record", "gain",
                        "buy", "adoption", "breakout", "moon", "up", "green", "bullish", "etf"}
        bearish_words = {"crash", "drop", "plunge", "bear", "dump", "low", "sell", "fear",
                        "ban", "hack", "scam", "fraud", "down", "red", "bearish", "regulation"}

        bull_count = 0
        bear_count = 0
        for h in headlines:
            words = set(h.lower().split())
            bull_count += len(words & bullish_words)
            bear_count += len(words & bearish_words)

        if bull_count > bear_count + 2:
            sentiment = "mostly_bullish"
        elif bear_count > bull_count + 2:
            sentiment = "mostly_bearish"
        else:
            sentiment = "mixed"

        return {"headlines": headlines[:7], "sentiment": sentiment}

    def _analyze_volume(self, bars: list) -> tuple[str, float]:
        """Analyze volume trend from OHLCV bars."""
        if len(bars) < 10:
            return "stable", 0.0

        recent_vol = sum(b.volume for b in bars[-5:]) / 5
        earlier_vol = sum(b.volume for b in bars[-10:-5]) / 5

        if earlier_vol == 0:
            return "stable", 0.0

        change_pct = (recent_vol - earlier_vol) / earlier_vol * 100

        if change_pct > 20:
            trend = "increasing"
        elif change_pct < -20:
            trend = "decreasing"
        else:
            trend = "stable"

        return trend, round(change_pct, 1)

    def _calc_momentum(self, bars: list, periods: int) -> float:
        """Calculate price momentum as percent change over N periods."""
        if len(bars) < periods + 1:
            # Use whatever we have
            if len(bars) < 2:
                return 0.0
            periods = len(bars) - 1

        old_price = bars[-(periods + 1)].close
        new_price = bars[-1].close

        if old_price == 0:
            return 0.0
        return round((new_price - old_price) / old_price * 100, 2)

    async def close(self):
        await self._http.aclose()
