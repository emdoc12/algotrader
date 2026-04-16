"""
Backtest Engine
===============
Polls the API for pending backtest jobs and runs them.
Supports Kraken crypto strategies: crypto_momentum, crypto_mean_reversion.

Flow:
  1. Poll GET /api/backtests every 10s for status=pending jobs
  2. Fetch OHLCV from Kraken REST API (no auth needed for public data)
  3. Simulate strategy logic bar-by-bar
  4. PATCH /api/backtests/:id with results (equity curve, trade log, stats)

Kraken OHLCV endpoint: https://api.kraken.com/0/public/OHLC
Intervals: 1=1m, 5=5m, 15=15m, 60=1h, 240=4h, 1440=1d
"""
import asyncio
import json
import logging
import math
from datetime import datetime, timezone

import httpx

import api_client

logger = logging.getLogger("backtest_engine")

KRAKEN_PUBLIC = "https://api.kraken.com/0/public"

# Map strategy symbol names → Kraken pairs
SYMBOL_MAP = {
    "BTC": "XXBTZUSD",
    "ETH": "XETHZUSD",
    "SOL": "SOLUSD",
    "MATIC": "MATICUSD",
    "ADA": "ADAUSD",
    "DOT": "DOTUSD",
    "AVAX": "AVAXUSD",
    "LINK": "LINKUSD",
    "DOGE": "XDGUSD",
    "XRP": "XXRPZUSD",
}

# Default symbol if none in params
DEFAULT_PAIR = "XETHZUSD"


async def fetch_ohlcv(pair: str, start_ts: int, end_ts: int, interval: int = 1440) -> list[dict]:
    """
    Fetch daily OHLCV bars from Kraken public API.
    Returns list of {time, open, high, low, close, volume} dicts.
    """
    bars = []
    since = start_ts
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                f"{KRAKEN_PUBLIC}/OHLC",
                params={"pair": pair, "interval": interval, "since": since},
            )
            data = resp.json()
            if data.get("error"):
                raise ValueError(f"Kraken OHLC error: {data['error']}")

            result = data.get("result", {})
            pair_key = next((k for k in result if k != "last"), None)
            if not pair_key:
                break

            raw = result[pair_key]
            for bar in raw:
                ts = int(bar[0])
                if ts > end_ts:
                    break
                bars.append({
                    "time": ts,
                    "open": float(bar[1]),
                    "high": float(bar[2]),
                    "low": float(bar[3]),
                    "close": float(bar[4]),
                    "volume": float(bar[6]),
                })

            last = result.get("last", 0)
            if not raw or last <= since:
                break
            since = last
            await asyncio.sleep(0.5)  # be polite to public API

    return bars


def calc_sma(closes: list[float], period: int, idx: int) -> float | None:
    if idx < period - 1:
        return None
    return sum(closes[idx - period + 1 : idx + 1]) / period


def simulate_momentum(bars: list[dict], params: dict, initial_capital: float) -> dict:
    """
    Crypto Momentum strategy:
    - Enter LONG when close > SMA(maPeriod) and today's change > breakoutPercent
    - Exit when stop-loss or take-profit hit (using next bar open as fill)
    """
    ma_period = int(params.get("maPeriod", 20))
    breakout_pct = float(params.get("breakoutPercent", 2)) / 100
    stop_loss_pct = float(params.get("stopLossPercent", 3)) / 100
    take_profit_pct = float(params.get("takeProfitPercent", 6)) / 100

    closes = [b["close"] for b in bars]
    capital = initial_capital
    position = None  # {entry_price, stop, target, qty, entry_date}
    trade_log = []
    equity_curve = []

    for i, bar in enumerate(bars):
        date_str = datetime.fromtimestamp(bar["time"], tz=timezone.utc).strftime("%Y-%m-%d")

        # Check exit first
        if position:
            low = bar["low"]
            high = bar["high"]
            pnl = 0.0
            exited = False

            if low <= position["stop"]:
                exit_price = position["stop"]
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                capital += pnl
                trade_log.append({
                    "date": date_str,
                    "action": "SELL",
                    "price": exit_price,
                    "qty": position["qty"],
                    "pnl": round(pnl, 2),
                    "reason": "stop_loss",
                })
                position = None
                exited = True
            elif high >= position["target"]:
                exit_price = position["target"]
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                capital += pnl
                trade_log.append({
                    "date": date_str,
                    "action": "SELL",
                    "price": exit_price,
                    "qty": position["qty"],
                    "pnl": round(pnl, 2),
                    "reason": "take_profit",
                })
                position = None
                exited = True

        # Check entry
        sma = calc_sma(closes, ma_period, i)
        if position is None and sma is not None and i > 0:
            prev_close = closes[i - 1]
            change = (bar["close"] - prev_close) / prev_close if prev_close else 0
            if bar["close"] > sma and change >= breakout_pct:
                entry_price = bar["close"]
                risk_per_unit = entry_price * stop_loss_pct
                # Risk 2% of capital per trade
                risk_capital = capital * 0.02
                qty = risk_capital / risk_per_unit if risk_per_unit > 0 else 0
                if qty > 0 and capital > entry_price * qty:
                    capital -= entry_price * qty
                    position = {
                        "entry_price": entry_price,
                        "stop": entry_price * (1 - stop_loss_pct),
                        "target": entry_price * (1 + take_profit_pct),
                        "qty": qty,
                        "entry_date": date_str,
                    }
                    trade_log.append({
                        "date": date_str,
                        "action": "BUY",
                        "price": entry_price,
                        "qty": round(qty, 6),
                        "pnl": 0,
                        "reason": "breakout",
                    })

        mark = (capital + position["qty"] * bar["close"]) if position else capital
        equity_curve.append({"date": date_str, "equity": round(mark, 2)})

    # Close any open position at last bar
    if position:
        last = bars[-1]
        exit_price = last["close"]
        pnl = (exit_price - position["entry_price"]) * position["qty"]
        capital += exit_price * position["qty"]
        trade_log.append({
            "date": equity_curve[-1]["date"],
            "action": "SELL",
            "price": exit_price,
            "qty": position["qty"],
            "pnl": round(pnl, 2),
            "reason": "end_of_backtest",
        })

    return {"trades": trade_log, "equity_curve": equity_curve, "final_capital": capital}


