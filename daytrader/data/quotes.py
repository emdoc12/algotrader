"""Single source of truth for live quotes.

The same number is returned to:
  * the market-state snapshot (so the agent reasons over the live price), and
  * the paper broker's fills (so the broker fills at the price the agent saw).

This eliminates the feed-vs-broker price gap that was flipping winning trades
into losses — the snapshot's "price" and the broker's "fill" are now the same
quote drawn from the same cache.

Quote source: Yahoo's chart-meta ``regularMarketPrice`` (the official last
trade), which is fresher and more reliable than the most-recent 1-minute bar's
close — particularly at session open and on names with sparse trade flow (the
"BA price discrepancy" pattern). Falls back to the last 1m bar close if the
meta is briefly unavailable.

Cache: a tiny module-level TTL keeps repeated lookups within a cycle cheap
without going stale. Within a trading cycle the cache typically serves every
caller; between cycles it refreshes naturally. For exact snapshot↔fill matching
across a cycle's lifetime, the competition loop pins the cycle's quote map onto
each team's broker via :meth:`PaperBroker.set_cycle_quotes`.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Iterable, Optional

from daytrader.data import loader

_TTL_SEC = 30.0
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; daytrader/1.0)"}
_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, fetched_at)


def _fetch_yahoo_meta(symbol: str) -> Optional[float]:
    """Last trade price from Yahoo's chart meta. None on any failure."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range=1d&interval=1m"
    )
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        raw = urllib.request.urlopen(req, timeout=8).read()
        doc = json.loads(raw)
        meta = doc.get("chart", {}).get("result", [{}])[0].get("meta", {})
        v = meta.get("regularMarketPrice")
        if v is None:
            return None
        return float(v)
    except Exception:  # noqa: BLE001 - degrade to fallback
        return None


def _fetch_bar_close(symbol: str) -> Optional[float]:
    """Fallback: last 1m bar close. Uses the loader's disk cache + force-refresh
    when the disk cache is older than ~3 minutes (0.05h)."""
    try:
        df = loader.load(symbol, interval="1m", max_age_hours=0.05)
    except Exception:  # noqa: BLE001
        return None
    if df is None or len(df) == 0:
        return None
    try:
        return float(df["close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None


def get_quote(symbol: str, max_age_sec: float = _TTL_SEC) -> Optional[float]:
    """Latest quote for one symbol, or None if unavailable."""
    sym = symbol.upper()
    now = time.time()
    hit = _cache.get(sym)
    if hit and now - hit[1] < max_age_sec:
        return hit[0]
    px = _fetch_yahoo_meta(sym)
    if px is None:
        px = _fetch_bar_close(sym)
    if px is None:
        return None
    _cache[sym] = (px, now)
    return px


def get_quotes(symbols: Iterable[str], max_age_sec: float = _TTL_SEC) -> dict[str, float]:
    """Bulk lookup; symbols whose quote can't be fetched are omitted."""
    out: dict[str, float] = {}
    for s in symbols:
        q = get_quote(s, max_age_sec=max_age_sec)
        if q is not None:
            out[s.upper()] = q
    return out


def clear_cache() -> None:
    """For tests."""
    _cache.clear()
