"""
v4.0 Multi-Agent System — The Trading Desk

Opus (the PM) sits at the top making portfolio decisions.
7 Haiku specialist agents do the legwork, each with deep domain expertise.
A Setup Tracker cross-cuts all agents and can wake Opus for emergencies.

RESEARCH TEAM (intelligence gathering):
    1. MarketResearchAgent  — news, sentiment analysis, macro events
    2. TechnicalAgent       — pattern recognition, multi-timeframe analysis
    3. OnChainAgent         — whale movements, exchange flows, network health
    4. DerivativesAgent     — funding rates, options flow, liquidation analysis

EXECUTION TEAM (portfolio operations):
    5. OrderManagerAgent    — optimizing fills, managing the order book
    6. RiskManagerAgent     — position sizing, correlation, drawdown monitoring
    7. BacktestAgent        — constantly testing strategies against historical data

CROSS-CUTTING:
    SetupTracker + WakeManager — monitors conditions, can escalate to Opus

Architecture:
    AgentRunner → manages all agents, runs them concurrently on fast loop
    WakeManager → rate-limited wake-up system (Haiku → Opus)
    BaseAgent   → shared lifecycle (claim task → execute → report)
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from database import Database
from config import BotConfig
from discord_notifier import DiscordNotifier

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  Wake-up system
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class WakeConfig:
    """Controls how often Haiku can wake Opus."""
    min_cooldown_seconds: float = 1800       # 30 minutes minimum between any wakes
    max_wakes_per_day: int = 6               # hard cap per 24 hours
    escalation_multipliers: list = field(default_factory=lambda: [1, 2, 4, 8])
    # Per-trigger-type cooldowns (seconds)
    type_cooldowns: dict = field(default_factory=lambda: {
        "price_move": 1800,
        "volume_spike": 3600,
        "indicator_cross": 1800,
        "liquidation_cascade": 1800,
        "whale_alert": 3600,
        "news_event": 3600,
        "setup_triggered": 1800,
        "drawdown": 7200,
        "risk_breach": 900,     # risk manager gets faster escalation
        "correlation_spike": 3600,
    })


class WakeManager:
    """Manages wake-up events with cooldowns and rate limiting."""

    def __init__(self, db: Database, config: WakeConfig = None,
                 discord: DiscordNotifier = None):
        self.db = db
        self.config = config or WakeConfig()
        self._consecutive_wakes = 0
        self.discord = discord

    def can_wake(self, trigger_type: str, severity: str) -> bool:
        """Check if a wake-up is allowed right now."""
        if severity not in ("high", "critical"):
            return False

        now = time.time()
        day_ago = now - 86400

        # Hard cap: max wakes per day
        day_wakes = self.db.get_wake_events_since(day_ago)
        if len(day_wakes) >= self.config.max_wakes_per_day:
            logger.info(f"Wake blocked: daily limit ({len(day_wakes)}/{self.config.max_wakes_per_day})")
            return False

        # Global cooldown with escalation
        idx = min(self._consecutive_wakes, len(self.config.escalation_multipliers) - 1)
        multiplier = self.config.escalation_multipliers[idx]
        effective_cooldown = self.config.min_cooldown_seconds * multiplier

        if day_wakes:
            last_time = max(w["created_at"] for w in day_wakes)
            elapsed = now - last_time
            if elapsed < effective_cooldown:
                logger.info(f"Wake blocked: cooldown ({effective_cooldown - elapsed:.0f}s left, {multiplier}x escalation)")
                return False

        # Per-trigger-type cooldown
        type_cd = self.config.type_cooldowns.get(trigger_type, 1800)
        type_wakes = self.db.get_wake_events_by_type(trigger_type, now - type_cd)
        if type_wakes:
            logger.info(f"Wake blocked: {trigger_type} cooldown active")
            return False

        return True

    def request_wake(self, trigger_type: str, severity: str, reason: str,
                     data: dict = None) -> Optional[int]:
        """Request a wake-up. Returns event ID if approved, None if blocked."""
        if not self.can_wake(trigger_type, severity):
            # Still queue as a report for Opus's next scheduled session
            self.db.add_agent_report(
                agent_type="wake_system",
                report_type="queued_wake",
                title=f"[{severity.upper()}] {trigger_type}: {reason[:80]}",
                summary=reason,
                data_json=json.dumps(data or {}),
                severity=severity,
            )
            return None

        wake_id = self.db.create_wake_event(
            trigger_type=trigger_type,
            severity=severity,
            reason=reason,
            data_json=json.dumps(data or {}),
        )
        self._consecutive_wakes += 1
        logger.info(f"🚨 WAKE EVENT #{wake_id}: [{severity}] {trigger_type} — {reason}")

        # Send to Discord so the operator knows immediately
        if self.discord:
            asyncio.ensure_future(self.discord.send_wake_alert(trigger_type, severity, reason))

        return wake_id

    def set_discord(self, discord: DiscordNotifier):
        """Attach Discord notifier (called after init)."""
        self.discord = discord

    def reset_escalation(self):
        """Reset consecutive wake counter after a scheduled PM session."""
        self._consecutive_wakes = 0


# ══════════════════════════════════════════════════════════════════════════════
#  Base Agent
# ══════════════════════════════════════════════════════════════════════════════

class BaseAgent:
    """Base class for all Haiku agents."""

    AGENT_TYPE = "base"
    AGENT_NAME = "Base Agent"

    def __init__(self, db: Database, config: BotConfig, http: httpx.AsyncClient,
                 discord: DiscordNotifier = None):
        self.db = db
        self.config = config
        self._http = http
        self.discord = discord

    async def run_cycle(self, market_data: dict = None):
        """Run one cycle: process tasks + autonomous work."""
        # Process assigned tasks
        tasks = self.db.get_pending_agent_tasks(agent_type=self.AGENT_TYPE, limit=3)
        for task in tasks:
            if self.db.claim_agent_task(task["id"], self.AGENT_TYPE):
                try:
                    result = await self.execute_task(task, market_data)
                    self.db.complete_agent_task(task["id"], result=result or "done")
                except Exception as e:
                    logger.error(f"{self.AGENT_NAME} task #{task['id']} failed: {e}")
                    self.db.complete_agent_task(task["id"], error=str(e))
                    # Flag task failures to Discord
                    await self.report_and_notify(
                        report_type="task_error",
                        title=f"Task Failed: {task.get('title', 'unknown')[:50]}",
                        summary=f"{self.AGENT_NAME} task #{task['id']} error: {str(e)[:200]}",
                        severity="high",
                        task_id=task["id"],
                    )

        # Run autonomous work
        try:
            await self.autonomous_work(market_data)
        except Exception as e:
            logger.warning(f"{self.AGENT_NAME} autonomous work error: {e}")
            # Flag autonomous failures — these are data/API issues
            await self.report_and_notify(
                report_type="system_error",
                title=f"{self.AGENT_NAME}: System Error",
                summary=f"Autonomous work failed: {str(e)[:200]}",
                severity="critical",
            )

    async def execute_task(self, task: dict, market_data: dict = None) -> str:
        """Execute a queued task. Override in subclasses."""
        return "not implemented"

    async def autonomous_work(self, market_data: dict = None):
        """Run autonomous background work. Override in subclasses."""
        pass

    async def report_and_notify(self, report_type: str, title: str,
                                summary: str, body: str = "",
                                severity: str = "info", task_id: int = 0,
                                data: dict = None):
        """Save a report AND send to Discord if severity is high/critical."""
        self.db.add_agent_report(
            agent_type=self.AGENT_TYPE,
            report_type=report_type,
            title=title,
            summary=summary,
            body=body or summary,
            severity=severity,
            task_id=task_id,
            data_json=json.dumps(data or {}),
        )
        # Push to Discord for anything medium or above
        if self.discord and severity in ("medium", "high", "critical"):
            await self.discord.send_agent_alert(
                agent_name=self.AGENT_NAME,
                title=title,
                message=summary,
                severity=severity,
                data=data,
            )

    async def call_haiku(self, system_prompt: str, user_message: str,
                         max_tokens: int = 500) -> str:
        """Call Claude Haiku for cheap, fast processing."""
        api_key = self.config.anthropic_api_key
        if not api_key:
            return ""
        try:
            resp = await self._http.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.config.haiku_model,
                    "max_tokens": max_tokens,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_message}],
                },
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        except Exception as e:
            logger.warning(f"Haiku call failed ({self.AGENT_NAME}): {e}")
            return ""


# ══════════════════════════════════════════════════════════════════════════════
#  RESEARCH TEAM
# ══════════════════════════════════════════════════════════════════════════════

class MarketResearchAgent(BaseAgent):
    """Agent 1: News, sentiment analysis, macro events.

    Scans headlines, Reddit, Fear & Greed shifts, and macro events.
    Flags narrative changes and breaking news.
    """

    AGENT_TYPE = "market_research"
    AGENT_NAME = "Market Research"

    ANALYSIS_PROMPT = """You are a crypto market research analyst on a trading desk.
