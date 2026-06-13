"""Market data loader.

Pulls OHLCV bars from the free Yahoo Finance chart API and caches them on
disk so repeated backtests are fast and reproducible. Everything is returned
as a tidy pandas DataFrame indexed by Eastern-time timestamps.

Free-data reality (documented honestly):
    interval  max history     use
    1m        ~7 days         micro-validation only
    5m        ~60 days        high-fidelity intraday
    15m       ~60 days        high-fidelity intraday
    1h        ~730 days       multi-regime intraday (lower fidelity)
    1d        full history    swing / benchmark

We cannot get years of 5m data for free, so longer backtests use 1h bars.
The backtester is granularity-agnostic; the report states which feed was used.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Iterable

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parents[2] / "cache"
CACHE_DIR.mkdir(exist_ok=True)

_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; daytrader/1.0)"}

# How much history each interval can serve, used to pick a safe default range.
MAX_RANGE = {
    "1m": "7d",
    "2m": "60d",
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",
    "1h": "730d",
    "1d": "max",
}


def _normalize_interval(interval: str) -> str:
    return "1h" if interval == "60m" else interval


def _fetch_yahoo(symbol: str, rng: str, interval: str, retries: int = 4) -> pd.DataFrame:
    iv = "60m" if interval == "1h" else interval
    url = _BASE.format(symbol=symbol) + f"?range={rng}&interval={iv}&includePrePost=false"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            raw = urllib.request.urlopen(req, timeout=20).read()
            doc = json.loads(raw)
            res = doc["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
            df = pd.DataFrame(
                {
                    "open": q["open"],
                    "high": q["high"],
                    "low": q["low"],
                    "close": q["close"],
                    "volume": q["volume"],
                },
                index=pd.to_datetime(ts, unit="s", utc=True),
            )
            df.index = df.index.tz_convert("America/New_York")
            df.index.name = "ts"
            df = df.dropna(subset=["open", "high", "low", "close"])
            df["symbol"] = symbol
            return df
        except Exception as e:  # noqa: BLE001 - network/json variability
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Failed to fetch {symbol} {interval} {rng}: {last_err}")


def _cache_path(symbol: str, interval: str, rng: str) -> Path:
    return CACHE_DIR / f"{symbol}_{interval}_{rng}.pkl"


def load(
    symbol: str,
    interval: str = "5m",
    rng: str | None = None,
    rth_only: bool = True,
    use_cache: bool = True,
    max_age_hours: float = 12.0,
) -> pd.DataFrame:
    """Load OHLCV bars for one symbol.

    Args:
        symbol: e.g. "SPY", "AAPL". (SPX index uses "^GSPC".)
        interval: 1m/5m/15m/30m/1h/1d.
        rng: yahoo range string; defaults to the max the interval supports.
        rth_only: keep only 09:30-16:00 ET regular session bars.
        use_cache: read/write parquet cache.
        max_age_hours: refetch if the cache is older than this.
    """
    interval = _normalize_interval(interval)
    rng = rng or MAX_RANGE.get(interval, "60d")
    path = _cache_path(symbol, interval, rng)

    if use_cache and path.exists():
        age_h = (time.time() - path.stat().st_mtime) / 3600.0
        if age_h <= max_age_hours:
            df = pd.read_pickle(path)
        else:
            df = _fetch_yahoo(symbol, rng, interval)
            df.to_pickle(path)
    else:
        df = _fetch_yahoo(symbol, rng, interval)
        if use_cache:
            df.to_pickle(path)

    if rth_only and interval not in ("1d",):
        t = df.index.time
        from datetime import time as dtime
        mask = (t >= dtime(9, 30)) & (t < dtime(16, 0))
        df = df[mask]
    return df.sort_index()


def load_many(
    symbols: Iterable[str],
    interval: str = "5m",
    rng: str | None = None,
    rth_only: bool = True,
    **kw,
) -> dict[str, pd.DataFrame]:
    """Load several symbols into a {symbol: DataFrame} dict."""
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        try:
            out[s] = load(s, interval=interval, rng=rng, rth_only=rth_only, **kw)
        except Exception as e:  # noqa: BLE001
            print(f"[data] WARNING: could not load {s}: {e}")
    return out


# Convenience universes
MAG7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
DEFAULT_UNIVERSE = ["SPY"] + MAG7
BENCHMARK = "SPY"


if __name__ == "__main__":  # smoke test
    df = load("SPY", "5m")
    print(df.tail())
    print(f"{len(df)} bars  {df.index.min()} -> {df.index.max()}")
