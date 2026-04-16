"""
AI-powered trading strategy using Claude as the decision engine.

Instead of fixed indicator thresholds, this sends all market data to Claude
and lets it reason about what to do. Claude sees:
  - Current price, volume, and technical indicators
  - Fear & Greed Index and trend
  - Recent news headlines
  - Current position and P&L
  - Account balance
  - Full trading history context

Claude returns a structured JSON decision with action, quantity, and reasoning.

DUAL OBJECTIVE: Grow the USD cash balance AND accumulate more Bitcoin over time.
"""

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import httpx

from config import BotConfig
from database import Database, Position, Trade
from indicators import Signals, generate_signals
from kraken_client import KrakenClient, OHLCV
from paper_trader import PaperTrader
from market_scanner import MarketScanner
from sentiment import SentimentFetcher, SentimentData

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert cryptocurrency trader with FULL CONTROL over a BTC/USD portfolio on Kraken.
You run 24/7 and make decisions every scan cycle based on technical analysis, market sentiment, and news.
You are the decision engine. You decide EVERYTHING — position sizing, entries, exits, scaling in/out, risk management.

## YOUR DUAL OBJECTIVE
1. **Grow the USD cash balance** — take profits when conditions warrant
2. **Accumulate more Bitcoin** — buy dips, stack sats when the price is favorable

You must balance these objectives dynamically based on market conditions.

## RISK PROFILE: AGGRESSIVE
- You control your own position sizing — go big on high-conviction setups
- You can scale into positions (buy more to average down or add to winners)
- You can partially sell to lock in profits while keeping exposure
- You use stops and targets flexibly based on your conviction and market structure
- You actively look for momentum trades, mean reversion, and accumulation opportunities
- You manage your own cash reserves — allocate as you see fit

## CRITICAL: FEE AWARENESS (ONE HARD RULE)
Kraken charges a 0.26% taker fee per trade. A full round trip (buy + sell) costs 0.52% in fees.
**THE ONLY HARD CONSTRAINT: sells where profit is under 0.6% will be blocked by the system (except stop-losses).**
Everything else is your call. Factor fees into your decisions:
- Round-trip cost is 0.52%, so any take-profit below that is a net loss
- Quick in-and-out scalps are fee destroyers — make sure the move justifies the cost
- For long-term accumulation buys, fees matter less
- At current BTC prices, 0.26% fee = roughly $180-220 per BTC traded

## YOUR CAPABILITIES
- You can BUY any amount of BTC (limited by available cash)
- You can SELL any amount of BTC you're holding (full or partial sells)
- You can scale into positions — buy more when already holding
- You can average down on dips or add to winning positions
- You can adjust your stop-loss and take-profit levels every scan
- You can set a trailing stop by providing trailing_stop_pct (e.g., 2.0 means the stop follows price at 2% below the highest price since entry)
- Trailing stops automatically ratchet up as price rises — they never move down
- Set trailing_stop_pct to 0 for a fixed stop-loss, or a value like 1.5-3.0 for a trailing stop
- You can hold through volatility or cut losses fast — YOUR CALL
- Minimum order size: 0.0001 BTC
- Only BTC/USD spot (no shorting, no futures)

## RESPONSE FORMAT
You MUST respond with valid JSON only, no other text. Use this exact structure:
{
  "action": "BUY" | "SELL" | "HOLD",
  "quantity": 0.0,
  "stop_loss": 0.0,
  "take_profit": 0.0,
  "trailing_stop_pct": 0.0,
  "confidence": 0.0,
  "reasoning": "2-3 sentence explanation",
  "market_outlook": "bullish" | "bearish" | "neutral",
  "strategy_used": "momentum" | "mean_reversion" | "trend_following" | "sentiment" | "accumulation" | "scaling" | "profit_taking" | "stop_loss"
}

