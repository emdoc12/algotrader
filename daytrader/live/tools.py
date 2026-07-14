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

    _LONG_WORDS = {"long", "buy", "b", "bull", "bullish"}
    _SHORT_WORDS = {"short", "sell", "s", "bear", "bearish"}

    def place_trade(inp: dict) -> dict:
        raw_side = str(inp.get("side", "")).strip().lower()
        if raw_side in _LONG_WORDS:
            side = Side.LONG
        elif raw_side in _SHORT_WORDS:
            side = Side.SHORT
        else:
            return {"ok": False, "error": f"side must be long or short (got {inp.get('side')!r})"}
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
            horizon=inp.get("horizon", "day"),
            trail_atr_mult=inp.get("trail_atr_mult"),
            trail_pct=inp.get("trail_pct"),
            auto_scale_r=inp.get("auto_scale_r"),
            auto_scale_frac=inp.get("auto_scale_frac"),
        )
        db.log_agent("trader", "place_trade", str({k: inp.get(k) for k in ("symbol", "side", "qty", "horizon")}))
        return res

    def close_position(inp: dict) -> dict:
        res = broker.close(inp["symbol"].upper(), reason=inp.get("reason", "agent_close"))
        db.log_agent("trader", "close_position", inp.get("symbol", ""))
        return res

    def flatten_all(inp: dict) -> dict:
        res = broker.flatten_all(reason=inp.get("reason", "agent_flatten"))
        db.log_agent("trader", "flatten_all", inp.get("reason", ""))
        return {"ok": True, "closed": res}

    def take_partial(inp: dict) -> dict:
        try:
            fraction = float(inp.get("fraction", 0.5))
        except (TypeError, ValueError):
            return {"ok": False, "error": "fraction must be a number in (0,1)"}
        return broker.reduce_position(inp["symbol"].upper(), fraction,
                                      reason=inp.get("reason", "partial_take"))

    def modify_stops(inp: dict) -> dict:
        stop = inp.get("stop")
        target = inp.get("target")
        return broker.modify_position(inp["symbol"].upper(),
                                      stop=float(stop) if stop is not None else None,
                                      target=float(target) if target is not None else None)

    def move_stop_to_breakeven(inp: dict) -> dict:
        return broker.move_stop_to_breakeven(inp["symbol"].upper())

    def stage_order(inp: dict) -> dict:
        """Pre-stage an order to auto-fire at/after a time IF conditions hold."""
        raw_side = str(inp.get("side", "")).strip().lower()
        if raw_side in _LONG_WORDS:
            side = "long"
        elif raw_side in _SHORT_WORDS:
            side = "short"
        else:
            return {"ok": False, "error": f"side must be long or short (got {inp.get('side')!r})"}
        try:
            qty = float(inp["qty"])
            assert qty > 0
        except (KeyError, TypeError, ValueError, AssertionError):
            return {"ok": False, "error": "qty must be a positive number"}
        oid = db.add_staged_order({
            "symbol": inp["symbol"].upper(), "side": side, "qty": qty,
            "stop": inp.get("stop"), "target": inp.get("target"),
            "strategy": inp.get("strategy", "staged"), "rationale": inp.get("rationale", ""),
            "horizon": inp.get("horizon", "day"),
            "fire_after": inp.get("fire_after", "09:35"),
            "max_ema9_dist_atr": inp.get("max_ema9_dist_atr"),
            "min_adx": inp.get("min_adx"),
        })
        db.log_agent("trader", "stage_order", f"{side} {qty} {inp['symbol'].upper()} @>{inp.get('fire_after','09:35')}")
        return {"ok": True, "id": oid,
                "note": "Staged. Auto-fires at/after fire_after (ET) IF the entry conditions "
                        "(distance from EMA9, min ADX) still hold; otherwise it's skipped."}

    def list_staged_orders(_inp: dict) -> dict:
        return {"ok": True, "pending": db.list_staged_orders(status="pending")}

    def cancel_staged_order(inp: dict) -> dict:
        try:
            oid = int(inp.get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "id (integer) required"}
        ok = db.update_staged_order(oid, status="cancelled", result="cancelled by trader")
        return {"ok": ok, "id": oid}

    def get_positions(_inp: dict) -> dict:
        return {"ok": True, "positions": broker.positions(), "cash": broker.cash(),
                "equity": broker.equity(), "drawdown_pct": broker.drawdown_pct()}

    def get_performance(_inp: dict) -> dict:
        return {"ok": True, "performance": broker.performance()}

    def get_performance_breakdown(inp: dict) -> dict:
        """Realized P&L / win-rate / PF grouped by strategy and/or time-of-day."""
        from daytrader.live import analytics
        inp = inp or {}
        group_by = inp.get("group_by") or ["strategy"]
        if isinstance(group_by, str):
            group_by = [group_by]
        try:
            trades = db.recent_trades(limit=2000)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        rows = analytics.performance_breakdown(trades, group_by=group_by)
        return {"ok": True, "group_by": [g for g in group_by if g in ("strategy", "tod_bucket")] or ["strategy"],
                "breakdown": rows,
                "note": ("Sorted by total P&L; time-of-day buckets are ET "
                         "(open 9:30-10:00, morning 10:00-12:00, midday 12:00-14:00, late 14:00-16:00). "
                         "Realized trades only.")}

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
                min_trend_duration_bars=int(inp.get("min_trend_duration_bars", 1)),
                adx_decay_exit=inp.get("adx_decay_exit"),
            )
        except Exception as e:  # noqa: BLE001
            return {"error": repr(e)}

    def _resolve_custom_config(inp: dict):
        """Get a custom config from inline 'config' or a saved 'name'. Returns
        (config|None, error|None)."""
        cfg = inp.get("config")
        if cfg:
            return cfg, None
        name = inp.get("name")
        if name:
            row = db.get_custom_strategy(name)
            if not row:
                return None, f"no saved custom strategy named {name!r}"
            import json as _json
            try:
                return _json.loads(row["config"]), None
            except Exception as e:  # noqa: BLE001
                return None, f"saved config for {name!r} is corrupt: {e!r}"
        return None, "provide either an inline 'config' or a saved 'name'"

    def backtest_custom_strategy(inp: dict) -> dict:
        """Backtest an agent-authored custom strategy (inline config or saved name)."""
        from daytrader.live import strategy_lab
        inp = inp or {}
        cfg, err = _resolve_custom_config(inp)
        if err:
            return {"error": err}
        try:
            return strategy_lab.run_backtest(
                custom=cfg,
                symbols=inp.get("symbols"),
                lookback_days=int(inp.get("lookback_days", 30)),
                interval=inp.get("interval", "5m"),
                regimes=inp.get("regimes"),
                adx_threshold=float(inp.get("adx_threshold", 25.0)),
                market_filter=bool(inp.get("market_filter", True)),
                starting_equity=float(inp.get("starting_equity", 25000.0)),
                pessimistic_costs=bool(inp.get("pessimistic_costs", False)),
                min_trend_duration_bars=int(inp.get("min_trend_duration_bars", 1)),
                adx_decay_exit=inp.get("adx_decay_exit"),
            )
        except Exception as e:  # noqa: BLE001
            return {"error": repr(e)}

    def save_custom_strategy(inp: dict) -> dict:
        """Validate and persist a custom strategy so it can be reused/deployed."""
        from daytrader.strategies.custom import validate_config, StrategyConfigError
        import json as _json
        inp = inp or {}
        cfg = inp.get("config")
        if not cfg:
            return {"ok": False, "error": "config required"}
        name = (inp.get("name") or (cfg.get("name") if isinstance(cfg, dict) else None) or "").strip()
        if not name:
            return {"ok": False, "error": "name required (in 'name' or config.name)"}
        try:
            norm = validate_config({**cfg, "name": name})
        except StrategyConfigError as e:
            return {"ok": False, "error": f"invalid config: {e}"}
        try:
            stored = {**cfg, "name": name}
            db.save_custom_strategy(name, _json.dumps(stored), inp.get("notes", ""))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        db.log_agent("strategist", "save_custom_strategy", name)
        return {"ok": True, "name": name, "conditions": len(norm["entry"]),
                "note": "Saved. Backtest it with backtest_custom_strategy(name=...) anytime."}

    def list_custom_strategies(_inp: dict) -> dict:
        import json as _json
        rows = db.list_custom_strategies()
        out = []
        for r in rows:
            try:
                cfg = _json.loads(r["config"])
            except Exception:  # noqa: BLE001
                cfg = {}
            out.append({"name": r.get("name"), "ts": r.get("ts"),
                        "side": cfg.get("side"), "conditions": len(cfg.get("entry", []) or []),
                        "notes": r.get("notes")})
        return {"ok": True, "count": len(out), "strategies": out}

    def journal_write(inp: dict) -> dict:
        jid = db.add_journal(inp.get("author", "team"), inp.get("topic", "note"), inp.get("note", ""))
        # Read back to CONFIRM persistence — the snapshot you were handed was
        # built before this write, so your entry won't appear there; it IS saved
        # and carries to the next session (topic 'lesson'/'plan' surface in
        # 'recent_lessons'). This confirmation stops the "silently dropped" doubt.
        persisted = any(j.get("id") == jid for j in db.recent_journal(limit=5))
        return {"ok": True, "id": jid, "persisted": persisted,
                "note": "Saved. Not in your current snapshot (built pre-write); "
                        "visible to all roles next cycle and carried forward as a lesson/plan."}

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
        "take_partial": take_partial,
        "modify_stops": modify_stops,
        "move_stop_to_breakeven": move_stop_to_breakeven,
        "stage_order": stage_order,
        "list_staged_orders": list_staged_orders,
        "cancel_staged_order": cancel_staged_order,
        "get_positions": get_positions,
        "get_performance": get_performance,
        "get_performance_breakdown": get_performance_breakdown,
        "get_recent_trades": get_recent_trades,
        "get_opening_range": get_opening_range,
        "get_relative_strength_vs_spy": get_relative_strength_vs_spy,
        "backtest_strategy": backtest_strategy,
        "backtest_custom_strategy": backtest_custom_strategy,
        "save_custom_strategy": save_custom_strategy,
        "list_custom_strategies": list_custom_strategies,
        "journal_write": journal_write,
        "request_dev_help": request_dev_help,
        "resolve_dev_request": resolve_dev_request,
    }

    schemas = [
        {
            "name": "place_trade",
            "description": "Open a paper position. One position per symbol; rejected if one is already open or if a long exceeds available cash. ALWAYS include a protective stop and a profit target. Fractional shares are supported — size to your risk, not to a whole-share lot. Set 'horizon' to control holding: 'day' (default, flattened at the close), 'swing' (held for days), or 'long' (held weeks+) — swing/long survive the close and ride their stop.",
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
                    "horizon": {"type": "string", "enum": ["day", "swing", "long"], "description": "Intended hold. Default 'day' (flattened at close). 'swing'/'long' survive the close. Prefer 'day' unless the setup genuinely warrants more time."},
                    "trail_atr_mult": {"type": "number", "description": "Optional trailing stop = this many ATRs behind price; ratchets in your favor each cycle and auto-closes when hit. Use to let a winner run instead of a fixed target."},
                    "trail_pct": {"type": "number", "description": "Optional trailing stop as a percent of price (alternative to trail_atr_mult). E.g. 1.5 = trail 1.5%."},
                    "auto_scale_r": {"type": "number", "description": "Server-enforced scale-out trigger, in R multiples (R = entry→stop). DEFAULT 1.0: at +1R the system auto-banks part of the position and moves the stop to breakeven. Set with auto_scale_frac."},
                    "auto_scale_frac": {"type": "number", "description": "Fraction to auto-bank at +auto_scale_r (DEFAULT 0.5 = half). Set to 0 to DISABLE server-enforced scale-out for this trade and manage exits yourself."},
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
            "name": "take_partial",
            "description": "Take a PARTIAL profit: close a fraction of an open position and leave the rest running. E.g. fraction 0.5 sells half. The classic move: take 50-60% at +1R, then move_stop_to_breakeven and let the runner ride your trailing stop. Records the partial as a trade.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "fraction": {"type": "number", "description": "Portion to close, 0<f<1 (e.g. 0.5 = half). f>=1 closes fully."},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "fraction"],
            },
        },
        {
            "name": "modify_stops",
            "description": "Modify an open position's protective stop and/or profit target (e.g. tighten the stop as the trade works, or extend the target). Provide stop and/or target.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "stop": {"type": "number", "description": "New protective stop price."},
                    "target": {"type": "number", "description": "New profit target price."},
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "move_stop_to_breakeven",
            "description": "Move an open position's stop to its entry price (lock in a no-loss runner). Typically done after taking a partial profit at +1R.",
            "input_schema": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        },
        {
            "name": "stage_order",
            "description": "Pre-stage an order (e.g. before the open) that AUTO-FIRES at/after a target ET time IF the entry conditions still hold — removing the calculation step from the time-critical 9:30-10:00 window. You specify symbol/side/qty/stop/target now; at fire_after (ET, e.g. '09:35') the system checks the live conditions and submits, or SKIPS if they no longer hold. Fires within ~2 min of fire_after (checked on the stop-poll). Use with ema_scan to pre-stage the day's best candidates.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["long", "short"]},
                    "qty": {"type": "number"},
                    "stop": {"type": "number"},
                    "target": {"type": "number"},
                    "strategy": {"type": "string"},
                    "rationale": {"type": "string"},
                    "horizon": {"type": "string", "enum": ["day", "swing", "long"]},
                    "fire_after": {"type": "string", "description": "ET time to fire at/after, 'HH:MM' (default 09:35)."},
                    "max_ema9_dist_atr": {"type": "number", "description": "Skip if |price-EMA9| exceeds this many ATRs at fire time (entry still near EMA9)."},
                    "min_adx": {"type": "number", "description": "Skip if the symbol's ADX is below this at fire time."},
                },
                "required": ["symbol", "side", "qty", "stop", "target"],
            },
        },
        {
            "name": "list_staged_orders",
            "description": "List your pending pre-staged auto-fire orders.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "cancel_staged_order",
            "description": "Cancel a pending staged order by its id (from list_staged_orders).",
            "input_schema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
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
            "name": "get_performance_breakdown",
            "description": "Realized performance grouped by setup / direction / trend / time-of-day, so you can see which combos actually carry positive expectancy and concentrate risk there. Free-text strategy labels are auto-normalized to the 8 canonical built-ins (+ 'other'), so 'MACD', 'macd_with_trend_short', 'MACD trend' all collapse into 'macd' — no more 40 one-off rows. Each row has the group keys plus n_trades, win_rate, profit_factor, total_pnl, avg_win, avg_loss. group_by accepts 'strategy' (canonical), 'direction' (long/short), 'with_trend' (with_trend/counter_trend), and 'tod_bucket' (ET: open 9:30-10:00, morning 10:00-12:00, midday 12:00-14:00, late 14:00-16:00). Combine e.g. ['strategy','direction','tod_bucket']. Realized trades only.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "group_by": {"type": "array", "items": {"type": "string", "enum": ["strategy", "strategy_raw", "direction", "with_trend", "tod_bucket"]},
                                 "description": "Dimensions to group by (default ['strategy']). 'strategy' = canonical built-in bucket; 'strategy_raw' = the exact label (so custom strategies like 'trend_follow' don't collapse into 'other'); 'direction' = long/short; 'with_trend' = with_trend/counter_trend (recorded from SPY direction at entry); 'tod_bucket' = ET session window. Combine for a setup×direction×time matrix."},
                },
            },
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
                    "min_trend_duration_bars": {"type": "integer", "description": "Only enter after the symbol's ADX has been >= adx_threshold AND strictly rising for this many consecutive bars (default 1 = no filter). Use to test whether the edge survives when filtering out short-lived regime spikes."},
                    "adx_decay_exit": {"type": "object", "description": "Intra-trade ADX-decay early exit, e.g. {\"adx_drop_from_peak\": 2.0, \"negative_slope_bars\": 3}: force-close a held position if its ADX drops >= adx_drop_from_peak from its post-entry peak OR slopes negative for >= negative_slope_bars bars. Tests cutting during mid-trend deceleration."},
                },
            },
        },
        {
            "name": "backtest_custom_strategy",
            "description": (
                "Backtest a CUSTOM strategy you design from rules (no developer needed). "
                "Provide either an inline 'config' or the 'name' of a saved one. The config "
                "is: {name, side: long|short, entry: [conditions], stop_atr_mult, rr, "
                "max_entries_per_day, no_entry_before, no_entry_after}. Each condition is "
                "{left, op, right} where left is a FEATURE, op is one of < <= > >= == != "
                "cross_above cross_below, and right is a number or another feature. All "
                "conditions are AND-ed. Append '_prev' to a feature for the prior bar (e.g. "
                "crossovers). FEATURES: price, open, high, low, volume, ema9, ema21, ema50, "
                "sma20, rsi, rsi2, atr, atr_pct, adx, vwap, vs_vwap_pct, macd, macd_signal, "
                "macd_hist, bb_upper, bb_lower, bb_mid, bb_pct, day_change_pct, gap_pct, "
                "ret1, ret3. Exits (ATR stop, rr target, EOD-flat) are handled by the engine "
                "— same as the built-ins, so results are directly comparable. Returns the "
                "same metrics + verdict as backtest_strategy."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "config": {"type": "object", "description": "Inline strategy config (see description)."},
                    "name": {"type": "string", "description": "Name of a previously saved custom strategy (alternative to config)."},
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "Tickers to test (default: today's watchlist)."},
                    "lookback_days": {"type": "integer", "description": "Days of history (default 30)."},
                    "interval": {"type": "string", "description": "Bar size: 5m/15m/30m/1h (default 5m)."},
                    "regimes": {"type": "array", "items": {"type": "string"}, "description": "Pin regime gating (trend/range/any)."},
                    "adx_threshold": {"type": "number", "description": "ADX cutoff for trend vs range (default 25)."},
                    "market_filter": {"type": "boolean", "description": "Require SPY-trend alignment (default true)."},
                    "pessimistic_costs": {"type": "boolean", "description": "Stress-test with harsh slippage (default false)."},
                    "min_trend_duration_bars": {"type": "integer", "description": "Only enter after the symbol's ADX has been >= adx_threshold AND strictly rising for this many consecutive bars (default 1 = no filter)."},
                    "adx_decay_exit": {"type": "object", "description": "Intra-trade ADX-decay early exit, e.g. {\"adx_drop_from_peak\": 2.0, \"negative_slope_bars\": 3} — force-close if ADX drops from its post-entry peak or slopes negative for N bars."},
                },
            },
        },
        {
            "name": "save_custom_strategy",
            "description": (
                "Save a custom strategy config (validated) to your team's library so you can "
                "re-backtest or reference it later. Provide 'config' (and optional 'name', "
                "'notes'). Use after a config backtests well so the idea isn't lost."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "config": {"type": "object", "description": "The strategy config to save."},
                    "name": {"type": "string", "description": "Unique name (defaults to config.name)."},
                    "notes": {"type": "string", "description": "Why it works / backtest result summary."},
                },
                "required": ["config"],
            },
        },
        {
            "name": "list_custom_strategies",
            "description": "List your team's saved custom strategies (name, side, #conditions, notes).",
            "input_schema": {"type": "object", "properties": {}},
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
