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
from alerts import AlertManager
from derivatives_data import DerivativesDataFetcher
from onchain_data import OnChainDataFetcher
from macro_data import MacroDataFetcher
from social_sentiment import SocialSentimentFetcher
from liquidation_data import LiquidationDataFetcher
from volume_profile import VolumeProfileAnalyzer
from backtester import Backtester, run_backtest_from_tag
from agent_runner import AgentRunner, WakeManager, WakeConfig

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the PORTFOLIO MANAGER (PM) of a multi-coin crypto trading desk on Kraken.
You are Claude Opus — the smartest model in the room. You run the show.

## WHO YOU ARE
You're the decision maker. You have a team of 7 specialist AI agents (powered by Haiku) who
do the legwork — research, analysis, monitoring, backtesting. They report to YOU. You read their
reports, synthesize the big picture, and make the actual trading decisions.

You're one continuous mind. You have persistent memory through your journal, research notebook,
and agent reports. You remember conversations with your operator. You follow through on promises.

## HOW YOU TALK
Talk like a real person. A sharp PM who runs a crypto desk.
- "The desk is telling me funding rates are getting spicy while on-chain shows distribution. I'm trimming."
- NOT: "Analysis of multiple data streams suggests elevated risk parameters."
Write like you'd brief your boss, not a whitepaper.

## YOUR TEAM — 7 SPECIALIST AGENTS
You have a full trading desk. They run every 5 minutes and file reports for you.

RESEARCH TEAM:
  1. Market Research Agent — news, sentiment analysis, macro events, Reddit scanning
  2. Technical Analysis Agent — pattern recognition, multi-timeframe analysis, indicator synthesis
  3. On-Chain Agent — whale movements, exchange flows, network health, stablecoin supply
  4. Derivatives Agent — funding rates, options flow, liquidation analysis, L/S ratios

EXECUTION TEAM:
  5. Order Manager Agent — order book analysis, fill optimization, pending order monitoring
  6. Risk Manager Agent — position sizing, correlation analysis, drawdown monitoring, risk scoring
  7. Backtest Agent — strategy testing against historical data, performance validation

HOW TO USE YOUR TEAM — TASK TAGS:
[TASK: agent=market_research, title=Research ETH Pectra upgrade, priority=3, instructions=Find latest news on ETH Pectra upgrade timeline and trading implications]
[TASK: agent=backtest, title=Test EMA crossover on SOL, priority=5, instructions=[BACKTEST: strategy=ema_crossover, pair=SOL/USD, interval=60, hours=336, fast=9, slow=21]]
[TASK: agent=setup_tracker, title=Watch BTC 60K support, priority=2, instructions=Alert me if BTC drops below 60000 or if buying volume spikes above 60500]
[TASK: agent=technical, title=Deep dive on AVAX chart, priority=4, instructions=Full technical breakdown of AVAX across all timeframes]

- agent = market_research, technical, onchain, derivatives, order_manager, risk_manager, backtest, setup_tracker
- priority = 1 (critical) to 10 (low). Priority 1-3 tasks get done first.
- Use these LIBERALLY. Your agents are cheap to run. Delegate everything that isn't a trade decision.

## YOUR GOALS
1. Grow the USD cash balance — take profits when the setup is right
2. Accumulate crypto — buy dips on coins you believe in
Balance these based on what the market is doing.

## TRADEABLE COINS
BTC/USD, ETH/USD, SOL/USD, DOGE/USD, ADA/USD, AVAX/USD, LINK/USD, DOT/USD, POL/USD, XRP/USD

## RISK RULES
- Aggressive but smart. Go big on high-conviction setups, small on speculative ones.
- You can scale in, partial sell, trail stops — your call.
- DRAWDOWN BREAKER: 5%+ drawdown from peak = system halves your sizes automatically.
- FEE RULE: Kraken charges 0.26% per trade (0.52% round trip). Sells under 0.6% profit get blocked (except stop-losses).
- No shorting, no futures — spot only.
- LISTEN TO YOUR RISK MANAGER. If they flag HIGH or CRITICAL risk, address it.

## HOW TO TAKE ACTIONS
Write your thoughts naturally, then use ACTION TAGS for anything the system should execute.

TRADE TAGS (one per response max):
[BUY: symbol=BTC/USD, qty=0.001, stop=79000, target=88000, trail=2.0, confidence=0.8, strategy=accumulation]
[SELL: symbol=SOL/USD, qty=0.5, confidence=0.75, strategy=profit_taking]

ADVANCED ORDER TAGS — place orders at specific prices/triggers:
[LIMIT_BUY: symbol=ETH/USD, qty=0.5, price=1800, stop=1700, target=2200, confidence=0.75, strategy=accumulation, expires=48]
[LIMIT_SELL: symbol=BTC/USD, qty=0.01, price=90000, confidence=0.7, strategy=profit_taking, expires=24]
[STOP_LOSS: symbol=BTC/USD, qty=0.05, trigger=75000, confidence=0.9, strategy=stop_loss]
[STOP_LOSS_LIMIT: symbol=BTC/USD, qty=0.05, trigger=75000, price=74800, confidence=0.9, strategy=stop_loss]
[TAKE_PROFIT: symbol=SOL/USD, qty=2.0, trigger=200, confidence=0.8, strategy=profit_taking]
[TAKE_PROFIT_LIMIT: symbol=SOL/USD, qty=2.0, trigger=200, price=199, confidence=0.8, strategy=profit_taking]
[TRAILING_STOP: symbol=BTC/USD, qty=0.05, offset=1500, confidence=0.8, strategy=trend_following]
[TRAILING_STOP_LIMIT: symbol=BTC/USD, qty=0.05, offset=1500, price_offset=200, confidence=0.8, strategy=trend_following]
[ICEBERG: symbol=BTC/USD, qty=0.1, price=77000, visible=0.02, confidence=0.7, strategy=accumulation]

CANCEL PENDING ORDERS:
[CANCEL_ORDER: 3, 7]

- symbol MUST be one of the 10 tradeable pairs
- qty = amount of the coin (not USD)
- stop/target/trail are optional (trail = trailing stop %, 0 = fixed stop)
- confidence must be >= 0.6 to execute
- strategy: momentum, mean_reversion, trend_following, sentiment, accumulation, scaling, profit_taking, stop_loss
- expires = hours until order expires (0 = no expiry, default)