For HOLD actions, quantity/stop_loss/take_profit can be 0.
For BUY when already holding: this ADDS to your position (scaling in). Set updated stop/target for the full position.
For SELL: quantity is how much BTC to sell. Can be partial — sell some, keep some.
Confidence is 0.0 to 1.0 — only act on confidence >= 0.6.
"""


@dataclass
class AIDecision:
    """Structured decision from the AI."""
    action: str = "HOLD"
    quantity: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    trailing_stop_pct: float = 0.0
    confidence: float = 0.0
    reasoning: str = ""
    market_outlook: str = "neutral"
    strategy_used: str = ""
    raw_response: str = ""


class AIStrategy:
    """
    Trading strategy powered by Claude AI.

    Each scan cycle:
    1. Fetches market data (price, OHLCV, indicators)
    2. Fetches sentiment (Fear & Greed, news)
    3. Builds a context prompt with all data
    4. Sends to Claude for a decision
    5. Executes the decision (or holds)
    """

    def __init__(self, config: BotConfig, db: Database,
                 kraken: KrakenClient, paper_trader: Optional[PaperTrader] = None):
        self.config = config
        self.db = db
        self.kraken = kraken
        self.paper_trader = paper_trader
        self.is_paper = config.mode == "paper"
        self.sentiment = SentimentFetcher()
        self._http = httpx.AsyncClient(timeout=60.0)
        self._scanner = MarketScanner(self._http)
        self._last_market_overview = None
        self._last_decision = None
        self._highest_price_since_entry = 0.0
        self._trailing_stop_pct = 0.0

    async def run_scan(self) -> dict:
        """Run one AI-powered scan cycle."""
        sc = self.config.strategy

        # --- 1. Fetch OHLCV data ---
        try:
            bars = await self.kraken.get_ohlcv(
                interval=self.config.candle_interval,
                count=sc.history_bars,
            )
        except Exception as e:
            logger.error(f"Failed to fetch OHLCV: {e}")
            return {"error": str(e), "action": "none"}

        # --- 2. Compute technical indicators ---
        closes = [bar.close for bar in bars]
        signals = None
        if len(closes) >= max(sc.ema_slow_period, sc.bb_period, sc.rsi_period + 1):
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
                logger.warning(f"Indicator computation failed: {e}")

        # --- 3. Get current price ---
        try:
            ticker = await self.kraken.get_ticker()
            current_price = float(ticker.mid)
        except Exception as e:
            logger.error(f"Failed to fetch ticker: {e}")
            current_price = closes[-1] if closes else 0

        # --- 4. Fetch sentiment and multi-coin market data ---
        try:
            sentiment_data = await self.sentiment.fetch_all(ohlcv_bars=bars)
        except Exception as e:
            logger.warning(f"Sentiment fetch failed: {e}")
            sentiment_data = SentimentData(timestamp=time.time())

        try:
            market_overview = await self._scanner.scan_all()
            self._last_market_overview = market_overview
            logger.info(
                f"Market scan: {len(market_overview.coin_snapshots)} coins | "
                f"Momentum: {market_overview.market_momentum} | "
                f"Rotation: {market_overview.sector_rotation_signal}"
            )
        except Exception as e:
            logger.warning(f"Market scan failed: {e}")
            market_overview = None

        # --- 5. Update equity ---
        if self.is_paper and self.paper_trader:
            self.paper_trader.update_equity(current_price)

        # --- 6. Get current state ---
        position = self.db.get_open_position()
        recent_trades = self.db.get_trades(limit=10)
        balance = self.paper_trader.get_balance() if self.paper_trader else None

        # --- 7. Build prompt and call Claude ---
        context = self._build_context(
            current_price, bars, signals, sentiment_data,
            position, recent_trades, balance, market_overview,
        )

        decision = await self._call_claude(context)
        self._last_decision = decision

        # --- 8. Execute decision ---
        # Fee constants
        TAKER_FEE_PCT = 0.26
        ROUND_TRIP_FEE_PCT = TAKER_FEE_PCT * 2  # 0.52% for buy + sell
        MIN_PROFIT_PCT = 0.60  # minimum profit % to justify selling (above round-trip fees)

        action_taken = "hold"
        if decision.confidence >= 0.6:
            if decision.action == "BUY":
                if position is not None:
                    # Scaling in — add to existing position
                    action_taken = await self._execute_scale_in(decision, position, current_price)
                else:
                    # New position
                    action_taken = await self._execute_buy(decision, current_price)

            elif decision.action == "SELL" and position is not None:
                # Fee guard: the ONE hard rule — block sells that don't cover fees
                profit_pct = (current_price - position.entry_price) / position.entry_price * 100
                if 0 < profit_pct < MIN_PROFIT_PCT and decision.strategy_used not in ("stop_loss",):
                    logger.info(
                        f"Blocking sell: {profit_pct:.2f}% gain doesn't cover "
                        f"{ROUND_TRIP_FEE_PCT:.2f}% round-trip fees. Holding."
                    )
                    action_taken = f"blocked_insufficient_profit ({profit_pct:.2f}%)"
                    self.db.log("INFO",
                        f"Sell blocked: {profit_pct:.2f}% profit < {MIN_PROFIT_PCT:.2f}% minimum after fees")
                else:
                    action_taken = await self._execute_sell(decision, position, current_price)

        elif decision.action != "HOLD":
            logger.info(f"AI suggested {decision.action} but confidence too low ({decision.confidence:.2f})")
            action_taken = f"low_confidence_{decision.action.lower()}"

        # --- 9. Check stop-loss / take-profit on existing position ---
        # Claude sets these each scan, so respect them — but Claude can also update them
        if position and action_taken == "hold":
            # Update stop/target if Claude provided new ones (even on HOLD)
            if decision.stop_loss > 0 and decision.action == "HOLD":
                position.stop_loss = decision.stop_loss
            if decision.take_profit > 0 and decision.action == "HOLD":
                position.take_profit = decision.take_profit

            # Trailing stop logic: ratchet stop-loss up as price rises
            if self._trailing_stop_pct > 0:
                self._highest_price_since_entry = max(self._highest_price_since_entry, current_price)
                trailing_stop_price = self._highest_price_since_entry * (1 - self._trailing_stop_pct / 100)
                if trailing_stop_price > position.stop_loss:
                    logger.info(
                        f"Trailing stop ratcheted up: ${position.stop_loss:,.2f} -> ${trailing_stop_price:,.2f} "
                        f"(highest: ${self._highest_price_since_entry:,.2f}, trail: {self._trailing_stop_pct:.1f}%)"
                    )
                    position.stop_loss = trailing_stop_price

            if current_price <= position.stop_loss:
                action_taken = await self._execute_stop_loss(position, current_price)
            elif current_price >= position.take_profit:
                action_taken = await self._execute_take_profit(position, current_price)
            else:
                position.unrealized_pnl = (current_price - position.entry_price) * position.quantity
                self.db.save_position(position)

        # --- Build result ---
        result = {
            "price": current_price,
            "action": action_taken,
            "ai_decision": decision,
            "recommendation": decision.action,
            "composite_score": decision.confidence,
            "has_position": position is not None,
            "signals": signals,
            "sentiment": sentiment_data,
            "market_overview": market_overview,
        }

        if position and action_taken == "hold":
            result["position_entry"] = position.entry_price
            result["unrealized_pnl_pct"] = (
                (current_price - position.entry_price) / position.entry_price * 100
            )

        return result

    def _build_context(self, price, bars, signals, sentiment, position, trades, balance, market_overview=None) -> str:
        """Build the data context string for Claude."""
        parts = []

        # Current price
        parts.append(f"## CURRENT MARKET DATA")
        parts.append(f"BTC/USD Price: ${price:,.2f}")
        parts.append(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")

        # Recent price action (last 10 candles)
        if bars and len(bars) >= 10:
            parts.append(f"\nRecent 15m candles (last 10):")
            for bar in bars[-10:]:
                t = time.strftime('%H:%M', time.gmtime(bar.timestamp))
                parts.append(f"  {t} | O:{bar.open:.0f} H:{bar.high:.0f} L:{bar.low:.0f} C:{bar.close:.0f} V:{bar.volume:.2f}")

        # Technical indicators
        if signals:
            parts.append(f"\n## TECHNICAL INDICATORS")
            parts.append(f"EMA Fast (9): ${signals.ema.fast_ema:,.2f}")
            parts.append(f"EMA Slow (21): ${signals.ema.slow_ema:,.2f}")
            parts.append(f"EMA Crossover: {signals.ema.crossover}")
            parts.append(f"EMA Fast > Slow: {signals.ema.fast_above_slow}")
            parts.append(f"RSI (14): {signals.rsi.rsi:.1f} ({signals.rsi.signal})")
            parts.append(f"Bollinger Upper: ${signals.bollinger.upper:,.2f}")
            parts.append(f"Bollinger Lower: ${signals.bollinger.lower:,.2f}")
            parts.append(f"Bollinger Middle (SMA): ${signals.bollinger.middle:,.2f}")
            parts.append(f"Price position in BB: {signals.bollinger.price_position:.2%}")
            parts.append(f"BB Bandwidth: {signals.bollinger.bandwidth:.4f}")
            parts.append(f"Composite indicator score: {signals.composite_score:+.3f}")

        # Sentiment
        parts.append(f"\n## MARKET SENTIMENT")
        parts.append(f"Fear & Greed Index: {sentiment.fear_greed_value} ({sentiment.fear_greed_label})")
        parts.append(f"Yesterday: {sentiment.fear_greed_yesterday} | Week ago: {sentiment.fear_greed_week_ago}")
        parts.append(f"Volume trend: {sentiment.volume_trend} ({sentiment.volume_24h_change_pct:+.1f}%)")
        parts.append(f"1h momentum: {sentiment.price_momentum_1h:+.2f}%")
        parts.append(f"24h momentum: {sentiment.price_momentum_24h:+.2f}%")
        parts.append(f"News sentiment: {sentiment.news_sentiment_summary}")

        if sentiment.news_headlines:
            parts.append(f"\nRecent headlines:")
            for h in sentiment.news_headlines[:5]:
                parts.append(f"  - {h}")

        # Multi-coin market overview
        if market_overview and market_overview.coin_snapshots:
            parts.append(market_overview.format_for_ai())

        # Account state
        parts.append(f"\n## ACCOUNT STATE")
        if balance:
            parts.append(f"Cash (USD): ${balance.cash_usd:,.2f}")
            parts.append(f"BTC held: {balance.btc_quantity:.6f} BTC (${balance.btc_quantity * price:,.2f})")
            parts.append(f"Total equity: ${balance.total_equity:,.2f}")
            starting = self.config.paper.starting_capital
            pnl = balance.total_equity - starting
            parts.append(f"Total P&L: ${pnl:,.2f} ({pnl/starting*100:+.2f}%)")

        # Current position with fee analysis
        if position:
            upnl = (price - position.entry_price) * position.quantity
            upnl_pct = (price - position.entry_price) / position.entry_price * 100
            buy_fee = position.entry_price * position.quantity * 0.0026
            sell_fee = price * position.quantity * 0.0026
            total_fees = buy_fee + sell_fee
            net_profit = upnl - total_fees
            net_profit_pct = net_profit / (position.entry_price * position.quantity) * 100
            breakeven_price = position.entry_price * 1.0052  # 0.52% above entry

            parts.append(f"\n## OPEN POSITION")
            parts.append(f"Side: LONG")
            parts.append(f"Entry: ${position.entry_price:,.2f}")
            parts.append(f"Quantity: {position.quantity:.6f} BTC")
            parts.append(f"Stop-loss: ${position.stop_loss:,.2f}")
            parts.append(f"Take-profit: ${position.take_profit:,.2f}")
            parts.append(f"Unrealized P&L (before fees): ${upnl:,.2f} ({upnl_pct:+.2f}%)")
            parts.append(f"Estimated round-trip fees: ${total_fees:,.2f}")
            parts.append(f"NET P&L (after fees): ${net_profit:,.2f} ({net_profit_pct:+.2f}%)")
            parts.append(f"Breakeven price (including fees): ${breakeven_price:,.2f}")
            if self._trailing_stop_pct > 0:
                trail_price = self._highest_price_since_entry * (1 - self._trailing_stop_pct / 100)
                parts.append(f"Trailing stop: {self._trailing_stop_pct:.1f}% (highest since entry: ${self._highest_price_since_entry:,.2f}, current trail: ${trail_price:,.2f})")
            if net_profit < 0 and upnl > 0:
                parts.append(f"⚠ WARNING: Position is profitable before fees but a LOSS after fees. Do NOT sell unless thesis has changed.")
        else:
            parts.append(f"\nNo open position.")

        # Recent trades
        if trades:
            parts.append(f"\n## RECENT TRADES (last {len(trades)})")
            for t in trades[:5]:
                ts = time.strftime('%m/%d %H:%M', time.gmtime(t.timestamp))
                parts.append(f"  {ts} | {t.side.upper()} {t.quantity:.6f} BTC @ ${t.price:,.2f} | ${t.value:,.2f}")

        # Goals and progress
        goals = self.db.get_goals()
        weekly_pnl = self.db.get_period_pnl(7 * 86400)
        monthly_pnl = self.db.get_period_pnl(30 * 86400)

        has_goals = any([
            goals.get("weekly_profit_target", 0) > 0,
            goals.get("monthly_profit_target", 0) > 0,
            goals.get("weekly_btc_target", 0) > 0,
            goals.get("monthly_btc_target", 0) > 0,
        ])

        if has_goals:
            parts.append(f"\n## PROFIT GOALS & PROGRESS")
            wpt = goals.get("weekly_profit_target", 0)
            mpt = goals.get("monthly_profit_target", 0)
            wbt = goals.get("weekly_btc_target", 0)
            mbt = goals.get("monthly_btc_target", 0)

            if wpt > 0:
                wprog = weekly_pnl["realized_pnl"]
                parts.append(f"Weekly USD target: ${wpt:,.2f} | Progress: ${wprog:,.2f} ({wprog/wpt*100:.0f}%)")
            if mpt > 0:
                mprog = monthly_pnl["realized_pnl"]
                parts.append(f"Monthly USD target: ${mpt:,.2f} | Progress: ${mprog:,.2f} ({mprog/mpt*100:.0f}%)")
            if wbt > 0:
                parts.append(f"Weekly BTC accumulation target: {wbt:.6f} BTC")
            if mbt > 0:
                parts.append(f"Monthly BTC accumulation target: {mbt:.6f} BTC")

            notes = goals.get("notes", "")
            if notes:
                parts.append(f"User notes: {notes}")

            parts.append(f"\nThis week: {weekly_pnl['trade_count']} trades | This month: {monthly_pnl['trade_count']} trades")
            parts.append(f"Adjust your aggressiveness based on whether you're ahead or behind on these goals.")

        # Include recent operator chat messages so trading AI sees instructions
        try:
            recent_chat = self.db.get_chat_history(limit=6)
            operator_msgs = [m for m in recent_chat if m["role"] == "user"]
            if operator_msgs:
                parts.append(f"\n## RECENT OPERATOR INSTRUCTIONS (from chat)")
                parts.append(f"Your operator has been talking to you via the dashboard chat.")
                parts.append(f"Consider their instructions when making decisions:")
                for msg in operator_msgs[-3:]:  # Last 3 operator messages
                    ts = time.strftime('%m/%d %H:%M', time.gmtime(msg.get("timestamp", 0)))
                    parts.append(f"  [{ts}] Operator: {msg['message'][:200]}")
        except Exception as e:
            logger.debug(f"Could not load chat history for context: {e}")

        return "\n".join(parts)

    async def _call_claude(self, context: str) -> AIDecision:
        """Call the Anthropic API and parse the response."""
        api_key = self.config.anthropic_api_key
        if not api_key:
            logger.warning("No Anthropic API key — using indicator-based fallback")
            return AIDecision(action="HOLD", reasoning="No API key configured")

        model = self.config.ai_model

        try:
            resp = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 500,
                    "system": SYSTEM_PROMPT,
                    "messages": [
                        {"role": "user", "content": f"Analyze this market data and make a trading decision:\n\n{context}"}
                    ],
                },
            )
            resp.raise_for_status()
            result = resp.json()

            # Extract text from response
            content = result["content"][0]["text"]

            # Parse JSON from response (handle markdown code blocks)
            json_str = content.strip()
            if json_str.startswith("```"):
                json_str = json_str.split("\n", 1)[1]
                json_str = json_str.rsplit("```", 1)[0]

            decision_data = json.loads(json_str)

            decision = AIDecision(
                action=decision_data.get("action", "HOLD").upper(),
                quantity=float(decision_data.get("quantity", 0)),
                stop_loss=float(decision_data.get("stop_loss", 0)),
                take_profit=float(decision_data.get("take_profit", 0)),
                trailing_stop_pct=float(decision_data.get("trailing_stop_pct", 0)),
                confidence=float(decision_data.get("confidence", 0)),
                reasoning=decision_data.get("reasoning", ""),
                market_outlook=decision_data.get("market_outlook", "neutral"),
                strategy_used=decision_data.get("strategy_used", ""),
                raw_response=content,
            )

            logger.info(
                f"AI Decision: {decision.action} | Confidence: {decision.confidence:.2f} | "
                f"Outlook: {decision.market_outlook} | Strategy: {decision.strategy_used}"
            )
            logger.info(f"AI Reasoning: {decision.reasoning}")

            return decision

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI response as JSON: {e}")
            return AIDecision(action="HOLD", reasoning=f"JSON parse error: {e}")
        except Exception as e:
            logger.error(f"Claude API call failed: {e}")
            return AIDecision(action="HOLD", reasoning=f"API error: {e}")

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    async def _execute_buy(self, decision: AIDecision, price: float) -> str:
        """Execute a buy based on AI decision."""
        quantity = decision.quantity

        # Cap to available cash (Claude decides how much to use)
        if self.paper_trader:
            max_qty = self.paper_trader.balance.cash_usd / price
            quantity = min(quantity, max_qty)

        if quantity < 0.0001:
            logger.info("AI buy quantity too small after caps, skipping")
            return "skip_too_small"

        signals_json = json.dumps({
            "ai_action": decision.action,
            "ai_confidence": decision.confidence,
            "ai_reasoning": decision.reasoning,
            "ai_outlook": decision.market_outlook,
            "ai_strategy": decision.strategy_used,
        })

        try:
            if self.is_paper and self.paper_trader:
                self.paper_trader.execute_buy(
                    price=price, quantity=quantity,
                    strategy=f"ai_{decision.strategy_used}",
                    signals_json=signals_json,
                )
            else:
                result = await self.kraken.place_market_order(
                    side="buy", volume=Decimal(str(round(quantity, 8))),
                    validate=(self.config.mode != "live"),
                )
                self.db.record_trade(Trade(
                    timestamp=time.time(), side="buy", price=price,
                    quantity=quantity, value=price * quantity,
                    order_id=result.order_id, mode="live",
                    strategy=f"ai_{decision.strategy_used}",
                    signals=signals_json, status=result.status,
                ))

            # Save position
            position = Position(
                symbol=self.config.kraken.display_symbol,
                side="long", entry_price=price, quantity=quantity,
                entry_time=time.time(),
                stop_loss=decision.stop_loss if decision.stop_loss > 0 else price * 0.95,
                take_profit=decision.take_profit if decision.take_profit > 0 else price * 1.10,
            )
            self.db.save_position(position)

            # Initialize trailing stop tracking
            self._trailing_stop_pct = decision.trailing_stop_pct
            self._highest_price_since_entry = price

            self.db.log("TRADE",
                f"AI BUY: {quantity:.6f} BTC @ ${price:,.2f} | "
                f"Strategy: {decision.strategy_used} | {decision.reasoning}",
                signals_json)

            logger.info(
                f"OPENED LONG: {quantity:.6f} BTC @ ${price:,.2f} | "
                f"SL: ${position.stop_loss:,.2f} | TP: ${position.take_profit:,.2f}"
            )
            return "buy"

        except Exception as e:
            logger.error(f"Buy execution failed: {e}")
            return f"error: {e}"

    async def _execute_scale_in(self, decision: AIDecision, position: Position, price: float) -> str:
        """Add to an existing position (scale in / average down/up)."""
        quantity = decision.quantity

        # Cap to available cash
        if self.paper_trader:
            max_qty = self.paper_trader.balance.cash_usd / price
            quantity = min(quantity, max_qty)

        if quantity < 0.0001:
            logger.info("Scale-in quantity too small, skipping")
            return "skip_too_small"

        signals_json = json.dumps({
            "ai_action": "BUY (scale-in)",
            "ai_confidence": decision.confidence,
            "ai_reasoning": decision.reasoning,
            "ai_outlook": decision.market_outlook,
            "ai_strategy": decision.strategy_used,
        })

        try:
            if self.is_paper and self.paper_trader:
                self.paper_trader.execute_buy(
                    price=price, quantity=quantity,
                    strategy=f"ai_{decision.strategy_used}_scalein",
                    signals_json=signals_json,
                )
            else:
                result = await self.kraken.place_market_order(
                    side="buy", volume=Decimal(str(round(quantity, 8))),
                    validate=(self.config.mode != "live"),
                )
                self.db.record_trade(Trade(
                    timestamp=time.time(), side="buy", price=price,
                    quantity=quantity, value=price * quantity,
                    order_id=result.order_id, mode="live",
                    strategy=f"ai_{decision.strategy_used}_scalein",
                    signals=signals_json, status=result.status,
                ))

            # Update position with weighted average entry
            old_cost = position.entry_price * position.quantity
            new_cost = price * quantity
            total_qty = position.quantity + quantity
            avg_entry = (old_cost + new_cost) / total_qty

            position.entry_price = avg_entry
            position.quantity = total_qty
            position.stop_loss = decision.stop_loss if decision.stop_loss > 0 else position.stop_loss
            position.take_profit = decision.take_profit if decision.take_profit > 0 else position.take_profit
            self.db.save_position(position)

            # Update trailing stop tracking for scale-in
            self._trailing_stop_pct = decision.trailing_stop_pct
            self._highest_price_since_entry = price

            self.db.log("TRADE",
                f"AI SCALE-IN: +{quantity:.6f} BTC @ ${price:,.2f} | "
                f"Total: {total_qty:.6f} BTC | Avg entry: ${avg_entry:,.2f} | "
                f"{decision.reasoning}", signals_json)

            logger.info(
                f"SCALED IN: +{quantity:.6f} BTC @ ${price:,.2f} | "
                f"Total: {total_qty:.6f} @ ${avg_entry:,.2f} avg | "
                f"SL: ${position.stop_loss:,.2f} | TP: ${position.take_profit:,.2f}"
            )
            return "scale_in"

        except Exception as e:
            logger.error(f"Scale-in execution failed: {e}")
            return f"error: {e}"

    async def _execute_sell(self, decision: AIDecision, position: Position, price: float) -> str:
        """Execute a sell based on AI decision."""
        # AI can suggest partial sells
        quantity = min(decision.quantity, position.quantity) if decision.quantity > 0 else position.quantity

        signals_json = json.dumps({
            "ai_action": decision.action,
            "ai_confidence": decision.confidence,
            "ai_reasoning": decision.reasoning,
            "ai_outlook": decision.market_outlook,
            "ai_strategy": decision.strategy_used,
        })

        try:
            if self.is_paper and self.paper_trader:
                self.paper_trader.execute_sell(
                    price=price, quantity=quantity,
                    strategy=f"ai_{decision.strategy_used}",
                    signals_json=signals_json,
                )
            else:
                result = await self.kraken.place_market_order(
                    side="sell", volume=Decimal(str(round(quantity, 8))),
                    validate=(self.config.mode != "live"),
                )
                self.db.record_trade(Trade(
                    timestamp=time.time(), side="sell", price=price,
                    quantity=quantity, value=price * quantity,
                    order_id=result.order_id, mode="live",
                    strategy=f"ai_{decision.strategy_used}",
                    signals=signals_json, status=result.status,
                ))

            pnl = (price - position.entry_price) * quantity
            pnl_pct = (price - position.entry_price) / position.entry_price * 100

            remaining = position.quantity - quantity
            if remaining >= 0.0001:
                # Partial sell — keep position open with reduced quantity
                position.quantity = remaining
                position.unrealized_pnl = (price - position.entry_price) * remaining
                if decision.stop_loss > 0:
                    position.stop_loss = decision.stop_loss
                if decision.take_profit > 0:
                    position.take_profit = decision.take_profit
                self.db.save_position(position)
                sell_type = "PARTIAL SELL"
            else:
                # Full close
                self.db.close_position(position.id)
                self._trailing_stop_pct = 0.0
                self._highest_price_since_entry = 0.0
                sell_type = "SELL"

            self.db.log("TRADE",
                f"AI {sell_type}: {quantity:.6f} BTC @ ${price:,.2f} | "
                f"P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%) | "
                f"{'Remaining: ' + f'{remaining:.6f} BTC' if remaining >= 0.0001 else 'Position closed'} | "
                f"{decision.reasoning}",
                signals_json)

            if remaining >= 0.0001:
                logger.info(
                    f"PARTIAL SELL: {quantity:.6f} BTC @ ${price:,.2f} | "
                    f"P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%) | Keeping {remaining:.6f} BTC"
                )
                return f"partial_sell (ai_{decision.strategy_used})"
            else:
                logger.info(
                    f"CLOSED LONG: {quantity:.6f} BTC @ ${price:,.2f} | "
                    f"P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%)"
                )
                return f"sell (ai_{decision.strategy_used})"

        except Exception as e:
            logger.error(f"Sell execution failed: {e}")
            return f"error: {e}"

    async def _execute_stop_loss(self, position: Position, price: float) -> str:
        """Execute stop-loss exit."""
        return await self._execute_sell(
            AIDecision(action="SELL", quantity=position.quantity,
                      confidence=1.0, reasoning="Stop-loss triggered",
                      strategy_used="stop_loss"),
            position, price)

    async def _execute_take_profit(self, position: Position, price: float) -> str:
        """Execute take-profit exit."""
        return await self._execute_sell(
            AIDecision(action="SELL", quantity=position.quantity,
                      confidence=1.0, reasoning="Take-profit triggered",
                      strategy_used="take_profit"),
            position, price)

    async def close(self):
        """Clean up."""
        await self.sentiment.close()
        await self._http.aclose()
