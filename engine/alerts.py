"""
Self-Alert System: Claude sets his own price alerts and condition triggers.

Claude can create alerts during trading scans or chat conversations.
Each scan cycle, all active alerts are checked against current market data.
When triggered, the alert context is injected into the next scan so Claude
knows it fired and can act on it.

Alerts persist in SQLite across reboots.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    """A single alert condition."""
    id: int = 0
    coin: str = ""
    condition: str = ""       # "price_above", "price_below", "rsi_above", "rsi_below", "volume_spike"
    threshold: float = 0.0
    reason: str = ""          # Why Claude set this alert
    action_plan: str = ""     # What Claude plans to do when it triggers
    created_at: float = 0.0
    triggered_at: float = 0.0
    active: bool = True


# SQL for the alerts table (called from database.py init)
ALERTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ai_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coin TEXT NOT NULL,
    condition TEXT NOT NULL,
    threshold REAL NOT NULL,
    reason TEXT DEFAULT '',
    action_plan TEXT DEFAULT '',
    created_at REAL NOT NULL,
    triggered_at REAL DEFAULT 0,
    active INTEGER DEFAULT 1
);
"""


class AlertManager:
    """Manages Claude's self-set alerts."""

    def __init__(self, db):
        self.db = db
        self._recently_triggered: list[Alert] = []  # Alerts that fired this cycle

    def create_alert(self, coin: str, condition: str, threshold: float,
                     reason: str = "", action_plan: str = "") -> int:
        """Create a new alert. Returns the alert ID."""
        cursor = self.db.conn.execute(
            """INSERT INTO ai_alerts (coin, condition, threshold, reason, action_plan, created_at, active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (coin.upper(), condition, threshold, reason, action_plan, time.time()),
        )
        self.db.conn.commit()
        alert_id = cursor.lastrowid
        logger.info(f"Alert #{alert_id} created: {coin} {condition} {threshold} — {reason}")
        return alert_id

    def get_active_alerts(self) -> list[dict]:
        """Get all active alerts."""
        rows = self.db.conn.execute(
            "SELECT * FROM ai_alerts WHERE active = 1 ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def cancel_alert(self, alert_id: int):
        """Cancel an alert."""
        self.db.conn.execute(
            "UPDATE ai_alerts SET active = 0 WHERE id = ?", (alert_id,)
        )
        self.db.conn.commit()
        logger.info(f"Alert #{alert_id} cancelled")

    def check_alerts(self, coin_prices: dict, coin_indicators: dict = None) -> list[Alert]:
        """Check all active alerts against current market data.

        Args:
            coin_prices: {"BTC": 84500.0, "ETH": 1620.0, ...}
            coin_indicators: {"BTC": {"rsi": 72.5, "volume_change_pct": 45.0}, ...}

        Returns:
            List of alerts that just triggered.
        """
        self._recently_triggered = []
        active = self.get_active_alerts()

        for alert_dict in active:
            coin = alert_dict["coin"]
            condition = alert_dict["condition"]
            threshold = alert_dict["threshold"]

            triggered = False

            if condition == "price_above":
                price = coin_prices.get(coin, 0)
                if price > 0 and price >= threshold:
                    triggered = True

            elif condition == "price_below":
                price = coin_prices.get(coin, 0)
                if price > 0 and price <= threshold:
                    triggered = True

            elif condition == "rsi_above" and coin_indicators:
                rsi = (coin_indicators.get(coin) or {}).get("rsi", 0)
                if rsi > 0 and rsi >= threshold:
                    triggered = True

            elif condition == "rsi_below" and coin_indicators:
                rsi = (coin_indicators.get(coin) or {}).get("rsi", 0)
                if rsi > 0 and rsi <= threshold:
                    triggered = True

            elif condition == "volume_spike" and coin_indicators:
                vol_change = (coin_indicators.get(coin) or {}).get("volume_change_pct", 0)
                if abs(vol_change) >= threshold:
                    triggered = True

            if triggered:
                # Mark as triggered and deactivate
                self.db.conn.execute(
                    "UPDATE ai_alerts SET triggered_at = ?, active = 0 WHERE id = ?",
                    (time.time(), alert_dict["id"]),
                )
                self.db.conn.commit()

                alert = Alert(
                    id=alert_dict["id"],
                    coin=coin,
                    condition=condition,
                    threshold=threshold,
                    reason=alert_dict.get("reason", ""),
                    action_plan=alert_dict.get("action_plan", ""),
                    created_at=alert_dict["created_at"],
                    triggered_at=time.time(),
                    active=False,
                )
                self._recently_triggered.append(alert)
                logger.info(
                    f"ALERT #{alert.id} TRIGGERED: {coin} {condition} {threshold} — {alert.reason}"
                )

        return self._recently_triggered

    def format_for_context(self) -> str:
        """Format active alerts + recently triggered for AI context."""
        parts = []

        # Active alerts
        active = self.get_active_alerts()
        if active:
            parts.append(f"\n## YOUR ACTIVE ALERTS ({len(active)} set)")
            parts.append("These are alerts YOU created. They're checked every scan cycle.")
            for a in active:
                ts = time.strftime('%m/%d %H:%M', time.gmtime(a["created_at"]))
                parts.append(
                    f"  #{a['id']}: {a['coin']} {a['condition']} {a['threshold']}"
                    f" — {a.get('reason', '')} → Plan: {a.get('action_plan', 'none')}"
                    f" (set {ts})"
                )

        # Recently triggered
        if self._recently_triggered:
            parts.append(f"\n## ⚡ ALERTS JUST TRIGGERED THIS CYCLE")
            parts.append("These alerts YOU set have just fired. Act on your plan.")
            for a in self._recently_triggered:
                parts.append(
                    f"  ⚡ #{a.id}: {a.coin} {a.condition} hit {a.threshold}"
                    f" — Your reason: {a.reason}"
                    f" — Your plan: {a.action_plan}"
                )

        return "\n".join(parts) if parts else ""