JOURNAL TAG — save a lesson to your persistent memory:
[JOURNAL: category=lesson | Bought SOL too early, should have waited for 4h confirmation next time]

RESEARCH NOTE TAG — save longer analysis to your notebook:
[NOTE: topic=macro, coins=BTC,ETH | Title here | Full body of your research note goes here.]

STALE NOTES — mark outdated notes for removal:
[STALE: 5, 12, 23]

WEB RESEARCH — queue a search for next cycle:
[RESEARCH: bitcoin ETF inflows this week]

ALERT TAGS — set or cancel price/indicator alerts:
[ALERT: coin=ETH, condition=price_below, threshold=1500, reason=watching for breakdown, plan=buy 0.5 ETH if structure holds]
[CANCEL_ALERT: 3, 7]

## PM SESSION STRUCTURE
Each session, you receive:
1. AGENT REPORTS — intelligence from your 7 specialists since your last session
2. MARKET DATA — current prices, positions, account state
3. MEMORY — your journal, research notes, chat history, directives

Your job each session:
1. READ all agent reports — acknowledge what's important, dismiss what's noise
2. SYNTHESIZE the big picture — what's the market telling you across all domains?
3. DECIDE — trade, adjust positions, or hold. Explain your reasoning.
4. DELEGATE — create tasks for your agents: what do you need them watching, researching, testing?
5. PLAN — what are you watching for your next session?

## WHAT TO WRITE
Share your thinking in 3-5 paragraphs:
- What are your agents telling you? What stands out from their reports?
- What's the market doing? What's your read on it?
- What are you doing and why? (trade decisions or deliberate holds)
- What tasks are you assigning your team?
- What's your plan until next session?

