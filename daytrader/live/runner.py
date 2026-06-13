"""Market-hours runner for the autonomous paper-trading desk.

Drives the team through the trading day:
  * near the open  -> Strategist sets the plan
  * each interval  -> Trader runs a cycle (after a hard daily-loss circuit breaker)
  * near the close -> flatten everything, then the Reviewer writes lessons
  * outside RTH    -> idle

Hard risk limits live here in code (daily loss circuit breaker, forced EOD flat)
so they hold regardless of what the LLM decides. Everything persists through the
broker's DB, so a container restart resumes mid-day with positions and memory intact.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from daytrader.live.agents import TradingTeam
from daytrader.live.db import LiveDB
from daytrader.live.market_state import snapshot as build_snapshot
from daytrader.live.paper_broker import PaperBroker

ET = ZoneInfo("America/New_York")
OPEN = dtime(9, 30)
PLAN_BY = dtime(9, 45)
EOD_FLAT = dtime(15, 50)
CLOSE = dtime(16, 0)

INTERVAL_SEC = int(os.environ.get("AGENT_INTERVAL_SECONDS", "900"))      # 15 min
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "3.0"))
START_EQUITY = float(os.environ.get("START_EQUITY", "100000"))


def _now_et() -> datetime:
    return datetime.now(ET)


def _is_rth(now: datetime) -> bool:
    return now.weekday() < 5 and OPEN <= now.time() < CLOSE


def _notify(msg: str):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    try:
        data = json.dumps({"content": msg[:1900]}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:  # noqa: BLE001
        pass


class DeskRunner:
    def __init__(self):
        self.db = LiveDB()
        self.broker = PaperBroker(self.db, starting_equity=START_EQUITY)
        self.team = TradingTeam(self.broker, self.db)
        self._day = None
        self._planned = False
        self._reviewed = False
        self._halted = False
        self._day_start_equity = self.broker.equity()

    # ---- single-cycle entry points (also used by the CLI for testing) ----
    def plan(self):
        snap = build_snapshot(self.broker)
        res = self.team.plan_day(snap)
        _notify(f"📋 Strategist set the plan ({len(res.actions)} actions).")
        return res

    def trade(self):
        snap = build_snapshot(self.broker)
        res = self.team.trade_cycle(snap)
        eq = self.broker.equity()
        if res.actions:
            _notify(f"📈 Trader cycle: {len(res.actions)} actions · equity ${eq:,.0f} · "
                    f"DD {self.broker.drawdown_pct():.1f}%")
        return res

    def review(self):
        snap = build_snapshot(self.broker)
        res = self.team.review_day(snap)
        perf = self.broker.performance()
        _notify(f"🧾 Day review · equity ${self.broker.equity():,.0f} · "
                f"PF {perf.get('profit_factor', 0):.2f} · trades {perf.get('n_trades', 0)}")
        return res

    # ---- the always-on loop ----
    def _new_day(self, now):
        self._day = now.date()
        self._planned = self._reviewed = self._halted = False
        self._day_start_equity = self.broker.equity()
        self.db.log_agent("runner", "new_day", str(self._day))

    def _risk_check(self) -> bool:
        """Return True if the daily loss circuit breaker just tripped."""
        if self._halted or self._day_start_equity <= 0:
            return False
        day_pnl_pct = (self.broker.equity() / self._day_start_equity - 1) * 100
        if day_pnl_pct <= -DAILY_LOSS_LIMIT_PCT:
            self._halted = True
            closed = self.broker.flatten_all(reason="daily_loss_limit")
            self.db.log_agent("runner", "circuit_breaker", f"{day_pnl_pct:.2f}% — flattened {len(closed)}")
            _notify(f"🛑 Daily loss limit hit ({day_pnl_pct:.1f}%). Flattened and halted for the day.")
            return True
        return False

    def run_forever(self):
        _notify(f"🤖 Trading desk online · equity ${self.broker.equity():,.0f}")
        self.db.log_agent("runner", "start", f"equity={self.broker.equity():.2f}")
        while True:
            try:
                now = _now_et()
                if self._day != now.date():
                    self._new_day(now)

                if not _is_rth(now):
                    time.sleep(60)
                    continue

                self._risk_check()
                t = now.time()

                if not self._planned and t < PLAN_BY:
                    self.plan()
                    self._planned = True
                elif t >= EOD_FLAT:
                    if self.broker.positions():
                        self.broker.flatten_all(reason="eod_flat")
                        _notify("🌙 Flattened all positions for EOD.")
                    if not self._reviewed:
                        self.review()
                        self._reviewed = True
                    time.sleep(120)
                    continue
                elif not self._halted:
                    self.trade()

                self.broker.db.record_equity(
                    self.broker.cash(), self.broker.equity(),
                    len(self.broker.positions()), self.broker.drawdown_pct())
                time.sleep(INTERVAL_SEC)
            except Exception as e:  # noqa: BLE001 - never let the loop die
                self.db.log_agent("runner", "error", repr(e))
                time.sleep(60)
