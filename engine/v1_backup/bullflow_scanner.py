"""
Bullflow Options Flow Scanner
==============================
Connects to the Bullflow real-time SSE alert stream and auto-executes trades
via Tastytrade when a high-score signal is detected.

Alert payload (from Bullflow API):
  {
    "alertType": "algo" | "custom",
    "symbol":    "O:AMD251205C00120000",   # OCC option symbol
    "alertName": "Urgent Repeater",
    "alertPremium": 16965.0,
    "timestamp": 1764708086.0
  }

OCC symbol format: O:{TICKER}{YY}{MM}{DD}{C|P}{STRIKE * 1000 zero-padded to 8}
  e.g. O:AMD251205C00120000  →  AMD, exp 2025-12-05, Call, $120 strike

Strategy parameters (all configurable in UI):
  - minPremium          : float  (default 25000)   minimum alert premium in $
  - minScore            : float  (default 5.0)     composite score threshold to execute
  - callsOnly           : bool   (default true)    only take call signals
  - excludeEtfs         : bool   (default true)    skip ETF underlyings
  - maxContracts        : int    (default 1)        contracts to buy per signal
  - execution           : str    (default "calls")  "calls" or "stock"
  - otmOffset           : int    (default 1)        strikes above current price for calls
  - minDTE              : int    (default 7)        minimum days to expiry
  - maxDTE              : int    (default 60)       maximum days to expiry
  - scoreWeightPremium  : float  (default 0.6)     weight for premium in score
  - scoreWeightRepeater : float  (default 0.4)     weight for "Repeater" alert type
"""
import asyncio
import json
import logging
import re
from datetime import datetime, date

import httpx

import api_client
from config import BULLFLOW_API_KEY

logger = logging.getLogger("bullflow_scanner")

# ── Well-known ETF tickers to skip ────────────────────────────────────────────
KNOWN_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG", "XLF", "XLE",
    "XLK", "XLV", "XLI", "XLU", "XLP", "XLY", "XLB", "XLRE", "SMH", "ARKK",
    "SQQQ", "TQQQ", "SPXU", "UVXY", "VIX", "VXX", "VIXY", "EEM", "EFA",
}

# ── OCC symbol parser ─────────────────────────────────────────────────────────
_OCC_RE = re.compile(
    r"^O:([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$"
)


def parse_occ(symbol: str) -> dict | None:
    """
    Parse an OCC option symbol string.
    Returns dict with ticker, expiry (date), option_type ('C'|'P'),
    strike (float), dte (int) or None if unrecognisable.
    """
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    ticker, yy, mm, dd, opt_type, strike_raw = m.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
        dte = (expiry - date.today()).days
        strike = int(strike_raw) / 1000.0
    except (ValueError, OverflowError):
        return None
    return {
        "ticker": ticker,
        "expiry": expiry.isoformat(),
        "option_type": opt_type,   # 'C' or 'P'
        "strike": strike,
        "dte": dte,
    }


def score_alert(alert: dict, params: dict) -> float:
    """
    Compute a composite score for an alert (0–10 scale).
    Higher = stronger signal.

    Factors:
      - Premium size relative to minPremium threshold
      - Alert name contains "Repeater" (institutional accumulation pattern)
    """
    w_premium  = float(params.get("scoreWeightPremium",  0.6))
    w_repeater = float(params.get("scoreWeightRepeater", 0.4))

    premium     = float(alert.get("alertPremium", 0))
    min_premium = float(params.get("minPremium", 25000))

    # Premium sub-score: scales from 0 to 10 as premium grows 1x→5x threshold
    premium_score = min(10.0, (premium / max(min_premium, 1)) * 2.0)

    # Repeater sub-score: full points if alert name includes "Repeater"
    alert_name = alert.get("alertName", "")
    repeater_score = 10.0 if "repeater" in alert_name.lower() else 0.0

    return round(w_premium * premium_score + w_repeater * repeater_score, 2)


