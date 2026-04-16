"""
Trading strategy that decides when to buy and sell BTC.

Uses the combined indicator signals (EMA crossover + RSI + Bollinger Bands)
plus risk management (stop-loss, take-profit, position sizing).
"""

import json
import logging
import time
from decimal import Decimal
from typing import Optional

from config import BotConfig
from database import Database, Position, Trade
from indicators import Signals, generate_signals
from kraken_client import KrakenClient, OHLCV
from paper_trader import PaperTrader

logger = logging.getLogger(__name__)


class TradingStrategy:
    """
    BTC trading strategy with signal-based entries and risk-managed exits.

    Entry rules:
        - BUY when composite signal is BUY or STRONG_BUY
        - Only if we have no open position
        - Only if we have enough capital

    Exit rules:
        - SELL when composite signal is SELL or STRONG_SELL
        - SELL if stop-loss is hit (price drops X% below entry)
        - SELL if take-profit is hit (price rises X% above entry)

    Position sizing:
        - Risk a fixed % of capital per trade
        - Never exceed max_position_pct of total equity
    """

    def __init__(self, config: BotConfig, db: Database,
                 kraken: KrakenClient, paper_trader: Optional[PaperTrader] = None):
        self.config = config
        self.db = db
        self.kraken = kraken
        self.paper_trader = paper_trader
        self.is_paper = config.mode == "paper"
        self._last_context = ""  # Compatibility with chat context cache

    async def run_scan(self) -> dict:
        """
        Run one scan cycle: fetch data, compute signals, decide action.

        Returns a dict with scan results for logging.
        """
        sc = self.config.strategy

        # --- Fetch OHLCV data ---
        try:
            bars = await self.kraken.get_ohlcv(
                interval=self.config.candle_interval,
                count=sc.history_bars,
            )
        except Exception as e:
            logger.error(f"Failed to fetch OHLCV data: {e}")
            return {"error": str(e), "action": "none"}

        if len(bars) < max(sc.ema_slow_period, sc.bb_period, sc.rsi_period + 1):
            logger.warning(f"Not enough candle data ({len(bars)} bars), waiting...")
            return {"action": "none", "reason": "insufficient_data", "bars": len(bars)}

        # --- Compute signals ---
        closes = [bar.close for bar in bars]
        try:
            signals = generate_signals(
                prices=closes,
                ema_fast_period=sc.ema_fast_period,
                ema_slow_period=sc.ema_slow_period,
                rsi_period=sc.rsi_period,
                rsi_overbought=sc.rsi_overbought,
                rsi_oversold=sc.rsi_oversold,
                bb_period=sc.bb_period,
                bb_std_dev=sc.bb_std_dev,
            )
        except Exception as e:
            logger.error(f"Failed to compute signals: {e}")
            return {"error": str(e), "action": "none"}

        # --- Get current ticker for live price ---
        try:
            ticker = await self.kraken.get_ticker()
            current_price = float(ticker.mid)
        except Exception as e:
            logger.error(f"Failed to fetch ticker: {e}")
            current_price = signals.price  # fall back to last candle close

        # --- Check current position ---
        position = self.db.get_open_position()

        # --- Update equity ---
        if self.is_paper and self.paper_trader:
            self.paper_trader.update_equity(current_price)

        signals_json = json.dumps({
            "price": current_price,
            "ema_fast": round(signals.ema.fast_ema, 2),
            "ema_slow": round(signals.ema.slow_ema, 2),
            "ema_crossover": signals.ema.crossover,
            "rsi": round(signals.rsi.rsi, 2),
            "rsi_signal": signals.rsi.signal,
            "bb_upper": round(signals.bollinger.upper, 2),
            "bb_lower": round(signals.bollinger.lower, 2),
            "bb_position": round(signals.bollinger.price_position, 4),
            "composite": signals.composite_score,
            "recommendation": signals.recommendation,
        })

        result = {
            "price": current_price,
            "signals": signals,
            "recommendation": signals.recommendation,
            "composite_score": signals.composite_score,
            "has_position": position is not None,
        }

        # --- Decision logic ---
        if position is None:
            # No position — look for entry
            action = await self._check_entry(signals, current_price, signals_json)
            result["action"] = action
        else:
            # Have position — check exit conditions
            action = await self._check_exit(position, signals, current_price, signals_json)
            result["action"] = action
            result["position_entry"] = position.entry_price
            result["unrealized_pnl_pct"] = (
                (current_price - position.entry_price) / position.entry_price * 100
            )

        return result

    async def _check_entry(self, signals: Signals, price: float, signals_json: str) -> str:
        """Check if we should open a new position."""
        rec = signals.recommendation
        if rec not in ("BUY", "STRONG_BUY"):
            return "hold"

        # Calculate position size
        quantity = self._calculate_position_size(price)
        if quantity <= 0:
            logger.info("Position size too small, skipping entry")
            return "skip_small_position"

        try:
            if self.is_paper and self.paper_trader:
                trade = self.paper_trader.execute_buy(
                    price=price,
                    quantity=quantity,
                    strategy="btc_signals",
                    signals_json=signals_json,
                )
            else:
                # Live trading
                result = await self.kraken.place_market_order(
                    side="buy",
                    volume=Decimal(str(round(quantity, 8))),
                    validate=(self.config.mode != "live"),
                )
                trade_record = Trade(
                    timestamp=time.time(), side="buy", price=price,
                    quantity=quantity, value=price * quantity,
                    order_id=result.order_id, mode="live",
                    strategy="btc_signals", signals=signals_json,
                    status=result.status,
                )
                self.db.record_trade(trade_record)

            # Save position with stop-loss and take-profit
            sc = self.config.strategy
            position = Position(
                symbol=self.config.kraken.display_symbol,
                side="long",
                entry_price=price,
                quantity=quantity,
                entry_time=time.time(),
                stop_loss=price * (1 - sc.stop_loss_pct / 100),
                take_profit=price * (1 + sc.take_profit_pct / 100),
            )
            self.db.save_position(position)

            self.db.log("TRADE", f"OPENED LONG: {quantity:.6f} BTC @ ${price:,.2f}", signals_json)
            logger.info(
                f"OPENED LONG: {quantity:.6f} BTC @ ${price:,.2f} | "
                f"SL: ${position.stop_loss:,.2f} | TP: ${position.take_profit:,.2f}"
            )
            return "buy"

        except Exception as e:
            logger.error(f"Failed to execute buy: {e}")
            self.db.log("ERROR", f"Buy execution failed: {e}")
            return f"error: {e}"

    async def _check_exit(self, position: Position, signals: Signals,
                          price: float, signals_json: str) -> str:
        """Check if we should close the current position."""
        exit_reason = None

        # Stop-loss check
        if price <= position.stop_loss:
            exit_reason = "stop_loss"
        # Take-profit check
        elif price >= position.take_profit:
            exit_reason = "take_profit"
        # Signal-based exit
        elif signals.recommendation in ("SELL", "STRONG_SELL"):
            exit_reason = "signal_sell"

        if exit_reason is None:
            # Update unrealized P&L
            position.unrealized_pnl = (price - position.entry_price) * position.quantity
            self.db.save_position(position)
            return "hold"

        # Execute sell
        try:
            if self.is_paper and self.paper_trader:
                trade = self.paper_trader.execute_sell(
                    price=price,
                    quantity=position.quantity,
                    strategy="btc_signals",
                    signals_json=signals_json,
                )
            else:
                result = await self.kraken.place_market_order(
                    side="sell",
                    volume=Decimal(str(round(position.quantity, 8))),
                    validate=(self.config.mode != "live"),
                )
                trade_record = Trade(
                    timestamp=time.time(), side="sell", price=price,
                    quantity=position.quantity, value=price * position.quantity,
                    order_id=result.order_id, mode="live",
                    strategy="btc_signals", signals=signals_json,
                    status=result.status,
                )
                self.db.record_trade(trade_record)

            # Calculate P&L
            pnl = (price - position.entry_price) * position.quantity
            pnl_pct = (price - position.entry_price) / position.entry_price * 100

            self.db.close_position(position.id)
            self.db.log(
                "TRADE",
                f"CLOSED LONG ({exit_reason}): {position.quantity:.6f} BTC @ ${price:,.2f} | "
                f"P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%)",
                signals_json,
            )
            logger.info(
                f"CLOSED LONG ({exit_reason}): {position.quantity:.6f} BTC @ ${price:,.2f} | "
                f"P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%)"
            )
            return f"sell ({exit_reason})"

        except Exception as e:
            logger.error(f"Failed to execute sell: {e}")
            self.db.log("ERROR", f"Sell execution failed: {e}")
            return f"error: {e}"

    def _calculate_position_size(self, price: float) -> float:
        """
        Calculate how much BTC to buy based on risk management rules.

        - Risk `risk_per_trade_pct` of capital
        - Never exceed `max_position_pct` of total equity
        """
        sc = self.config.strategy

        if self.is_paper and self.paper_trader:
            available_cash = self.paper_trader.balance.cash_usd
            total_equity = self.paper_trader.balance.total_equity
        else:
            # For live trading, we'd fetch the actual balance from Kraken
            available_cash = 0
            total_equity = 0

        # Max we can allocate based on position size limit
        max_allocation = total_equity * (sc.max_position_pct / 100)
        # Don't exceed available cash
        allocation = min(max_allocation, available_cash)

        if allocation <= 0 or price <= 0:
            return 0.0

        quantity = allocation / price

        # Kraken minimum BTC order is 0.0001
        if quantity < 0.0001:
            return 0.0

        return round(quantity, 8)