def simulate_mean_reversion(bars: list[dict], params: dict, initial_capital: float) -> dict:
    """
    Crypto Mean Reversion strategy:
    - Enter LONG when close < SMA(maPeriod) * (1 - deviationPercent/100)
    - Exit at SMA or stop-loss
    """
    ma_period = int(params.get("maPeriod", 50))
    deviation_pct = float(params.get("deviationPercent", 5)) / 100
    stop_loss_pct = float(params.get("stopLossPercent", 3)) / 100
    take_profit_pct = float(params.get("takeProfitPercent", 4)) / 100

    closes = [b["close"] for b in bars]
    capital = initial_capital
    position = None
    trade_log = []
    equity_curve = []

    for i, bar in enumerate(bars):
        date_str = datetime.fromtimestamp(bar["time"], tz=timezone.utc).strftime("%Y-%m-%d")
        sma = calc_sma(closes, ma_period, i)

        # Exit
        if position:
            low = bar["low"]
            high = bar["high"]
            exited = False
            pnl = 0.0

            if low <= position["stop"]:
                exit_price = position["stop"]
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                capital += exit_price * position["qty"]
                trade_log.append({
                    "date": date_str,
                    "action": "SELL",
                    "price": exit_price,
                    "qty": position["qty"],
                    "pnl": round(pnl, 2),
                    "reason": "stop_loss",
                })
                position = None
                exited = True
            elif sma and high >= sma:
                exit_price = sma
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                capital += exit_price * position["qty"]
                trade_log.append({
                    "date": date_str,
                    "action": "SELL",
                    "price": exit_price,
                    "qty": position["qty"],
                    "pnl": round(pnl, 2),
                    "reason": "reversion_to_mean",
                })
                position = None
                exited = True

        # Entry
        if position is None and sma is not None:
            threshold = sma * (1 - deviation_pct)
            if bar["close"] <= threshold:
                entry_price = bar["close"]
                risk_per_unit = entry_price * stop_loss_pct
                risk_capital = capital * 0.02
                qty = risk_capital / risk_per_unit if risk_per_unit > 0 else 0
                if qty > 0 and capital > entry_price * qty:
                    capital -= entry_price * qty
                    position = {
                        "entry_price": entry_price,
                        "stop": entry_price * (1 - stop_loss_pct),
                        "target": entry_price * (1 + take_profit_pct),
                        "qty": qty,
                        "entry_date": date_str,
                    }
                    trade_log.append({
                        "date": date_str,
                        "action": "BUY",
                        "price": entry_price,
                        "qty": round(qty, 6),
                        "pnl": 0,
                        "reason": "mean_reversion_dip",
                    })

        mark = (capital + position["qty"] * bar["close"]) if position else capital
        equity_curve.append({"date": date_str, "equity": round(mark, 2)})

    # Close open position at end
    if position:
        last = bars[-1]
        exit_price = last["close"]
        pnl = (exit_price - position["entry_price"]) * position["qty"]
        capital += exit_price * position["qty"]
        trade_log.append({
            "date": equity_curve[-1]["date"],
            "action": "SELL",
            "price": exit_price,
            "qty": position["qty"],
            "pnl": round(pnl, 2),
            "reason": "end_of_backtest",
        })

    return {"trades": trade_log, "equity_curve": equity_curve, "final_capital": capital}