Your job: analyze news, sentiment, and macro data to identify tradeable events.

Rules:
- Separate signal from noise. Most news is noise.
- Flag narrative shifts (new bull/bear thesis emerging)
- Rate impact: HIGH (trade within hours), MEDIUM (watch closely), LOW (background)
- Flag any contrarian signals (extreme fear = potential buy, extreme greed = potential sell)
- Be specific and brief — max 250 words."""

    async def autonomous_work(self, market_data: dict = None):
        """Analyze sentiment and news data each cycle."""
        if not market_data:
            return

        # Combine all sentiment/news data
        parts = []
        if market_data.get("sentiment_text"):
            parts.append(f"SENTIMENT:\n{market_data['sentiment_text']}")
        if market_data.get("social_summary"):
            parts.append(f"SOCIAL:\n{market_data['social_summary']}")
        if market_data.get("macro_text"):
            parts.append(f"MACRO:\n{market_data['macro_text']}")
        if market_data.get("news_headlines"):
            parts.append(f"HEADLINES:\n" + "\n".join(f"- {h}" for h in market_data["news_headlines"][:10]))

        if not parts:
            return

        combined = "\n\n".join(parts)

        result = await self.call_haiku(
            system_prompt=self.ANALYSIS_PROMPT,
            user_message=combined[:6000],
            max_tokens=350,
        )

        if result:
            # Determine severity based on content
            severity = "info"
            if any(w in result.lower() for w in ["high impact", "breaking", "critical", "urgent"]):
                severity = "high"
            elif any(w in result.lower() for w in ["medium impact", "notable", "watch"]):
                severity = "medium"

            self.db.add_agent_report(
                agent_type=self.AGENT_TYPE,
                report_type="market_research",
                title="Market Research Briefing",
                summary=result[:200],
                body=result,
                severity=severity,
            )

    async def execute_task(self, task: dict, market_data: dict = None) -> str:
        """Research a specific topic Opus requested."""
        from web_research import WebResearcher

        query = task.get("instructions", "")
        if not query:
            return "no query provided"

        try:
            researcher = WebResearcher(self._http)
            results = await researcher.search(query)

            if results and results.results:
                raw = "\n".join(f"- {r.title}: {r.snippet}" for r in results.results[:8])
                note = await self.call_haiku(
                    system_prompt=self.ANALYSIS_PROMPT,
                    user_message=f"Research query: {query}\n\nFindings:\n{raw}",
                    max_tokens=400,
                )
                if note:
                    self.db.add_research_note(
                        title=f"Research: {query[:60]}",
                        body=note, topic="research", source="market_research_agent",
                    )
                    self.db.add_agent_report(
                        agent_type=self.AGENT_TYPE,
                        report_type="research_note",
                        title=f"Research: {query[:60]}",
                        summary=note[:200], body=note,
                        severity="info", task_id=task["id"],
                    )
                    return note[:200]
            return "no results found"
        except Exception as e:
            return f"research failed: {e}"


class TechnicalAgent(BaseAgent):
    """Agent 2: Pattern recognition, multi-timeframe analysis, indicator synthesis.

    Reads all technical data and produces actionable TA summaries.
    Identifies divergences, confluences, and high-probability setups.
    """

    AGENT_TYPE = "technical"
    AGENT_NAME = "Technical Analysis"

    TA_PROMPT = """You are a crypto technical analysis specialist on a trading desk.
