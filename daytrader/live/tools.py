"""Tool definitions + handlers the trading agents act through.

These are the *only* ways an agent can affect the world: it cannot touch cash
or positions directly, only express intent through these typed tools, which the
broker executes in paper mode and persists. Keeping the surface small and
auditable is what makes an autonomous trader safe to run unattended.
"""
from __future__ import annotations

from daytrader.core.types import Side
from daytrader.live.dev_requests import file_dev_request


def build_tools(broker, db) -> tuple[list[dict], dict]:
    """Return (tool_schemas, handlers) bound to a broker + db."""

    def place_trade(inp: dict) -> dict:
        side = Side.LONG if str(inp.get("side", "long")).lower() == "long" else Side.SHORT
        try:
            qty = float(inp["qty"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "qty must be a number"}
        if qty <= 0:
            return {"ok": False, "error": "qty must be positive"}
        res = broker.open(
            symbol=inp["symbol"].upper(), side=side, qty=qty,
            stop=inp.get("stop"), target=inp.get("target"),
            strategy=inp.get("strategy", "agent"),
            rationale=inp.get("rationale", ""),
        )
        db.log_agent("trader", "place_trade", str({k: inp.get(k) for k in ("symbol", "side", "qty")}))
        return res

    def close_position(inp: dict) -> dict:
        res = broker.close(inp["symbol"].upper(), reason=inp.get("reason", "agent_close"))
        db.log_agent("trader", "close_position", inp.get("symbol", ""))
        return res

    def flatten_all(inp: dict) -> dict:
        res = broker.flatten_all(reason=inp.get("reason", "agent_flatten"))
        db.log_agent("trader", "flatten_all", inp.get("reason", ""))
        return {"ok": True, "closed": res}

    def get_positions(_inp: dict) -> dict:
        return {"ok": True, "positions": broker.positions(), "cash": broker.cash(),
                "equity": broker.equity(), "drawdown_pct": broker.drawdown_pct()}

    def get_performance(_inp: dict) -> dict:
        return {"ok": True, "performance": broker.performance()}

    def get_recent_trades(inp: dict) -> dict:
        """Detailed round-trip trade blotter for post-trade review."""
        try:
            limit = int((inp or {}).get("limit", 30))
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 200))
        try:
            rows = db.recent_trades(limit=limit)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        return {"ok": True, "count": len(rows), "trades": rows}

    def get_opening_range(inp: dict) -> dict:
        """Today's first N minutes for a symbol — for early trend-day detection."""
        from daytrader.data import loader as _loader
        symbol = (inp or {}).get("symbol")
        if not symbol:
            return {"ok": False, "error": "symbol required"}
        try:
            minutes = int((inp or {}).get("minutes", 15))
        except (TypeError, ValueError):
            minutes = 15
        minutes = max(1, min(minutes, 60))
        sym = str(symbol).upper()
        try:
            df = _loader.load(sym, interval="1m", max_age_hours=0.05)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"data unavailable: {e!r}"}
        if df is None or len(df) == 0:
            return {"ok": False, "error": "no intraday data"}
        # Today's session bars (the loader already filters to RTH).
        last_day = df.index[-1].normalize()
        today = df[df.index.normalize() == last_day]
        if len(today) == 0:
            return {"ok": False, "error": "no session bars yet"}
        window = today.iloc[:minutes]
        if len(window) == 0:
            return {"ok": False, "error": "no opening-range bars yet"}
        # Prior session close (last bar of the previous day).
        prior_close = None
        prev_days = df[df.index.normalize() != last_day]
        if len(prev_days):
            try:
                prior_close = float(prev_days["close"].iloc[-1])
            except Exception:  # noqa: BLE001
                prior_close = None
        o = float(window["open"].iloc[0])
        h = float(window["high"].max())
        l_ = float(window["low"].min())
        c = float(window["close"].iloc[-1])
        v = int(window["volume"].sum()) if "volume" in window else None
        return {
            "ok": True,
            "symbol": sym,
            "minutes": int(len(window)),
            "open": round(o, 2),
            "high": round(h, 2),
            "low": round(l_, 2),
            "close": round(c, 2),
            "volume": v,
            "range_pct": round((h - l_) / o * 100, 2) if o else 0.0,
            "gap_pct": round((o / prior_close - 1) * 100, 2) if prior_close else None,
            "prior_close": round(prior_close, 2) if prior_close else None,
        }

    def get_relative_strength_vs_spy(inp: dict) -> dict:
        """Rank symbols by intraday % change relative to SPY (RS = sym% - SPY%)."""
        from daytrader.data import loader as _loader
        syms_in = (inp or {}).get("symbols")
        if not syms_in or not isinstance(syms_in, list):
            return {"ok": False, "error": "symbols (list) required"}
        try:
            lookback_min = int((inp or {}).get("lookback_minutes", 30))
        except (TypeError, ValueError):
            lookback_min = 30
        lookback_min = max(5, min(lookback_min, 240))

        def _change(symbol: str) -> float | None:
            try:
                df = _loader.load(symbol.upper(), interval="1m", max_age_hours=0.05)
            except Exception:  # noqa: BLE001
                return None
            if df is None or len(df) == 0:
                return None
            last_day = df.index[-1].normalize()
            today = df[df.index.normalize() == last_day]
            if len(today) == 0:
                return None
            window = today.tail(lookback_min)
            if len(window) < 2:
                return None
            try:
                first = float(window["close"].iloc[0])
                last = float(window["close"].iloc[-1])
                return ((last / first) - 1) * 100 if first else None
            except Exception:  # noqa: BLE001
                return None

        spy_chg = _change("SPY")
        if spy_chg is None:
            return {"ok": False, "error": "SPY change unavailable"}
        rows = []
        for s in syms_in:
            sym = str(s).upper()
            if sym == "SPY":
                continue
            chg = _change(sym)
            if chg is None:
                continue
            rows.append({
                "symbol": sym,
                "pct_change": round(chg, 2),
                "spy_pct": round(spy_chg, 2),
                "rs": round(chg - spy_chg, 2),
            })
        rows.sort(key=lambda r: r["rs"], reverse=True)
        return {"ok": True, "lookback_minutes": lookback_min,
                "spy_pct": round(spy_chg, 2), "count": len(rows), "rankings": rows}

    def journal_write(inp: dict) -> dict:
        jid = db.add_journal(inp.get("author", "team"), inp.get("topic", "note"), inp.get("note", ""))
        return {"ok": True, "id": jid}

    def request_dev_help(inp: dict) -> dict:
        return file_dev_request(inp["title"], inp.get("body", ""), inp.get("labels"), db=db)

    handlers = {
        "place_trade": place_trade,
        "close_position": close_position,
        "flatten_all": flatten_all,
        "get_positions": get_positions,
        "get_performance": get_performance,
        "get_recent_trades": get_recent_trades,
        "get_opening_range": get_opening_range,
        "get_relative_strength_vs_spy": get_relative_strength_vs_spy,
        "journal_write": journal_write,
        "request_dev_help": request_dev_help,
    }

    schemas = [
        {
            "name": "place_trade",
            "description": "Open a paper position. One position per symbol; rejected if one is already open or if a long exceeds available cash. ALWAYS include a protective stop and a profit target. Fractional shares are supported — size to your risk, not to a whole-share lot.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Any liquid US stock/ETF ticker on the watchlist."},
                    "side": {"type": "string", "enum": ["long", "short"]},
                    "qty": {"type": "number", "description": "Number of shares; FRACTIONAL supported (e.g. 0.5, 0.05). Size so the entry-to-stop loss is ~0.2–0.5% of equity."},
                    "stop": {"type": "number", "description": "Protective stop price"},
                    "target": {"type": "number", "description": "Profit target price"},
                    "strategy": {"type": "string", "description": "Strategy/setup name driving this trade"},
                    "rationale": {"type": "string", "description": "One-sentence reason for the trade"},
                },
                "required": ["symbol", "side", "qty", "stop", "target", "rationale"],
            },
        },
        {
            "name": "close_position",
            "description": "Close an open paper position at the current market price.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "flatten_all",
            "description": "Close ALL open positions immediately (e.g. end of day or risk event).",
            "input_schema": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
            },
        },
        {
            "name": "get_positions",
            "description": "Get current open positions, cash, equity, and drawdown.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_performance",
            "description": "Get realized performance so far: trade count, win rate, profit factor, P&L.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_recent_trades",
            "description": "Detailed round-trip trade blotter for post-trade review: each row has symbol, side, strategy, entry/exit time + price, qty, commission, slippage, pnl, exit reason, rationale.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max rows (1-200; default 30)."},
                },
            },
        },
        {
            "name": "get_opening_range",
            "description": "Today's first N minutes for a symbol (default 15) — open/high/low/close, volume, range %, and gap from prior close. Useful for early trend-day detection and opening-range breakouts.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Ticker, e.g. SPY."},
                    "minutes": {"type": "integer", "description": "Lookback in minutes (1-60; default 15)."},
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "get_relative_strength_vs_spy",
            "description": "Rank a list of symbols by intraday relative strength vs SPY (RS = symbol % change − SPY % change over the lookback window). Returns rankings sorted by RS descending.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "Tickers to rank."},
                    "lookback_minutes": {"type": "integer", "description": "Lookback in minutes (5-240; default 30)."},
                },
                "required": ["symbols"],
            },
        },
        {
            "name": "journal_write",
            "description": "Record a lesson, observation, or plan to the persistent team journal (survives restarts). Use this to build memory across sessions.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "author": {"type": "string", "description": "Which agent is writing"},
                    "topic": {"type": "string", "description": "e.g. lesson, plan, risk, market"},
                    "note": {"type": "string"},
                },
                "required": ["topic", "note"],
            },
        },
        {
            "name": "request_dev_help",
            "description": "File a GitHub issue asking the developer (Claude) for help: a new data source, a bug fix, or a new feature/strategy. Check existing open requests first to avoid duplicates.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short, specific issue title"},
                    "body": {"type": "string", "description": "What you need and why, with enough detail for a dev to act"},
                },
                "required": ["title", "body"],
            },
        },
    ]

    # Merge optional external research-data tools (Polygon, Unusual Whales,
    # BullFlow, Quiver, Finviz) for whichever providers have a key configured.
    # These are READ-ONLY lookups the desks call on demand to hunt for an edge.
    try:
        from daytrader.data.feeds.base import data_tools
        dschemas, dhandlers = data_tools()
        schemas.extend(dschemas)
        handlers.update(dhandlers)
    except Exception as e:  # noqa: BLE001 - feeds are optional, never fatal
        print(f"[tools] data feeds unavailable: {e}")

    return schemas, handlers
