"""
Macroeconomic & Correlation Data: Traditional markets, BTC-SPX correlation, FOMC calendar.

Fetches from free public APIs (no auth/keys needed):
- Yahoo Finance (public chart endpoint): S&P 500, DXY, 10Y yields, Gold, VIX
- CoinGecko: BTC price history for correlation calculation

Gives Claude visibility into macro conditions — dollar strength, risk appetite,
yield moves, and upcoming Fed decisions that drive crypto regime shifts.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Cache TTL: macro data is slow-moving
CACHE_TTL = 900.0  # 15 minutes

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Symbols we track
YAHOO_SYMBOLS = {
    "sp500": "^GSPC",
    "dxy": "DX-Y.NYB",
    "us10y": "^TNX",
    "gold": "GC=F",
    "vix": "^VIX",
}

# ------------------------------------------------------------------
# FOMC meeting dates (decision day, published in advance)
# ------------------------------------------------------------------
FOMC_DATES_2025 = [
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7),
    date(2025, 6, 18), date(2025, 7, 30), date(2025, 9, 17),
    date(2025, 10, 29), date(2025, 12, 10),
]

FOMC_DATES_2026 = [
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 5, 6),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 9),
]

ALL_FOMC_DATES = sorted(FOMC_DATES_2025 + FOMC_DATES_2026)

# CPI is typically released on the 2nd Tuesday or Wednesday of each month.
# Hardcode known/estimated dates for 2025-2026.
CPI_DATES_2025 = [
    date(2025, 1, 15), date(2025, 2, 12), date(2025, 3, 12),
    date(2025, 4, 10), date(2025, 5, 13), date(2025, 6, 11),
    date(2025, 7, 11), date(2025, 8, 12), date(2025, 9, 10),
    date(2025, 10, 14), date(2025, 11, 12), date(2025, 12, 10),
]

CPI_DATES_2026 = [
    date(2026, 1, 14), date(2026, 2, 11), date(2026, 3, 11),
    date(2026, 4, 14), date(2026, 5, 12), date(2026, 6, 10),
    date(2026, 7, 14), date(2026, 8, 12), date(2026, 9, 15),
    date(2026, 10, 13), date(2026, 11, 10), date(2026, 12, 9),
]

ALL_CPI_DATES = sorted(CPI_DATES_2025 + CPI_DATES_2026)


# ------------------------------------------------------------------
# Dataclasses
# ------------------------------------------------------------------

@dataclass
class MarketQuote:
    """Price data for a single traditional market instrument."""
    symbol: str = ""
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    daily_prices: list = field(default_factory=list)  # last 5 days of closes


@dataclass
class CorrelationData:
    """BTC vs S&P 500 directional correlation."""
    btc_daily_changes: list = field(default_factory=list)   # last 5 days pct changes
    spx_daily_changes: list = field(default_factory=list)   # last 5 days pct changes
    directional_agreement: float = 0.0   # fraction of days both moved same direction
    correlation_label: str = ""          # "correlated", "decorrelated", "inverse"
    btc_leading: Optional[bool] = None   # True if BTC moved first on most days


@dataclass
class EconomicCalendar:
    """Upcoming macro events."""
    next_fomc_date: Optional[date] = None
    days_until_fomc: int = -1
    fomc_is_imminent: bool = False       # within 3 days
    next_cpi_date: Optional[date] = None
    days_until_cpi: int = -1
    cpi_is_imminent: bool = False        # within 3 days


@dataclass
class MacroRegime:
    """Macro environment classification."""
    regime: str = ""             # "risk_on", "risk_off", "mixed"
    vix_level: str = ""          # "low", "elevated", "high", "extreme"
    dxy_trend: str = ""          # "rising", "falling", "flat"
    yield_trend: str = ""        # "rising", "falling", "flat"
    signals: list = field(default_factory=list)  # human-readable signal descriptions


@dataclass
class MacroSnapshot:
    """Full macroeconomic data snapshot."""
    timestamp: float = 0.0
    quotes: dict = field(default_factory=dict)          # {key: MarketQuote}
    correlation: Optional[CorrelationData] = None
    calendar: Optional[EconomicCalendar] = None
    regime: Optional[MacroRegime] = None
    fetch_errors: list = field(default_factory=list)


class MacroDataFetcher:
    """Fetches macroeconomic data from free public APIs."""

    def __init__(self, http_client: httpx.AsyncClient):
        self._http = http_client
        self._cache: dict[str, tuple[float, object]] = {}
        self._cache_ttl = CACHE_TTL
        self._last_snapshot: Optional[MacroSnapshot] = None
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

    async def fetch_all(self) -> MacroSnapshot:
        """Fetch all macro data concurrently. Graceful on partial failure."""
        # Return cached snapshot if still fresh
        if self._last_snapshot and (time.time() - self._last_fetch_time) < self._cache_ttl:
            return self._last_snapshot

        snapshot = MacroSnapshot(timestamp=time.time())

        # Fire all Yahoo Finance fetches + BTC price fetch concurrently
        tasks = [
            self._fetch_yahoo_quote("sp500", "^GSPC", "S&P 500"),
            self._fetch_yahoo_quote("dxy", "DX-Y.NYB", "US Dollar Index"),
            self._fetch_yahoo_quote("us10y", "^TNX", "US 10Y Treasury Yield"),
            self._fetch_yahoo_quote("gold", "GC=F", "Gold"),
            self._fetch_yahoo_quote("vix", "^VIX", "VIX Volatility Index"),
            self._fetch_btc_daily_prices(),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        task_names = ["sp500", "dxy", "us10y", "gold", "vix", "btc_prices"]

        # Process quote results
        for i in range(5):
            if isinstance(results[i], Exception):
                snapshot.fetch_errors.append(f"{task_names[i]}: {results[i]}")
                logger.debug(f"Macro fetch error ({task_names[i]}): {results[i]}")
            elif results[i] is not None:
                quote = results[i]
                snapshot.quotes[task_names[i]] = quote

        # BTC daily prices for correlation
        btc_prices = results[5] if not isinstance(results[5], Exception) else []
        if isinstance(results[5], Exception):
            snapshot.fetch_errors.append(f"btc_prices: {results[5]}")
            logger.debug(f"Macro fetch error (btc_prices): {results[5]}")

        # --- Compute BTC-SPX correlation ---
        spx_quote = snapshot.quotes.get("sp500")
        if spx_quote and spx_quote.daily_prices and btc_prices and len(btc_prices) >= 2:
            snapshot.correlation = self._compute_correlation(
                btc_prices, spx_quote.daily_prices
            )

        # --- Economic calendar ---
        snapshot.calendar = self._compute_calendar()

        # --- Macro regime ---
        snapshot.regime = self._classify_regime(snapshot.quotes)

        self._last_snapshot = snapshot
        self._last_fetch_time = time.time()
        return snapshot

    # ------------------------------------------------------------------
    # Yahoo Finance fetches
    # ------------------------------------------------------------------

    async def _fetch_yahoo_quote(self, key: str, symbol: str, name: str) -> MarketQuote:
        """Fetch a single instrument from Yahoo Finance public chart API."""
        cached = self._get_cached(key)
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                f"{YAHOO_BASE}/{symbol}",
                params={"range": "5d", "interval": "1d"},
                headers=YAHOO_HEADERS,
                timeout=15.0,
            )
            if resp.status_code in (403, 429):
                logger.debug(f"Yahoo Finance blocked for {symbol} (HTTP {resp.status_code})")
                raise RuntimeError(f"Yahoo Finance returned {resp.status_code} for {symbol}")
            resp.raise_for_status()
            data = resp.json()

            chart = data.get("chart", {}).get("result", [])
            if not chart:
                raise ValueError(f"No chart data for {symbol}")

            result = chart[0]
            meta = result.get("meta", {})
            indicators = result.get("indicators", {})
            adj_close = indicators.get("adjclose", [{}])[0].get("adjclose", [])
            closes = indicators.get("quote", [{}])[0].get("close", [])

            # Use adjclose if available, else close
            prices = adj_close if adj_close else closes
            # Filter out None values
            prices = [p for p in prices if p is not None]

            current_price = meta.get("regularMarketPrice", 0.0)
            prev_close = meta.get("chartPreviousClose", 0.0)

            if current_price == 0 and prices:
                current_price = prices[-1]
            if prev_close == 0 and len(prices) >= 2:
                prev_close = prices[-2]

            change = current_price - prev_close if prev_close else 0.0
            change_pct = (change / prev_close * 100) if prev_close else 0.0

            quote = MarketQuote(
                symbol=symbol,
                name=name,
                price=current_price,
                prev_close=prev_close,
                change=round(change, 4),
                change_pct=round(change_pct, 4),
                daily_prices=prices,
            )
            self._set_cached(key, quote)
            return quote
        except Exception as e:
            logger.debug(f"Yahoo Finance fetch failed for {symbol}: {e}")
            raise

    async def _fetch_btc_daily_prices(self) -> list:
        """Fetch BTC daily prices from CoinGecko (free, no key) for correlation calc."""
        cached = self._get_cached("btc_daily_prices")
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(
                "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
                params={"vs_currency": "usd", "days": "7", "interval": "daily"},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            # Returns list of [timestamp_ms, price]
            prices = [p[1] for p in data.get("prices", []) if p[1] is not None]
            self._set_cached("btc_daily_prices", prices)
            return prices
        except Exception as e:
            logger.debug(f"CoinGecko BTC daily price fetch failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Correlation computation
    # ------------------------------------------------------------------

    def _compute_correlation(
        self, btc_prices: list, spx_prices: list
    ) -> CorrelationData:
        """Compute simple directional correlation between BTC and S&P 500.

        Uses daily percentage changes to see if they move in the same direction.
        """
        corr = CorrelationData()

        # Compute daily pct changes
        btc_changes = []
        for i in range(1, len(btc_prices)):
            if btc_prices[i - 1] > 0:
                btc_changes.append(
                    (btc_prices[i] - btc_prices[i - 1]) / btc_prices[i - 1] * 100
                )
        spx_changes = []
        for i in range(1, len(spx_prices)):
            if spx_prices[i - 1] > 0:
                spx_changes.append(
                    (spx_prices[i] - spx_prices[i - 1]) / spx_prices[i - 1] * 100
                )

        corr.btc_daily_changes = [round(c, 3) for c in btc_changes]
        corr.spx_daily_changes = [round(c, 3) for c in spx_changes]

        # Align to same length (use the shorter series)
        n = min(len(btc_changes), len(spx_changes))
        if n == 0:
            corr.correlation_label = "insufficient data"
            return corr

        btc_aligned = btc_changes[-n:]
        spx_aligned = spx_changes[-n:]

        # Count directional agreement
        same_direction = 0
        btc_leads = 0
        for i in range(n):
            btc_dir = 1 if btc_aligned[i] > 0 else (-1 if btc_aligned[i] < 0 else 0)
            spx_dir = 1 if spx_aligned[i] > 0 else (-1 if spx_aligned[i] < 0 else 0)
            if btc_dir == spx_dir and btc_dir != 0:
                same_direction += 1
                # Check magnitude — if BTC moved more, it may be leading
                if abs(btc_aligned[i]) > abs(spx_aligned[i]):
                    btc_leads += 1

        agreement = same_direction / n if n > 0 else 0
        corr.directional_agreement = round(agreement, 3)

        if agreement >= 0.7:
            corr.correlation_label = "correlated"
        elif agreement <= 0.3:
            corr.correlation_label = "inverse"
        else:
            corr.correlation_label = "decorrelated"

        # BTC leading if it had larger moves on most agreement days
        if same_direction > 0:
            corr.btc_leading = btc_leads > (same_direction / 2)

        return corr

    # ------------------------------------------------------------------
    # Economic calendar
    # ------------------------------------------------------------------

    def _compute_calendar(self) -> EconomicCalendar:
        """Compute days until next FOMC and CPI releases."""
        cal = EconomicCalendar()
        today = date.today()

        # Next FOMC
        for d in ALL_FOMC_DATES:
            if d >= today:
                cal.next_fomc_date = d
                cal.days_until_fomc = (d - today).days
                cal.fomc_is_imminent = cal.days_until_fomc <= 3
                break

        # Next CPI
        for d in ALL_CPI_DATES:
            if d >= today:
                cal.next_cpi_date = d
                cal.days_until_cpi = (d - today).days
                cal.cpi_is_imminent = cal.days_until_cpi <= 3
                break

        return cal

    # ------------------------------------------------------------------
    # Macro regime classification
    # ------------------------------------------------------------------

    def _classify_regime(self, quotes: dict) -> MacroRegime:
        """Classify the macro environment based on DXY, VIX, and yields.

        Rules:
        - risk_on:  low VIX + falling DXY + falling yields
        - risk_off: high VIX + rising DXY + rising yields
        - mixed:    conflicting signals
        """
        regime = MacroRegime()

        # --- VIX level ---
        vix_q = quotes.get("vix")
        if vix_q and vix_q.price > 0:
            vix_val = vix_q.price
            if vix_val < 15:
                regime.vix_level = "low"
            elif vix_val < 20:
                regime.vix_level = "elevated"
            elif vix_val < 30:
                regime.vix_level = "high"
            else:
                regime.vix_level = "extreme"

        # --- DXY trend (based on 5d daily changes) ---
        dxy_q = quotes.get("dxy")
        if dxy_q and len(dxy_q.daily_prices) >= 2:
            dxy_change_pct = dxy_q.change_pct
            if dxy_change_pct > 0.3:
                regime.dxy_trend = "rising"
            elif dxy_change_pct < -0.3:
                regime.dxy_trend = "falling"
            else:
                regime.dxy_trend = "flat"

        # --- Yield trend ---
        y10_q = quotes.get("us10y")
        if y10_q and len(y10_q.daily_prices) >= 2:
            yield_change = y10_q.change
            if yield_change > 0.03:
                regime.yield_trend = "rising"
            elif yield_change < -0.03:
                regime.yield_trend = "falling"
            else:
                regime.yield_trend = "flat"

        # --- Classify regime ---
        risk_on_score = 0
        risk_off_score = 0

        if regime.vix_level in ("low",):
            risk_on_score += 1
        elif regime.vix_level in ("high", "extreme"):
            risk_off_score += 1

        if regime.dxy_trend == "falling":
            risk_on_score += 1
        elif regime.dxy_trend == "rising":
            risk_off_score += 1

        if regime.yield_trend == "falling":
            risk_on_score += 1
        elif regime.yield_trend == "rising":
            risk_off_score += 1

        if risk_on_score >= 2 and risk_off_score == 0:
            regime.regime = "risk_on"
        elif risk_off_score >= 2 and risk_on_score == 0:
            regime.regime = "risk_off"
        else:
            regime.regime = "mixed"

        # Build signal descriptions
        if vix_q and vix_q.price > 0:
            regime.signals.append(f"VIX at {vix_q.price:.1f} ({regime.vix_level})")
        if dxy_q and dxy_q.price > 0:
            regime.signals.append(f"DXY {regime.dxy_trend} ({dxy_q.change_pct:+.2f}%)")
        if y10_q and y10_q.price > 0:
            regime.signals.append(
                f"10Y yield {regime.yield_trend} at {y10_q.price:.3f}% ({y10_q.change:+.3f})"
            )

        return regime

    # ------------------------------------------------------------------
    # Formatting for AI context
    # ------------------------------------------------------------------

    def format_for_context(self, data: MacroSnapshot) -> str:
        """Format macro data for injection into Claude's context window.

        Focuses on actionable signals and interpretation hints.
        """
        if not data:
            return ""

        parts = ["\n## MACROECONOMIC DATA (Traditional Markets + Correlation)"]

        # --- Traditional Market Quotes ---
        if data.quotes:
            parts.append("\n### Traditional Markets")
            display_order = [
                ("sp500", "S&P 500"),
                ("dxy", "US Dollar (DXY)"),
                ("us10y", "10Y Treasury Yield"),
                ("gold", "Gold"),
                ("vix", "VIX"),
            ]
            for key, label in display_order:
                q = data.quotes.get(key)
                if q:
                    if key == "us10y":
                        # Yield is displayed as percentage
                        parts.append(
                            f"  {label}: {q.price:.3f}% ({q.change:+.3f})"
                        )
                    else:
                        parts.append(
                            f"  {label}: {q.price:,.2f} ({q.change_pct:+.2f}%)"
                        )

            # Interpretation hints
            dxy_q = data.quotes.get("dxy")
            if dxy_q and dxy_q.change_pct > 0.3:
                parts.append(
                    "  -> Strong dollar (DXY rising) typically pressures BTC"
                )
            elif dxy_q and dxy_q.change_pct < -0.3:
                parts.append(
                    "  -> Weakening dollar (DXY falling) is generally bullish for BTC"
                )

            vix_q = data.quotes.get("vix")
            if vix_q:
                if vix_q.price >= 30:
                    parts.append(
                        "  -> VIX extreme — fear in equities, crypto usually sells off too"
                    )
                elif vix_q.price >= 25:
                    parts.append(
                        "  -> VIX elevated — risk aversion rising, watch for spillover into crypto"
                    )

            gold_q = data.quotes.get("gold")
            if gold_q and gold_q.change_pct > 1.0:
                parts.append(
                    "  -> Gold surging — flight to safety, mixed for BTC (competes as hedge)"
                )

        # --- BTC-SPX Correlation ---
        if data.correlation:
            c = data.correlation
            parts.append(f"\n### BTC-SPX Correlation — {c.correlation_label.upper()}")
            parts.append(
                f"  Directional agreement (5d): {c.directional_agreement:.0%}"
            )
            if c.btc_daily_changes:
                parts.append(f"  BTC daily changes: {c.btc_daily_changes}")
            if c.spx_daily_changes:
                parts.append(f"  SPX daily changes: {c.spx_daily_changes}")
            if c.btc_leading is True:
                parts.append(
                    "  -> BTC appears to be LEADING equities (larger moves on agreement days)"
                )
            elif c.btc_leading is False:
                parts.append(
                    "  -> BTC appears to be LAGGING equities (following SPX direction)"
                )
            if c.correlation_label == "correlated":
                parts.append(
                    "  -> High correlation: BTC trading as a risk asset, macro matters more"
                )
            elif c.correlation_label == "decorrelated":
                parts.append(
                    "  -> Decorrelated: BTC trading on its own drivers (crypto-native catalysts)"
                )
            elif c.correlation_label == "inverse":
                parts.append(
                    "  -> Inverse correlation: BTC diverging from equities — possible regime shift"
                )

        # --- Macro Regime ---
        if data.regime:
            r = data.regime
            regime_emoji = {
                "risk_on": "RISK-ON",
                "risk_off": "RISK-OFF",
                "mixed": "MIXED",
            }
            parts.append(
                f"\n### Macro Regime: {regime_emoji.get(r.regime, r.regime.upper())}"
            )
            for sig in r.signals:
                parts.append(f"  {sig}")
            if r.regime == "risk_on":
                parts.append(
                    "  -> Favorable macro backdrop for crypto — "
                    "low vol + weak dollar + falling yields = liquidity tailwind"
                )
            elif r.regime == "risk_off":
                parts.append(
                    "  -> Hostile macro environment — "
                    "consider defensive positioning, tighter stops, smaller sizes"
                )
            else:
                parts.append(
                    "  -> Mixed signals — no clear macro tailwind or headwind, "
                    "focus on crypto-specific catalysts"
                )

        # --- Economic Calendar ---
        if data.calendar:
            cal = data.calendar
            parts.append("\n### Economic Calendar")
            if cal.next_fomc_date:
                parts.append(
                    f"  Next FOMC decision: {cal.next_fomc_date.isoformat()} "
                    f"({cal.days_until_fomc} days away)"
                )
                if cal.fomc_is_imminent:
                    parts.append(
                        f"  \u26a0 FOMC decision in {cal.days_until_fomc} day(s) "
                        f"— expect volatility, consider reducing position sizes"
                    )
            if cal.next_cpi_date:
                parts.append(
                    f"  Next CPI release: {cal.next_cpi_date.isoformat()} "
                    f"({cal.days_until_cpi} days away)"
                )
                if cal.cpi_is_imminent:
                    parts.append(
                        f"  \u26a0 CPI release in {cal.days_until_cpi} day(s) "
                        f"— inflation data can trigger sharp moves"
                    )

        # --- Errors ---
        if data.fetch_errors:
            parts.append(f"\n[Macro data gaps: {', '.join(data.fetch_errors)}]")

        return "\n".join(parts)
