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
from web_research import WebResearcher, ResearchResult
from whale_monitor import WhaleMonitor
from discord_notifier import DiscordNotifier

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert cryptocurrency trader with FULL CONTROL over a multi-coin portfolio on Kraken.
You run 24/7 and make decisions every scan cycle based on technical analysis, market sentiment, and news.
You are the decision engine. You decide EVERYTHING — which coins to trade, position sizing, entries, exits, scaling in/out, risk management.

## YOU ARE ONE CONTINUOUS AGENT
You are the SAME mind that trades AND chats with your operator on the dashboard. When your operator talks
to you via chat, those conversations are fed back to you here. If you told your operator "I'll buy the dip
on SOL" — follow through. If they told you "stop buying DOT" — respect it. Your chat history and any
standing directives from your operator appear in the data below. You are not two separate systems — you are
one agent with persistent memory across both trading scans and conversations.

## YOUR DUAL OBJECTIVE
1. **Grow the USD cash balance** — take profits when conditions warrant
2. **Accumulate crypto assets** — buy dips across any coin you believe in

You must balance these objectives dynamically based on market conditions.

## TRADEABLE COINS
You can trade ANY of these Kraken pairs:
BTC/USD, ETH/USD, SOL/USD, DOGE/USD, ADA/USD, AVAX/USD, LINK/USD, DOT/USD, POL/USD, XRP/USD

Choose the best opportunity each cycle. You can hold multiple positions across different coins simultaneously.

## YOUR DATA EDGE
You have access to data most traders don't see together:
- Multi-timeframe analysis: 15m, 1h, and 4h indicators for ALL 10 coins — confirm signals across timeframes
- Order book depth: see where buy/sell walls are, spread, and order imbalance
- Whale monitoring: large BTC transactions and exchange inflow/outflow signals
- BTC dominance: real-time from CoinGecko — know when alts will outperform vs underperform
- ATR (Average True Range): volatility measurement for ALL coins at ALL timeframes — use for position sizing
- Full technicals on all 10 coins: EMA, RSI, Bollinger, composite scores, ATR

USE the multi-timeframe data: a bullish 15m signal aligned with bullish 1h and 4h = high conviction.
A bullish 15m fighting a bearish 4h = low conviction trap. Check alignment for EVERY coin before trading it.
The order book shows real support/resistance.

## RISK PROFILE: AGGRESSIVE (WITH GUARDRAILS)
- You control your own position sizing — go big on high-conviction setups
- You can scale into positions (buy more to average down or add to winners)
- You can partially sell to lock in profits while keeping exposure
- You use stops and targets flexibly based on your conviction and market structure
- You actively look for momentum trades, mean reversion, and accumulation opportunities
- You manage your own cash reserves — allocate as you see fit
- You can hold positions in multiple coins at the same time
- DRAWDOWN BREAKER: When equity drops 5%+ from peak, the system halves your position sizes automatically
- Use ATR for smarter sizing: volatile coins (high ATR%) get smaller positions, calm coins get bigger ones

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

## STRATEGY JOURNAL — YOUR PERSISTENT MEMORY
You have a strategy journal that persists across reboots and rebuilds. Use it to record lessons learned,
observations about specific coins or strategies, and insights from research. Your journal entries are
fed back to you every scan cycle, so you REMEMBER what you've learned.

Write journal entries whenever you learn something — after a good trade, a bad trade, when you notice
a pattern, or when research reveals something useful. Be specific and actionable.

## RESEARCH NOTEBOOK — YOUR LONG-FORM THINKING SPACE
You have a dedicated research notebook separate from the trade journal. This is YOUR space to think deeply.
Use it to write longer notes about:
- Market hypotheses ("I think BTC is forming a head-and-shoulders because...")
- Coin deep dives ("SOL ecosystem analysis: TVL growing, new DEXs launching...")
- Strategy development ("Mean reversion works better for DOT during low-vol regimes")
- Macro observations ("Fed meeting next week — historically BTC drops 2-3% day-of then recovers")
- Risk assessments ("Portfolio too concentrated in alts, should rebalance if BTC dominance rises above 58%")
- News analysis ("ETH ETF approval rumors — if confirmed, likely 10-15% pump within 48h")
- Pattern tracking ("Third time this week DOGE pumped at 2am UTC then dumped by 6am")

ALL your research notes are fed back to you every cycle with NO LIMIT. Write as many as you need.
You can also mark old notes as stale by providing their IDs in "stale_note_ids" when they're outdated.