def calc_stats(trade_log: list[dict], equity_curve: list[dict], initial_capital: float) -> dict:
    """Compute win rate, max drawdown, Sharpe ratio."""
    closed = [t for t in trade_log if t["action"] == "SELL"]
    total = len(closed)
    winners = [t for t in closed if t["pnl"] > 0]
    losers = [t for t in closed if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in closed)
    win_rate = len(winners) / total if total > 0 else 0

    # Max drawdown
    peak = initial_capital
    max_dd = 0.0
    for pt in equity_curve:
        eq = pt["equity"]
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Daily returns for Sharpe
    equities = [pt["equity"] for pt in equity_curve]
    if len(equities) > 1:
        returns = [(equities[i] - equities[i - 1]) / equities[i - 1] for i in range(1, len(equities))]
        avg_r = sum(returns) / len(returns)
        std_r = math.sqrt(sum((r - avg_r) ** 2 for r in returns) / len(returns)) if len(returns) > 1 else 0
        sharpe = (avg_r / std_r) * math.sqrt(252) if std_r > 0 else 0
    else:
        sharpe = 0

    return {
        "totalTrades": total,
        "winningTrades": len(winners),
        "losingTrades": len(losers),
        "totalPnl": round(total_pnl, 2),
        "maxDrawdown": round(max_dd * 100, 2),  # as percentage
        "winRate": round(win_rate * 100, 2),     # as percentage
        "sharpeRatio": round(sharpe, 3),
    }


async def run_backtest(bt: dict):
    """Run a single backtest job end-to-end."""
    bt_id = bt["id"]
    params = json.loads(bt.get("parameters", "{}"))
    strategy_type = bt["strategyType"]
    start_date = bt["startDate"]
    end_date = bt["endDate"]
    initial_capital = float(params.get("initialCapital", 10000))

    logger.info("Running backtest %d: %s %s→%s", bt_id, strategy_type, start_date, end_date)

    # Mark as running
    await api_client.patch_backtest(bt_id, {"status": "running"})

    try:
        # Determine pair
        symbol = params.get("symbol", "ETH").upper()
        pair = SYMBOL_MAP.get(symbol, DEFAULT_PAIR)

        start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

        bars = await fetch_ohlcv(pair, start_ts, end_ts, interval=1440)
        if len(bars) < 10:
            raise ValueError(f"Not enough historical data ({len(bars)} bars). Try a wider date range.")

        if strategy_type == "crypto_momentum":
            result = simulate_momentum(bars, params, initial_capital)
        elif strategy_type == "crypto_mean_reversion":
            result = simulate_mean_reversion(bars, params, initial_capital)
        else:
            raise ValueError(f"Backtest not supported for strategy type '{strategy_type}'. Only Kraken crypto strategies are supported.")

        stats = calc_stats(result["trades"], result["equity_curve"], initial_capital)

        await api_client.patch_backtest(bt_id, {
            "status": "completed",
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "trades": json.dumps(result["trades"]),
            "equityCurve": json.dumps(result["equity_curve"]),
            **stats,
        })
        logger.info(
            "Backtest %d complete: %d trades, PnL=%.2f, WinRate=%.1f%%",
            bt_id, stats["totalTrades"], stats["totalPnl"], stats["winRate"],
        )

    except Exception as e:
        logger.exception("Backtest %d failed: %s", bt_id, e)
        await api_client.patch_backtest(bt_id, {
            "status": "failed",
            "errorMessage": str(e),
            "completedAt": datetime.now(timezone.utc).isoformat(),
        })


class BacktestEngine:
    """Polls for pending backtest jobs and runs them one at a time."""

    def __init__(self):
        self._running = False
        self._active: set[int] = set()

    async def start(self):
        self._running = True
        logger.info("Backtest engine started — polling every 10s")
        while self._running:
            try:
                backtests = await api_client.get_backtests()
                pending = [b for b in backtests if b["status"] == "pending" and b["id"] not in self._active]
                for bt in pending:
                    self._active.add(bt["id"])
                    asyncio.create_task(self._run(bt))
            except Exception as e:
                logger.exception("Backtest poll error: %s", e)
            await asyncio.sleep(10)

    async def _run(self, bt: dict):
        try:
            await run_backtest(bt)
        finally:
            self._active.discard(bt["id"])

    def stop(self):
        self._running = False
