"""
Social Sentiment: Google News RSS + CoinGecko trending/community.

v4.0.2: Replaced direct Reddit scraping (403 from server IPs) with
Google News RSS feeds for crypto headlines. No API keys, no auth,
no cost — just public RSS feeds + keyword sentiment analysis.

Fetches from:
- Google News RSS: crypto headlines from major outlets (BTC, ETH, crypto)
- CoinGecko: trending coins, community data for BTC/ETH

Gives Claude visibility into retail sentiment and social momentum —
crowd euphoria/fear, trending narratives, and FOMO signals that often
precede short-term price moves (frequently as contrarian indicators).
"""

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html import unescape
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Cache TTL: social data is noisy, no need to refetch constantly
CACHE_TTL = 600.0  # 10 minutes

# Google News RSS — free, no auth, no rate limit issues
GNEWS_RSS_BASE = "https://news.google.com/rss/search"
GNEWS_QUERIES = [
    "bitcoin OR BTC cryptocurrency",
    "ethereum OR ETH crypto",
    "solana OR XRP OR cardano crypto",
]

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COMMUNITY_COINS = ["bitcoin", "ethereum"]

# ------------------------------------------------------------------
# Keyword sentiment scoring
# ------------------------------------------------------------------

BULLISH_WORDS = [
    "moon", "pump", "bull", "breakout", "ath", "accumulate", "buy", "long",
    "rocket", "milestone", "adopt", "institutional", "etf", "halving",
    "recovery", "surge", "rally", "bullish", "soar", "green", "gain",
    "uptrend", "support", "bounce", "approval", "inflow", "record",
    "upgrade", "partnership", "launch", "growth",
]

BEARISH_WORDS = [
    "crash", "dump", "bear", "sell", "short", "scam", "rug", "fear",
    "collapse", "ban", "hack", "liquidat", "bubble", "ponzi", "dead",
    "bearish", "plunge", "tank", "red", "drop", "capitulat", "fraud",
    "sec", "lawsuit", "warning", "outflow", "decline", "slump", "tumble",
    "crackdown", "investigate", "risk", "concern",
]

_BULLISH_PATTERNS = [re.compile(rf"\b{w}", re.IGNORECASE) for w in BULLISH_WORDS]
_BEARISH_PATTERNS = [re.compile(rf"\b{w}", re.IGNORECASE) for w in BEARISH_WORDS]

COIN_KEYWORDS = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
    "SOL": ["sol", "solana"],
    "XRP": ["xrp", "ripple"],
    "ADA": ["ada", "cardano"],
    "DOGE": ["doge", "dogecoin"],
    "AVAX": ["avax", "avalanche"],
    "DOT": ["dot", "polkadot"],
    "LINK": ["link", "chainlink"],
    "MATIC": ["matic", "polygon"],
}


# ------------------------------------------------------------------
# Dataclasses
# ------------------------------------------------------------------

@dataclass
class NewsPost:
    """A single news headline from Google News RSS."""
    title: str = ""
    source: str = ""
    published: str = ""
    link: str = ""


@dataclass
class NewsSentiment:
    """Aggregated news sentiment analysis."""
    score: float = 0.0              # -1.0 to +1.0
    label: str = ""                 # "bullish", "bearish", "neutral"
    post_count: int = 0
    total_sources: int = 0
    top_titles: list = field(default_factory=list)
    coin_mentions: dict = field(default_factory=dict)
    bullish_count: int = 0
    bearish_count: int = 0
    sources: dict = field(default_factory=dict)
    avg_upvote_ratio: float = 0.0   # not used, kept for compat


@dataclass
class TrendingCoin:
    """A trending coin from CoinGecko."""
    name: str = ""
    symbol: str = ""
    market_cap_rank: Optional[int] = None
    score_rank: int = 0


@dataclass
class CommunityData:
    """Community metrics for a single coin."""
    coin_id: str = ""
    reddit_subscribers: int = 0
    reddit_active_accounts: int = 0
    twitter_followers: int = 0


@dataclass
class SocialSnapshot:
    """Full social sentiment snapshot."""
    timestamp: float = 0.0
    reddit: Optional[NewsSentiment] = None  # kept as 'reddit' for backward compat
    trending_coins: list = field(default_factory=list)
    community: dict = field(default_factory=dict)
    fetch_errors: list = field(default_factory=list)


