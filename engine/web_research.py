"""
Web research module for the AI trading bot.

Provides web search capability so Claude can research trading strategies,
market analysis, coin fundamentals, and current events.

Uses DuckDuckGo HTML search (no API key required) with fallback to
direct news site scraping for crypto-specific queries.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# User agent to avoid blocks
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class SearchResult:
    """A single search result."""
    title: str = ""
    snippet: str = ""
    url: str = ""


@dataclass
class ResearchResult:
    """Complete research results for a query."""
    query: str = ""
    timestamp: float = 0.0
    results: list[SearchResult] = field(default_factory=list)
    summary: str = ""
    error: str = ""


class WebResearcher:
    """Web research capability for the trading AI."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http = http_client or httpx.AsyncClient(timeout=15.0)
        self._owns_http = http_client is None
        # Cache to avoid hammering search for repeated queries
        self._cache: dict[str, ResearchResult] = {}
        self._cache_ttl = 1800  # 30 minutes

    async def search(self, query: str, max_results: int = 8) -> ResearchResult:
        """Search the web and return structured results.

        Tries DuckDuckGo HTML search first, falls back to crypto news APIs.
        """
        # Check cache
        cache_key = query.lower().strip()
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - cached.timestamp < self._cache_ttl:
                logger.debug(f"Research cache hit: {query}")
                return cached

        result = ResearchResult(query=query, timestamp=time.time())

        # Try DuckDuckGo HTML search
        try:
            ddg_results = await self._search_ddg(query, max_results)
            result.results.extend(ddg_results)
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")

        # If crypto-related, also try CoinGecko for fundamentals
        crypto_terms = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol",
                        "crypto", "coin", "token", "defi", "altcoin",
                        "doge", "ada", "avax", "link", "dot", "pol", "xrp"]
        if any(term in query.lower() for term in crypto_terms):
            try:
                crypto_results = await self._search_crypto_news(query, max_results=4)
                result.results.extend(crypto_results)
            except Exception as e:
                logger.debug(f"Crypto news search failed: {e}")

        if not result.results:
            result.error = "No results found"

        # Build a summary for AI consumption
        result.summary = self._build_summary(result)

        # Cache it
        self._cache[cache_key] = result

        logger.info(f"Web research: '{query}' → {len(result.results)} results")
        return result

    async def _search_ddg(self, query: str, max_results: int) -> list[SearchResult]:
        """Search DuckDuckGo HTML and parse results."""
        results = []
        try:
            resp = await self._http.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
            html = resp.text

            # Parse result blocks — DuckDuckGo HTML has <a class="result__a"> and <a class="result__snippet">
            # Simple regex-based parsing (no BeautifulSoup dependency)
            title_pattern = re.compile(
                r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                re.DOTALL,
            )
            snippet_pattern = re.compile(
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                re.DOTALL,
            )

            titles = title_pattern.findall(html)
            snippets = snippet_pattern.findall(html)

            for i, (url, title) in enumerate(titles[:max_results]):
                snippet = snippets[i] if i < len(snippets) else ""
                # Clean HTML tags from title and snippet
                clean_title = re.sub(r'<[^>]+>', '', title).strip()
                clean_snippet = re.sub(r'<[^>]+>', '', snippet).strip()
                # DuckDuckGo wraps URLs in a redirect — extract the actual URL
                actual_url = url
                if "uddg=" in url:
                    match = re.search(r'uddg=([^&]+)', url)
                    if match:
                        from urllib.parse import unquote
                        actual_url = unquote(match.group(1))

                if clean_title:
                    results.append(SearchResult(
                        title=clean_title,
                        snippet=clean_snippet,
                        url=actual_url,
                    ))

        except Exception as e:
            logger.warning(f"DDG HTML search error: {e}")

        return results

    async def _search_crypto_news(self, query: str, max_results: int) -> list[SearchResult]:
        """Search crypto-specific news via CoinGecko's free status/trending endpoint
        and CryptoPanic's free tier."""
        results = []

        # CryptoPanic public API (no key needed for basic access)
        try:
            # Extract coin-related keywords for filtering
            resp = await self._http.get(
                "https://cryptopanic.com/api/free/v1/posts/",
                params={
                    "auth_token": "free",
                    "public": "true",
                    "filter": "hot",
                },
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            if resp.status_code == 200:
                data = resp.json()
                for post in data.get("results", [])[:max_results]:
                    results.append(SearchResult(
                        title=post.get("title", ""),
                        snippet=f"Source: {post.get('source', {}).get('title', 'Unknown')} | "
                                f"Votes: {post.get('votes', {}).get('positive', 0)} positive",
                        url=post.get("url", ""),
                    ))
        except Exception as e:
            logger.debug(f"CryptoPanic fetch failed: {e}")

        return results

    def _build_summary(self, result: ResearchResult) -> str:
        """Build a concise summary string for AI consumption."""
        if not result.results:
            return f"No results found for: {result.query}"

        parts = [f"Web research results for: \"{result.query}\""]
        for i, r in enumerate(result.results[:10], 1):
            parts.append(f"\n{i}. {r.title}")
            if r.snippet:
                # Truncate long snippets
                snippet = r.snippet[:300] + "..." if len(r.snippet) > 300 else r.snippet
                parts.append(f"   {snippet}")

        return "\n".join(parts)

    def format_for_context(self, result: ResearchResult) -> str:
        """Format research results for inclusion in trading AI context."""
        if not result.results:
            return ""
        return f"\n## RECENT WEB RESEARCH\n{result.summary}"

    async def close(self):
        """Clean up."""
        if self._owns_http:
            await self._http.aclose()