Your job: analyze price action, indicators, and multi-timeframe data to identify setups.

Rules:
- Focus on CONFLUENCE — signals that align across timeframes and indicators
- Flag divergences (price making new high but RSI declining, etc.)
- Identify key levels: support, resistance, VWAP, POC, value area edges
- Rate setup quality: A+ (3+ confirmations), A (2), B (1), C (speculative)
- Note timeframe alignment: 15m/1h/4h all bullish = strong, mixed = weak
- Max 300 words. Lead with the highest-conviction setup."""

    async def autonomous_work(self, market_data: dict = None):
        """Analyze technical data and identify setups."""
        if not market_data:
            return

        parts = []
        if market_data.get("technical_text"):
            parts.append(market_data["technical_text"])
        if market_data.get("mtf_text"):
            parts.append(market_data["mtf_text"])
        if market_data.get("volume_text"):
            parts.append(market_data["volume_text"])
        if market_data.get("coin_data_text"):
            parts.append(market_data["coin_data_text"])

        if not parts:
            return

        combined = "\n\n".join(parts)

        result = await self.call_haiku(
            system_prompt=self.TA_PROMPT,
            user_message=combined[:8000],
            max_tokens=400,
        )

        if result:
            severity = "info"
            if any(w in result.lower() for w in ["a+ setup", "strong confluence", "high conviction"]):
                severity = "high"
            elif any(w in result.lower() for w in ["a setup", "forming", "watch for"]):
                severity = "medium"

            self.db.add_agent_report(
                agent_type=self.AGENT_TYPE,
                report_type="technical_analysis",
                title="Technical Analysis Report",
                summary=result[:200],
                body=result,
                severity=severity,
            )


class OnChainAgent(BaseAgent):
    """Agent 3: Whale movements, exchange flows, network health.

    Monitors on-chain data for supply/demand signals that precede price moves.
    """

    AGENT_TYPE = "onchain"
    AGENT_NAME = "On-Chain Analysis"

    ONCHAIN_PROMPT = """You are an on-chain analyst on a crypto trading desk.
