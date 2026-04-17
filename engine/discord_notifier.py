"""
Discord webhook notifications for the trading bot.

Sends rich embeds to a Discord channel for:
- Trade executions (buy/sell) with full details
- Stop-loss and take-profit triggers
- Periodic equity snapshots
- Drawdown alerts
- Bot startup/shutdown

No bot token needed — uses simple webhook POST.
"""

import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Embed colors (Discord uses decimal, not hex)
COLOR_BUY = 0x2ECC71       # green
COLOR_SELL = 0xE74C3C       # red
COLOR_STOP_LOSS = 0xE74C3C  # red
COLOR_PROFIT = 0xF1C40F     # gold
COLOR_INFO = 0x3498DB       # blue
COLOR_WARNING = 0xE67E22    # orange
COLOR_STARTUP = 0x9B59B6    # purple


class DiscordNotifier:
    """Sends trade alerts and status updates to Discord via webhook."""

    def __init__(self, webhook_url: str = "", http_client: Optional[httpx.AsyncClient] = None):
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_http = http_client is None
        self.enabled = bool(self.webhook_url)
        self._last_equity_notify: float = 0
        self._equity_notify_interval: float = 3600  # 1 hour between equity updates

        if not self.enabled:
            logger.info("Discord notifications disabled (no DISCORD_WEBHOOK_URL)")
        else:
            logger.info("Discord notifications enabled")

    async def send_trade_alert(
        self,
        side: str,
        symbol: str,
        quantity: float,
        price: float,
        value: float,
        fee: float,
        strategy: str = "",
        reasoning: str = "",
        confidence: float = 0.0,
        pnl: float = 0.0,
        pnl_pct: float = 0.0,
        cash_usd: float = 0.0,
        total_equity: float = 0.0,
        holdings: dict = None,
    ):
        """Send a trade execution alert."""
        if not self.enabled:
            return

        is_buy = side.lower() == "buy"
        color = COLOR_BUY if is_buy else COLOR_SELL
        action_emoji = "🟢" if is_buy else "🔴"
        side_label = "BUY" if is_buy else "SELL"

        # Build fields
        fields = [
            {"name": "Coin", "value": symbol, "inline": True},
            {"name": "Side", "value": f"{action_emoji} {side_label}", "inline": True},
            {"name": "Price", "value": f"${price:,.4f}", "inline": True},
            {"name": "Quantity", "value": f"{quantity:.6f}", "inline": True},
            {"name": "Value", "value": f"${value:,.2f}", "inline": True},
            {"name": "Fee", "value": f"${fee:.2f}", "inline": True},
        ]

        if not is_buy and pnl != 0:
            pnl_emoji = "💰" if pnl > 0 else "📉"
            fields.append({
                "name": "P&L",
                "value": f"{pnl_emoji} ${pnl:,.2f} ({pnl_pct:+.2f}%)",
                "inline": True,
            })

        if strategy:
            fields.append({
                "name": "Strategy",
                "value": strategy.replace("ai_", ""),
                "inline": True,
            })

        if confidence > 0:
            fields.append({
                "name": "Confidence",
                "value": f"{confidence:.0%}",
                "inline": True,
            })

        # Balance summary
        balance_parts = []
        if cash_usd > 0:
            balance_parts.append(f"💵 Cash: ${cash_usd:,.2f}")
        if total_equity > 0:
            balance_parts.append(f"📊 Equity: ${total_equity:,.2f}")
        if holdings:
            held = ", ".join(f"{qty:.4f} {sym}" for sym, qty in holdings.items() if qty > 0)
            if held:
                balance_parts.append(f"🪙 Holdings: {held}")

        if balance_parts:
            fields.append({
                "name": "Account",
                "value": "\n".join(balance_parts),
                "inline": False,
            })

        embed = {
            "title": f"{action_emoji} {side_label} {symbol}",
            "color": color,
            "fields": fields,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "footer": {"text": "AlgoTrader"},
        }

        if reasoning:
            embed["description"] = reasoning

        await self._send(embed)

    async def send_stop_loss_alert(
        self,
        symbol: str,
        quantity: float,
        entry_price: float,
        stop_price: float,
        pnl: float,
        pnl_pct: float,
    ):
        """Send a stop-loss trigger alert."""
        if not self.enabled:
            return

        embed = {
            "title": f"🛑 STOP-LOSS {symbol}",
            "color": COLOR_STOP_LOSS,
            "description": f"Stop-loss triggered — position closed",
            "fields": [
                {"name": "Entry", "value": f"${entry_price:,.4f}", "inline": True},
                {"name": "Stop", "value": f"${stop_price:,.4f}", "inline": True},
                {"name": "Qty", "value": f"{quantity:.6f}", "inline": True},
                {"name": "P&L", "value": f"📉 ${pnl:,.2f} ({pnl_pct:+.2f}%)", "inline": False},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "footer": {"text": "AlgoTrader"},
        }
        await self._send(embed)

    async def send_take_profit_alert(
        self,
        symbol: str,
        quantity: float,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
    ):
        """Send a take-profit trigger alert."""
        if not self.enabled:
            return

        embed = {
            "title": f"🎯 TAKE-PROFIT {symbol}",
            "color": COLOR_PROFIT,
            "description": f"Take-profit target hit — locking in gains",
            "fields": [
                {"name": "Entry", "value": f"${entry_price:,.4f}", "inline": True},
                {"name": "Exit", "value": f"${exit_price:,.4f}", "inline": True},
                {"name": "Qty", "value": f"{quantity:.6f}", "inline": True},
                {"name": "P&L", "value": f"💰 ${pnl:,.2f} ({pnl_pct:+.2f}%)", "inline": False},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "footer": {"text": "AlgoTrader"},
        }
        await self._send(embed)

    async def send_equity_update(
        self,
        cash_usd: float,
        total_equity: float,
        starting_capital: float,
        holdings: dict = None,
        positions: list = None,
        drawdown_pct: float = 0.0,
        force: bool = False,
    ):
        """Send periodic equity snapshot. Throttled to once per hour unless forced."""
        if not self.enabled:
            return

        now = time.time()
        if not force and (now - self._last_equity_notify < self._equity_notify_interval):
            return

        self._last_equity_notify = now

        pnl = total_equity - starting_capital
        pnl_pct = (pnl / starting_capital) * 100 if starting_capital > 0 else 0
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        fields = [
            {"name": "Cash", "value": f"${cash_usd:,.2f}", "inline": True},
            {"name": "Equity", "value": f"${total_equity:,.2f}", "inline": True},
            {"name": "P&L", "value": f"{pnl_emoji} ${pnl:,.2f} ({pnl_pct:+.2f}%)", "inline": True},
        ]

        if drawdown_pct > 0:
            fields.append({
                "name": "Drawdown",
                "value": f"⚠️ {drawdown_pct:.1f}%",
                "inline": True,
            })

        if holdings:
            held_lines = []
            for sym, qty in holdings.items():
                if qty > 0:
                    held_lines.append(f"{sym}: {qty:.6f}")
            if held_lines:
                fields.append({
                    "name": "Holdings",
                    "value": "\n".join(held_lines),
                    "inline": False,
                })

        if positions:
            pos_lines = []
            for pos in positions[:5]:
                upnl = pos.unrealized_pnl or 0
                pos_lines.append(
                    f"{pos.symbol}: {pos.quantity:.6f} @ ${pos.entry_price:,.2f} "
                    f"(P&L: ${upnl:,.2f})"
                )
            if pos_lines:
                fields.append({
                    "name": "Open Positions",
                    "value": "\n".join(pos_lines),
                    "inline": False,
                })

        embed = {
            "title": "📊 Equity Snapshot",
            "color": COLOR_INFO,
            "fields": fields,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "footer": {"text": "AlgoTrader — hourly update"},
        }

        await self._send(embed)

    async def send_drawdown_alert(self, drawdown_pct: float, equity: float, peak: float):
        """Send alert when drawdown circuit breaker activates."""
        if not self.enabled:
            return

        embed = {
            "title": "⚠️ DRAWDOWN ALERT",
            "color": COLOR_WARNING,
            "description": (
                f"Drawdown circuit breaker **activated** at {drawdown_pct:.1f}%.\n"
                f"Position sizes are now halved until equity recovers."
            ),
            "fields": [
                {"name": "Peak Equity", "value": f"${peak:,.2f}", "inline": True},
                {"name": "Current Equity", "value": f"${equity:,.2f}", "inline": True},
                {"name": "Drawdown", "value": f"{drawdown_pct:.1f}%", "inline": True},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "footer": {"text": "AlgoTrader"},
        }
        await self._send(embed)

    async def send_startup(self, mode: str, version: str = "2.10.1"):
        """Send bot startup notification."""
        if not self.enabled:
            return

        embed = {
            "title": "🚀 AlgoTrader Online",
            "color": COLOR_STARTUP,
            "description": f"Bot started in **{mode.upper()}** mode",
            "fields": [
                {"name": "Version", "value": f"v{version}", "inline": True},
                {"name": "Mode", "value": mode.upper(), "inline": True},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "footer": {"text": "AlgoTrader"},
        }
        await self._send(embed)

    async def send_weekly_digest(self, digest_text: str, week_stats: dict):
        """Send the Monday morning weekly digest.

        digest_text: Claude's written summary (can be long).
        week_stats: dict with trade_count, realized_pnl, total_bought, total_sold,
                    equity, starting_capital, win_rate, research_notes_count.
        """
        if not self.enabled:
            return

        pnl = week_stats.get("realized_pnl", 0)
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        equity = week_stats.get("equity", 0)
        starting = week_stats.get("starting_capital", 0)
        total_pnl = equity - starting if starting > 0 else 0
        total_pnl_pct = (total_pnl / starting * 100) if starting > 0 else 0

        # Stats embed
        fields = [
            {"name": "Trades This Week", "value": str(week_stats.get("trade_count", 0)), "inline": True},
            {"name": "Week P&L", "value": f"{pnl_emoji} ${pnl:,.2f}", "inline": True},
            {"name": "Win Rate", "value": f"{week_stats.get('win_rate', 0):.0f}%", "inline": True},
            {"name": "Current Equity", "value": f"${equity:,.2f}", "inline": True},
            {"name": "All-Time P&L", "value": f"${total_pnl:,.2f} ({total_pnl_pct:+.1f}%)", "inline": True},
            {"name": "Research Notes", "value": str(week_stats.get("research_notes_count", 0)), "inline": True},
        ]

        embed = {
            "title": "📋 Weekly Digest — Monday Morning Briefing",
            "color": COLOR_INFO,
            "fields": fields,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "footer": {"text": "AlgoTrader — Weekly Digest"},
        }

        # Discord embed description maxes at 4096 chars. Send the AI's
        # written digest as a plain message, then the stats embed separately.
        try:
            # First: the AI's written digest as a plain message
            if len(digest_text) > 1900:
                # Split into chunks for Discord's 2000-char message limit
                chunks = []
                while digest_text:
                    if len(digest_text) <= 1900:
                        chunks.append(digest_text)
                        break
                    # Find a good break point
                    split_at = digest_text.rfind("\n", 0, 1900)
                    if split_at == -1:
                        split_at = 1900
                    chunks.append(digest_text[:split_at])
                    digest_text = digest_text[split_at:].lstrip("\n")

                for i, chunk in enumerate(chunks):
                    header = "**📋 Weekly Digest — Monday Morning Briefing**\n\n" if i == 0 else ""
                    await self._send_message(f"{header}{chunk}")
            else:
                await self._send_message(
                    f"**📋 Weekly Digest — Monday Morning Briefing**\n\n{digest_text}"
                )

            # Then: the stats embed
            await self._send(embed)
        except Exception as e:
            logger.warning(f"Weekly digest send failed: {e}")

    async def _send_message(self, content: str):
        """Send a plain text message to the Discord webhook."""
        try:
            resp = await self._http.post(
                self.webhook_url,
                json={"username": "AlgoTrader", "content": content},
            )
            if resp.status_code >= 400:
                logger.warning(f"Discord message error: {resp.status_code}")
        except Exception as e:
            logger.debug(f"Discord message failed: {e}")

    async def _send(self, embed: dict):
        """Send an embed to the Discord webhook."""
        try:
            resp = await self._http.post(
                self.webhook_url,
                json={
                    "username": "AlgoTrader",
                    "embeds": [embed],
                },
            )
            if resp.status_code == 429:
                # Rate limited — log but don't crash
                logger.warning("Discord rate limited, skipping notification")
            elif resp.status_code >= 400:
                logger.warning(f"Discord webhook error: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.debug(f"Discord notification failed: {e}")

    async def close(self):
        """Clean up."""
        if self._owns_http:
            await self._http.aclose()
