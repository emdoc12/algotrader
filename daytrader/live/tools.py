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

    def backtest_strategy(inp: dict) -> dict:
        """Self-serve backtest of the built-in strategies over recent data."""
        from daytrader.live import strategy_lab
        inp = inp or {}
        try:
            return strategy_lab.run_backtest(
                strategy=inp.get("strategy"),
                symbols=inp.get("symbols"),
                lookback_days=int(inp.get("lookback_days", 30)),
                interval=inp.get("interval", "5m"),
                regimes=inp.get("regimes"),
                adx_threshold=float(inp.get("adx_threshold", 25.0)),
                market_filter=bool(inp.get("market_filter", True)),
                starting_equity=float(inp.get("starting_equity", 25000.0)),
                pessimistic_costs=bool(inp.get("pessimistic_costs", False)),
                strategy_params=inp.get("strategy_params"),
            )
        except Exception as e:  # noqa: BLE001
            return {"error": repr(e)}

    def journal_write(inp: dict) -> dict:
        jid = db.add_journal(inp.get("author", "team"), inp.get("topic", "note"), inp.get("note", ""))
        return {"ok": True, "id": jid}

    def request_dev_help(inp: dict) -> dict:
        res = file_dev_request(inp["title"], inp.get("body", ""), inp.get("labels"), db=db)
        recorded = bool(res.get("recorded") or res.get("ok"))
        if res.get("ok"):
            note = "Filed as a GitHub issue and saved to the dev-requests page."
        elif recorded:
            note = ("Saved to the dev-requests page (visible on the dashboard). "
                    "GitHub mirror skipped — no GITHUB_TOKEN set — but your request "
                    "IS persisted and the dev will see it.")
        else:
            note = "Could not record the request."
        return {
            "ok": recorded,
            "recorded_locally": recorded,
            "github_issue": bool(res.get("ok")),
            "url": res.get("url"),
            "note": note,
            "error": None if recorded else res.get("error"),
        }

    def resolve_dev_request(inp: dict) -> dict:
        """Close / update a dev request once it's been delivered or rejected."""
        try:
            rid = int((inp or {}).get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "id (integer) required — see open_dev_requests in the snapshot"}
        status = str((inp or {}).get("status", "closed")).lower()
        if status not in ("closed", "wont_fix", "open"):
            status = "closed"
        resolution = (inp or {}).get("resolution", "")
        existing = db.get_dev_request(rid)
        if existing is None:
            return {"ok": False, "error": f"no dev request #{rid}"}
        changed = db.update_dev_request(rid, status=status, resolution=resolution)
        db.log_agent("reviewer", "resolve_dev_request", f"#{rid} -> {status}")
        return {"ok": bool(changed), "id": rid, "status": status,
                "title": existing.get("title")}

    handlers = {
        "place_trade": place_trade,
        "close_position": close_position,
        "flatten_all": flatten_all,
        "get_positions": get_positions,
        "get_performance": get_performance,
        "get_recent_trades": get_recent_trades,
        "get_opening_range": get_opening_range,
        "get_relative_strength_vs_spy": get_relative_strength_vs_spy,
        "backtest_strategy": backtest_strategy,
        "journal_write": journal_write,
        "request_dev_help": request_dev_help,
        "resolve_dev_request": resolve_dev_request,
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
            "name": "backtest_strategy",
            "description": (
                "Backtest one or more of the 8 built-in strategies over recent intraday "
                "data and get win rate, profit factor, avg win/loss, max drawdown, "
                "expectancy, return, and alpha vs SPY — plus an equity curve and sample "
                "trades. Use it to test a hypothesis before risking real cycles: which "
                "setup works in which regime, what stop/target/ADX params help, etc. "
                "strategy can be a name (orb, vwap_trend, vwap_reversion, rsi2, bollinger, "
                "ema_pullback, macd, pivot, gap_fade), a profile (trend, momentum, all), "
                "or a list. Tune via strategy_params (e.g. {\"atr_stop_mult\": 1.5}), "
                "regimes ([\"trend\"]/[\"range\"]/[\"any\"]), adx_threshold, and "
                "market_filter. Uses the same engine + cost model as the production book; "
                "samples under ~10 trades are not conclusive."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "strategy": {"description": "Strategy name, profile (trend/momentum/all), or a list of names."},
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "Tickers to test (default: today's watchlist)."},
                    "lookback_days": {"type": "integer", "description": "Days of history (default 30; 5m data caps ~55d)."},
                    "interval": {"type": "string", "description": "Bar size: 5m/15m/30m/1h (default 5m)."},
                    "regimes": {"type": "array", "items": {"type": "string"}, "description": "Pin regime gating: trend, range, or any. Omit to use each strategy's natural regime."},
                    "adx_threshold": {"type": "number", "description": "ADX cutoff for trend vs range (default 25)."},
                    "market_filter": {"type": "boolean", "description": "Require SPY-trend alignment (default true)."},
                    "pessimistic_costs": {"type": "boolean", "description": "Stress-test with harsh slippage (default false)."},
                    "strategy_params": {"type": "object", "description": "Per-strategy parameter overrides passed to the strategy constructor."},
                },
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
            "description": "Ask the developer (Claude) for help: a new data source, a bug fix, or a new feature/strategy. The request is ALWAYS saved to the dev-requests page (visible on the dashboard) and mirrored to a GitHub issue when a token is configured — no token is required for it to persist. Check existing open requests (in the snapshot's open_dev_requests) first to avoid duplicates.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short, specific issue title"},
                    "body": {"type": "string", "description": "What you need and why, with enough detail for a dev to act"},
                },
                "required": ["title", "body"],
            },
        },
        {
            "name": "resolve_dev_request",
            "description": "Close or update a dev request once it's been delivered (or you've decided not to pursue it). Find the id in the snapshot's open_dev_requests list. Use status 'closed' for done, 'wont_fix' to drop it, 'open' to reopen.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "The dev request id from open_dev_requests."},
                    "status": {"type": "string", "enum": ["closed", "wont_fix", "open"], "description": "New status (default closed)."},
                    "resolution": {"type": "string", "description": "Short note on how it was resolved / why closed."},
                },
                "required": ["id"],
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