IMPORTANT: Use the research notebook PROACTIVELY. Don't just trade — THINK. During HOLD cycles especially,
analyze what you're seeing, form hypotheses, and write them down. Your future self will thank you.

## WEB RESEARCH
You can request web research on trading strategies, market analysis, coin fundamentals, or any topic
that would help your trading. Add a "research_query" field to your response when you want to learn
something. The system will search the web and feed results back to you on the next scan cycle.
Use this to stay current on market events, learn new strategies, or investigate specific coins.

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
  "strategy_used": "momentum" | "mean_reversion" | "trend_following" | "sentiment" | "accumulation" | "scaling" | "profit_taking" | "stop_loss",
  "journal_entry": "Optional: Write a lesson, observation, or insight to remember. Be specific. Leave empty string if nothing to note.",
  "journal_category": "observation" | "lesson" | "strategy_idea" | "coin_insight" | "risk_note" | "research_finding",
  "research_query": "Optional: A web search query if you want to research something. Leave empty string if no research needed.",
  "research_note_title": "Optional: Title for a research notebook entry. Leave empty if no note to write.",
  "research_note_body": "Optional: Full research note — be detailed, write paragraphs. Hypotheses, analysis, deep dives. No length limit.",
  "research_note_topic": "macro" | "technical" | "coin_analysis" | "strategy" | "risk" | "news" | "hypothesis",
  "research_note_coins": "Optional: Comma-separated coins this note relates to, e.g. 'BTC,ETH'. Leave empty if general.",
  "stale_note_ids": "Optional: Comma-separated IDs of research notes that are outdated and should be retired. Leave empty if none."
}

