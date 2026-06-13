"""Vectorized technical indicators.

Pure pandas/numpy, no external TA dependency. Each function takes a price/OHLC
Series or DataFrame and returns a Series aligned to the input index. Strategies
should precompute indicators once per symbol rather than per-bar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def sma(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder's smoothing
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder). df needs high/low/close."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bollinger(close: pd.Series, window: int = 20, n_std: float = 2.0):
    mid = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=0)
    upper = mid + n_std * std
    lower = mid - n_std * std
    width = (upper - lower) / mid
    return mid, upper, lower, width


def vwap_session(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP, reset each trading day. df needs high/low/close/volume."""
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.normalize()
    pv = typical * df["volume"]
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum().replace(0.0, np.nan)
    return cum_pv / cum_v


def session_vwap_bands(df: pd.DataFrame, n_std: float = 2.0):
    """VWAP plus rolling intraday standard-deviation bands."""
    vw = vwap_session(df)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.normalize()
    dev2 = ((typical - vw) ** 2 * df["volume"]).groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum().replace(0.0, np.nan)
    std = np.sqrt(dev2 / cum_v)
    return vw, vw + n_std * std, vw - n_std * std


def opening_range(df: pd.DataFrame, minutes: int = 30):
    """High/low of each day's opening range.

    Returns two Series (or_high, or_low) broadcast to every bar of that day.
    Assumes an intraday RTH-filtered frame starting at 09:30 ET.
    """
    from datetime import time as dtime, datetime, timedelta
    day = df.index.normalize()
    cutoff = (datetime(2000, 1, 1, 9, 30) + timedelta(minutes=minutes)).time()
    in_or = df.index.time <= cutoff
    or_df = df[in_or]
    or_high = or_df["high"].groupby(or_df.index.normalize()).transform("max")
    or_low = or_df["low"].groupby(or_df.index.normalize()).transform("min")
    # map per-day OR levels back to the full frame
    high_by_day = or_high.groupby(or_df.index.normalize()).first()
    low_by_day = or_low.groupby(or_df.index.normalize()).first()
    return day.map(high_by_day), day.map(low_by_day)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index — trend strength."""
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    tr = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().fillna(0.0)


def realized_vol(close: pd.Series, window: int = 20) -> pd.Series:
    """Rolling realized volatility of bar returns (not annualized)."""
    return close.pct_change().rolling(window).std()
