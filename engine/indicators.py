"""
Technical indicators computed from real OHLCV history.

All functions take a list of floats (closing prices) and return indicator values.
Unlike the original code, these compute over actual historical candles — not
a single running value that drifts over time.
"""

import math
from dataclasses import dataclass


@dataclass
class EMAResult:
    """EMA crossover result."""
    fast_ema: float
    slow_ema: float
    crossover: str  # "bullish", "bearish", or "neutral"
    fast_above_slow: bool


@dataclass
class RSIResult:
    rsi: float
    signal: str  # "oversold", "overbought", or "neutral"


@dataclass
class BollingerResult:
    upper: float
    middle: float  # SMA
    lower: float
    bandwidth: float
    price_position: float  # 0.0 = at lower band, 1.0 = at upper band


@dataclass
class ATRResult:
    """Average True Range — measures volatility."""
    atr: float            # current ATR value in USD
    atr_pct: float        # ATR as % of current price
    volatility: str       # "low", "medium", "high", "extreme"


@dataclass
class Signals:
    """Combined indicator signals for a single point in time."""
    price: float
    ema: EMAResult
    rsi: RSIResult
    bollinger: BollingerResult
    atr: ATRResult = None
    # Composite signal: -1.0 (strong sell) to +1.0 (strong buy)
    composite_score: float = 0.0
    recommendation: str = "HOLD"  # "STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"


# ---------------------------------------------------------------------------
# Core indicator calculations
# ---------------------------------------------------------------------------

def compute_ema(prices: list[float], period: int) -> list[float]:
    """
    Compute Exponential Moving Average over a price series.

    Uses the standard formula: EMA = price * k + prev_ema * (1 - k)
    where k = 2 / (period + 1).

    The first `period` values use a simple average as the seed.

    Returns a list the same length as `prices` (first period-1 values are NaN).
    """
    if len(prices) < period:
        return [float("nan")] * len(prices)

    k = 2.0 / (period + 1)
    ema_values = [float("nan")] * len(prices)

    # Seed: SMA of first `period` prices
    seed = sum(prices[:period]) / period
    ema_values[period - 1] = seed

    # Compute EMA from period onward
    for i in range(period, len(prices)):
        ema_values[i] = prices[i] * k + ema_values[i - 1] * (1 - k)

    return ema_values