class BullflowScanner:
    """
    Long-running SSE consumer that:
      1. Streams alerts from api.bullflow.io/v1/streaming/alerts
      2. Filters by user-configured params
      3. Scores each alert
      4. On score >= minScore, places a Tastytrade order (or dry-runs it)
      5. Logs every signal + decision to the AlgoTrader dashboard
    """

    STREAM_URL = "https://api.bullflow.io/v1/streaming/alerts"
    RECONNECT_DELAY = 10  # seconds between reconnect attempts

    def __init__(self, strategy_config: dict, session=None, account=None):
        self.strategy_id: int = strategy_config["id"]
        self.account_id: int  = strategy_config["accountId"]
        self.name: str        = strategy_config["name"]
        self.session          = session
        self.account          = account
        self._running         = False
        self._daily_count     = 0
        self._logger          = logging.getLogger(f"bullflow.{self.name}")

        raw = strategy_config.get("parameters", "{}")
        self.params: dict = json.loads(raw) if isinstance(raw, str) else (raw or {})

        self.max_daily_trades: int  = strategy_config.get("maxDailyTrades", 5)
        self.max_position_size: int = int(strategy_config.get("maxPositionSize", 1))

        # Paper trading = dry run. Defaults to paper for safety.
        trading_mode = strategy_config.get("tradingMode", "paper")
        self.dry_run: bool = (trading_mode != "live")

    # ── Public ─────────────────────────────────────────────────────────────────

    async def run(self):
        """Main entry point — keeps reconnecting until stopped."""
        self._running = True
        self._logger.info("Bullflow scanner '%s' started.", self.name)
        await api_client.post_log(
            "info",
            f"Bullflow scanner '{self.name}' started (mode={'paper' if self.dry_run else 'LIVE'}).",
            strategy_id=self.strategy_id,
        )

        while self._running:
            try:
                await self._stream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.warning("Stream error: %s — reconnecting in %ds", e, self.RECONNECT_DELAY)
                await api_client.post_log(
                    "warning",
                    f"Bullflow stream error: {e}. Reconnecting in {self.RECONNECT_DELAY}s.",
                    strategy_id=self.strategy_id,
                )
                await asyncio.sleep(self.RECONNECT_DELAY)

    async def stop(self):
        self._running = False

    def reset_daily_count(self):
        self._daily_count = 0

    # ── Private ────────────────────────────────────────────────────────────────

    async def _stream(self):
        """Open SSE connection and process incoming events."""
        if not BULLFLOW_API_KEY:
            raise RuntimeError("BULLFLOW_API_KEY is not set in .env")

        params = {"key": BULLFLOW_API_KEY}
        self._logger.info("Connecting to Bullflow SSE stream...")

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", self.STREAM_URL, params=params) as response:
                response.raise_for_status()
                self._logger.info("Bullflow SSE stream connected.")
                await api_client.post_log(
                    "info",
                    f"Bullflow stream connected for '{self.name}'.",
                    strategy_id=self.strategy_id,
                )

                event_data: str | None = None

                async for line in response.aiter_lines():
                    if not self._running:
                        break

                    line = line.strip()

                    if line.startswith("data:"):
                        event_data = line[5:].strip()

                    elif line == "":
                        # Blank line = end of event block
                        if event_data:
                            await self._handle_event(event_data)
                            event_data = None

    async def _handle_event(self, raw: str):
        """Parse a single SSE data payload and decide whether to act."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return

        event_type = payload.get("event", "")

        if event_type == "heartbeat":
            return  # silent keep-alive

        if event_type != "alert":
            return

        alert = payload.get("data", {})
        await self._process_alert(alert)

    async def _process_alert(self, alert: dict):
        """Apply filters, score, and conditionally execute."""
        symbol        = alert.get("symbol", "")
        alert_premium = float(alert.get("alertPremium", 0))
        alert_name    = alert.get("alertName", "Unknown")
        alert_type    = alert.get("alertType", "")

        # ── Parse OCC symbol ──────────────────────────────────────────────────
        parsed = parse_occ(symbol)
        if not parsed:
            self._logger.debug("Unparseable symbol '%s' — skipping.", symbol)
            return

        ticker     = parsed["ticker"]
        option_type = parsed["option_type"]   # 'C' or 'P'
        dte        = parsed["dte"]
        strike     = parsed["strike"]
        expiry     = parsed["expiry"]

        # ── Load current params (live refresh each event) ─────────────────────
        p = self.params

        min_premium  = float(p.get("minPremium",  25000))
        min_score    = float(p.get("minScore",    5.0))
        calls_only   = str(p.get("callsOnly",   "true")).lower() == "true"
        exclude_etfs = str(p.get("excludeEtfs", "true")).lower() == "true"
        min_dte      = int(p.get("minDTE",  7))
        max_dte      = int(p.get("maxDTE", 60))

        # ── Hard filters ─────────────────────────────────────────────────────
        if alert_premium < min_premium:
            return  # too small, common noise

        if calls_only and option_type != "C":
            return

        if exclude_etfs and ticker in KNOWN_ETFS:
            return

        if not (min_dte <= dte <= max_dte):
            return

        # Past expiry
        if dte < 0:
            return

        # ── Score ─────────────────────────────────────────────────────────────
        score = score_alert(alert, p)

        signal_msg = (
            f"[SIGNAL] {ticker} {option_type} ${strike:.0f} exp {expiry} | "
            f"Premium: ${alert_premium:,.0f} | DTE: {dte} | "
            f"Alert: {alert_name} ({alert_type}) | Score: {score:.1f}"
        )
        self._logger.info(signal_msg)
        await api_client.post_log("info", signal_msg, strategy_id=self.strategy_id)

        # ── Execute if score threshold met and daily limit not reached ────────
        if score < min_score:
            await api_client.post_log(
                "info",
                f"[SKIP] {ticker} score {score:.1f} < threshold {min_score} — watching only.",
                strategy_id=self.strategy_id,
            )
            return

        if self._daily_count >= self.max_daily_trades:
            await api_client.post_log(
                "info",
                f"[SKIP] Daily trade limit ({self.max_daily_trades}) reached — not executing.",
                strategy_id=self.strategy_id,
            )
            return

        await self._execute(ticker, option_type, strike, expiry, dte, alert_premium, score, p)

    async def _execute(
        self,
        ticker: str,
        option_type: str,
        strike: float,
        expiry: str,
        dte: int,
        premium: float,
        score: float,
        p: dict,
    ):
        """Place (or dry-run) a Tastytrade order for the signal."""
        execution = p.get("execution", "calls")   # "calls" or "stock"
        contracts = min(int(p.get("maxContracts", 1)), self.max_position_size)

        if self.dry_run:
            msg = (
                f"[DRY RUN] Would buy {contracts}x {ticker} {option_type} "
                f"${strike:.0f} exp {expiry} (score {score:.1f}, premium ${premium:,.0f})"
            )
            self._logger.info(msg)
            await api_client.post_log("info", msg, strategy_id=self.strategy_id)
            self._daily_count += 1
            return

        # ── Live execution ────────────────────────────────────────────────────
        if not self.session or not self.account:
            await api_client.post_log(
                "error",
                f"[ERROR] Cannot execute for '{self.name}' — no Tastytrade session. "
                "Check TT_USERNAME / TT_PASSWORD in .env.",
                strategy_id=self.strategy_id,
            )
            return

        try:
            from order_executor import OrderExecutor
            executor = OrderExecutor(self.session, self.account, self.strategy_id)

            if execution == "stock":
                result = await executor.buy_stock(ticker, contracts)
            else:
                # Buy the specific call from the alert
                result = await executor.buy_option(
                    ticker=ticker,
                    expiry=expiry,
                    strike=strike,
                    option_type=option_type,
                    quantity=contracts,
                )

            if result:
                self._daily_count += 1
                msg = (
                    f"[EXECUTED] {contracts}x {ticker} {option_type} ${strike:.0f} "
                    f"exp {expiry} | Score {score:.1f} | Premium ${premium:,.0f}"
                )
                self._logger.info(msg)
                await api_client.post_log("info", msg, strategy_id=self.strategy_id)

                # Record trade
                await api_client.post_trade({
                    "strategyId": self.strategy_id,
                    "accountId":  self.account_id,
                    "symbol":     f"{ticker} {option_type} {strike} {expiry}",
                    "action":     "BUY_TO_OPEN",
                    "quantity":   contracts,
                    "price":      0,   # filled price returned by executor
                    "status":     "filled",
                    "notes":      f"Bullflow alert: {result}",
                })
            else:
                await api_client.post_log(
                    "error",
                    f"[FAILED] Order for {ticker} returned no result.",
                    strategy_id=self.strategy_id,
                )

        except Exception as e:
            self._logger.exception("Order execution error: %s", e)
            await api_client.post_log(
                "error",
                f"[ERROR] Order execution failed for {ticker}: {e}",
                strategy_id=self.strategy_id,
            )