Your job: interpret blockchain data to find supply/demand signals.

Key signals to flag:
- Exchange inflows (selling pressure) vs outflows (accumulation)
- Whale movements (>100 BTC transfers, large stablecoin mints/burns)
- Mempool congestion (network stress indicator)
- Stablecoin supply changes (new capital entering/leaving)
- Hash rate trends (miner health/confidence)
- MVRV, NVT, or other on-chain valuations if available

Rules:
- Lead with the most actionable signal
- Compare current readings to recent trends (getting better/worse)
- Flag anything that disagrees with price action (on-chain bearish but price rallying = warning)
- Max 200 words."""

    async def autonomous_work(self, market_data: dict = None):
        """Analyze on-chain data."""
        if not market_data:
            return

        onchain_text = market_data.get("onchain_text", "")
        whale_text = market_data.get("whale_text", "")

        if not onchain_text and not whale_text:
            return

        combined = f"{onchain_text}\n\n{whale_text}".strip()

        result = await self.call_haiku(
            system_prompt=self.ONCHAIN_PROMPT,
            user_message=combined[:5000],
            max_tokens=250,
        )

        if result:
            severity = "info"
            if any(w in result.lower() for w in ["massive", "unusual", "diverge", "warning"]):
                severity = "medium"

            self.db.add_agent_report(
                agent_type=self.AGENT_TYPE,
                report_type="onchain_analysis",
                title="On-Chain Intelligence",
                summary=result[:200],
                body=result,
                severity=severity,
            )


class DerivativesAgent(BaseAgent):
    """Agent 4: Funding rates, options flow, liquidation analysis.

    Monitors leverage, positioning, and liquidation risk.
    """

    AGENT_TYPE = "derivatives"
    AGENT_NAME = "Derivatives Analysis"

    DERIV_PROMPT = """You are a crypto derivatives analyst on a trading desk.
Your job: analyze funding rates, open interest, liquidation levels, and options flow.

Key signals:
- Funding rates: positive = longs paying shorts (crowded longs), negative = shorts paying longs
- Open interest changes: rising OI + rising price = new money, rising OI + falling price = short buildup
- Liquidation clusters: where are the leveraged positions? Price gets pulled toward these "magnets"
- Long/short ratios: extreme readings are contrarian signals
- Options: put/call ratio shifts, large put buying = hedging/bearish