Your operator sees this on the dashboard — make it worth reading.
Most sessions you'll be holding. That's fine. A great PM knows when not to trade.
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
    """Parsed decision from Claude's conversational response."""
    action: str = "HOLD"
    symbol: str = "BTC/USD"
    quantity: float = 0.0
    order_type: str = "market"  # market, limit, stop-loss, stop-loss-limit, take-profit, take-profit-limit, trailing-stop, trailing-stop-limit, iceberg
    limit_price: float = 0.0   # limit price (for limit, stop-limit, tp-limit, iceberg)
    trigger_price: float = 0.0 # trigger/stop price (for stop-loss, take-profit, trailing)
    price2: float = 0.0        # secondary price (price_offset for trailing-stop-limit)
    offset: float = 0.0        # trailing offset in USD
    visible_size: float = 0.0  # visible portion for iceberg orders
    expires_hours: float = 0.0 # order expiry (0 = no expiry)
    stop_loss: float = 0.0
    take_profit: float = 0.0
    trailing_stop_pct: float = 0.0
    confidence: float = 0.0
    reasoning: str = ""         # Claude's full conversational response (shown on dashboard)
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
        # Derivatives (funding rates, OI, liquidations, options)
        self._derivatives = DerivativesDataFetcher(self._http)
        self._last_derivatives = None
        # On-chain data (exchange flows, mempool, stablecoin supply, network metrics)
        self._onchain = OnChainDataFetcher(self._http)
        self._last_onchain = None
        # Macro/economic data (DXY, SPX, yields, FOMC calendar, regime)
        self._macro = MacroDataFetcher(self._http)
        self._last_macro = None
        # Social sentiment (Reddit, CoinGecko trending)
        self._social = SocialSentimentFetcher(self._http)
        self._last_social = None
        # Liquidation heatmap (long/short ratios, OI, liquidation levels)
        self._liquidation = LiquidationDataFetcher(self._http)
        self._last_liquidation = None
        # Volume profile / VWAP analysis (computed from candles, no API)
        self._volume_analyzer = VolumeProfileAnalyzer()
        self._last_volume_analysis = None
        # Backtester (on-demand historical strategy testing)
        self._backtester = Backtester(self._http)
        self._pending_backtest_result: str = ""
        # Self-alert system
        self._alert_manager = AlertManager(db)
        # v4.0 Multi-agent system
        self._wake_config = WakeConfig(
            min_cooldown_seconds=config.agents.wake_cooldown_seconds,
            max_wakes_per_day=config.agents.max_wakes_per_day,
        )
        self._wake_manager = WakeManager(db, self._wake_config)
        self._agent_runner = AgentRunner(db, config, self._http, self._wake_manager)

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

        # Derivatives data (funding rates, OI, liquidations, options)
        try:
            self._last_derivatives = await self._derivatives.fetch_all()
            if self._last_derivatives.fetch_errors:
                logger.debug(f"Derivatives partial errors: {len(self._last_derivatives.fetch_errors)}")
        except Exception as e:
            logger.debug(f"Derivatives fetch failed: {e}")

        # --- Fetch all new data sources concurrently ---
        async def _fetch_onchain():
            try:
                self._last_onchain = await self._onchain.fetch_all()
            except Exception as e:
                logger.debug(f"On-chain fetch failed: {e}")

        async def _fetch_macro():
            try:
                self._last_macro = await self._macro.fetch_all()
            except Exception as e:
                logger.debug(f"Macro fetch failed: {e}")

        async def _fetch_social():
            try:
                self._last_social = await self._social.fetch_all()
            except Exception as e:
                logger.debug(f"Social sentiment fetch failed: {e}")

        async def _fetch_liquidation():
            try:
                self._last_liquidation = await self._liquidation.fetch_all(btc_price=current_price)
            except Exception as e:
                logger.debug(f"Liquidation data fetch failed: {e}")

        import asyncio as _asyncio
        await _asyncio.gather(
            _fetch_onchain(), _fetch_macro(), _fetch_social(), _fetch_liquidation(),
            return_exceptions=True,
        )

        # Volume profile / VWAP (computed from candle data, no API call)
        try:
            if bars and len(bars) >= 20:
                self._last_volume_analysis = self._volume_analyzer.analyze(bars)
        except Exception as e:
            logger.debug(f"Volume analysis failed: {e}")

        # Self-alerts — check against current market data
        try:
            coin_prices = {"BTC": current_price}
            if market_overview:
                for snap in market_overview.coin_snapshots:
                    coin_prices[snap.symbol] = snap.price
            coin_indicators = {}
            if market_overview:
                for snap in market_overview.coin_snapshots:
                    coin_indicators[snap.symbol] = {
                        "rsi": snap.rsi if hasattr(snap, 'rsi') else 0,
                        "volume_change_pct": snap.volume_24h_change if hasattr(snap, 'volume_24h_change') else 0,
                    }
            triggered_alerts = self._alert_manager.check_alerts(coin_prices, coin_indicators)
            if triggered_alerts:
                for a in triggered_alerts:
                    logger.info(f"ALERT TRIGGERED: #{a.id} {a.coin} {a.condition} {a.threshold} — {a.reason}")
        except Exception as e:
            logger.debug(f"Alert check failed: {e}")

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

        # --- 7b. Build market data for agents (v4.0) ---
        agent_market_data = self.build_agent_market_data(
            current_price, bars, signals, sentiment_data,
            positions, balance, market_overview,
        )

        # --- 7c. Run Haiku agent desk ---
        try:
            await self._agent_runner.run_cycle(agent_market_data)
        except Exception as e:
            logger.warning(f"Agent runner cycle failed: {e}")

        # --- 8. Build prompt and call Opus PM ---
        context = self._build_context(
            current_price, bars, signals, sentiment_data,
            positions, recent_trades, balance, market_overview,
        )
        self._last_context = context  # Cache for chat to reuse
        agent_market_data["raw_context"] = context  # For data agent next cycle

        # Record PM session
        session_id = self.db.start_pm_session(
            session_type="scan",
            trigger_reason="scheduled scan cycle",
        )

        decision = await self._call_claude(context)
        self._last_decision = decision

        # Complete PM session
        self.db.complete_pm_session(
            session_id=session_id,
            summary=decision.reasoning[:200] if decision.reasoning else "",
        )

        # Reset wake escalation after a normal scheduled session
        self._wake_manager.reset_escalation()

        # (Journal, research notes, alerts, and stale note processing all happen
        #  inside _call_claude now — parsed from action tags in the response)

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
            # Advanced order types → save as pending order (checked each scan)
            if decision.order_type != "market":
                action_taken = await self._execute_advanced_order(decision, target_price)

            elif decision.action == "BUY":
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

        # --- Check pending orders for fills ---
        await self._check_pending_orders(current_price, market_overview)

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

    def build_agent_market_data(self, price, bars, signals, sentiment,
                                positions, balance, market_overview=None) -> dict:
        """Build the market_data dict that agents consume.

        Agents get pre-segmented text blocks so each specialist only reads
        the data relevant to its domain.
        """
        data = {
            "btc_price": price,
            "coin_prices": {},
            "coin_data": [],
            "equity": balance.total_equity if balance else 0,
            "peak_equity": self._peak_equity,
            "drawdown_pct": self._drawdown_pct,
        }

        # Coin prices map
        if market_overview and hasattr(market_overview, 'coin_snapshots'):
            for snap in market_overview.coin_snapshots:
                data["coin_prices"][snap.symbol] = snap.price
                data["coin_data"].append({
                    "symbol": snap.symbol,
                    "price": snap.price,
                    "rsi": snap.rsi if hasattr(snap, 'rsi') else 50,
                    "change_1h": snap.change_1h if hasattr(snap, 'change_1h') else 0,
                    "change_24h": snap.change_24h if hasattr(snap, 'change_24h') else 0,
                })

        # Technical text for TechnicalAgent
        tech_parts = []
        if signals:
            tech_parts.append(f"BTC/USD: ${price:,.2f}")
            tech_parts.append(f"EMA {signals.ema.fast_ema:.0f}/{signals.ema.slow_ema:.0f} cross={signals.ema.crossover}")
            tech_parts.append(f"RSI: {signals.rsi.rsi:.1f} ({signals.rsi.signal})")
            tech_parts.append(f"BB: pos={signals.bollinger.price_position:.2%} bw={signals.bollinger.bandwidth:.4f}")
            if signals.atr:
                tech_parts.append(f"ATR: ${signals.atr.atr:.2f} ({signals.atr.volatility})")
        data["technical_text"] = "\n".join(tech_parts)

        # Multi-timeframe
        if self._last_mtf_signals:
            data["mtf_text"] = self._scanner.format_mtf_for_ai(self._last_mtf_signals)
        else:
            data["mtf_text"] = ""

        # Coin data text for TechnicalAgent
        if market_overview and market_overview.coin_snapshots:
            data["coin_data_text"] = self._scanner.format_for_ai(market_overview)
        else:
            data["coin_data_text"] = ""

        # Volume text
        if self._last_volume_analysis:
            data["volume_text"] = self._volume_analyzer.format_for_context(self._last_volume_analysis, price)
        else:
            data["volume_text"] = ""

        # Sentiment/news for MarketResearchAgent
        if sentiment:
            data["sentiment_text"] = (
                f"Fear & Greed: {sentiment.fear_greed_value} ({sentiment.fear_greed_label})\n"
                f"Yesterday: {sentiment.fear_greed_yesterday} | Week ago: {sentiment.fear_greed_week_ago}\n"
                f"Volume trend: {sentiment.volume_trend} ({sentiment.volume_24h_change_pct:+.1f}%)\n"
                f"1h momentum: {sentiment.price_momentum_1h:+.2f}%\n"
                f"24h momentum: {sentiment.price_momentum_24h:+.2f}%\n"
                f"News: {sentiment.news_sentiment_summary}"
            )
            data["news_headlines"] = sentiment.news_headlines or []
        else:
            data["sentiment_text"] = ""
            data["news_headlines"] = []

        # Social for MarketResearchAgent
        if self._last_social:
            data["social_summary"] = self._social.format_for_context(self._last_social)
        else:
            data["social_summary"] = ""

        # Macro for MarketResearchAgent
        if self._last_macro:
            data["macro_text"] = self._macro.format_for_context(self._last_macro)
        else:
            data["macro_text"] = ""

        # On-chain for OnChainAgent
        if self._last_onchain:
            data["onchain_text"] = self._onchain.format_for_context(self._last_onchain)
        else:
            data["onchain_text"] = ""

        # Whale data for OnChainAgent
        if self._last_whale_data:
            data["whale_text"] = self._whale_monitor.format_for_context(self._last_whale_data)
        else:
            data["whale_text"] = ""

        # Derivatives for DerivativesAgent
        if self._last_derivatives:
            data["derivatives_text"] = self._derivatives.format_for_context(self._last_derivatives)
        else:
            data["derivatives_text"] = ""

        # Liquidation for DerivativesAgent
        if self._last_liquidation:
            data["liquidation_text"] = self._liquidation.format_for_context(self._last_liquidation)
        else:
            data["liquidation_text"] = ""

        # Order book for OrderManagerAgent
        if self._last_order_book:
            ob = self._last_order_book
            data["orderbook_text"] = (
                f"Spread: ${ob.get('spread', 0):,.2f} ({ob.get('spread_pct', 0):.4f}%)\n"
                f"Bid depth: ${ob.get('bid_depth_usd', 0):,.0f} | Ask depth: ${ob.get('ask_depth_usd', 0):,.0f}\n"
                f"Imbalance: {ob.get('imbalance', 0):+.3f}\n"
                f"Bid wall: ${ob.get('bid_wall_price', 0):,.2f} | Ask wall: ${ob.get('ask_wall_price', 0):,.2f}"
            )
        else:
            data["orderbook_text"] = ""

        # Balance for RiskManagerAgent
        if balance:
            data["balance_text"] = (
                f"Cash: ${balance.cash_usd:,.2f}\n"
                f"Equity: ${balance.total_equity:,.2f}\n"
                f"Holdings: {json.dumps(balance.holdings or {})}"
            )
        else:
            data["balance_text"] = ""

        # Positions for RiskManagerAgent
        if positions:
            pos_lines = []
            for p in (positions if isinstance(positions, list) else [positions]):
                upnl = p.unrealized_pnl or 0
                pos_lines.append(
                    f"{p.symbol}: {p.quantity:.6f} @ ${p.entry_price:,.2f} "
                    f"(uPnL: ${upnl:,.2f}, SL: ${p.stop_loss:,.2f}, TP: ${p.take_profit:,.2f})"
                )
            data["positions_text"] = "\n".join(pos_lines)
        else:
            data["positions_text"] = "No open positions"

        # Raw context — full text for data condensation
        # (Built by _build_context below, set after)
        data["raw_context"] = ""

        return data

    def _build_context(self, price, bars, signals, sentiment, positions, trades, balance, market_overview=None) -> str:
        """Build the data context string for Opus PM sessions.

        v4.0: Now includes agent reports as the PRIMARY intelligence source.
        Raw data is still included but agents have pre-analyzed it.
        """
        parts = []

        # ── Agent Intelligence Briefing (v4.0) ──
        unread_reports = self.db.get_unread_reports(limit=30)
        if unread_reports:
            parts.append("## AGENT INTELLIGENCE BRIEFING")
            parts.append(f"Your team has filed {len(unread_reports)} reports since your last session.\n")

            # Group by agent type
            by_agent = {}
            for r in unread_reports:
                at = r["agent_type"]
                if at not in by_agent:
                    by_agent[at] = []
                by_agent[at].append(r)

            agent_labels = {
                "market_research": "📰 Market Research",
                "technical": "📊 Technical Analysis",
                "onchain": "⛓️ On-Chain Intelligence",
                "derivatives": "📈 Derivatives Desk",
                "order_manager": "📋 Order Manager",
                "risk_manager": "🛡️ Risk Manager",
                "backtest": "🧪 Backtester",
                "setup_tracker": "🎯 Setup Tracker",
                "wake_system": "🚨 Wake System",
            }

            for agent_type, reports in by_agent.items():
                label = agent_labels.get(agent_type, agent_type)
                parts.append(f"\n### {label} ({len(reports)} reports)")
                for r in reports:
                    sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡"}.get(r["severity"], "⚪")
                    ts = time.strftime('%m/%d %H:%M', time.gmtime(r["created_at"]))
                    parts.append(f"  {sev_icon} [{ts}] {r['title']}")
                    if r["summary"]:
                        parts.append(f"     {r['summary'][:300]}")

            # Mark all as read
            self.db.mark_reports_read([r["id"] for r in unread_reports])
        else:
            parts.append("## AGENT INTELLIGENCE BRIEFING")
            parts.append("No new reports from your team since last session.")

        # ── Wake events ──
        recent_wakes = self.db.get_wake_events_since(time.time() - 7200)
        unacked = [w for w in recent_wakes if not w.get("acknowledged")]
        if unacked:
            parts.append(f"\n## ⚠️ WAKE EVENTS ({len(unacked)} unacknowledged)")
            for w in unacked:
                ts = time.strftime('%m/%d %H:%M', time.gmtime(w["created_at"]))
                parts.append(f"  🚨 [{w['severity'].upper()}] {w['trigger_type']}: {w['reason']}")
            self.db.acknowledge_wake_events()

        # ── PM session budget ──
        usage = self.db.get_pm_token_usage_today()
        parts.append(f"\n## SESSION INFO")
        parts.append(f"Today: {usage['session_count']} PM sessions so far")
        wake_events_today = self.db.get_wake_events_since(time.time() - 86400)
        remaining_wakes = max(0, self._wake_config.max_wakes_per_day - len(wake_events_today))
        parts.append(f"Emergency wakes remaining today: {remaining_wakes}/{self._wake_config.max_wakes_per_day}")

        # Current price
        parts.append(f"\n## CURRENT MARKET DATA")
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

        # Derivatives data (funding rates, OI, liquidations, options)
        if self._last_derivatives:
            deriv_ctx = self._derivatives.format_for_context(self._last_derivatives)
            if deriv_ctx:
                parts.append(deriv_ctx)

        # On-chain data (exchange flows, mempool, stablecoins, network metrics)
        if self._last_onchain:
            onchain_ctx = self._onchain.format_for_context(self._last_onchain)
            if onchain_ctx:
                parts.append(onchain_ctx)

        # Macro/economic data (DXY, SPX, yields, FOMC, regime)
        if self._last_macro:
            macro_ctx = self._macro.format_for_context(self._last_macro)
            if macro_ctx:
                parts.append(macro_ctx)

        # Social sentiment (Reddit, trending coins)
        if self._last_social:
            social_ctx = self._social.format_for_context(self._last_social)
            if social_ctx:
                parts.append(social_ctx)

        # Liquidation heatmap (long/short ratios, OI, liquidation levels)
        if self._last_liquidation:
            liq_ctx = self._liquidation.format_for_context(self._last_liquidation)
            if liq_ctx:
                parts.append(liq_ctx)

        # Volume profile / VWAP
        if self._last_volume_analysis:
            vol_ctx = self._volume_analyzer.format_for_context(self._last_volume_analysis, price)
            if vol_ctx:
                parts.append(vol_ctx)

        # Self-alerts (active + recently triggered)
        alert_ctx = self._alert_manager.format_for_context()
        if alert_ctx:
            parts.append(alert_ctx)

        # Backtest results (from previous cycle's [BACKTEST:] tag)
        if self._pending_backtest_result:
            parts.append(f"\n## BACKTEST RESULTS")
            parts.append(self._pending_backtest_result)
            self._pending_backtest_result = ""  # Clear after showing once

        # Pending orders
        pending = self.db.get_pending_orders()
        if pending:
            parts.append(f"\n## PENDING ORDERS ({len(pending)})")
            for o in pending:
                base = SYMBOL_MAP.get(o["symbol"], {}).get("base", "???")
                age_hrs = (time.time() - o["created_at"]) / 3600
                exp_str = ""
                if o["expires_at"] > 0:
                    remaining = (o["expires_at"] - time.time()) / 3600
                    exp_str = f" | Expires in {remaining:.1f}h" if remaining > 0 else " | EXPIRED"
                parts.append(
                    f"  #{o['id']}: {o['order_type'].upper()} {o['side'].upper()} "
                    f"{o['quantity']:.6f} {base} @ ${o['price']:,.2f} "
                    f"(placed {age_hrs:.1f}h ago{exp_str})"
                )
            parts.append("Use [CANCEL_ORDER: id] to cancel any of these.")

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
        """Call Claude and parse action tags from conversational response."""
        import re as _re

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
                        {"role": "user", "content": f"Here's your latest market data. Share your thinking and act if you see a setup.\n\n{context}"}
                    ],
                },
            )
            resp.raise_for_status()
            result = resp.json()
            content = result["content"][0]["text"]

            # --- Parse action tags from conversational response ---
            decision = AIDecision(raw_response=content)

            # The conversational text (minus tags) becomes the reasoning shown on dashboard
            clean_text = content

            # Parse all trade/order tags
            _ORDER_TAGS = (
                "LIMIT_BUY|LIMIT_SELL|STOP_LOSS_LIMIT|STOP_LOSS|"
                "TAKE_PROFIT_LIMIT|TAKE_PROFIT|TRAILING_STOP_LIMIT|TRAILING_STOP|"
                "ICEBERG|BUY|SELL"
            )
            trade_match = _re.search(
                r'\[(' + _ORDER_TAGS + r'):\s*(.*?)\]', content, _re.IGNORECASE
            )
            if trade_match:
                raw_action = trade_match.group(1).upper()
                params_str = trade_match.group(2)

                # Map tag to action (BUY/SELL) and order_type
                _ORDER_TYPE_MAP = {
                    "BUY": ("BUY", "market"),
                    "SELL": ("SELL", "market"),
                    "LIMIT_BUY": ("BUY", "limit"),
                    "LIMIT_SELL": ("SELL", "limit"),
                    "STOP_LOSS": ("SELL", "stop-loss"),
                    "STOP_LOSS_LIMIT": ("SELL", "stop-loss-limit"),
                    "TAKE_PROFIT": ("SELL", "take-profit"),
                    "TAKE_PROFIT_LIMIT": ("SELL", "take-profit-limit"),
                    "TRAILING_STOP": ("SELL", "trailing-stop"),
                    "TRAILING_STOP_LIMIT": ("SELL", "trailing-stop-limit"),
                    "ICEBERG": ("BUY", "iceberg"),
                }
                action, order_type = _ORDER_TYPE_MAP.get(raw_action, ("HOLD", "market"))
                decision.action = action
                decision.order_type = order_type

                # Parse key=value pairs from the tag
                params = {}
                for pair in _re.findall(r'(\w+)\s*=\s*([^,\]]+)', params_str):
                    params[pair[0].lower().strip()] = pair[1].strip()

                # Symbol
                raw_symbol = params.get("symbol", "BTC/USD")
                if "/" not in raw_symbol and raw_symbol.endswith("USD"):
                    raw_symbol = raw_symbol[:-3] + "/USD"
                elif "/" not in raw_symbol:
                    raw_symbol = raw_symbol + "/USD"
                if raw_symbol not in SYMBOL_MAP:
                    logger.warning(f"Unknown symbol '{raw_symbol}' from AI, defaulting to BTC/USD")
                    raw_symbol = "BTC/USD"
                decision.symbol = raw_symbol

                decision.quantity = float(params.get("qty", 0))
                decision.stop_loss = float(params.get("stop", 0))
                decision.take_profit = float(params.get("target", 0))
                decision.trailing_stop_pct = float(params.get("trail", 0))
                decision.confidence = float(params.get("confidence", 0))
                decision.strategy_used = params.get("strategy", "")
                decision.expires_hours = float(params.get("expires", 0))

                # Price fields for advanced orders
                decision.limit_price = float(params.get("price", 0))
                decision.trigger_price = float(params.get("trigger", 0))
                decision.price2 = float(params.get("price_offset", 0))
                decision.visible_size = float(params.get("visible", 0))
                decision.offset = float(params.get("offset", 0))

                clean_text = _re.sub(r'\[(' + _ORDER_TAGS + r'):\s*.*?\]', '', clean_text, flags=_re.IGNORECASE)

            # Parse [CANCEL_ORDER: id, id, ...]
            cancel_order_match = _re.search(r'\[CANCEL_ORDER:\s*([\d,\s]+)\]', content)
            if cancel_order_match:
                for oid in cancel_order_match.group(1).split(","):
                    oid = oid.strip()
                    if oid.isdigit():
                        try:
                            self.db.cancel_pending_order(int(oid))
                            logger.info(f"Pending order #{oid} cancelled by AI")
                        except Exception as e:
                            logger.warning(f"Failed to cancel order #{oid}: {e}")
                clean_text = _re.sub(r'\[CANCEL_ORDER:\s*[\d,\s]+\]', '', clean_text)

            # Parse [JOURNAL: category=... | text]
            journal_match = _re.search(r'\[JOURNAL:\s*(.*?)\]', content)
            if journal_match:
                jtext = journal_match.group(1)
                category = "observation"
                if "|" in jtext:
                    cat_part, lesson = jtext.split("|", 1)
                    cat_match = _re.search(r'category\s*=\s*(\w+)', cat_part)
                    if cat_match:
                        category = cat_match.group(1)
                    jtext = lesson.strip()
                try:
                    coin = SYMBOL_MAP.get(decision.symbol, {}).get("base", "")
                    self.db.add_journal_entry(
                        lesson=jtext, category=category, coin=coin,
                        strategy=decision.strategy_used,
                        confidence=decision.confidence or 0.5,
                        source="ai_scan_cycle",
                    )
                    logger.info(f"Journal saved: [{category}] {jtext[:80]}...")
                except Exception as e:
                    logger.warning(f"Failed to save journal: {e}")
                clean_text = _re.sub(r'\[JOURNAL:\s*.*?\]', '', clean_text)

            # Parse [NOTE: topic=..., coins=... | title | body]
            note_match = _re.search(r'\[NOTE:\s*(.*?)\]', content, _re.DOTALL)
            if note_match:
                ntext = note_match.group(1)
                topic = "general"
                coins = ""
                # Extract metadata before first |
                if "|" in ntext:
                    parts = ntext.split("|", 2)
                    meta = parts[0]
                    title = parts[1].strip() if len(parts) > 1 else "Untitled"
                    body = parts[2].strip() if len(parts) > 2 else title
                    topic_m = _re.search(r'topic\s*=\s*(\w+)', meta)
                    if topic_m:
                        topic = topic_m.group(1)
                    coins_m = _re.search(r'coins\s*=\s*([\w,]+)', meta)
                    if coins_m:
                        coins = coins_m.group(1)
                else:
                    title = ntext[:60]
                    body = ntext
                try:
                    nid = self.db.add_research_note(
                        title=title, body=body, topic=topic,
                        coins=coins, source="ai_scan_cycle",
                    )
                    logger.info(f"Research note #{nid} saved: [{topic}] {title[:60]}...")
                except Exception as e:
                    logger.warning(f"Failed to save research note: {e}")
                clean_text = _re.sub(r'\[NOTE:\s*.*?\]', '', clean_text, flags=_re.DOTALL)

            # Parse [STALE: id, id, ...]
            stale_match = _re.search(r'\[STALE:\s*([\d,\s]+)\]', content)
            if stale_match:
                for nid in stale_match.group(1).split(","):
                    nid = nid.strip()
                    if nid.isdigit():
                        try:
                            self.db.mark_research_note_stale(int(nid))
                            logger.info(f"Research note #{nid} marked stale")
                        except Exception as e:
                            logger.warning(f"Failed to mark note #{nid} stale: {e}")
                clean_text = _re.sub(r'\[STALE:\s*[\d,\s]+\]', '', clean_text)

            # Parse [RESEARCH: query]
            research_match = _re.search(r'\[RESEARCH:\s*(.*?)\]', content)
            if research_match:
                self._pending_research_query = research_match.group(1).strip()
                logger.info(f"Research queued: {self._pending_research_query}")
                clean_text = _re.sub(r'\[RESEARCH:\s*.*?\]', '', clean_text)

            # Parse [ALERT: coin=..., condition=..., threshold=..., reason=..., plan=...]
            alert_match = _re.search(r'\[ALERT:\s*(.*?)\]', content)
            if alert_match:
                aparams = {}
                for pair in _re.findall(r'(\w+)\s*=\s*([^,\]]+)', alert_match.group(1)):
                    aparams[pair[0].lower().strip()] = pair[1].strip()
                if aparams.get("coin") and aparams.get("condition") and aparams.get("threshold"):
                    try:
                        aid = self._alert_manager.create_alert(
                            coin=aparams["coin"].upper(),
                            condition=aparams["condition"],
                            threshold=float(aparams["threshold"]),
                            reason=aparams.get("reason", ""),
                            action_plan=aparams.get("plan", ""),
                        )
                        logger.info(f"Self-alert #{aid} created")
                    except Exception as e:
                        logger.warning(f"Failed to create alert: {e}")
                clean_text = _re.sub(r'\[ALERT:\s*.*?\]', '', clean_text)

            # Parse [CANCEL_ALERT: id, id, ...]
            cancel_match = _re.search(r'\[CANCEL_ALERT:\s*([\d,\s]+)\]', content)
            if cancel_match:
                for aid in cancel_match.group(1).split(","):
                    aid = aid.strip()
                    if aid.isdigit():
                        try:
                            self._alert_manager.cancel_alert(int(aid))
                            logger.info(f"Alert #{aid} cancelled")
                        except Exception as e:
                            logger.warning(f"Failed to cancel alert #{aid}: {e}")
                clean_text = _re.sub(r'\[CANCEL_ALERT:\s*[\d,\s]+\]', '', clean_text)

            # Parse [DIRECTIVE: instruction]
            directive_match = _re.search(r'\[DIRECTIVE:\s*(.*?)\]', content)
            if directive_match:
                try:
                    self.db.add_directive(directive_match.group(1).strip())
                except Exception:
                    pass
                clean_text = _re.sub(r'\[DIRECTIVE:\s*.*?\]', '', clean_text)

            # v4.0: Parse [TASK: agent=..., title=..., priority=..., instructions=...]
            task_matches = _re.findall(r'\[TASK:\s*(.*?)\]', content, _re.DOTALL)
            tasks_created = 0
            for tmatch in task_matches:
                tparams = {}
                for pair in _re.findall(r'(\w+)\s*=\s*([^,\]]+)', tmatch):
                    tparams[pair[0].lower().strip()] = pair[1].strip()
                agent_type = tparams.get("agent", "general")
                title = tparams.get("title", "Untitled task")
                priority = int(tparams.get("priority", "5"))
                instructions = tparams.get("instructions", "")
                try:
                    tid = self.db.create_agent_task(
                        task_type=agent_type,
                        title=title,
                        instructions=instructions,
                        agent_type=agent_type,
                        priority=priority,
                        created_by="opus",
                    )
                    tasks_created += 1
                    logger.info(f"PM created task #{tid} for {agent_type}: {title}")
                except Exception as e:
                    logger.warning(f"Failed to create agent task: {e}")
            if task_matches:
                clean_text = _re.sub(r'\[TASK:\s*.*?\]', '', clean_text, flags=_re.DOTALL)

            # Parse [BACKTEST: strategy=..., pair=..., interval=..., hours=..., ...]
            backtest_match = _re.search(r'\[BACKTEST:\s*(.*?)\]', content)
            if backtest_match:
                bt_tag = backtest_match.group(0)
                try:
                    import asyncio as _aio
                    bt_result = await run_backtest_from_tag(bt_tag, self._http)
                    if bt_result:
                        self._pending_backtest_result = bt_result
                        logger.info(f"Backtest queued: {bt_tag}")
                except Exception as e:
                    logger.warning(f"Backtest failed: {e}")
                clean_text = _re.sub(r'\[BACKTEST:\s*.*?\]', '', clean_text)

            # Clean text = what the operator sees on the dashboard
            decision.reasoning = clean_text.strip()

            logger.info(
                f"AI Decision: {decision.action} {decision.symbol} | "
                f"Confidence: {decision.confidence:.2f} | Strategy: {decision.strategy_used}"
            )
            logger.info(f"AI Thinking: {decision.reasoning[:200]}...")

            return decision

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
                # Full close — remove ALL position rows for this symbol
                self.db.close_all_positions_for_symbol(decision.symbol)
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

    async def _execute_advanced_order(self, decision: AIDecision, current_price: float) -> str:
        """Place an advanced (non-market) order — either as pending in paper mode, or on Kraken in live."""
        sym_info = SYMBOL_MAP.get(decision.symbol, SYMBOL_MAP["BTC/USD"])
        base_coin = sym_info["base"]
        order_type = decision.order_type

        if self.is_paper:
            # In paper mode, save to pending_orders and check each scan
            oid = self.db.create_pending_order(
                symbol=decision.symbol,
                side=decision.action.lower(),
                price=decision.limit_price or decision.trigger_price or decision.offset,
                quantity=decision.quantity,
                stop_loss=decision.stop_loss,
                take_profit=decision.take_profit,
                trailing_stop_pct=decision.trailing_stop_pct,
                strategy=decision.strategy_used,
                reasoning=decision.reasoning[:200],
                confidence=decision.confidence,
                expires_hours=decision.expires_hours,
            )
            # Store the full order_type so we know how to check fills
            self.db.conn.execute(
                "UPDATE pending_orders SET order_type=? WHERE id=?",
                (order_type, oid),
            )
            self.db.conn.commit()

            price_label = decision.limit_price or decision.trigger_price or decision.offset
            logger.info(
                f"PENDING {order_type.upper()} {decision.action} {decision.symbol}: "
                f"{decision.quantity:.6f} {base_coin} @ ${price_label:,.2f} | Order #{oid}"
            )
            self.db.log("ORDER",
                f"Pending {order_type} {decision.action.lower()} {decision.symbol}: "
                f"{decision.quantity:.6f} @ ${price_label:,.2f} (#{oid})")

            # Discord notification
            await self.discord.send_trade_alert(
                side=f"pending_{decision.action.lower()}",
                symbol=decision.symbol,
                quantity=decision.quantity,
                price=price_label,
                value=price_label * decision.quantity,
                fee=0,
                strategy=f"{order_type}_{decision.strategy_used}",
                reasoning=f"Limit order placed: {decision.reasoning[:150]}",
                confidence=decision.confidence,
            )

            return f"pending_{order_type}_{decision.action.lower()}"
        else:
            # Live mode — place on Kraken using the universal order method
            try:
                orig_symbol = self.kraken.symbol
                self.kraken.symbol = sym_info["kraken"]

                # Map our order params to Kraken's API params
                price = None
                price2 = None
                oflags = ""

                if order_type == "limit":
                    price = Decimal(str(decision.limit_price))
                elif order_type == "stop-loss":
                    price = Decimal(str(decision.trigger_price))
                elif order_type == "stop-loss-limit":
                    price = Decimal(str(decision.trigger_price))
                    price2 = Decimal(str(decision.limit_price))
                elif order_type == "take-profit":
                    price = Decimal(str(decision.trigger_price))
                elif order_type == "take-profit-limit":
                    price = Decimal(str(decision.trigger_price))
                    price2 = Decimal(str(decision.limit_price))
                elif order_type == "trailing-stop":
                    price = Decimal(str(decision.offset))
                elif order_type == "trailing-stop-limit":
                    price = Decimal(str(decision.offset))
                    price2 = Decimal(str(decision.price2))
                elif order_type == "iceberg":
                    price = Decimal(str(decision.limit_price))
                    oflags = f"viqc" if decision.visible_size else ""

                result = await self.kraken.place_order(
                    side=decision.action.lower(),
                    volume=Decimal(str(round(decision.quantity, 8))),
                    ordertype=order_type,
                    price=price,
                    price2=price2,
                    oflags=oflags,
                    validate=(self.config.mode != "live"),
                )
                self.kraken.symbol = orig_symbol

                # Also save to pending_orders for tracking
                oid = self.db.create_pending_order(
                    symbol=decision.symbol,
                    side=decision.action.lower(),
                    price=float(price) if price else 0,
                    quantity=decision.quantity,
                    stop_loss=decision.stop_loss,
                    take_profit=decision.take_profit,
                    strategy=decision.strategy_used,
                    reasoning=decision.reasoning[:200],
                    confidence=decision.confidence,
                    expires_hours=decision.expires_hours,
                )
                self.db.conn.execute(
                    "UPDATE pending_orders SET order_type=? WHERE id=?",
                    (order_type, oid),
                )
                self.db.conn.commit()

                logger.info(
                    f"LIVE {order_type.upper()} {decision.action} {decision.symbol}: "
                    f"{decision.quantity:.6f} | Kraken order: {result.order_id}"
                )
                return f"{order_type}_{decision.action.lower()}"

            except Exception as e:
                logger.error(f"Advanced order failed for {decision.symbol}: {e}")
                self.kraken.symbol = orig_symbol
                return f"error: {e}"

    async def _check_pending_orders(self, btc_price: float, market_overview=None):
        """Check all pending (paper) orders for fill conditions each scan cycle."""
        if not self.is_paper:
            return  # live orders are on Kraken's matching engine

        # Expire old orders first
        self.db.expire_pending_orders()

        pending = self.db.get_pending_orders()
        if not pending:
            return

        # Build price map for all coins
        coin_prices = {"BTC": btc_price}
        if market_overview and hasattr(market_overview, 'coin_snapshots'):
            for snap in market_overview.coin_snapshots:
                coin_prices[snap.symbol] = snap.price

        for order in pending:
            symbol = order["symbol"]
            base_coin = SYMBOL_MAP.get(symbol, {}).get("base", "BTC")
            current_price = coin_prices.get(base_coin, 0)
            if current_price <= 0:
                continue

            order_type = order["order_type"]
            side = order["side"]
            order_price = order["price"]
            filled = False

            # Check fill conditions based on order type
            if order_type == "limit":
                if side == "buy" and current_price <= order_price:
                    filled = True
                elif side == "sell" and current_price >= order_price:
                    filled = True

            elif order_type == "stop-loss":
                if current_price <= order_price:
                    filled = True

            elif order_type == "stop-loss-limit":
                # Trigger at stop price, but only fill at limit (we simplify: fill at trigger)
                if current_price <= order_price:
                    filled = True

            elif order_type == "take-profit":
                if current_price >= order_price:
                    filled = True

            elif order_type == "take-profit-limit":
                if current_price >= order_price:
                    filled = True

            elif order_type in ("trailing-stop", "trailing-stop-limit"):
                # Track highest price, trigger when price drops by offset from peak
                trail_key = f"pending_{order['id']}"
                trail_data = self._trailing_stops.get(trail_key, {"highest_price": current_price})
                trail_data["highest_price"] = max(trail_data["highest_price"], current_price)
                self._trailing_stops[trail_key] = trail_data
                trigger_price = trail_data["highest_price"] - order_price  # offset in USD
                if current_price <= trigger_price:
                    filled = True

            elif order_type == "iceberg":
                # Treat like limit for paper trading
                if side == "buy" and current_price <= order_price:
                    filled = True
                elif side == "sell" and current_price >= order_price:
                    filled = True

            if filled:
                await self._fill_pending_order(order, current_price)

    async def _fill_pending_order(self, order: dict, fill_price: float):
        """Execute a pending order that has been triggered."""
        symbol = order["symbol"]
        side = order["side"]
        quantity = order["quantity"]
        sym_info = SYMBOL_MAP.get(symbol, SYMBOL_MAP["BTC/USD"])
        base_coin = sym_info["base"]

        logger.info(
            f"FILLING pending {order['order_type']} {side} {symbol}: "
            f"{quantity:.6f} {base_coin} @ ${fill_price:,.2f} (order #{order['id']})"
        )

        try:
            if side == "buy":
                if self.paper_trader:
                    self.paper_trader.execute_buy(
                        price=fill_price, quantity=quantity,
                        symbol=base_coin, display_symbol=symbol,
                        strategy=f"filled_{order['order_type']}_{order.get('strategy', '')}",
                    )
                # Save position
                position = Position(
                    symbol=symbol, side="long", entry_price=fill_price,
                    quantity=quantity, entry_time=time.time(),
                    stop_loss=order.get("stop_loss", 0) or fill_price * 0.95,
                    take_profit=order.get("take_profit", 0) or fill_price * 1.10,
                )
                self.db.save_position(position)

            elif side == "sell":
                if self.paper_trader:
                    self.paper_trader.execute_sell(
                        price=fill_price, quantity=quantity,
                        symbol=base_coin, display_symbol=symbol,
                        strategy=f"filled_{order['order_type']}_{order.get('strategy', '')}",
                    )
                # Close position
                position = self.db.get_open_position(symbol=symbol)
                if position:
                    remaining = position.quantity - quantity
                    min_size = MIN_ORDER_SIZE.get(base_coin, 0.0001)
                    if remaining >= min_size:
                        position.quantity = remaining
                        self.db.save_position(position)
                    else:
                        self.db.close_all_positions_for_symbol(symbol)

            # Mark order as filled
            self.db.fill_pending_order(order["id"])

            # Discord notification
            pnl = 0
            position = self.db.get_open_position(symbol=symbol)
            if position and side == "sell":
                pnl = (fill_price - position.entry_price) * quantity

            bal = self.paper_trader.get_balance() if self.paper_trader else None
            await self.discord.send_trade_alert(
                side=side, symbol=symbol, quantity=quantity,
                price=fill_price, value=fill_price * quantity,
                fee=fill_price * quantity * 0.0026,
                strategy=f"filled_{order['order_type']}",
                reasoning=f"Pending {order['order_type']} order #{order['id']} filled",
                confidence=order.get("confidence", 0),
                pnl=pnl,
                cash_usd=bal.cash_usd if bal else 0,
                total_equity=bal.total_equity if bal else 0,
                holdings=bal.holdings if bal else None,
            )

            self.db.log("TRADE",
                f"FILLED {order['order_type']} {side} {symbol}: "
                f"{quantity:.6f} @ ${fill_price:,.2f} (order #{order['id']})")

        except Exception as e:
            logger.error(f"Failed to fill pending order #{order['id']}: {e}")
            self.db.log("ERROR", f"Pending order fill failed: {e}")

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
