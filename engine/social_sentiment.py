"""
Social Sentiment: Reddit crypto sentiment, CoinGecko trending, community data.

Fetches from free public APIs (no auth/keys needed):
- Reddit public JSON API: hot posts from r/cryptocurrency and r/bitcoin
- CoinGecko: trending coins, community data for BTC/ETH

Gives Claude visibility into retail sentiment and social momentum —
crowd euphoria/fear, trending narratives, and FOMO signals that often
precede short-term price moves (frequently as contrarian indicators).
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Cache TTL: social data is noisy, no need to refetch constantly
CACHE_TTL = 600.0  # 10 minutes

REDDIT_BASE = "https://www.reddit.com/r"
REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

SUBREDDITS = ["cryptocurrency", "bitcoin"]

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Coins to fetch community data for (keep minimal to avoid rate limits)
COMMUNITY_COINS = ["bitcoin", "ethereum"]

# ------------------------------------------------------------------
# Keyword sentiment scoring
# ------------------------------------------------------------------

BULLISH_WORDS = [
    "moon", "pump", "bull", "breakout", "ath", "accumulate", "buy", "long",
    "rocket", "milestone", "adopt", "institutional", "etf", "halving",
    "recovery", "surge", "rally", "bullish", "soar", "green", "gain",
    "uptrend", "support", "bounce",
]

BEARISH_WORDS = [
    "crash", "dump", "bear", "sell", "short", "scam", "rug", "fear",
    "collapse", "ban", "hack", "liquidat", "bubble", "ponzi", "dead",
    "bearish", "plunge", "tank", "red", "drop", "capitulat", "fraud",
    "sec", "lawsuit", "warning",
]

# Pre-compile patterns for performance (word boundary matching)
_BULLISH_PATTERNS = [re.compile(rf"\b{w}", re.IGNORECASE) for w in BULLISH_WORDS]
_BEARISH_PATTERNS = [re.compile(rf"\b{w}", re.IGNORECASE) for w in BEARISH_WORDS]

# Common coin tickers/names to track mentions
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
class RedditPost:
    """A single Reddit post summary."""
    title: str = ""
    score: int = 0
    num_comments: int = 0
    upvote_ratio: float = 0.0
    subreddit: str = ""


@dataclass
class RedditSentiment:
    """Aggregated Reddit sentiment analysis."""
    score: float = 0.0              # -1.0 to +1.0
    label: str = ""                 # "bullish", "bearish", "neutral"
    post_count: int = 0             # total posts analyzed
    total_score: int = 0            # sum of post scores (engagement proxy)
    total_comments: int = 0         # sum of comment counts
    avg_upvote_ratio: float = 0.0
    top_titles: list = field(default_factory=list)   # top 5 by score
    coin_mentions: dict = field(default_factory=dict)  # {ticker: count}
    bullish_count: int = 0          # posts with bullish keywords
    bearish_count: int = 0          # posts with bearish keywords


@dataclass
class TrendingCoin:
    """A trending coin from CoinGecko."""
    name: str = ""
    symbol: str = ""
    market_cap_rank: Optional[int] = None
    score_rank: int = 0             # position in trending list (0 = most trending)


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
    reddit: Optional[RedditSentiment] = None
    trending_coins: list = field(default_factory=list)   # list[TrendingCoin]
    community: dict = field(default_factory=dict)         # {coin_id: CommunityData}
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

    # Net sentiment normalized to [-1, 1]
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
                break  # one match per ticker per title
    return mentions


class SocialSentimentFetcher:
    """Fetches social sentiment data from free public APIs."""

    def __init__(self, http_client: httpx.AsyncClient):
        self._http = http_client
        self._cache: dict[str, tuple[float, object]] = {}
        self._cache_ttl = CACHE_TTL
        self._last_snapshot: Optional[SocialSnapshot] = None
        self._last_fetch_time: float = 0.0

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

    async def fetch_all(self) -> SocialSnapshot:
        """Fetch all social sentiment data concurrently. Graceful on partial failure."""
        # Return cached snapshot if still fresh
        if self._last_snapshot and (time.time() - self._last_fetch_time) < self._cache_ttl:
            return self._last_snapshot

        snapshot = SocialSnapshot(timestamp=time.time())

        # Build task list: Reddit subs + CoinGecko trending + community data
        tasks = [
            self._fetch_reddit_hot("cryptocurrency"),
            self._fetch_reddit_hot("bitcoin"),
            self._fetch_trending_coins(),
            self._fetch_community_data("bitcoin"),
            self._fetch_community_data("ethereum"),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        task_names = [
            "reddit_cryptocurrency", "reddit_bitcoin",
            "coingecko_trending",
            "community_bitcoin", "community_ethereum",
        ]

        # Log errors
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                snapshot.fetch_errors.append(f"{task_names[i]}: {r}")
                logger.debug(f"Social fetch error ({task_names[i]}): {r}")

        # --- Assemble Reddit sentiment ---
        all_posts: list[RedditPost] = []
        for i in range(2):  # reddit results
            if not isinstance(results[i], Exception) and results[i]:
                all_posts.extend(results[i])

        if all_posts:
            snapshot.reddit = self._analyze_reddit_posts(all_posts)

        # --- Trending coins ---
        if not isinstance(results[2], Exception) and results[2]:
            snapshot.trending_coins = results[2]

        # --- Community data ---
        for i, coin_id in enumerate(COMMUNITY_COINS):
            idx = 3 + i
            if not isinstance(results[idx], Exception) and results[idx]:
                snapshot.community[coin_id] = results[idx]

        self._last_snapshot = snapshot
        self._last_fetch_time = time.time()
        return snapshot

    # ------------------------------------------------------------------
    # Reddit fetches
    # ------------------------------------------------------------------

    async def _fetch_reddit_hot(self, subreddit: str) -> list[RedditPost]:
        """Fetch hot posts from a subreddit via Reddit's public JSON API."""
        cache_key = f"reddit_{subreddit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                f"{REDDIT_BASE}/{subreddit}/hot.json",
                params={"limit": 25},
                headers=REDDIT_HEADERS,
                timeout=15.0,
            )
            if resp.status_code in (403, 429):
                logger.debug(
                    f"Reddit blocked for r/{subreddit} (HTTP {resp.status_code})"
                )
                raise RuntimeError(
                    f"Reddit returned {resp.status_code} for r/{subreddit}"
                )
            resp.raise_for_status()
            data = resp.json()

            posts = []
            for child in data.get("data", {}).get("children", []):
                post_data = child.get("data", {})
                # Skip stickied/pinned posts (mod announcements)
                if post_data.get("stickied", False):
                    continue
                posts.append(RedditPost(
                    title=post_data.get("title", ""),
                    score=post_data.get("score", 0),
                    num_comments=post_data.get("num_comments", 0),
                    upvote_ratio=post_data.get("upvote_ratio", 0.0),
                    subreddit=subreddit,
                ))

            self._set_cached(cache_key, posts)
            return posts
        except Exception as e:
            logger.debug(f"Reddit fetch failed for r/{subreddit}: {e}")
            raise

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
                logger.debug(
                    f"CoinGecko trending blocked (HTTP {resp.status_code})"
                )
                raise RuntimeError(
                    f"CoinGecko returned {resp.status_code} for trending"
                )
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
                logger.debug(
                    f"CoinGecko community blocked for {coin_id} "
                    f"(HTTP {resp.status_code})"
                )
                raise RuntimeError(
                    f"CoinGecko returned {resp.status_code} for {coin_id}"
                )
            resp.raise_for_status()
            data = resp.json()

            community = data.get("community_data", {})
            cd = CommunityData(
                coin_id=coin_id,
                reddit_subscribers=community.get("reddit_subscribers", 0) or 0,
                reddit_active_accounts=community.get(
                    "reddit_accounts_active_48h", 0
                ) or 0,
                twitter_followers=community.get("twitter_followers", 0) or 0,
            )
            self._set_cached(cache_key, cd)
            return cd
        except Exception as e:
            logger.debug(f"CoinGecko community data fetch failed for {coin_id}: {e}")
            raise

    # ------------------------------------------------------------------
    # Reddit sentiment analysis
    # ------------------------------------------------------------------

    def _analyze_reddit_posts(self, posts: list[RedditPost]) -> RedditSentiment:
        """Analyze a batch of Reddit posts for overall crypto sentiment."""
        sentiment = RedditSentiment()
        sentiment.post_count = len(posts)

        total_bullish = 0
        total_bearish = 0
        weighted_score_sum = 0.0
        weight_sum = 0.0
        coin_mentions: dict[str, int] = {}

        for post in posts:
            sentiment.total_score += post.score
            sentiment.total_comments += post.num_comments

            # Score the title
            title_score, bull_hits, bear_hits = score_title_sentiment(post.title)
            if bull_hits > 0:
                total_bullish += 1
            if bear_hits > 0:
                total_bearish += 1

            # Weight by engagement (log scale to avoid dominance by viral posts)
            weight = max(1.0, (post.score + post.num_comments) ** 0.5)
            weighted_score_sum += title_score * weight
            weight_sum += weight

            # Track coin mentions
            for ticker, count in count_coin_mentions(post.title).items():
                coin_mentions[ticker] = coin_mentions.get(ticker, 0) + count

        sentiment.bullish_count = total_bullish
        sentiment.bearish_count = total_bearish
        sentiment.coin_mentions = dict(
            sorted(coin_mentions.items(), key=lambda x: x[1], reverse=True)
        )

        # Compute weighted average sentiment
        if weight_sum > 0:
            sentiment.score = round(
                max(-1.0, min(1.0, weighted_score_sum / weight_sum)), 3
            )

        # Label
        if sentiment.score > 0.2:
            sentiment.label = "bullish"
        elif sentiment.score < -0.2:
            sentiment.label = "bearish"
        else:
            sentiment.label = "neutral"

        # Average upvote ratio
        ratios = [p.upvote_ratio for p in posts if p.upvote_ratio > 0]
        if ratios:
            sentiment.avg_upvote_ratio = round(sum(ratios) / len(ratios), 3)

        # Top 5 posts by score
        sorted_posts = sorted(posts, key=lambda p: p.score, reverse=True)
        sentiment.top_titles = [p.title for p in sorted_posts[:5]]

        return sentiment

    # ------------------------------------------------------------------
    # Formatting for AI context
    # ------------------------------------------------------------------

    def format_for_context(self, data: SocialSnapshot) -> str:
        """Format social sentiment data for injection into Claude's context window.

        Focuses on actionable signals and contrarian indicators.
        """
        if not data:
            return ""

        parts = ["\n## SOCIAL SENTIMENT (Reddit + CoinGecko Trending)"]

        # --- Reddit Sentiment ---
        if data.reddit:
            r = data.reddit
            parts.append(
                f"\n### Reddit Crypto Sentiment: "
                f"{r.label.upper()} ({r.score:+.3f})"
            )
            parts.append(
                f"  Posts analyzed: {r.post_count} | "
                f"Bullish posts: {r.bullish_count} | "
                f"Bearish posts: {r.bearish_count}"
            )
            parts.append(
                f"  Total engagement: {r.total_score:,} upvotes, "
                f"{r.total_comments:,} comments | "
                f"Avg upvote ratio: {r.avg_upvote_ratio:.1%}"
            )

            # Top mentioned coins
            if r.coin_mentions:
                mentions_str = ", ".join(
                    f"{t}: {c}" for t, c in list(r.coin_mentions.items())[:8]
                )
                parts.append(f"  Coin mentions: {mentions_str}")

            # Top titles
            if r.top_titles:
                parts.append("  Top posts:")
                for i, title in enumerate(r.top_titles[:5], 1):
                    # Truncate long titles
                    display = title[:100] + "..." if len(title) > 100 else title
                    parts.append(f"    {i}. {display}")

            # Interpretation hints
            if r.score > 0.6:
                parts.append(
                    "  -> CONTRARIAN SIGNAL: Reddit extremely bullish "
                    "-- be cautious of FOMO, historically a top signal"
                )
            elif r.score > 0.2:
                parts.append(
                    "  -> Reddit leaning bullish -- positive narrative, "
                    "but not yet at contrarian extremes"
                )
            elif r.score < -0.6:
                parts.append(
                    "  -> CONTRARIAN SIGNAL: Reddit extremely bearish "
                    "-- extreme fear often marks local bottoms"
                )
            elif r.score < -0.2:
                parts.append(
                    "  -> Reddit leaning bearish -- negative narrative, "
                    "but not yet at contrarian extremes"
                )
            else:
                parts.append(
                    "  -> Reddit sentiment neutral -- no strong crowd bias"
                )

            # High engagement signal
            if r.post_count > 0:
                avg_comments = r.total_comments / r.post_count
                if avg_comments > 200:
                    parts.append(
                        "  -> High discussion volume -- market event or "
                        "narrative shift driving engagement"
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

            # Check if any low-cap coins are trending (retail FOMO signal)
            low_cap_trending = [
                c for c in data.trending_coins
                if c.market_cap_rank and c.market_cap_rank > 100
            ]
            if len(low_cap_trending) >= 3:
                parts.append(
                    "  -> SIGNAL: Multiple low-cap coins trending "
                    "-- retail FOMO elevated, late-cycle behavior"
                )

            # Check if major coins are trending (broad interest)
            major_trending = [
                c for c in data.trending_coins
                if c.symbol.upper() in ("BTC", "ETH")
            ]
            if major_trending:
                names = ", ".join(c.name for c in major_trending)
                parts.append(
                    f"  -> {names} trending in searches "
                    "-- mainstream attention rising"
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
                        f"    Twitter/X: {cd.twitter_followers:,} followers"
                    )
                # Activity ratio as engagement signal
                if cd.reddit_subscribers > 0 and cd.reddit_active_accounts > 0:
                    activity_pct = (
                        cd.reddit_active_accounts / cd.reddit_subscribers
                    ) * 100
                    if activity_pct > 5:
                        parts.append(
                            f"    -> High activity ratio ({activity_pct:.1f}%) "
                            "-- community highly engaged"
                        )

        # --- Errors ---
        if data.fetch_errors:
            parts.append(
                f"\n[Social data gaps: {', '.join(data.fetch_errors)}]"
            )

        return "\n".join(parts)
