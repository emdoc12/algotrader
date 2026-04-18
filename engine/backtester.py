"""
Backtester — on-demand strategy backtesting against Kraken historical data.
============================================================================
Triggered by Claude via action tag:
  [BACKTEST: strategy=ema_crossover, pair=BTC/USD, interval=60, hours=168, fast=9, slow=21]

Self-contained: all indicator math is built in (no imports from indicators.py)
so this module can be tested and used independently.

Kraken public OHLC endpoint (free, no API key required):
  https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}&since={since}
  Returns up to 720 candles per request.

Supported strategies:
  - ema_crossover   (params: fast, slow)
  - rsi_reversal    (params: period, oversold, overbought)
  - bollinger_bounce (params: period, std_dev)
  - vwap_reversion  (params: — none required)
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("backtester")

# ---------------------------------------------------------------------------
# Symbol mapping — mirrors ai_strategy.SYMBOL_MAP
# ---------------------------------------------------------------------------
SYMBOL_MAP = {
    "BTC/USD": {"kraken": "XBTUSD", "base": "BTC"},
    "ETH/USD": {"kraken": "ETHUSD", "base": "ETH"},
    "SOL/USD": {"kraken": "SOLUSD", "base": "SOL"},
    "DOGE/USD": {"kraken": "DOGEUSD", "base": "DOGE"},
    "ADA/USD": {"kraken": "ADAUSD", "base": "ADA"},
    "AVAX/USD": {"kraken": "AVAXUSD", "base": "AVAX"},
    "LINK/USD": {"kraken": "LINKUSD", "base": "LINK"},
    "DOT/USD": {"kraken": "DOTUSD", "base": "DOT"},
    "POL/USD": {"kraken": "POLUSD", "base": "POL"},
    "XRP/USD": {"kraken": "XRPUSD", "base": "XRP"},
}

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

# Valid Kraken intervals (minutes)
VALID_INTERVALS = {15, 60, 240, 1440}  # 15m, 1h, 4h, 1d

STRATEGY_NAMES = ("ema_crossover", "rsi_reversal", "bollinger_bounce", "vwap_reversion")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class BacktestTrade:
    """A single round-trip trade (entry + exit)."""
    entry_time: int          # unix ts
    exit_time: int
    side: str                # "long"
    entry_price: float
    exit_price: float
    quantity: float
    fee_paid: float
    pnl: float               # net after fees
    pnl_pct: float
    bars_held: int

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


@dataclass
class BacktestResult:
    """Full result of a backtest run."""
    strategy: str
    pair: str
    interval: int             # minutes
    hours: int                # lookback
    params: dict
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    initial_capital: float = 10_000.0
    final_capital: float = 10_000.0
    total_return_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    avg_trade_duration_bars: float = 0.0
    total_trades: int = 0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Indicator math (self-contained)
# ---------------------------------------------------------------------------

def _ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential moving average. Returns list same length as values."""
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period or period < 1:
        return out
    # seed with SMA
    sma = sum(values[:period]) / period
    out[period - 1] = sma
    k = 2.0 / (period + 1)
    prev = sma
    for i in range(period, len(values)):
        val = values[i] * k + prev * (1 - k)
        out[i] = val
        prev = val
    return out