Rules:
- Flag any extreme readings (funding >0.05%, OI spike >20%, L/S ratio >2 or <0.5)
- Identify liquidation magnets — clusters that price may be drawn toward
- Rate the leverage environment: healthy, elevated, dangerous
- Max 250 words."""

    async def autonomous_work(self, market_data: dict = None):
        """Analyze derivatives data."""
        if not market_data:
            return

        parts = []
        if market_data.get("derivatives_text"):
            parts.append(market_data["derivatives_text"])
        if market_data.get("liquidation_text"):
            parts.append(market_data["liquidation_text"])

        if not parts:
            return

        combined = "\n\n".join(parts)

        result = await self.call_haiku(
            system_prompt=self.DERIV_PROMPT,
            user_message=combined[:5000],
            max_tokens=300,
        )

        if result:
            severity = "info"
            if any(w in result.lower() for w in ["dangerous", "extreme", "cascade", "squeeze"]):
                severity = "high"
            elif any(w in result.lower() for w in ["elevated", "crowded", "imbalanced"]):
                severity = "medium"

            self.db.add_agent_report(
                agent_type=self.AGENT_TYPE,
                report_type="derivatives_analysis",
                title="Derivatives Intelligence",
                summary=result[:200],
                body=result,
                severity=severity,
            )


# ══════════════════════════════════════════════════════════════════════════════
#  EXECUTION TEAM
# ══════════════════════════════════════════════════════════════════════════════

class OrderManagerAgent(BaseAgent):
    """Agent 5: Order optimization, fill management, order book analysis.

    Recommends optimal order types and prices based on order book state.
    Monitors pending orders and suggests adjustments.
    """

    AGENT_TYPE = "order_manager"
    AGENT_NAME = "Order Manager"

    ORDER_PROMPT = """You are an order execution specialist on a crypto trading desk.
Your job: optimize how orders are placed and filled.

Analyze:
- Order book depth and imbalance — where's the liquidity?
- Spread conditions — is it tight enough for market orders, or should we use limits?
- Pending orders — should any be adjusted based on current conditions?
- Slippage risk — for the position sizes we trade, how much slippage should we expect?

Rules:
- For each pending order, evaluate: keep/adjust price/cancel
- Suggest optimal order type for current conditions
- Flag any liquidity concerns (thin books, wide spreads)
- Max 200 words."""

    async def autonomous_work(self, market_data: dict = None):
        """Monitor order book and pending orders."""
        if not market_data:
            return

        ob_text = market_data.get("orderbook_text", "")
        pending = self.db.get_pending_orders()

        if not ob_text and not pending:
            return

        parts = []
        if ob_text:
            parts.append(f"ORDER BOOK:\n{ob_text}")
        if pending:
            pending_text = "\n".join(
                f"#{o['id']}: {o['order_type']} {o['side']} {o['symbol']} "
                f"{o['quantity']:.6f} @ ${o['price']:,.2f} (status: {o['status']})"
                for o in pending
            )
            parts.append(f"PENDING ORDERS:\n{pending_text}")

        combined = "\n\n".join(parts)

        result = await self.call_haiku(
            system_prompt=self.ORDER_PROMPT,
            user_message=combined[:4000],
            max_tokens=250,
        )

        if result:
            self.db.add_agent_report(
                agent_type=self.AGENT_TYPE,
                report_type="order_management",
                title="Order Book & Execution Report",
                summary=result[:200],
                body=result,
                severity="info",
            )


class RiskManagerAgent(BaseAgent):
    """Agent 6: Position sizing, correlation analysis, drawdown monitoring.

    The safety net. Monitors portfolio risk and can wake Opus for emergencies.
    """

    AGENT_TYPE = "risk_manager"
    AGENT_NAME = "Risk Manager"

    RISK_PROMPT = """You are the risk manager on a crypto trading desk.
Your job: protect capital and flag any risk concerns.

Monitor:
- Portfolio concentration — is too much capital in one coin?
- Drawdown status — how far from peak equity? Is it accelerating?
- Correlation risk — are all positions moving together?
- Position sizing — are any positions outsized for the account?
- Unrealized P&L — any positions that should be cut?
- Fee drag — is overtrading eating into returns?