IMPORTANT: "symbol" MUST be one of: BTC/USD, ETH/USD, SOL/USD, DOGE/USD, ADA/USD, AVAX/USD, LINK/USD, DOT/USD, POL/USD, XRP/USD
For HOLD actions, symbol should be whichever coin you're monitoring most closely. quantity/stop_loss/take_profit can be 0.
For BUY when already holding that coin: this ADDS to your position (scaling in). Set updated stop/target for the full position.
For SELL: quantity is how much of that coin to sell. Can be partial — sell some, keep some.
Confidence is 0.0 to 1.0 — only act on confidence >= 0.6.
journal_entry, research_query, and research_note fields are optional — use empty strings if not needed.
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
    journal_entry: str = ""      # lesson/observation to persist
    journal_category: str = ""   # observation, lesson, strategy_idea, etc.
    research_query: str = ""     # web search query for next cycle
    # Research notebook — long-form notes, ideas, hypotheses
    research_note_title: str = ""
    research_note_body: str = ""
    research_note_topic: str = ""   # macro, technical, coin_analysis, strategy, risk, news, hypothesis
    research_note_coins: str = ""   # comma-separated coin symbols this note relates to
    stale_note_ids: str = ""        # comma-separated IDs of notes to mark as outdated


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
        self._researcher = WebResearcher(self._http)
        self._whale_monitor = WhaleMonitor(self._http)
        self.discord = DiscordNotifier(http_client=self._http)
        self._last_market_overview = None
        self._last_decision = None
        self._last_context = ""  # Full context string from last scan — reused by chat
        self._last_research: Optional[ResearchResult] = None  # Cached research results
        self._pending_research_query: str = ""  # Query to run on next scan cycle
        # Per-symbol trailing stop tracking: {symbol: {highest_price, trailing_pct}}
        self._trailing_stops = {}
        # Drawdown circuit breaker tracking
        self._peak_equity: float = config.paper.starting_capital if config.mode == "paper" else 0
        self._drawdown_pct: float = 0.0
        self._drawdown_active: bool = False
        # Cached data for context
        self._last_order_book: dict = {}
        self._last_whale_data = None
        self._last_mtf_signals: dict = {}  # multi-timeframe signals

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

        # --- 2. Compute technical indicators (with ATR) ---
        closes = [bar.close for bar in bars]
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
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
                    highs=highs,
                    lows=lows,
                )
            except Exception as e:
                logger.warning(f"Indicator computation failed: {e}")

        # --- 2b. Multi-timeframe analysis (1h and 4h) for ALL coins ---
        try:
            self._last_mtf_signals = await self._scanner.scan_multi_timeframe()
        except Exception as e:
            logger.debug(f"Multi-timeframe scan failed: {e}")
            self._last_mtf_signals = {}

        # --- 3. Get current price ---
        try:
            ticker = await self.kraken.get_ticker()
            current_price = float(ticker.mid)
        except Exception as e:
            logger.error(f"Failed to fetch ticker: {e}")
            current_price = closes[-1] if closes else 0

        # --- 4. Fetch sentiment, market data, order book, and whale activity ---
        try:
            sentiment_data = await self.sentiment.fetch_all(ohlcv_bars=bars)
        except Exception as e:
            logger.warning(f"Sentiment fetch failed: {e}")
            sentiment_data = SentimentData(timestamp=time.time())

        try:
            market_overview = await self._scanner.scan_all()
            self._last_market_overview = market_overview
            dom_str = ""
            if market_overview.global_data:
                dom_str = f" | BTC dom: {market_overview.global_data.btc_dominance:.1f}%"
            logger.info(
                f"Market scan: {len(market_overview.coin_snapshots)} coins | "
                f"Momentum: {market_overview.market_momentum} | "
                f"Rotation: {market_overview.sector_rotation_signal}{dom_str}"
            )
        except Exception as e:
            logger.warning(f"Market scan failed: {e}")
            market_overview = None

        # Order book depth (BTC — primary pair)
        try:
            self._last_order_book = await self.kraken.get_order_book(depth=15)
        except Exception as e:
            logger.debug(f"Order book fetch failed: {e}")

        # Whale activity
        try:
            self._last_whale_data = await self._whale_monitor.get_whale_activity()
        except Exception as e:
            logger.debug(f"Whale monitor failed: {e}")

        # --- 5. Update equity and drawdown tracking ---
        if self.is_paper and self.paper_trader:
            # Build price map from market scanner data
            prices = {"BTC": current_price}
            if market_overview:
                for snap in market_overview.coin_snapshots:
                    prices[snap.symbol] = snap.price
            self.paper_trader.update_equity(prices)

            # Drawdown circuit breaker
            equity = self.paper_trader.balance.total_equity
            if equity > self._peak_equity:
                self._peak_equity = equity
            if self._peak_equity > 0:
                self._drawdown_pct = ((self._peak_equity - equity) / self._peak_equity) * 100
            was_active = self._drawdown_active
            self._drawdown_active = self._drawdown_pct >= 5.0  # 5% drawdown threshold
            if self._drawdown_active and not was_active:
                logger.warning(
                    f"DRAWDOWN CIRCUIT BREAKER ACTIVE: {self._drawdown_pct:.1f}% drawdown "
                    f"(peak: ${self._peak_equity:,.2f}, current: ${equity:,.2f}). "
                    f"Position sizes will be halved."
                )
                self.db.log("WARNING", f"Drawdown breaker active: {self._drawdown_pct:.1f}%")
                await self.discord.send_drawdown_alert(self._drawdown_pct, equity, self._peak_equity)
            elif not self._drawdown_active and was_active:
                logger.info(f"Drawdown circuit breaker cleared. Equity recovered to ${equity:,.2f}")
                self.db.log("INFO", "Drawdown breaker cleared — back to full sizing")

            # --- 5b. Periodic equity snapshot to Discord (throttled hourly) ---
            holdings = {sym: qty for sym, qty in (self.paper_trader.balance.holdings or {}).items()}
            await self.discord.send_equity_update(
                cash_usd=self.paper_trader.balance.cash_usd,
                total_equity=equity,
                starting_capital=self.config.paper.starting_capital,
                holdings=holdings,
                positions=self.db.get_open_positions(),
                drawdown_pct=self._drawdown_pct,
            )

        # --- 6. Get current state (all positions) ---
        positions = self.db.get_open_positions()
        recent_trades = self.db.get_trades(limit=10)
        balance = self.paper_trader.get_balance() if self.paper_trader else None

        # --- 7. Execute any pending web research from last cycle ---
        if self._pending_research_query:
            try:
                self._last_research = await self._researcher.search(self._pending_research_query)
                logger.info(f"Web research completed: '{self._pending_research_query}' → {len(self._last_research.results)} results")
                # Save research findings to journal automatically
                if self._last_research.results:
                    summary_snippet = "; ".join(
                        r.title for r in self._last_research.results[:3]
                    )
                    self.db.add_journal_entry(
                        lesson=f"Research on '{self._pending_research_query}': {summary_snippet}",
                        category="research_finding",
                        confidence=0.4,
                        source="web_research",
                    )
            except Exception as e:
                logger.warning(f"Web research failed: {e}")
            self._pending_research_query = ""

        # --- 8. Build prompt and call Claude ---
        context = self._build_context(
            current_price, bars, signals, sentiment_data,
            positions, recent_trades, balance, market_overview,
        )
        self._last_context = context  # Cache for chat to reuse

        decision = await self._call_claude(context)
        self._last_decision = decision

        # --- 8b. Process journal entry and research request ---
        if decision.journal_entry:
            try:
                self.db.add_journal_entry(
                    lesson=decision.journal_entry,
                    category=decision.journal_category or "observation",
                    coin=SYMBOL_MAP.get(decision.symbol, {}).get("base", ""),
                    strategy=decision.strategy_used,
                    confidence=decision.confidence,
                    source="ai_trade_cycle",
                )
                logger.info(f"Journal entry saved: [{decision.journal_category}] {decision.journal_entry[:80]}...")
            except Exception as e:
                logger.warning(f"Failed to save journal entry: {e}")

        if decision.research_query:
            self._pending_research_query = decision.research_query
            logger.info(f"Research queued for next cycle: {decision.research_query}")

        # Process research notebook entry
        if decision.research_note_title and decision.research_note_body:
            try:
                note_id = self.db.add_research_note(
                    title=decision.research_note_title,
                    body=decision.research_note_body,
                    topic=decision.research_note_topic or "general",
                    coins=decision.research_note_coins or "",
                    source="ai_scan_cycle",
                )
                logger.info(
                    f"Research note #{note_id} saved: [{decision.research_note_topic}] "
                    f"{decision.research_note_title[:60]}..."
                )
            except Exception as e:
                logger.warning(f"Failed to save research note: {e}")

        # Mark stale research notes
        if decision.stale_note_ids:
            try:
                for nid in decision.stale_note_ids.split(","):
                    nid = nid.strip()
                    if nid.isdigit():
                        self.db.mark_research_note_stale(int(nid))
                        logger.info(f"Research note #{nid} marked as stale")
            except Exception as e:
                logger.warning(f"Failed to mark stale notes: {e}")

        # --- 9. Execute decision ---
        # Resolve the target symbol and get its current price
        target_symbol = decision.symbol  # e.g. "ETH/USD"
        sym_info = SYMBOL_MAP.get(target_symbol, SYMBOL_MAP["BTC/USD"])
        base_coin = sym_info["base"]

        # Get current price for the target coin
        if target_symbol == "BTC/USD" or target_symbol == self.config.kraken.display_symbol:
            target_price = current_price
        elif market_overview:
            # Look up price from market scanner — scanner stores friendly name (e.g. "DOT")
            target_price = next(
                (s.price for s in market_overview.coin_snapshots if s.symbol == base_coin),
                0  # no fallback to BTC price!
            )
            if target_price <= 0:
                logger.error(f"Could not find price for {base_coin} in market scanner — skipping trade")
                return {"action": "hold", "reason": f"no price for {base_coin}", "price": current_price}
        else:
            # No market overview available — only safe for BTC since current_price IS BTC
            if target_symbol != "BTC/USD":
                logger.error(f"No market overview — cannot get price for {base_coin}, skipping trade")
                return {"action": "hold", "reason": f"no market data for {base_coin}", "price": current_price}
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
            pos_base = pos_sym_info.get("base", "BTC")
            if pos.symbol == "BTC/USD":
                pos_price = current_price
            elif market_overview:
                # Match on friendly name (e.g. "DOT"), not Kraken pair ("DOTUSD")
                pos_price = next(
                    (s.price for s in market_overview.coin_snapshots if s.symbol == pos_base),
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
            if signals.atr:
                parts.append(f"ATR (14): ${signals.atr.atr:,.2f} ({signals.atr.atr_pct:.2f}% of price)")
                parts.append(f"Volatility: {signals.atr.volatility}")

        # Multi-timeframe analysis (all coins)
        if self._last_mtf_signals:
            parts.append(self._scanner.format_mtf_for_ai(self._last_mtf_signals))

        # Order book depth
        if self._last_order_book:
            ob = self._last_order_book
            parts.append(f"\n## ORDER BOOK (BTC/USD)")
            parts.append(f"Spread: ${ob.get('spread', 0):,.2f} ({ob.get('spread_pct', 0):.4f}%)")
            parts.append(f"Bid depth: ${ob.get('bid_depth_usd', 0):,.0f} | Ask depth: ${ob.get('ask_depth_usd', 0):,.0f}")
            imb = ob.get('imbalance', 0)
            imb_label = "buyers dominant" if imb > 0.1 else "sellers dominant" if imb < -0.1 else "balanced"
            parts.append(f"Imbalance: {imb:+.3f} ({imb_label})")
            parts.append(f"Bid wall: ${ob.get('bid_wall_price', 0):,.2f} ({ob.get('bid_wall_volume', 0):.4f} BTC)")
            parts.append(f"Ask wall: ${ob.get('ask_wall_price', 0):,.2f} ({ob.get('ask_wall_volume', 0):.4f} BTC)")

        # Whale activity
        if self._last_whale_data:
            whale_ctx = self._whale_monitor.format_for_context(self._last_whale_data)
            if whale_ctx:
                parts.append(whale_ctx)

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
            parts.append(f"Peak equity: ${self._peak_equity:,.2f}")
            parts.append(f"Current drawdown: {self._drawdown_pct:.1f}%")
            if self._drawdown_active:
                parts.append(f"⚠ DRAWDOWN CIRCUIT BREAKER ACTIVE — position sizes halved until recovery")
                parts.append(f"  Reduce risk. Focus on high-conviction setups only.")

        # All open positions with fee analysis
        if positions:
            parts.append(f"\n## OPEN POSITIONS ({len(positions)})")
            for position in (positions if isinstance(positions, list) else [positions]):
                # Get current price for this position's coin
                pos_price = price  # default to BTC price
                if market_overview and position.symbol != "BTC/USD":
                    pos_base = SYMBOL_MAP.get(position.symbol, {}).get("base", "")
                    for snap in (market_overview.coin_snapshots if market_overview else []):
                        if snap.symbol == pos_base:
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
                coin = getattr(t, 'symbol', 'BTC/USD')
                ts = time.strftime('%m/%d %H:%M', time.gmtime(t.timestamp))
                parts.append(f"  {ts} | {t.side.upper()} {t.quantity:.6f} {coin} @ ${t.price:,.2f} | ${t.value:,.2f}")

        # Performance stats — win rate, drawdown, profit factor, per-coin breakdown
        try:
            perf = self.db.get_performance_stats()
            if perf["total_sells"] > 0:
                parts.append(f"\n## YOUR PERFORMANCE STATS")
                parts.append(f"Total completed trades: {perf['total_sells']} sells out of {perf['total_trades']} total")
                parts.append(f"Win rate: {perf['win_rate']:.1f}% ({perf['winners']}W / {perf['losers']}L)")
                parts.append(f"Average win: ${perf['avg_win']:,.2f} | Average loss: ${perf['avg_loss']:,.2f}")
                parts.append(f"Profit factor: {perf['profit_factor']:.2f} (gross wins / gross losses)")
                parts.append(f"Total realized P&L: ${perf['total_pnl']:,.2f}")
                parts.append(f"Total fees paid: ${perf['total_fees']:,.2f}")
                parts.append(f"Max drawdown: ${perf['max_drawdown']:,.2f}")

                if perf["by_coin"]:
                    parts.append(f"\nPerformance by coin:")
                    for sym, stats in sorted(perf["by_coin"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                        parts.append(
                            f"  {sym}: {stats['win_rate']:.0f}% win rate "
                            f"({stats['wins']}W/{stats['losses']}L) | P&L: ${stats['pnl']:,.2f}"
                        )

                if perf["by_strategy"]:
                    parts.append(f"\nPerformance by strategy:")
                    for strat, stats in sorted(perf["by_strategy"].items(), key=lambda x: x[1]["pnl"], reverse=True):
                        parts.append(
                            f"  {strat}: {stats['win_rate']:.0f}% win rate "
                            f"({stats['wins']}W/{stats['losses']}L) | P&L: ${stats['pnl']:,.2f}"
                        )

                parts.append(f"\nUse these stats to refine your strategy. Double down on what works, cut what doesn't.")
        except Exception as e:
            logger.debug(f"Could not load performance stats: {e}")

        # Strategy journal — persistent memory
        try:
            journal = self.db.get_journal_summary(limit=15)
            if journal:
                parts.append(f"\n## YOUR STRATEGY JOURNAL (persistent memory)")
                parts.append(f"These are YOUR notes from previous cycles. You wrote them. Use them.")
                for entry in journal:
                    ts = time.strftime('%m/%d %H:%M', time.gmtime(entry["timestamp"]))
                    cat = entry.get("category", "note")
                    coin = entry.get("coin", "")
                    coin_tag = f" [{coin}]" if coin else ""
                    parts.append(f"  [{ts}] ({cat}){coin_tag}: {entry['lesson']}")
        except Exception as e:
            logger.debug(f"Could not load journal entries: {e}")

        # Research notebook — long-form thinking (no limit)
        try:
            notes = self.db.get_research_notes()
            if notes:
                parts.append(f"\n## YOUR RESEARCH NOTEBOOK ({len(notes)} notes)")
                parts.append("These are YOUR research notes — hypotheses, deep dives, analysis.")
                parts.append("You wrote them. Reference them when making decisions.")
                parts.append("Mark notes as stale via stale_note_ids when they're outdated.\n")
                for note in notes:
                    ts = time.strftime('%m/%d %H:%M', time.gmtime(note["timestamp"]))
                    topic = note.get("topic", "general")
                    coins = note.get("coins", "")
                    coins_tag = f" [{coins}]" if coins else ""
                    note_id = note["id"]
                    parts.append(f"  --- Note #{note_id} [{ts}] ({topic}){coins_tag} ---")
                    parts.append(f"  {note['title']}")
                    parts.append(f"  {note['body']}")
                    parts.append("")
            else:
                parts.append(f"\n## YOUR RESEARCH NOTEBOOK (empty)")
                parts.append("You haven't written any research notes yet. Use HOLD cycles to analyze the market,")
                parts.append("form hypotheses, and write detailed notes. They persist forever and help you trade smarter.")
        except Exception as e:
            logger.debug(f"Could not load research notebook: {e}")

        # Web research results from last cycle
        if self._last_research and self._last_research.results:
            parts.append(self._researcher.format_for_context(self._last_research))

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

        # Operator directives — standing instructions from chat
        try:
            directives = self.db.get_active_directives()
            if directives:
                parts.append(f"\n## STANDING OPERATOR DIRECTIVES")
                parts.append("Your operator gave you these instructions via chat. Follow them until told otherwise.")
                parts.append("You can mark a directive as completed by including its ID in your response.")
                for d in directives:
                    ts = time.strftime('%m/%d %H:%M', time.gmtime(d["timestamp"]))
                    parts.append(f"  [#{d['id']} {ts}]: {d['directive']}")
        except Exception as e:
            logger.debug(f"Could not load directives: {e}")

        # Recent chat conversation — full context (both sides) so you remember what was discussed
        try:
            recent_chat = self.db.get_chat_history(limit=20)
            if recent_chat:
                # Only show messages from last 24 hours
                cutoff = time.time() - 86400
                recent_chat = [m for m in recent_chat if m.get("timestamp", 0) >= cutoff]
            if recent_chat:
                parts.append(f"\n## RECENT CONVERSATION WITH OPERATOR")
                parts.append("This is YOUR chat history with your operator. You said these things. Remember them.")
                parts.append("If you made promises or acknowledged instructions, follow through.\n")
                for msg in recent_chat:
                    ts = time.strftime('%m/%d %H:%M', time.gmtime(msg.get("timestamp", 0)))
                    role = "Operator" if msg["role"] == "user" else "You"
                    text = msg["message"][:300]
                    parts.append(f"  [{ts}] {role}: {text}")
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
                    "max_tokens": 1500,
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

            # Parse JSON from response — robust extraction
            json_str = content.strip()
            # Strip markdown code blocks
            if json_str.startswith("```"):
                json_str = json_str.split("\n", 1)[1]
                json_str = json_str.rsplit("```", 1)[0].strip()
            # If still not valid JSON, try to find JSON object in the text
            if not json_str.startswith("{"):
                import re as _re
                match = _re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', json_str)
                if match:
                    json_str = match.group(0)
                else:
                    logger.error(f"No JSON found in AI response: {content[:200]}")
                    return AIDecision(action="HOLD", reasoning="No JSON in response")

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
                journal_entry=decision_data.get("journal_entry", ""),
                journal_category=decision_data.get("journal_category", "observation"),
                research_query=decision_data.get("research_query", ""),
                research_note_title=decision_data.get("research_note_title", ""),
                research_note_body=decision_data.get("research_note_body", ""),
                research_note_topic=decision_data.get("research_note_topic", "general"),
                research_note_coins=decision_data.get("research_note_coins", ""),
                stale_note_ids=decision_data.get("stale_note_ids", ""),
            )

            logger.info(
                f"AI Decision: {decision.action} {decision.symbol} | Confidence: {decision.confidence:.2f} | "
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

        # Drawdown circuit breaker — halve position size when in drawdown
        if self._drawdown_active:
            quantity = quantity * 0.5
            logger.info(f"Drawdown breaker: halving buy size to {quantity:.6f} {base_coin}")

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

            # Discord alert
            bal = self.paper_trader.get_balance() if self.paper_trader else None
            await self.discord.send_trade_alert(
                side="buy", symbol=decision.symbol, quantity=quantity,
                price=price, value=price * quantity,
                fee=price * quantity * 0.0026,
                strategy=decision.strategy_used, reasoning=decision.reasoning,
                confidence=decision.confidence,
                cash_usd=bal.cash_usd if bal else 0,
                total_equity=bal.total_equity if bal else 0,
                holdings=bal.holdings if bal else None,
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

        # Drawdown circuit breaker — halve position size
        if self._drawdown_active:
            quantity = quantity * 0.5
            logger.info(f"Drawdown breaker: halving scale-in size to {quantity:.6f} {base_coin}")

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

            bal = self.paper_trader.get_balance() if self.paper_trader else None
            await self.discord.send_trade_alert(
                side="buy", symbol=decision.symbol, quantity=quantity,
                price=price, value=price * quantity,
                fee=price * quantity * 0.0026,
                strategy=f"{decision.strategy_used} (scale-in)",
                reasoning=decision.reasoning, confidence=decision.confidence,
                cash_usd=bal.cash_usd if bal else 0,
                total_equity=bal.total_equity if bal else 0,
                holdings=bal.holdings if bal else None,
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

            # Discord alert
            bal = self.paper_trader.get_balance() if self.paper_trader else None
            if decision.strategy_used == "stop_loss":
                await self.discord.send_stop_loss_alert(
                    symbol=decision.symbol, quantity=quantity,
                    entry_price=position.entry_price, stop_price=price,
                    pnl=pnl, pnl_pct=pnl_pct,
                )
            elif decision.strategy_used == "profit_taking" and pnl > 0:
                await self.discord.send_take_profit_alert(
                    symbol=decision.symbol, quantity=quantity,
                    entry_price=position.entry_price, exit_price=price,
                    pnl=pnl, pnl_pct=pnl_pct,
                )
            else:
                await self.discord.send_trade_alert(
                    side="sell", symbol=decision.symbol, quantity=quantity,
                    price=price, value=price * quantity,
                    fee=price * quantity * 0.0026,
                    strategy=decision.strategy_used, reasoning=decision.reasoning,
                    confidence=decision.confidence,
                    pnl=pnl, pnl_pct=pnl_pct,
                    cash_usd=bal.cash_usd if bal else 0,
                    total_equity=bal.total_equity if bal else 0,
                    holdings=bal.holdings if bal else None,
                )

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

    async def generate_weekly_digest(self):
        """Generate and send the Monday morning weekly digest via Discord."""
        logger.info("Generating weekly digest...")

        # Gather the week's data
        week_seconds = 7 * 86400
        week_cutoff = time.time() - week_seconds

        # Trades from this week
        week_trades = self.db.get_trades_with_pnl(since_ts=week_cutoff)
        week_pnl_data = self.db.get_period_pnl(week_seconds)

        # Performance stats (all-time for context)
        try:
            perf = self.db.get_performance_stats()
        except Exception:
            perf = {}

        # Research notes written this week
        try:
            all_notes = self.db.get_research_notes()
            week_notes = [n for n in all_notes if n["timestamp"] >= week_cutoff]
        except Exception:
            all_notes = []
            week_notes = []

        # Journal entries from this week
        try:
            week_journal = self.db.get_journal_entries(limit=100)
            week_journal = [j for j in week_journal if j["timestamp"] >= week_cutoff]
        except Exception:
            week_journal = []

        # Current portfolio state
        balance = self.paper_trader.get_balance() if self.paper_trader else None
        positions = self.db.get_open_positions()

        # Build the context for Claude to write the digest
        digest_prompt_parts = [
            "You are writing your WEEKLY DIGEST — a Monday morning briefing for your owner.",
            "Write in first person. Be specific, reference actual trades and numbers.",
            "Structure it as: 1) Week in Review, 2) Key Lessons, 3) Plan for This Week.",
            "Be honest about what went wrong. Be specific about what you plan to do differently.",
            "Keep it conversational but informative. Around 500-800 words.",
            "",
            "=== THIS WEEK'S DATA ===",
            "",
        ]

        # Trade summary
        trade_count = week_pnl_data.get("trade_count", 0)
        realized_pnl = week_pnl_data.get("realized_pnl", 0)
        digest_prompt_parts.append(f"Trades executed: {trade_count}")
        digest_prompt_parts.append(f"Realized P&L: ${realized_pnl:,.2f}")

        if week_trades:
            digest_prompt_parts.append("\nTrade details:")
            for t in week_trades[:30]:  # Cap to avoid context overflow
                side = t["side"].upper()
                sym = t.get("symbol", "BTC/USD")
                price = t.get("price", 0)
                qty = t.get("quantity", 0)
                pnl = t.get("pnl_dollar")
                pnl_str = f" | P&L: ${pnl:,.2f}" if pnl is not None else ""
                ts = time.strftime('%m/%d %H:%M', time.gmtime(t["timestamp"]))
                strat = t.get("strategy", "")
                strat_str = f" ({strat})" if strat else ""
                digest_prompt_parts.append(
                    f"  [{ts}] {side} {sym} {qty:.6f} @ ${price:,.4f}{pnl_str}{strat_str}"
                )

        # Win rate this week
        week_sells = [t for t in week_trades if t["side"] == "sell" and t.get("pnl_dollar") is not None]
        week_wins = [t for t in week_sells if t["pnl_dollar"] > 0]
        week_win_rate = (len(week_wins) / len(week_sells) * 100) if week_sells else 0
        digest_prompt_parts.append(f"\nWeek win rate: {week_win_rate:.0f}% ({len(week_wins)}W/{len(week_sells) - len(week_wins)}L)")

        # Portfolio state
        if balance:
            digest_prompt_parts.append(f"\nCurrent cash: ${balance.cash_usd:,.2f}")
            digest_prompt_parts.append(f"Current equity: ${balance.total_equity:,.2f}")
            starting = self.config.paper.starting_capital
            total_pnl = balance.total_equity - starting
            digest_prompt_parts.append(f"All-time P&L: ${total_pnl:,.2f} ({total_pnl/starting*100:+.1f}%)")
            if balance.holdings:
                held = ", ".join(f"{qty:.4f} {sym}" for sym, qty in balance.holdings.items() if qty > 0)
                if held:
                    digest_prompt_parts.append(f"Holdings: {held}")

        if positions:
            digest_prompt_parts.append(f"\nOpen positions: {len(positions)}")
            for pos in positions:
                upnl = pos.unrealized_pnl or 0
                digest_prompt_parts.append(
                    f"  {pos.symbol}: {pos.quantity:.6f} @ ${pos.entry_price:,.2f} (uPnL: ${upnl:,.2f})"
                )

        # Journal entries this week
        if week_journal:
            digest_prompt_parts.append(f"\nYour journal entries this week ({len(week_journal)}):")
            for j in week_journal[:20]:
                ts = time.strftime('%m/%d', time.gmtime(j["timestamp"]))
                digest_prompt_parts.append(f"  [{ts}] ({j.get('category', 'note')}): {j['lesson']}")

        # Research notes this week
        if week_notes:
            digest_prompt_parts.append(f"\nResearch notes written this week ({len(week_notes)}):")
            for n in week_notes[:15]:
                digest_prompt_parts.append(f"  [{n.get('topic', 'general')}] {n['title']}: {n['body'][:200]}")

        # All-time performance context
        if perf:
            digest_prompt_parts.append(f"\nAll-time stats: {perf.get('total_trades', 0)} trades, "
                                       f"{perf.get('win_rate', 0):.0f}% win rate, "
                                       f"profit factor {perf.get('profit_factor', 0):.2f}")

        digest_context = "\n".join(digest_prompt_parts)

        # Call Claude to write the digest
        try:
            resp = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.config.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.config.ai_model,
                    "max_tokens": 2000,
                    "system": (
                        "You are an AI crypto trader writing your weekly digest for your owner. "
                        "Write a thoughtful, specific Monday morning briefing. Use markdown formatting "
                        "that works in Discord (bold, bullet points). Be honest and direct. "
                        "No generic advice — reference YOUR actual trades, YOUR research notes, "
                        "YOUR specific numbers. End with a clear plan for the week ahead."
                    ),
                    "messages": [{"role": "user", "content": digest_context}],
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            data = resp.json()
            digest_text = data["content"][0]["text"]
        except Exception as e:
            logger.error(f"Failed to generate weekly digest: {e}")
            digest_text = (
                f"*Could not generate AI digest this week — error: {e}*\n\n"
                f"Quick stats: {trade_count} trades, ${realized_pnl:,.2f} realized P&L, "
                f"{week_win_rate:.0f}% win rate."
            )

        # Send to Discord
        week_stats = {
            "trade_count": trade_count,
            "realized_pnl": realized_pnl,
            "total_bought": week_pnl_data.get("total_bought", 0),
            "total_sold": week_pnl_data.get("total_sold", 0),
            "equity": balance.total_equity if balance else 0,
            "starting_capital": self.config.paper.starting_capital if self.paper_trader else 0,
            "win_rate": week_win_rate,
            "research_notes_count": len(all_notes),
        }

        await self.discord.send_weekly_digest(digest_text, week_stats)
        logger.info("Weekly digest sent to Discord")

    async def close(self):
        """Clean up."""
        await self.discord.close()
        await self.sentiment.close()
        await self._http.aclose()