def _rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Wilder RSI. Returns list same length as closes."""
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    # First average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out


def _bollinger(closes: list[float], period: int = 20, std_dev: float = 2.0):
    """Returns (upper, middle, lower) lists, same length as closes."""
    n = len(closes)
    upper = [None] * n
    middle = [None] * n
    lower = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1: i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        std = math.sqrt(var)
        middle[i] = mean
        upper[i] = mean + std_dev * std
        lower[i] = mean - std_dev * std
    return upper, middle, lower


def _vwap_cumulative(bars: list[dict]) -> list[Optional[float]]:
    """Cumulative VWAP across all bars. Returns list same length as bars."""
    out: list[Optional[float]] = []
    cum_vol = 0.0
    cum_tp_vol = 0.0
    for bar in bars:
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        cum_vol += bar["volume"]
        cum_tp_vol += typical * bar["volume"]
        if cum_vol > 0:
            out.append(cum_tp_vol / cum_vol)
        else:
            out.append(None)
    return out


# ---------------------------------------------------------------------------
# Strategy functions
# ---------------------------------------------------------------------------
# Each returns a list of signals: +1 = buy, -1 = sell, 0 = hold
# for every bar in the input.

def _strategy_ema_crossover(bars: list[dict], params: dict) -> list[int]:
    fast_p = int(params.get("fast", 9))
    slow_p = int(params.get("slow", 21))
    closes = [b["close"] for b in bars]
    fast = _ema(closes, fast_p)
    slow = _ema(closes, slow_p)
    signals = [0] * len(bars)
    for i in range(1, len(bars)):
        if fast[i] is None or slow[i] is None or fast[i - 1] is None or slow[i - 1] is None:
            continue
        # Crossover: fast crosses above slow -> buy
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            signals[i] = 1
        # Crossunder: fast crosses below slow -> sell
        elif fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            signals[i] = -1
    return signals


def _strategy_rsi_reversal(bars: list[dict], params: dict) -> list[int]:
    period = int(params.get("period", 14))
    oversold = float(params.get("oversold", 30))
    overbought = float(params.get("overbought", 70))
    closes = [b["close"] for b in bars]
    rsi = _rsi(closes, period)
    signals = [0] * len(bars)
    for i in range(1, len(bars)):
        if rsi[i] is None or rsi[i - 1] is None:
            continue
        # RSI crosses up through oversold -> buy
        if rsi[i - 1] <= oversold and rsi[i] > oversold:
            signals[i] = 1
        # RSI crosses down through overbought -> sell
        elif rsi[i - 1] >= overbought and rsi[i] < overbought:
            signals[i] = -1
    return signals


def _strategy_bollinger_bounce(bars: list[dict], params: dict) -> list[int]:
    period = int(params.get("period", 20))
    std = float(params.get("std_dev", 2.0))
    closes = [b["close"] for b in bars]
    upper, middle, lower = _bollinger(closes, period, std)
    signals = [0] * len(bars)
    for i in range(1, len(bars)):
        if lower[i] is None or lower[i - 1] is None:
            continue
        # Price touches/crosses lower band from below -> buy
        if closes[i - 1] >= lower[i - 1] and closes[i] <= lower[i]:
            signals[i] = 1
        # Price touches/crosses upper band from above -> sell
        elif closes[i - 1] <= upper[i - 1] and closes[i] >= upper[i]:
            signals[i] = -1
    return signals


def _strategy_vwap_reversion(bars: list[dict], params: dict) -> list[int]:
    """Buy below VWAP, sell above VWAP (mean reversion)."""
    closes = [b["close"] for b in bars]
    vwap = _vwap_cumulative(bars)
    threshold_pct = float(params.get("threshold", 0.5)) / 100.0  # default 0.5%
    signals = [0] * len(bars)
    for i in range(1, len(bars)):
        if vwap[i] is None or vwap[i] == 0:
            continue
        dev = (closes[i] - vwap[i]) / vwap[i]
        prev_dev = (closes[i - 1] - vwap[i - 1]) / vwap[i - 1] if vwap[i - 1] else 0
        # Cross below VWAP by threshold -> buy
        if prev_dev >= -threshold_pct and dev < -threshold_pct:
            signals[i] = 1
        # Cross above VWAP by threshold -> sell
        elif prev_dev <= threshold_pct and dev > threshold_pct:
            signals[i] = -1
    return signals


_STRATEGY_DISPATCH = {
    "ema_crossover": _strategy_ema_crossover,
    "rsi_reversal": _strategy_rsi_reversal,
    "bollinger_bounce": _strategy_bollinger_bounce,
    "vwap_reversion": _strategy_vwap_reversion,
}


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------

def _compute_metrics(
    trades: list[BacktestTrade],
    equity_curve: list[float],
    initial_capital: float,
) -> dict:
    """Compute all performance metrics from trades and equity curve."""
    total = len(trades)
    if total == 0:
        return {
            "total_return_pct": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe_ratio": 0.0,
            "avg_trade_duration_bars": 0.0,
            "total_trades": 0,
            "best_trade_pct": 0.0,
            "worst_trade_pct": 0.0,
        }

    winners = [t for t in trades if t.is_winner]
    losers = [t for t in trades if not t.is_winner]

    gross_profit = sum(t.pnl for t in winners) if winners else 0.0
    gross_loss = abs(sum(t.pnl for t in losers)) if losers else 0.0

    final = equity_curve[-1] if equity_curve else initial_capital
    total_return_pct = ((final - initial_capital) / initial_capital) * 100.0

    win_rate = (len(winners) / total) * 100.0

    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 else 0.0
    )

    # Max drawdown
    peak = initial_capital
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    max_drawdown_pct = max_dd * 100.0

    # Sharpe ratio (simple: annualized using bar returns)
    if len(equity_curve) > 1:
        returns = [
            (equity_curve[i] - equity_curve[i - 1]) / equity_curve[i - 1]
            for i in range(1, len(equity_curve))
            if equity_curve[i - 1] > 0
        ]
        if returns:
            avg_r = sum(returns) / len(returns)
            std_r = math.sqrt(sum((r - avg_r) ** 2 for r in returns) / len(returns))
            sharpe = (avg_r / std_r) * math.sqrt(252) if std_r > 0 else 0.0
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    avg_duration = sum(t.bars_held for t in trades) / total
    best_pct = max(t.pnl_pct for t in trades)
    worst_pct = min(t.pnl_pct for t in trades)

    return {
        "total_return_pct": round(total_return_pct, 2),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe, 2),
        "avg_trade_duration_bars": round(avg_duration, 1),
        "total_trades": total,
        "best_trade_pct": round(best_pct, 2),
        "worst_trade_pct": round(worst_pct, 2),
    }


# ---------------------------------------------------------------------------
# Backtester class
# ---------------------------------------------------------------------------

class Backtester:
    """On-demand strategy backtester using Kraken public OHLCV data."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        fee_pct: float = 0.26,
        initial_capital: float = 10_000.0,
    ):
        self.http = http_client
        self.fee_pct = fee_pct / 100.0  # convert to decimal (0.26% -> 0.0026)
        self.initial_capital = initial_capital
        self._last_request_ts: float = 0.0

    # ------------------------------------------------------------------
    # Historical data
    # ------------------------------------------------------------------

    async def fetch_historical(
        self,
        pair: str,
        interval: int = 60,
        since_hours: int = 168,
    ) -> list[dict]:
        """
        Fetch OHLCV candles from Kraken public API.

        Args:
            pair: Display pair like "BTC/USD"
            interval: Candle interval in minutes (15, 60, 240, 1440)
            since_hours: How many hours of history to fetch (default 168 = 7 days)

        Returns:
            List of dicts with keys: time, open, high, low, close, volume
        """
        if interval not in VALID_INTERVALS:
            raise ValueError(
                f"Invalid interval {interval}. Must be one of: {sorted(VALID_INTERVALS)}"
            )

        sym_info = SYMBOL_MAP.get(pair)
        if sym_info is None:
            raise ValueError(
                f"Unknown pair '{pair}'. Supported: {', '.join(SYMBOL_MAP.keys())}"
            )
        kraken_pair = sym_info["kraken"]

        since_ts = int(time.time()) - (since_hours * 3600)
        all_bars: list[dict] = []

        while True:
            # Rate-limit: at least 1s between requests
            elapsed = time.time() - self._last_request_ts
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)

            self._last_request_ts = time.time()

            try:
                resp = await self.http.get(
                    KRAKEN_OHLC_URL,
                    params={
                        "pair": kraken_pair,
                        "interval": interval,
                        "since": since_ts,
                    },
                    timeout=30.0,
                )
                data = resp.json()
            except Exception as exc:
                logger.error("Kraken OHLC request failed: %s", exc)
                break

            errors = data.get("error", [])
            if errors:
                # Rate limited — back off and retry once
                if any("EGeneral" in e or "Rate" in e for e in errors):
                    logger.warning("Kraken rate limit hit, backing off 3s")
                    await asyncio.sleep(3.0)
                    continue
                logger.error("Kraken OHLC error: %s", errors)
                break

            result = data.get("result", {})
            pair_key = next((k for k in result if k != "last"), None)
            if not pair_key:
                break

            raw = result[pair_key]
            for candle in raw:
                ts = int(candle[0])
                all_bars.append({
                    "time": ts,
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[6]),
                })

            last = result.get("last", 0)
            # Kraken returns up to 720 candles; if we got fewer, we're done
            if len(raw) < 720 or last <= since_ts:
                break
            since_ts = last

        # De-duplicate by timestamp and sort
        seen = set()
        unique: list[dict] = []
        for bar in all_bars:
            if bar["time"] not in seen:
                seen.add(bar["time"])
                unique.append(bar)
        unique.sort(key=lambda b: b["time"])

        logger.info(
            "Fetched %d bars for %s (%dm) over last %dh",
            len(unique), pair, interval, since_hours,
        )
        return unique

    # ------------------------------------------------------------------
    # Backtest engine
    # ------------------------------------------------------------------

    def run_backtest(
        self,
        bars: list[dict],
        strategy: str,
        params: dict | None = None,
        pair: str = "BTC/USD",
        interval: int = 60,
        hours: int = 168,
    ) -> BacktestResult:
        """
        Run a strategy against historical bars.

        Args:
            bars: OHLCV bar list from fetch_historical
            strategy: One of STRATEGY_NAMES
            params: Strategy-specific parameters
            pair: Display pair (for result metadata)
            interval: Interval in minutes (for result metadata)
            hours: Lookback hours (for result metadata)

        Returns:
            BacktestResult with all metrics computed
        """
        params = params or {}

        if strategy not in _STRATEGY_DISPATCH:
            return BacktestResult(
                strategy=strategy,
                pair=pair,
                interval=interval,
                hours=hours,
                params=params,
                error=f"Unknown strategy '{strategy}'. Available: {', '.join(STRATEGY_NAMES)}",
            )

        if len(bars) < 30:
            return BacktestResult(
                strategy=strategy,
                pair=pair,
                interval=interval,
                hours=hours,
                params=params,
                error=f"Not enough data: {len(bars)} bars (need at least 30).",
            )

        # Generate signals
        signals = _STRATEGY_DISPATCH[strategy](bars, params)

        # Simulate trades
        capital = self.initial_capital
        position: dict | None = None  # {entry_price, entry_time, entry_idx, qty}
        trades: list[BacktestTrade] = []
        equity_curve: list[float] = []

        for i, bar in enumerate(bars):
            sig = signals[i]

            # --- Exit logic ---
            if position is not None and sig == -1:
                exit_price = bar["close"]
                qty = position["qty"]
                gross = qty * exit_price
                exit_fee = gross * self.fee_pct
                net_proceeds = gross - exit_fee
                capital += net_proceeds

                entry_cost = position["entry_price"] * qty
                pnl = net_proceeds - entry_cost - position["entry_fee"]
                pnl_pct = (pnl / (entry_cost + position["entry_fee"])) * 100.0

                trades.append(BacktestTrade(
                    entry_time=position["entry_time"],
                    exit_time=bar["time"],
                    side="long",
                    entry_price=position["entry_price"],
                    exit_price=exit_price,
                    quantity=qty,
                    fee_paid=position["entry_fee"] + exit_fee,
                    pnl=round(pnl, 2),
                    pnl_pct=round(pnl_pct, 2),
                    bars_held=i - position["entry_idx"],
                ))
                position = None

            # --- Entry logic ---
            if position is None and sig == 1:
                entry_price = bar["close"]
                # Use full capital for position sizing (simplified)
                gross_cost = capital
                entry_fee = gross_cost * self.fee_pct
                investable = gross_cost - entry_fee
                qty = investable / entry_price if entry_price > 0 else 0

                if qty > 0:
                    capital = 0.0  # fully invested
                    position = {
                        "entry_price": entry_price,
                        "entry_time": bar["time"],
                        "entry_idx": i,
                        "qty": qty,
                        "entry_fee": entry_fee,
                    }

            # Track equity
            if position is not None:
                mark = position["qty"] * bar["close"]
                equity_curve.append(capital + mark)
            else:
                equity_curve.append(capital)

        # Close any open position at last bar
        if position is not None:
            last_bar = bars[-1]
            exit_price = last_bar["close"]
            qty = position["qty"]
            gross = qty * exit_price
            exit_fee = gross * self.fee_pct
            net_proceeds = gross - exit_fee
            capital += net_proceeds

            entry_cost = position["entry_price"] * qty
            pnl = net_proceeds - entry_cost - position["entry_fee"]
            pnl_pct = (pnl / (entry_cost + position["entry_fee"])) * 100.0

            trades.append(BacktestTrade(
                entry_time=position["entry_time"],
                exit_time=last_bar["time"],
                side="long",
                entry_price=position["entry_price"],
                exit_price=exit_price,
                quantity=qty,
                fee_paid=position["entry_fee"] + exit_fee,
                pnl=round(pnl, 2),
                pnl_pct=round(pnl_pct, 2),
                bars_held=len(bars) - 1 - position["entry_idx"],
            ))
            position = None
            # Update last equity point
            if equity_curve:
                equity_curve[-1] = capital

        # Compute metrics
        metrics = _compute_metrics(trades, equity_curve, self.initial_capital)

        return BacktestResult(
            strategy=strategy,
            pair=pair,
            interval=interval,
            hours=hours,
            params=params,
            trades=trades,
            equity_curve=equity_curve,
            initial_capital=self.initial_capital,
            final_capital=round(capital, 2),
            **metrics,
        )

    # ------------------------------------------------------------------
    # Formatting for AI context
    # ------------------------------------------------------------------

    def format_results(self, result: BacktestResult) -> str:
        """Format a single backtest result as a concise string for Claude context."""
        if result.error:
            return f"BACKTEST ERROR ({result.strategy} on {result.pair}): {result.error}"

        interval_labels = {15: "15m", 60: "1h", 240: "4h", 1440: "1d"}
        tf = interval_labels.get(result.interval, f"{result.interval}m")

        params_str = ", ".join(f"{k}={v}" for k, v in result.params.items())
        header = f"{result.strategy} on {result.pair} {tf} over last {result.hours}h"
        if params_str:
            header += f" ({params_str})"

        lines = [
            f"## Backtest: {header}",
            f"Return: {result.total_return_pct:+.2f}% | "
            f"Trades: {result.total_trades} | "
            f"Win rate: {result.win_rate:.1f}%",
            f"Profit factor: {result.profit_factor:.2f} | "
            f"Max drawdown: {result.max_drawdown_pct:.2f}% | "
            f"Sharpe: {result.sharpe_ratio:.2f}",
            f"Best trade: {result.best_trade_pct:+.2f}% | "
            f"Worst trade: {result.worst_trade_pct:+.2f}% | "
            f"Avg duration: {result.avg_trade_duration_bars:.1f} bars",
            f"Capital: ${result.initial_capital:,.0f} -> ${result.final_capital:,.2f}",
        ]

        # Trade log (abbreviated — last 10 trades max)
        if result.trades:
            lines.append("")
            show = result.trades[-10:] if len(result.trades) > 10 else result.trades
            if len(result.trades) > 10:
                lines.append(f"Last 10 of {len(result.trades)} trades:")
            for t in show:
                entry_dt = datetime.fromtimestamp(t.entry_time, tz=timezone.utc).strftime("%m/%d %H:%M")
                exit_dt = datetime.fromtimestamp(t.exit_time, tz=timezone.utc).strftime("%m/%d %H:%M")
                emoji = "W" if t.is_winner else "L"
                lines.append(
                    f"  [{emoji}] {entry_dt}->{exit_dt} "
                    f"${t.entry_price:,.2f}->${t.exit_price:,.2f} "
                    f"P&L: {t.pnl_pct:+.2f}% (${t.pnl:+,.2f})"
                )

        return "\n".join(lines)

    def compare_strategies(self, results: list[BacktestResult]) -> str:
        """Compare multiple backtest results side by side for Claude context."""
        if not results:
            return "No backtest results to compare."

        interval_labels = {15: "15m", 60: "1h", 240: "4h", 1440: "1d"}

        lines = ["## Strategy Comparison", ""]

        # Header
        lines.append(
            f"{'Strategy':<22} {'Return':>8} {'Trades':>7} {'Win%':>6} "
            f"{'PF':>6} {'MaxDD':>7} {'Sharpe':>7} {'Best':>7} {'Worst':>7}"
        )
        lines.append("-" * 90)

        for r in results:
            if r.error:
                lines.append(f"{r.strategy:<22} ERROR: {r.error}")
                continue

            tf = interval_labels.get(r.interval, f"{r.interval}m")
            params_short = "/".join(str(v) for v in r.params.values())
            label = f"{r.strategy}"
            if params_short:
                label += f"({params_short})"
            label = label[:21]

            lines.append(
                f"{label:<22} {r.total_return_pct:>+7.2f}% {r.total_trades:>6}  "
                f"{r.win_rate:>5.1f}% {r.profit_factor:>5.2f} "
                f"{r.max_drawdown_pct:>6.2f}% {r.sharpe_ratio:>6.2f} "
                f"{r.best_trade_pct:>+6.2f}% {r.worst_trade_pct:>+6.2f}%"
            )

        # Recommendation
        valid = [r for r in results if r.error is None and r.total_trades > 0]
        if valid:
            best_return = max(valid, key=lambda r: r.total_return_pct)
            best_sharpe = max(valid, key=lambda r: r.sharpe_ratio)
            lines.append("")
            lines.append(
                f"Best return: {best_return.strategy} ({best_return.total_return_pct:+.2f}%) | "
                f"Best risk-adjusted: {best_sharpe.strategy} (Sharpe {best_sharpe.sharpe_ratio:.2f})"
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Action tag parser
# ---------------------------------------------------------------------------

def parse_backtest_tag(tag_content: str) -> dict:
    """
    Parse a [BACKTEST: ...] action tag into parameters.

    Example input (the content between [ and ]):
        "BACKTEST: strategy=ema_crossover, pair=BTC/USD, interval=60, hours=168, fast=9, slow=21"

    Returns dict with keys: strategy, pair, interval, hours, plus any
    strategy-specific params.
    """
    # Strip prefix
    text = tag_content.strip()
    if text.upper().startswith("BACKTEST:"):
        text = text[len("BACKTEST:"):].strip()

    parts = [p.strip() for p in text.split(",")]
    raw: dict[str, str] = {}
    for part in parts:
        if "=" in part:
            key, val = part.split("=", 1)
            raw[key.strip().lower()] = val.strip()

    # Extract known top-level params
    strategy = raw.pop("strategy", "ema_crossover")
    pair = raw.pop("pair", "BTC/USD")
    interval = int(raw.pop("interval", "60"))
    hours = int(raw.pop("hours", "168"))

    # Everything else is strategy params — try to convert to numbers
    strategy_params: dict = {}
    for k, v in raw.items():
        try:
            if "." in v:
                strategy_params[k] = float(v)
            else:
                strategy_params[k] = int(v)
        except ValueError:
            strategy_params[k] = v

    return {
        "strategy": strategy,
        "pair": pair,
        "interval": interval,
        "hours": hours,
        "params": strategy_params,
    }


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------

async def run_backtest_from_tag(
    tag_content: str,
    http_client: httpx.AsyncClient | None = None,
    fee_pct: float = 0.26,
) -> str:
    """
    One-shot convenience: parse tag, fetch data, run backtest, return formatted result.

    Usage:
        result_text = await run_backtest_from_tag(
            "strategy=ema_crossover, pair=BTC/USD, interval=60, hours=168, fast=9, slow=21"
        )
    """
    parsed = parse_backtest_tag(tag_content)

    own_client = http_client is None
    if own_client:
        http_client = httpx.AsyncClient()

    try:
        bt = Backtester(http_client, fee_pct=fee_pct)

        bars = await bt.fetch_historical(
            pair=parsed["pair"],
            interval=parsed["interval"],
            since_hours=parsed["hours"],
        )

        result = bt.run_backtest(
            bars=bars,
            strategy=parsed["strategy"],
            params=parsed["params"],
            pair=parsed["pair"],
            interval=parsed["interval"],
            hours=parsed["hours"],
        )

        return bt.format_results(result)
    except Exception as exc:
        logger.exception("Backtest failed: %s", exc)
        return f"BACKTEST ERROR: {exc}"
    finally:
        if own_client:
            await http_client.aclose()