def compute_rsi(prices: list[float], period: int = 14) -> list[float]:
    """
    Compute Relative Strength Index using Wilder's smoothed moving average.

    Returns a list the same length as `prices` (first `period` values are NaN).
    """
    if len(prices) < period + 1:
        return [float("nan")] * len(prices)

    rsi_values = [float("nan")] * len(prices)
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    # First average gain/loss: simple average of first `period` changes
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [abs(min(d, 0)) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi_values[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi_values[period] = 100.0 - (100.0 / (1.0 + rs))

    # Subsequent values: Wilder's smoothing
    for i in range(period, len(deltas)):
        delta = deltas[i]
        gain = max(delta, 0)
        loss = abs(min(delta, 0))

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            rsi_values[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi_values[i + 1] = 100.0 - (100.0 / (1.0 + rs))

    return rsi_values


def compute_bollinger_bands(
    prices: list[float], period: int = 20, std_dev: float = 2.0
) -> tuple[list[float], list[float], list[float]]:
    """
    Compute Bollinger Bands (upper, middle/SMA, lower).

    Returns three lists, each the same length as `prices`.
    First `period-1` values are NaN.
    """
    n = len(prices)
    upper = [float("nan")] * n
    middle = [float("nan")] * n
    lower = [float("nan")] * n

    for i in range(period - 1, n):
        window = prices[i - period + 1 : i + 1]
        sma = sum(window) / period
        variance = sum((p - sma) ** 2 for p in window) / period
        sd = math.sqrt(variance)

        middle[i] = sma
        upper[i] = sma + std_dev * sd
        lower[i] = sma - std_dev * sd

    return upper, middle, lower


def compute_atr(highs: list[float], lows: list[float], closes: list[float],
                period: int = 14) -> list[float]:
    """
    Compute Average True Range (ATR) using Wilder's smoothing.

    True Range = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR = smoothed average of True Range over `period`.

    Requires highs, lows, closes of equal length with at least period+1 bars.
    Returns list same length as inputs (first `period` values are NaN).
    """
    n = len(closes)
    if n < period + 1:
        return [float("nan")] * n

    atr_values = [float("nan")] * n

    # Compute True Range series
    tr = [float("nan")]  # first bar has no prev close
    for i in range(1, n):
        high_low = highs[i] - lows[i]
        high_prev_close = abs(highs[i] - closes[i - 1])
        low_prev_close = abs(lows[i] - closes[i - 1])
        tr.append(max(high_low, high_prev_close, low_prev_close))

    # Seed: simple average of first `period` true ranges
    first_atr = sum(tr[1:period + 1]) / period
    atr_values[period] = first_atr

    # Wilder's smoothing: ATR = (prev_ATR * (period-1) + current_TR) / period
    for i in range(period + 1, n):
        atr_values[i] = (atr_values[i - 1] * (period - 1) + tr[i]) / period

    return atr_values


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------

def generate_signals(
    prices: list[float],
    ema_fast_period: int = 9,
    ema_slow_period: int = 21,
    rsi_period: int = 14,
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
    bb_period: int = 20,
    bb_std_dev: float = 2.0,
    highs: list[float] = None,
    lows: list[float] = None,
    atr_period: int = 14,
) -> Signals:
    """
    Generate combined trading signals from the latest candle data.

    Requires enough price history to compute all indicators (at least
    max(ema_slow_period, bb_period, rsi_period+1) bars).
    """
    min_bars = max(ema_slow_period, bb_period, rsi_period + 1)
    if len(prices) < min_bars:
        raise ValueError(
            f"Need at least {min_bars} price bars, got {len(prices)}"
        )

    current_price = prices[-1]

    # --- EMA ---
    fast_ema_series = compute_ema(prices, ema_fast_period)
    slow_ema_series = compute_ema(prices, ema_slow_period)
    fast_ema = fast_ema_series[-1]
    slow_ema = slow_ema_series[-1]
    prev_fast = fast_ema_series[-2]
    prev_slow = slow_ema_series[-2]

    fast_above = fast_ema > slow_ema
    # Crossover: fast was below slow, now above (bullish) or vice versa
    if prev_fast <= prev_slow and fast_ema > slow_ema:
        ema_crossover = "bullish"
    elif prev_fast >= prev_slow and fast_ema < slow_ema:
        ema_crossover = "bearish"
    else:
        ema_crossover = "neutral"

    ema_result = EMAResult(
        fast_ema=fast_ema,
        slow_ema=slow_ema,
        crossover=ema_crossover,
        fast_above_slow=fast_above,
    )

    # --- RSI ---
    rsi_series = compute_rsi(prices, rsi_period)
    rsi_val = rsi_series[-1]
    if math.isnan(rsi_val):
        rsi_signal = "neutral"
    elif rsi_val >= rsi_overbought:
        rsi_signal = "overbought"
    elif rsi_val <= rsi_oversold:
        rsi_signal = "oversold"
    else:
        rsi_signal = "neutral"

    rsi_result = RSIResult(rsi=rsi_val, signal=rsi_signal)

    # --- Bollinger Bands ---
    bb_upper, bb_middle, bb_lower = compute_bollinger_bands(prices, bb_period, bb_std_dev)
    bb_u = bb_upper[-1]
    bb_m = bb_middle[-1]
    bb_l = bb_lower[-1]
    bandwidth = (bb_u - bb_l) / bb_m if bb_m != 0 else 0
    price_pos = (current_price - bb_l) / (bb_u - bb_l) if (bb_u - bb_l) != 0 else 0.5

    bb_result = BollingerResult(
        upper=bb_u, middle=bb_m, lower=bb_l,
        bandwidth=bandwidth, price_position=price_pos,
    )

    # --- Composite score ---
    # EMA component: +0.4 if bullish crossover, -0.4 if bearish, +/-0.2 for trend direction
    ema_score = 0.0
    if ema_crossover == "bullish":
        ema_score = 0.4
    elif ema_crossover == "bearish":
        ema_score = -0.4
    elif fast_above:
        ema_score = 0.2
    else:
        ema_score = -0.2

    # RSI component: +0.3 if oversold (buy signal), -0.3 if overbought (sell signal)
    rsi_score = 0.0
    if not math.isnan(rsi_val):
        if rsi_val <= rsi_oversold:
            rsi_score = 0.3
        elif rsi_val >= rsi_overbought:
            rsi_score = -0.3
        else:
            # Scale linearly: 50 = 0, 30 = +0.15, 70 = -0.15
            rsi_score = (50.0 - rsi_val) / 50.0 * 0.15

    # Bollinger component: +0.3 near lower band (buy), -0.3 near upper band (sell)
    bb_score = 0.0
    if not math.isnan(bb_l):
        if current_price <= bb_l:
            bb_score = 0.3
        elif current_price >= bb_u:
            bb_score = -0.3
        else:
            # Linear interpolation: lower band = +0.15, upper band = -0.15
            bb_score = (0.5 - price_pos) * 0.3

    composite = ema_score + rsi_score + bb_score

    # Map to recommendation
    if composite >= 0.5:
        rec = "STRONG_BUY"
    elif composite >= 0.2:
        rec = "BUY"
    elif composite <= -0.5:
        rec = "STRONG_SELL"
    elif composite <= -0.2:
        rec = "SELL"
    else:
        rec = "HOLD"

    # --- ATR (if OHLCV data provided) ---
    atr_result = None
    if highs is not None and lows is not None and len(highs) == len(prices):
        atr_series = compute_atr(highs, lows, prices, atr_period)
        atr_val = atr_series[-1]
        if not math.isnan(atr_val) and current_price > 0:
            atr_pct = (atr_val / current_price) * 100
            if atr_pct < 1.5:
                vol_label = "low"
            elif atr_pct < 3.0:
                vol_label = "medium"
            elif atr_pct < 6.0:
                vol_label = "high"
            else:
                vol_label = "extreme"
            atr_result = ATRResult(
                atr=round(atr_val, 4),
                atr_pct=round(atr_pct, 3),
                volatility=vol_label,
            )

    return Signals(
        price=current_price,
        ema=ema_result,
        rsi=rsi_result,
        bollinger=bb_result,
        atr=atr_result,
        composite_score=round(composite, 4),
        recommendation=rec,
    )