def score_title_sentiment(title: str) -> tuple[float, int, int]:
    """Score a single title for crypto sentiment.

    Returns (score, bullish_hits, bearish_hits).
    Score is in range [-1.0, +1.0].
    """
    bullish_hits = sum(1 for p in _BULLISH_PATTERNS if p.search(title))
    bearish_hits = sum(1 for p in _BEARISH_PATTERNS if p.search(title))

    total = bullish_hits + bearish_hits
    if total == 0:
        return 0.0, 0, 0

    raw = (bullish_hits - bearish_hits) / total
    return round(raw, 3), bullish_hits, bearish_hits


def count_coin_mentions(title: str) -> dict[str, int]:
    """Count mentions of known coins in a title."""
    mentions = {}
    title_lower = title.lower()
    for ticker, keywords in COIN_KEYWORDS.items():
        for kw in keywords:
            if kw in title_lower:
                mentions[ticker] = mentions.get(ticker, 0) + 1
                break
    return mentions


class SocialSentimentFetcher:
    """Fetches social sentiment from Google News RSS + CoinGecko."""

    def __init__(self, http_client: httpx.AsyncClient):
        self._http = http_client
        self._cache: dict[str, tuple[float, object]] = {}
        self._cache_ttl = CACHE_TTL
        self._last_snapshot: Optional[SocialSnapshot] = None
        self._last_fetch_time: float = 0.0

    def _get_cached(self, key: str) -> Optional[object]:
        if key in self._cache:
            ts, val = self._cache[key]
            if (time.time() - ts) < self._cache_ttl:
                return val
        return None

    def _set_cached(self, key: str, value: object) -> None:
        self._cache[key] = (time.time(), value)

    async def fetch_all(self) -> SocialSnapshot:
        """Fetch all social sentiment data concurrently."""
        if self._last_snapshot and (time.time() - self._last_fetch_time) < self._cache_ttl:
            return self._last_snapshot

        snapshot = SocialSnapshot(timestamp=time.time())

        tasks = []
        task_names = []

        # Google News RSS feeds for crypto headlines
        for i, query in enumerate(GNEWS_QUERIES):
            tasks.append(self._fetch_google_news(query))
            task_names.append(f"gnews_{i}")

        # CoinGecko: trending + community
        tasks.append(self._fetch_trending_coins())
        task_names.append("coingecko_trending")
        tasks.append(self._fetch_community_data("bitcoin"))
        task_names.append("community_bitcoin")
        tasks.append(self._fetch_community_data("ethereum"))
        task_names.append("community_ethereum")

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, r in enumerate(results):
            if isinstance(r, Exception):
                snapshot.fetch_errors.append(f"{task_names[i]}: {r}")
                logger.debug(f"Social fetch error ({task_names[i]}): {r}")

        # --- Assemble news sentiment ---
        all_posts: list[NewsPost] = []
        for i, name in enumerate(task_names):
            if name.startswith("gnews") and not isinstance(results[i], Exception) and results[i]:
                all_posts.extend(results[i])

        # Deduplicate by title
        seen = set()
        unique = []
        for p in all_posts:
            if p.title not in seen:
                seen.add(p.title)
                unique.append(p)

        if unique:
            snapshot.reddit = self._analyze_posts(unique)  # 'reddit' for backward compat

        # --- Trending coins ---
        t_idx = task_names.index("coingecko_trending")
        if not isinstance(results[t_idx], Exception) and results[t_idx]:
            snapshot.trending_coins = results[t_idx]

        # --- Community data ---
        for coin_id in COMMUNITY_COINS:
            key = f"community_{coin_id}"
            idx = task_names.index(key)
            if not isinstance(results[idx], Exception) and results[idx]:
                snapshot.community[coin_id] = results[idx]

        self._last_snapshot = snapshot
        self._last_fetch_time = time.time()
        return snapshot

    # ------------------------------------------------------------------
    # Google News RSS
    # ------------------------------------------------------------------

    async def _fetch_google_news(self, query: str) -> list[NewsPost]:
        """Fetch crypto news headlines from Google News RSS."""
        cache_key = f"gnews_{query[:30]}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = await self._http.get(
                GNEWS_RSS_BASE,
                params={
                    "q": query,
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AlgoTrader/4.0)",
                    "Accept": "application/rss+xml, application/xml, text/xml",
                },
                timeout=15.0,
            )
            resp.raise_for_status()

            posts = self._parse_rss(resp.text)
            self._set_cached(cache_key, posts)
            logger.debug(f"Google News: {len(posts)} headlines for '{query[:30]}'")
            return posts

        except Exception as e:
            logger.debug(f"Google News fetch failed for '{query[:30]}': {e}")
            raise

    @staticmethod
    def _parse_rss(xml_text: str) -> list[NewsPost]:
        """Parse Google News RSS XML into NewsPost objects."""
        posts = []
        try:
            root = ET.fromstring(xml_text)
            channel = root.find("channel")
            if channel is None:
                return posts

            for item in channel.findall("item"):
                title_raw = item.findtext("title", "")
                # Google News format: "Headline - Source Name"
                source = ""
                title = title_raw
                if " - " in title_raw:
                    parts = title_raw.rsplit(" - ", 1)
                    title = parts[0].strip()
                    source = parts[1].strip()

                posts.append(NewsPost(
                    title=unescape(title),
                    source=source,
                    published=item.findtext("pubDate", ""),
                    link=item.findtext("link", ""),
                ))
        except ET.ParseError as e:
            logger.debug(f"RSS parse error: {e}")

        return posts[:25]  # cap at 25 per query

    # ------------------------------------------------------------------
    # CoinGecko fetches
    # ------------------------------------------------------------------

    async def _fetch_trending_coins(self) -> list[TrendingCoin]:
        """Fetch trending coins from CoinGecko search/trending endpoint."""
        cached = self._get_cached("trending_coins")
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                f"{COINGECKO_BASE}/search/trending",
                timeout=15.0,
            )
            if resp.status_code in (429, 403):
                raise RuntimeError(f"CoinGecko returned {resp.status_code} for trending")
            resp.raise_for_status()
            data = resp.json()

            coins = []
            for i, item in enumerate(data.get("coins", [])):
                coin = item.get("item", {})
                coins.append(TrendingCoin(
                    name=coin.get("name", ""),
                    symbol=coin.get("symbol", ""),
                    market_cap_rank=coin.get("market_cap_rank"),
                    score_rank=i,
                ))

            self._set_cached("trending_coins", coins)
            return coins
        except Exception as e:
            logger.debug(f"CoinGecko trending fetch failed: {e}")
            raise

    async def _fetch_community_data(self, coin_id: str) -> CommunityData:
        """Fetch community metrics for a coin from CoinGecko."""
        cache_key = f"community_{coin_id}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                f"{COINGECKO_BASE}/coins/{coin_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "market_data": "false",
                    "community_data": "true",
                    "developer_data": "false",
                },
                timeout=15.0,
            )
            if resp.status_code in (429, 403):
                raise RuntimeError(f"CoinGecko returned {resp.status_code} for {coin_id}")
            resp.raise_for_status()
            data = resp.json()

            community = data.get("community_data", {})
            cd = CommunityData(
                coin_id=coin_id,
                reddit_subscribers=community.get("reddit_subscribers", 0) or 0,
                reddit_active_accounts=community.get("reddit_accounts_active_48h", 0) or 0,
                twitter_followers=community.get("twitter_followers", 0) or 0,
            )
            self._set_cached(cache_key, cd)
            return cd
        except Exception as e:
            logger.debug(f"CoinGecko community data fetch failed for {coin_id}: {e}")
            raise

    # ------------------------------------------------------------------
    # Sentiment analysis
    # ------------------------------------------------------------------

    def _analyze_posts(self, posts: list[NewsPost]) -> NewsSentiment:
        """Analyze news headlines for overall crypto sentiment."""
        sentiment = NewsSentiment()
        sentiment.post_count = len(posts)

        total_bullish = 0
        total_bearish = 0
        weighted_score_sum = 0.0
        weight_sum = 0.0
        coin_mentions: dict[str, int] = {}
        sources: dict[str, int] = {}

        for post in posts:
            # Track sources
            if post.source:
                sources[post.source] = sources.get(post.source, 0) + 1

            # Score the title
            title_score, bull_hits, bear_hits = score_title_sentiment(post.title)
            if bull_hits > 0:
                total_bullish += 1
            if bear_hits > 0:
                total_bearish += 1

            # Equal weight for news (no upvote data)
            weight = 1.0
            weighted_score_sum += title_score * weight
            weight_sum += weight

            # Track coin mentions
            for ticker, count in count_coin_mentions(post.title).items():
                coin_mentions[ticker] = coin_mentions.get(ticker, 0) + count

        sentiment.bullish_count = total_bullish
        sentiment.bearish_count = total_bearish
        sentiment.total_sources = len(sources)
        sentiment.sources = dict(sorted(sources.items(), key=lambda x: x[1], reverse=True))
        sentiment.coin_mentions = dict(
            sorted(coin_mentions.items(), key=lambda x: x[1], reverse=True)
        )

        if weight_sum > 0:
            sentiment.score = round(max(-1.0, min(1.0, weighted_score_sum / weight_sum)), 3)

        if sentiment.score > 0.2:
            sentiment.label = "bullish"
        elif sentiment.score < -0.2:
            sentiment.label = "bearish"
        else:
            sentiment.label = "neutral"

        # Top headlines
        sentiment.top_titles = [
            f"{p.title} ({p.source})" if p.source else p.title
            for p in posts[:10]
        ]

        return sentiment

    # ------------------------------------------------------------------
    # Formatting for AI context
    # ------------------------------------------------------------------

    def format_for_context(self, data: SocialSnapshot) -> str:
        """Format social sentiment data for injection into Claude's context window."""
        if not data:
            return ""

        parts = ["\n## SOCIAL SENTIMENT (News Headlines + CoinGecko Trending)"]

        # --- News Sentiment ---
        if data.reddit:  # backward compat field name
            r = data.reddit
            parts.append(
                f"\n### Crypto News Sentiment: "
                f"{r.label.upper()} ({r.score:+.3f})"
            )
            parts.append(
                f"  Headlines analyzed: {r.post_count} | "
                f"Bullish: {r.bullish_count} | "
                f"Bearish: {r.bearish_count} | "
                f"Sources: {r.total_sources}"
            )

            if r.coin_mentions:
                mentions_str = ", ".join(
                    f"{t}: {c}" for t, c in list(r.coin_mentions.items())[:8]
                )
                parts.append(f"  Coin mentions: {mentions_str}")

            if r.sources:
                src_str = ", ".join(
                    f"{s} ({c})" for s, c in list(r.sources.items())[:6]
                )
                parts.append(f"  Top sources: {src_str}")

            if r.top_titles:
                parts.append("  Recent headlines:")
                for i, title in enumerate(r.top_titles[:8], 1):
                    display = title[:120] + "..." if len(title) > 120 else title
                    parts.append(f"    {i}. {display}")

            # Interpretation hints
            if r.score > 0.6:
                parts.append(
                    "  -> CONTRARIAN SIGNAL: News extremely bullish "
                    "— be cautious of FOMO, historically a top signal"
                )
            elif r.score > 0.2:
                parts.append(
                    "  -> News leaning bullish — positive narrative, "
                    "but not yet at contrarian extremes"
                )
            elif r.score < -0.6:
                parts.append(
                    "  -> CONTRARIAN SIGNAL: News extremely bearish "
                    "— extreme fear often marks local bottoms"
                )
            elif r.score < -0.2:
                parts.append(
                    "  -> News leaning bearish — negative narrative, "
                    "but not yet at contrarian extremes"
                )
            else:
                parts.append(
                    "  -> News sentiment neutral — no strong narrative bias"
                )

        # --- CoinGecko Trending ---
        if data.trending_coins:
            parts.append("\n### CoinGecko Trending Searches")
            for coin in data.trending_coins[:7]:
                rank_str = (
                    f"(mcap rank #{coin.market_cap_rank})"
                    if coin.market_cap_rank
                    else "(unranked)"
                )
                parts.append(
                    f"  #{coin.score_rank + 1}: "
                    f"{coin.name} ({coin.symbol}) {rank_str}"
                )

            low_cap_trending = [
                c for c in data.trending_coins
                if c.market_cap_rank and c.market_cap_rank > 100
            ]
            if len(low_cap_trending) >= 3:
                parts.append(
                    "  -> SIGNAL: Multiple low-cap coins trending "
                    "— retail FOMO elevated, late-cycle behavior"
                )

            major_trending = [
                c for c in data.trending_coins
                if c.symbol.upper() in ("BTC", "ETH")
            ]
            if major_trending:
                names = ", ".join(c.name for c in major_trending)
                parts.append(
                    f"  -> {names} trending in searches "
                    "— mainstream attention rising"
                )

        # --- Community Data ---
        if data.community:
            parts.append("\n### Community Metrics")
            for coin_id, cd in data.community.items():
                display_name = coin_id.upper()[:3]
                parts.append(f"  {display_name}:")
                if cd.reddit_subscribers > 0:
                    parts.append(
                        f"    Reddit: {cd.reddit_subscribers:,} subscribers, "
                        f"{cd.reddit_active_accounts:,} active (48h)"
                    )
                if cd.twitter_followers > 0:
                    parts.append(
                        f"    X/Twitter: {cd.twitter_followers:,} followers"
                    )
                if cd.reddit_subscribers > 0 and cd.reddit_active_accounts > 0:
                    activity_pct = (cd.reddit_active_accounts / cd.reddit_subscribers) * 100
                    if activity_pct > 5:
                        parts.append(
                            f"    -> High activity ratio ({activity_pct:.1f}%) "
                            "— community highly engaged"
                        )

        # --- Errors ---
        if data.fetch_errors:
            parts.append(f"\n[Social data gaps: {', '.join(data.fetch_errors)}]")

        return "\n".join(parts)
