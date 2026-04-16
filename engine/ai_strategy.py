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


SYSTEM_PROMPT = """You are an expert cryptocurrency trader with FULL CONTROL over a multi-coin portfolio on Kraken.
You run 24/7 and make decisions every scan cycle based on technical analysis, market sentiment, and news.
You are the decision engine. You decide EVERYTHING — which coins to trade, position sizing, entries, exits, scaling in/out, risk management.

## YOUR DUAL OBJECTIVE
1. **Grow the USD cash balance** — take profits when conditions warrant
2. **Accumulate crypto assets** — buy dips across any coin you believe in

You must balance these objectives dynamically based on market conditions.

## TRADEABLE COINS
You can trade ANY of these Kraken pairs:
BTC/USD, ETH/USD, SOL/USD, DOGE/USD, ADA/USD, AVAX/USD, LINK/USD, DOT/USD, POL/USD, XRP/USD

Choose the best opportunity each cycle. You can hold multiple positions across different coins simultaneously.

## RISK PROFILE: AGGRESSIVE
- You control your own position sizing — go big on high-conviction setups
- You can scale into positions (buy more to average down or add to winners)
- You can partially sell to lock in profits while keeping exposure
- You use stops and targets flexibly based on your conviction and market structure
- You actively look for momentum trades, mean reversion, and accumulation opportunities
- You manage your own cash reserves — allocate as you see fit
- You can hold positions in multiple coins at the same time

## CRITICAL: FEE AWARENESS (ONE HARD RULE)
Kraken charges a 0.26% taker fee per trade. A full round trip (buy + sell) costs 0.52% in fees.
**THE ONLY HARD CONSTRAINT: sells where profit is under 0.6% will be blocked by the system (except stop-losses).**
Everything else is your call. Factor fees into your decisions:
- Round-trip cost is 0.52%, so any take-profit below that is a net loss
- Quick in-and-out scalps are fee destroyers — make sure the move justifies the cost
- For long-term accumulation buys, fees matter less

## YOUR CAPABILITIES
- You can BUY any amount of any tradeable coin (limited by available cash)
- You can SELL any amount of any coin you're holding (full or partial sells)
- You can scale into positions — buy more when already holding
- You can average down on dips or add to winning positions
- You can adjust your stop-loss and take-profit levels every scan
- You can set a trailing stop by providing trailing_stop_pct (e.g., 2.0 means the stop follows price at 2% below the highest price since entry)
- Trailing stops automatically ratchet up as price rises — they never move down
- Set trailing_stop_pct to 0 for a fixed stop-loss, or a value like 1.5-3.0 for a trailing stop
- You can hold through volatility or cut losses fast — YOUR CALL
- No shorting, no futures — spot only

## RESPONSE FORMAT
You MUST respond with valid JSON only, no other text. Use this exact structure:
{
  "action": "BUY" | "SELL" | "HOLD",
  "symbol": "BTC/USD",
  "quantity": 0.0,
  "stop_loss": 0.0,
  "take_profit": 0.0,
  "trailing_stop_pct": 0.0,
  "confidence": 0.0,
  "reasoning": "2-3 sentence explanation",
  "market_outlook": "bullish" | "bearish" | "neutral",
  "strategy_used": "momentum" | "mean_reversion" | "trend_following" | "sentiment" | "accumulation" | "scaling" | "profit_taking" | "stop_loss"
}

IMPORTANT: "symbol" MUST be one of: BTC/USD, ETH/USD, SOL/USD, DOGE/USD, ADA/USD, AVAX/USD, LINK/USD, DOT/USD, POL/USD, XRP/USD
For HOLD actions, symbol should be whichever coin you're monitoring most closely. quantity/stop_loss/take_profit can be 0.
For BUY when already holding that coin: this ADDS to your position (scaling in). Set updated stop/target for the full position.
For SELL: quantity is how much of that coin to sell. Can be partial — sell some, keep some.
Confidence is 0.0 to 1.0 — only act on confidence >= 0.6.
"""


# Map display symbols to Kraken pair names and base coin symbols
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