Rules:
- Be conservative. Your job is to prevent blowups.
- Flag RISK BREACH if any hard limit is exceeded
- Recommend specific position adjustments (reduce X by Y%)
- Rate overall portfolio risk: LOW / MODERATE / ELEVATED / HIGH / CRITICAL
- Max 250 words."""

    def __init__(self, db: Database, config: BotConfig, http: httpx.AsyncClient,
                 wake_manager: WakeManager, discord: DiscordNotifier = None):
        super().__init__(db, config, http, discord=discord)
        self.wake_manager = wake_manager

    async def autonomous_work(self, market_data: dict = None):
        """Monitor portfolio risk every cycle."""
        if not market_data:
            return

        # Build risk context
        parts = []

        # Account state
        if market_data.get("balance_text"):
            parts.append(f"ACCOUNT:\n{market_data['balance_text']}")

        # Positions
        if market_data.get("positions_text"):
            parts.append(f"POSITIONS:\n{market_data['positions_text']}")

        # Drawdown
        drawdown = market_data.get("drawdown_pct", 0)
        peak_equity = market_data.get("peak_equity", 0)
        equity = market_data.get("equity", 0)
        parts.append(f"DRAWDOWN: {drawdown:.1f}% (peak: ${peak_equity:,.2f}, current: ${equity:,.2f})")

        # Performance stats
        try:
            perf = self.db.get_performance_stats()
            parts.append(
                f"PERFORMANCE: win rate {perf['win_rate']:.0f}%, "
                f"profit factor {perf['profit_factor']:.2f}, "
                f"total P&L ${perf['total_pnl']:,.2f}, "
                f"fees ${perf['total_fees']:,.2f}"
            )
        except Exception:
            pass

        if not parts:
            return

        combined = "\n\n".join(parts)

        result = await self.call_haiku(
            system_prompt=self.RISK_PROMPT,
            user_message=combined[:5000],
            max_tokens=300,
        )

        if result:
            severity = "info"
            if "RISK BREACH" in result.upper() or "CRITICAL" in result.upper():
                severity = "critical"
                # Wake Opus for critical risk issues
                self.wake_manager.request_wake(
                    trigger_type="risk_breach",
                    severity="critical",
                    reason=f"Risk manager flagged critical issue: {result[:100]}",
                    data={"drawdown": drawdown, "equity": equity},
                )
            elif "HIGH" in result.upper() or "ELEVATED" in result.upper():
                severity = "high"

            await self.report_and_notify(
                report_type="risk_assessment",
                title="Risk Assessment",
                summary=result[:200],
                body=result,
                severity=severity,
            )


class BacktestAgent(BaseAgent):
    """Agent 7: Constantly testing strategies against historical data.

    Runs backtests Opus requests, and also autonomously tests variations
    of current strategies to find improvements.
    """

    AGENT_TYPE = "backtest"
    AGENT_NAME = "Backtester"

    BACKTEST_SUMMARY_PROMPT = """Summarize this backtest result for a portfolio manager.