# Minimum order sizes per coin on Kraken
MIN_ORDER_SIZE = {
    "BTC": 0.0001,
    "ETH": 0.001,
    "SOL": 0.01,
    "DOGE": 10.0,
    "ADA": 1.0,
    "AVAX": 0.1,
    "LINK": 0.1,
    "DOT": 0.1,
    "POL": 1.0,
    "XRP": 1.0,
}


@dataclass
class AIDecision:
    """Structured decision from the AI."""
    action: str = "HOLD"
    symbol: str = "BTC/USD"     # which coin to trade
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
        self._last_context = ""  # Full context string from last scan — reused by chat
        # Per-symbol trailing stop tracking: {symbol: {highest_price, trailing_pct}}
        self._trailing_stops = {}

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

        # --- 5. Update equity with prices for all held coins ---
        if self.is_paper and self.paper_trader:
            # Build price map from market scanner data
            prices = {"BTC": current_price}
            if market_overview:
                for snap in market_overview.coin_snapshots:
                    base = self._scanner.SYMBOL_MAP.get(snap.symbol, snap.symbol.replace("USD", ""))
                    prices[base] = snap.price
            self.paper_trader.update_equity(prices)

        # --- 6. Get current state (all positions) ---
        positions = self.db.get_open_positions()
        recent_trades = self.db.get_trades(limit=10)
        balance = self.paper_trader.get_balance() if self.paper_trader else None

        # --- 7. Build prompt and call Claude ---
        context = self._build_context(
            current_price, bars, signals, sentiment_data,
            positions, recent_trades, balance, market_overview,
        )
        self._last_context = context  # Cache for chat to reuse

        decision = await self._call_claude(context)
        self._last_decision = decision

        # --- 8. Execute decision ---
        # Resolve the target symbol and get its current price
        target_symbol = decision.symbol  # e.g. "ETH/USD"
        sym_info = SYMBOL_MAP.get(target_symbol, SYMBOL_MAP["BTC/USD"])
        base_coin = sym_info["base"]

        # Get current price for the target coin
        if target_symbol == "BTC/USD" or target_symbol == self.config.kraken.display_symbol:
            target_price = current_price
        elif market_overview:
            # Look up price from market scanner
            kraken_pair = sym_info["kraken"]
            target_price = next(
                (s.price for s in market_overview.coin_snapshots if s.symbol == kraken_pair),
                current_price  # fallback
            )
        else:
            target_price = current_price

        # Find existing position for this specific coin
        position = self.db.get_open_position(symbol=target_symbol)

        # Fee constants
        TAKER_FEE_PCT = 0.26
        ROUND_TRIP_FEE_PCT = TAKER_FEE_PCT * 2  # 0.52%
        MIN_PROFIT_PCT = 0.60

        action_taken = "hold"
        if decision.confidence >= 0.6:
            if decision.action == "BUY":
                if position is not None:
                    action_taken = await self._execute_scale_in(decision, position, target_price)
                else:
                    action_taken = await self._execute_buy(decision, target_price)

            elif decision.action == "SELL" and position is not None:
                profit_pct = (target_price - position.entry_price) / position.entry_price * 100
                if 0 < profit_pct < MIN_PROFIT_PCT and decision.strategy_used not in ("stop_loss",):
                    logger.info(
                        f"Blocking sell on {target_symbol}: {profit_pct:.2f}% gain doesn't cover "
                        f"{ROUND_TRIP_FEE_PCT:.2f}% round-trip fees. Holding."
                    )
                    action_taken = f"blocked_insufficient_profit ({profit_pct:.2f}%)"
                    self.db.log("INFO",
                        f"Sell blocked on {target_symbol}: {profit_pct:.2f}% profit < {MIN_PROFIT_PCT:.2f}% minimum")
                else:
                    action_taken = await self._execute_sell(decision, position, target_price)

        elif decision.action != "HOLD":
            logger.info(f"AI suggested {decision.action} {target_symbol} but confidence too low ({decision.confidence:.2f})")
            action_taken = f"low_confidence_{decision.action.lower()}"

        # --- 9. Check stop-loss / take-profit on ALL open positions ---
        for pos in positions:
            # Get current price for this position's coin
            pos_sym_info = SYMBOL_MAP.get(pos.symbol, {})
            pos_kraken = pos_sym_info.get("kraken", "XBTUSD")
            if pos.symbol == "BTC/USD":
                pos_price = current_price
            elif market_overview:
                pos_price = next(
                    (s.price for s in market_overview.coin_snapshots if s.symbol == pos_kraken),
                    0
                )
            else:
                pos_price = 0

            if pos_price <= 0:
                continue

            # Update stop/target if Claude provided new ones for this specific coin
            if decision.stop_loss > 0 and decision.action == "HOLD" and decision.symbol == pos.symbol:
                pos.stop_loss = decision.stop_loss
            if decision.take_profit > 0 and decision.action == "HOLD" and decision.symbol == pos.symbol:
                pos.take_profit = decision.take_profit

            # Per-symbol trailing stop logic
            trail = self._trailing_stops.get(pos.symbol, {})
            trail_pct = trail.get("trailing_pct", 0)
            if trail_pct > 0:
                highest = max(trail.get("highest_price", pos_price), pos_price)
                trail["highest_price"] = highest
                self._trailing_stops[pos.symbol] = trail
                trailing_stop_price = highest * (1 - trail_pct / 100)
                if trailing_stop_price > pos.stop_loss:
                    logger.info(
                        f"Trailing stop on {pos.symbol} ratcheted: ${pos.stop_loss:,.2f} -> ${trailing_stop_price:,.2f}"
                    )
                    pos.stop_loss = trailing_stop_price

            if pos.stop_loss > 0 and pos_price <= pos.stop_loss:
                sell_decision = AIDecision(
                    action="SELL", symbol=pos.symbol, quantity=pos.quantity,
                    confidence=1.0, strategy_used="stop_loss",
                    reasoning=f"Stop-loss triggered at ${pos_price:,.2f}",
                )
                action_taken = await self._execute_sell(sell_decision, pos, pos_price)
            elif pos.take_profit > 0 and pos_price >= pos.take_profit:
                sell_decision = AIDecision(
                    action="SELL", symbol=pos.symbol, quantity=pos.quantity,
                    confidence=1.0, strategy_used="profit_taking",
                    reasoning=f"Take-profit triggered at ${pos_price:,.2f}",
                )
                action_taken = await self._execute_sell(sell_decision, pos, pos_price)
            else:
                pos.unrealized_pnl = (pos_price - pos.entry_price) * pos.quantity
                self.db.save_position(pos)

        # --- Build result ---
        result = {
            "price": current_price,
            "action": action_taken,
            "ai_decision": decision,
            "recommendation": decision.action,
            "composite_score": decision.confidence,
            "has_position": len(positions) > 0,
            "positions": positions,
            "signals": signals,
            "sentiment": sentiment_data,
            "market_overview": market_overview,
        }

        return result

    def _build_context(self, price, bars, signals, sentiment, positions, trades, balance, market_overview=None) -> str:
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
        if sentiment:
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
            parts.append(self._scanner.format_for_ai(market_overview))

        # Account state
        parts.append(f"\n## ACCOUNT STATE")
        if balance:
            parts.append(f"Cash (USD): ${balance.cash_usd:,.2f}")
            if balance.holdings:
                for sym, qty in balance.holdings.items():
                    parts.append(f"{sym} held: {qty:.6f}")
            parts.append(f"Total equity: ${balance.total_equity:,.2f}")
            starting = self.config.paper.starting_capital
            pnl = balance.total_equity - starting
            parts.append(f"Total P&L: ${pnl:,.2f} ({pnl/starting*100:+.2f}%)")

        # All open positions with fee analysis
        if positions:
            parts.append(f"\n## OPEN POSITIONS ({len(positions)})")
            for position in (positions if isinstance(positions, list) else [positions]):
                # Get current price for this position's coin
                pos_price = price  # default to BTC price
                if market_overview and position.symbol != "BTC/USD":
                    sym_info = SYMBOL_MAP.get(position.symbol, {})
                    kraken_pair = sym_info.get("kraken", "")
                    for snap in (market_overview.coin_snapshots if market_overview else []):
                        if snap.symbol == kraken_pair:
                            pos_price = snap.price
                            break

                upnl = (pos_price - position.entry_price) * position.quantity
                upnl_pct = (pos_price - position.entry_price) / position.entry_price * 100 if position.entry_price > 0 else 0
                buy_fee = position.entry_price * position.quantity * 0.0026
                sell_fee = pos_price * position.quantity * 0.0026
                total_fees = buy_fee + sell_fee
                net_profit = upnl - total_fees
                net_profit_pct = net_profit / (position.entry_price * position.quantity) * 100 if position.entry_price > 0 else 0
                breakeven_price = position.entry_price * 1.0052

                base = SYMBOL_MAP.get(position.symbol, {}).get("base", "???")
                parts.append(f"\n### {position.symbol}")
                parts.append(f"Side: LONG | Entry: ${position.entry_price:,.4f} | Qty: {position.quantity:.6f} {base}")
                parts.append(f"Current: ${pos_price:,.4f} | Stop: ${position.stop_loss:,.4f} | Target: ${position.take_profit:,.4f}")
                parts.append(f"Unrealized P&L: ${upnl:,.2f} ({upnl_pct:+.2f}%) | NET (after fees): ${net_profit:,.2f} ({net_profit_pct:+.2f}%)")
                parts.append(f"Breakeven: ${breakeven_price:,.4f}")

                trail = self._trailing_stops.get(position.symbol, {})
                if trail.get("trailing_pct", 0) > 0:
                    trail_price = trail["highest_price"] * (1 - trail["trailing_pct"] / 100)
                    parts.append(f"Trailing stop: {trail['trailing_pct']:.1f}% (highest: ${trail['highest_price']:,.2f}, trail: ${trail_price:,.2f})")

                if net_profit < 0 and upnl > 0:
                    parts.append(f"⚠ Position profitable before fees but a LOSS after. Hold unless thesis changed.")
        else:
            parts.append(f"\nNo open positions.")

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

            # Parse symbol — default to BTC/USD if not specified
            raw_symbol = decision_data.get("symbol", "BTC/USD")
            # Normalize: accept "ETHUSD", "ETH/USD", "ETH", etc.
            if "/" not in raw_symbol and raw_symbol.endswith("USD"):
                raw_symbol = raw_symbol[:-3] + "/USD"
            elif "/" not in raw_symbol:
                raw_symbol = raw_symbol + "/USD"
            # Validate it's a known symbol
            if raw_symbol not in SYMBOL_MAP:
                logger.warning(f"Unknown symbol '{raw_symbol}' from AI, defaulting to BTC/USD")
                raw_symbol = "BTC/USD"

            decision = AIDecision(
                action=decision_data.get("action", "HOLD").upper(),
                symbol=raw_symbol,
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
        sym_info = SYMBOL_MAP.get(decision.symbol, SYMBOL_MAP["BTC/USD"])
        base_coin = sym_info["base"]
        min_size = MIN_ORDER_SIZE.get(base_coin, 0.0001)
        quantity = decision.quantity

        # Cap to available cash (reserve 0.26% for taker fee)
        if self.paper_trader:
            max_qty = self.paper_trader.balance.cash_usd / (price * 1.0026)
            quantity = min(quantity, max_qty)

        if quantity < min_size:
            logger.info(f"AI buy quantity {quantity} below min {min_size} for {base_coin}, skipping")
            return "skip_too_small"

        signals_json = json.dumps({
            "ai_action": decision.action,
            "ai_symbol": decision.symbol,
            "ai_confidence": decision.confidence,
            "ai_reasoning": decision.reasoning,
            "ai_outlook": decision.market_outlook,
            "ai_strategy": decision.strategy_used,
        })

        try:
            if self.is_paper and self.paper_trader:
                self.paper_trader.execute_buy(
                    price=price, quantity=quantity,
                    symbol=base_coin, display_symbol=decision.symbol,
                    strategy=f"ai_{decision.strategy_used}",
                    signals_json=signals_json,
                )
            else:
                # For live trading, temporarily swap the kraken client symbol
                orig_symbol = self.kraken.symbol
                self.kraken.symbol = sym_info["kraken"]
                result = await self.kraken.place_market_order(
                    side="buy", volume=Decimal(str(round(quantity, 8))),
                    validate=(self.config.mode != "live"),
                )
                self.kraken.symbol = orig_symbol
                self.db.record_trade(Trade(
                    timestamp=time.time(), side="buy", price=price,
                    quantity=quantity, value=price * quantity,
                    order_id=result.order_id, mode="live",
                    strategy=f"ai_{decision.strategy_used}",
                    signals=signals_json, status=result.status,
                    symbol=decision.symbol,
                ))

            # Save position
            position = Position(
                symbol=decision.symbol,
                side="long", entry_price=price, quantity=quantity,
                entry_time=time.time(),
                stop_loss=decision.stop_loss if decision.stop_loss > 0 else price * 0.95,
                take_profit=decision.take_profit if decision.take_profit > 0 else price * 1.10,
            )
            self.db.save_position(position)

            # Initialize per-symbol trailing stop tracking
            if decision.trailing_stop_pct > 0:
                self._trailing_stops[decision.symbol] = {
                    "trailing_pct": decision.trailing_stop_pct,
                    "highest_price": price,
                }

            self.db.log("TRADE",
                f"AI BUY: {quantity:.6f} {base_coin} @ ${price:,.2f} | "
                f"Strategy: {decision.strategy_used} | {decision.reasoning}",
                signals_json)

            logger.info(
                f"OPENED LONG {decision.symbol}: {quantity:.6f} {base_coin} @ ${price:,.2f} | "
                f"SL: ${position.stop_loss:,.2f} | TP: ${position.take_profit:,.2f}"
            )
            return "buy"

        except Exception as e:
            logger.error(f"Buy execution failed for {decision.symbol}: {e}")
            return f"error: {e}"

    async def _execute_scale_in(self, decision: AIDecision, position: Position, price: float) -> str:
        """Add to an existing position (scale in / average down/up)."""
        sym_info = SYMBOL_MAP.get(decision.symbol, SYMBOL_MAP["BTC/USD"])
        base_coin = sym_info["base"]
        min_size = MIN_ORDER_SIZE.get(base_coin, 0.0001)
        quantity = decision.quantity

        # Cap to available cash (reserve 0.26% for taker fee)
        if self.paper_trader:
            max_qty = self.paper_trader.balance.cash_usd / (price * 1.0026)
            quantity = min(quantity, max_qty)

        if quantity < min_size:
            logger.info(f"Scale-in quantity {quantity} below min {min_size} for {base_coin}, skipping")
            return "skip_too_small"

        signals_json = json.dumps({
            "ai_action": "BUY (scale-in)",
            "ai_symbol": decision.symbol,
            "ai_confidence": decision.confidence,
            "ai_reasoning": decision.reasoning,
            "ai_outlook": decision.market_outlook,
            "ai_strategy": decision.strategy_used,
        })

        try:
            if self.is_paper and self.paper_trader:
                self.paper_trader.execute_buy(
                    price=price, quantity=quantity,
                    symbol=base_coin, display_symbol=decision.symbol,
                    strategy=f"ai_{decision.strategy_used}_scalein",
                    signals_json=signals_json,
                )
            else:
                orig_symbol = self.kraken.symbol
                self.kraken.symbol = sym_info["kraken"]
                result = await self.kraken.place_market_order(
                    side="buy", volume=Decimal(str(round(quantity, 8))),
                    validate=(self.config.mode != "live"),
                )
                self.kraken.symbol = orig_symbol
                self.db.record_trade(Trade(
                    timestamp=time.time(), side="buy", price=price,
                    quantity=quantity, value=price * quantity,
                    order_id=result.order_id, mode="live",
                    strategy=f"ai_{decision.strategy_used}_scalein",
                    signals=signals_json, status=result.status,
                    symbol=decision.symbol,
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

            # Update per-symbol trailing stop
            if decision.trailing_stop_pct > 0:
                self._trailing_stops[decision.symbol] = {
                    "trailing_pct": decision.trailing_stop_pct,
                    "highest_price": price,
                }

            self.db.log("TRADE",
                f"AI SCALE-IN {decision.symbol}: +{quantity:.6f} {base_coin} @ ${price:,.2f} | "
                f"Total: {total_qty:.6f} | Avg entry: ${avg_entry:,.2f} | "
                f"{decision.reasoning}", signals_json)

            logger.info(
                f"SCALED IN {decision.symbol}: +{quantity:.6f} {base_coin} @ ${price:,.2f} | "
                f"Total: {total_qty:.6f} @ ${avg_entry:,.2f} avg"
            )
            return "scale_in"

        except Exception as e:
            logger.error(f"Scale-in execution failed for {decision.symbol}: {e}")
            return f"error: {e}"

    async def _execute_sell(self, decision: AIDecision, position: Position, price: float) -> str:
        """Execute a sell based on AI decision."""
        sym_info = SYMBOL_MAP.get(decision.symbol, SYMBOL_MAP["BTC/USD"])
        base_coin = sym_info["base"]
        min_size = MIN_ORDER_SIZE.get(base_coin, 0.0001)

        # AI can suggest partial sells
        quantity = min(decision.quantity, position.quantity) if decision.quantity > 0 else position.quantity

        signals_json = json.dumps({
            "ai_action": decision.action,
            "ai_symbol": decision.symbol,
            "ai_confidence": decision.confidence,
            "ai_reasoning": decision.reasoning,
            "ai_outlook": decision.market_outlook,
            "ai_strategy": decision.strategy_used,
        })

        try:
            if self.is_paper and self.paper_trader:
                self.paper_trader.execute_sell(
                    price=price, quantity=quantity,
                    symbol=base_coin, display_symbol=decision.symbol,
                    strategy=f"ai_{decision.strategy_used}",
                    signals_json=signals_json,
                )
            else:
                orig_symbol = self.kraken.symbol
                self.kraken.symbol = sym_info["kraken"]
                result = await self.kraken.place_market_order(
                    side="sell", volume=Decimal(str(round(quantity, 8))),
                    validate=(self.config.mode != "live"),
                )
                self.kraken.symbol = orig_symbol
                self.db.record_trade(Trade(
                    timestamp=time.time(), side="sell", price=price,
                    quantity=quantity, value=price * quantity,
                    order_id=result.order_id, mode="live",
                    strategy=f"ai_{decision.strategy_used}",
                    signals=signals_json, status=result.status,
                    symbol=decision.symbol,
                ))

            pnl = (price - position.entry_price) * quantity
            pnl_pct = (price - position.entry_price) / position.entry_price * 100

            remaining = position.quantity - quantity
            if remaining >= min_size:
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
                self._trailing_stops.pop(decision.symbol, None)
                sell_type = "SELL"

            self.db.log("TRADE",
                f"AI {sell_type} {decision.symbol}: {quantity:.6f} {base_coin} @ ${price:,.2f} | "
                f"P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%) | "
                f"{'Remaining: ' + f'{remaining:.6f}' if remaining >= min_size else 'Position closed'} | "
                f"{decision.reasoning}",
                signals_json)

            if remaining >= min_size:
                logger.info(
                    f"PARTIAL SELL {decision.symbol}: {quantity:.6f} {base_coin} @ ${price:,.2f} | "
                    f"P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%) | Keeping {remaining:.6f}"
                )
                return f"partial_sell (ai_{decision.strategy_used})"
            else:
                logger.info(
                    f"CLOSED {decision.symbol}: {quantity:.6f} {base_coin} @ ${price:,.2f} | "
                    f"P&L: ${pnl:,.2f} ({pnl_pct:+.2f}%)"
                )
                return f"sell (ai_{decision.strategy_used})"

        except Exception as e:
            logger.error(f"Sell execution failed for {decision.symbol}: {e}")
            return f"error: {e}"

    async def close(self):
        """Clean up."""
        await self.sentiment.close()
        await self._http.aclose()