Focus on: profitability, risk (max drawdown, Sharpe), win rate, and whether the strategy
should be deployed, modified, or abandoned. Be specific with numbers. Max 150 words."""

    async def execute_task(self, task: dict, market_data: dict = None) -> str:
        """Run a backtest from Opus's instructions."""
        from backtester import run_backtest_from_tag

        instructions = task.get("instructions", "")
        if not instructions:
            return "no backtest instructions"

        try:
            result = await run_backtest_from_tag(instructions, self._http)
            if result:
                summary = await self.call_haiku(
                    system_prompt=self.BACKTEST_SUMMARY_PROMPT,
                    user_message=result,
                    max_tokens=200,
                )
                self.db.add_agent_report(
                    agent_type=self.AGENT_TYPE,
                    report_type="backtest_result",
                    title=f"Backtest: {task['title'][:60]}",
                    summary=summary[:200] if summary else result[:200],
                    body=f"{summary}\n\n--- RAW ---\n{result}" if summary else result,
                    severity="info",
                    task_id=task["id"],
                )
                return summary[:200] if summary else "completed"
            return "no results"
        except Exception as e:
            return f"failed: {e}"

    async def autonomous_work(self, market_data: dict = None):
        """Periodically test strategy variations autonomously."""
        # Only run autonomous backtests occasionally (every ~10 cycles)
        if int(time.time()) % 600 > 60:  # rough ~10% of cycles
            return

        # Pick a random strategy variation to test
        from backtester import Backtester
        strategies = ["ema_crossover", "rsi_reversal", "bollinger_bounce", "vwap_reversion"]

        import random
        strat = random.choice(strategies)
        tag = f"[BACKTEST: strategy={strat}, pair=BTC/USD, interval=60, hours=168]"

        try:
            from backtester import run_backtest_from_tag
            result = await run_backtest_from_tag(tag, self._http)
            if result:
                summary = await self.call_haiku(
                    system_prompt=self.BACKTEST_SUMMARY_PROMPT,
                    user_message=result,
                    max_tokens=150,
                )
                if summary:
                    self.db.add_agent_report(
                        agent_type=self.AGENT_TYPE,
                        report_type="auto_backtest",
                        title=f"Auto-test: {strat}",
                        summary=summary[:200],
                        body=f"{summary}\n\n--- RAW ---\n{result}",
                        severity="info",
                    )
        except Exception as e:
            logger.debug(f"Auto-backtest failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  Setup Tracker (cross-cutting wake-up system)
# ══════════════════════════════════════════════════════════════════════════════

class SetupTracker(BaseAgent):
    """Cross-cutting monitor that watches all conditions and can wake Opus.

    Checks price moves, indicator extremes, and Opus-defined setups.
    Only HIGH and CRITICAL events can trigger a wake-up.
    """

    AGENT_TYPE = "setup_tracker"
    AGENT_NAME = "Setup Tracker"

    def __init__(self, db: Database, config: BotConfig, http: httpx.AsyncClient,
                 wake_manager: WakeManager, discord: DiscordNotifier = None):
        super().__init__(db, config, http, discord=discord)
        self.wake_manager = wake_manager
        self._last_btc_price: float = 0
        self._last_prices: dict = {}

    async def autonomous_work(self, market_data: dict = None):
        """Check all tracked conditions."""
        if not market_data:
            return

        btc_price = market_data.get("btc_price", 0)
        coin_prices = market_data.get("coin_prices", {})
        coin_data = market_data.get("coin_data", [])

        # === Price move detection ===
        if self._last_btc_price > 0 and btc_price > 0:
            pct = abs(btc_price - self._last_btc_price) / self._last_btc_price * 100
            if pct >= 3.0:
                direction = "up" if btc_price > self._last_btc_price else "down"
                sev = "critical" if pct >= 5.0 else "high"
                self.wake_manager.request_wake(
                    trigger_type="price_move", severity=sev,
                    reason=f"BTC {pct:.1f}% {direction} (${self._last_btc_price:,.0f} → ${btc_price:,.0f})",
                    data={"btc_price": btc_price, "change_pct": pct, "direction": direction},
                )

        # Check all coins for big moves
        for coin, price in coin_prices.items():
            last = self._last_prices.get(coin, 0)
            if last > 0 and price > 0:
                pct = abs(price - last) / last * 100
                if pct >= 5.0:
                    direction = "up" if price > last else "down"
                    self.wake_manager.request_wake(
                        trigger_type="price_move", severity="high",
                        reason=f"{coin} {pct:.1f}% {direction}",
                        data={"coin": coin, "price": price, "change_pct": pct},
                    )

        # === Indicator extremes ===
        for cd in coin_data:
            sym = cd.get("symbol", "")
            rsi = cd.get("rsi", 50)
            if rsi >= 85 or rsi <= 15:
                label = "extremely overbought" if rsi >= 85 else "extremely oversold"
                self.db.add_agent_report(
                    agent_type=self.AGENT_TYPE,
                    report_type="indicator_extreme",
                    title=f"{sym} RSI {rsi:.0f} — {label}",
                    summary=f"{sym} RSI at {rsi:.0f}. Potential reversal.",
                    severity="medium",
                )

        # === Check Opus-defined custom setups ===
        tasks = self.db.get_pending_agent_tasks(agent_type="setup_tracker", limit=20)
        for task in tasks:
            await self._check_setup(task, market_data)

        # Update price memory
        self._last_btc_price = btc_price
        self._last_prices = dict(coin_prices)

    async def _check_setup(self, task: dict, market_data: dict):
        """Check if a custom setup condition is met."""
        instructions = task.get("instructions", "")
        if not instructions:
            return

        eval_msg = (
            f"SETUP CONDITION: {instructions}\n"
            f"CONTEXT: {task.get('context', '')}\n\n"
            f"CURRENT DATA:\n"
            f"BTC: ${market_data.get('btc_price', 0):,.2f}\n"
            f"Prices: {json.dumps(market_data.get('coin_prices', {}))}\n\n"
            f"Respond EXACTLY: TRIGGERED: [reason] | NOT_YET: [status] | EXPIRED: [reason]"
        )

        result = await self.call_haiku(
            system_prompt="Evaluate if a trading setup condition is met. Only say TRIGGERED if clearly met.",
            user_message=eval_msg,
            max_tokens=100,
        )

        if result and result.startswith("TRIGGERED"):
            reason = result.replace("TRIGGERED:", "").strip()
            if self.db.claim_agent_task(task["id"], self.AGENT_TYPE):
                self.db.complete_agent_task(task["id"], result=reason)

            self.db.add_agent_report(
                agent_type=self.AGENT_TYPE,
                report_type="setup_triggered",
                title=f"Setup: {task['title'][:60]}",
                summary=reason[:200],
                body=f"Condition: {instructions}\nResult: {reason}",
                severity="high",
                task_id=task["id"],
            )

            if task.get("priority", 5) <= 3:
                self.wake_manager.request_wake(
                    trigger_type="setup_triggered", severity="high",
                    reason=f"Setup triggered: {task['title'][:60]}",
                    data={"task_id": task["id"]},
                )
        elif result and result.startswith("EXPIRED"):
            if self.db.claim_agent_task(task["id"], self.AGENT_TYPE):
                self.db.complete_agent_task(task["id"], result="expired")


# ══════════════════════════════════════════════════════════════════════════════
#  Agent Runner (orchestrator)
# ══════════════════════════════════════════════════════════════════════════════

class AgentRunner:
    """Orchestrates all 7 Haiku agents + setup tracker.

    Runs on the fast loop (every agent_interval seconds, default 300 = 5 min).
    """

    def __init__(self, db: Database, config: BotConfig, http: httpx.AsyncClient,
                 wake_manager: WakeManager, discord: DiscordNotifier = None):
        self.db = db
        self.config = config
        self._http = http
        self.wake_manager = wake_manager
        self.discord = discord

        # Research Team
        self.market_research = MarketResearchAgent(db, config, http, discord=discord)
        self.technical = TechnicalAgent(db, config, http, discord=discord)
        self.onchain = OnChainAgent(db, config, http, discord=discord)
        self.derivatives = DerivativesAgent(db, config, http, discord=discord)

        # Execution Team
        self.order_manager = OrderManagerAgent(db, config, http, discord=discord)
        self.risk_manager = RiskManagerAgent(db, config, http, wake_manager, discord=discord)
        self.backtester = BacktestAgent(db, config, http, discord=discord)

        # Cross-cutting
        self.setup_tracker = SetupTracker(db, config, http, wake_manager, discord=discord)

        self._all_agents = [
            self.market_research, self.technical, self.onchain, self.derivatives,
            self.order_manager, self.risk_manager, self.backtester,
            self.setup_tracker,
        ]

    async def run_cycle(self, market_data: dict = None):
        """Run all agents concurrently."""
        logger.info(f"Agent desk: running {len(self._all_agents)} agents")
        start = time.time()

        results = await asyncio.gather(
            *[agent.run_cycle(market_data) for agent in self._all_agents],
            return_exceptions=True,
        )

        for agent, result in zip(self._all_agents, results):
            if isinstance(result, Exception):
                logger.warning(f"{agent.AGENT_NAME} failed: {result}")

        elapsed = time.time() - start
        logger.info(f"Agent desk: cycle complete ({elapsed:.1f}s)")

    def has_pending_wake(self) -> Optional[dict]:
        """Check for unacknowledged wake events (last 5 min)."""
        wakes = self.db.get_wake_events_since(time.time() - 300)
        unacked = [w for w in wakes if not w.get("acknowledged")]
        return unacked[0] if unacked else None

    def get_agent_status(self) -> list[dict]:
        """Get status of all agents for dashboard."""
        return [
            {"name": a.AGENT_NAME, "type": a.AGENT_TYPE}
            for a in self._all_agents
        ]
