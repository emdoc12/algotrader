"""Tool definitions + handlers the trading agents act through.

These are the *only* ways an agent can affect the world: it cannot touch cash
or positions directly, only express intent through these typed tools, which the
broker executes in paper mode and persists. Keeping the surface small and
auditable is what makes an autonomous trader safe to run unattended.
"""
from __future__ import annotations

from daytrader.core.types import Side
from daytrader.live.dev_requests import file_dev_request


def _team_from_db(db) -> str:
    """Desk name from the DB filename (``team_<name>.db``) — used to attribute
    research hypotheses without threading a team arg through every call site."""
    try:
        import os
        base = os.path.basename(getattr(db, "path", "") or "")
        if base.startswith("team_") and base.endswith(".db"):
            return base[5:-3]
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def unsupported_instrument(symbol) -> str | None:
    """Why this symbol cannot be traded here, or None if it's fine.

    Outside the futures table the broker prices a position as ``price * qty`` —
    the share model. Where that identity is false the system would NOT error:
    quotes and bars flow fine, orders fill, and every resulting dollar figure is
    silently wrong. Futures used to be exactly that trap; they now carry real
    contract specs (``core.contracts``), so a listed contract is tradeable and
    an UNLISTED one is refused — an unknown multiplier is the same silent-wrong
    -numbers problem wearing a different ticker.
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return "symbol is required"
    if s.endswith("=F"):
        from daytrader.core.contracts import spec_for, supported_symbols
        if spec_for(s) is None:
            return (f"{s} is a futures contract with no spec in the contract table, so its "
                    "multiplier, tick value and margin are unknown — trading it would put "
                    "wrong numbers in the book. Supported: "
                    f"{', '.join(supported_symbols())}.")
        return None
    if s.endswith("=X"):
        return (f"{s} is an FX pair — quote-only here, with no lot/pip model. "
                "Use a currency ETF (UUP, FXE, FXY) if you want the exposure.")
    if s.startswith("^"):
        return (f"{s} is an INDEX, not a tradeable instrument. Use its ETF: "
                "^GSPC -> SPY, ^NDX -> QQQ, ^RUT -> IWM, ^VIX -> VXX/UVXY.")
    # OCC / dxfeed style option symbols: ROOT + YYMMDD + C|P + strike.
    import re as _re
    if _re.search(r"[A-Z]{1,6}[ _.]*\d{6}[CP]\d+", s) or s.startswith("."):
        return (f"{s} looks like an OPTION contract, and options do NOT go through "
                "place_trade — that path prices everything as shares. Use "
                "place_option_trade instead: it takes the legs (expiration, strike, "
                "right, buy/sell) and handles the 100x multiplier, premium, collateral, "
                "assignment and exercise properly. get_option_chain gives you the strikes "
                "and Greeks to build it from.")
    if "-USD" in s or "-USDT" in s:
        return (f"{s} is a crypto pair. It trades 24/7, so the EOD flatten and the "
                "session-based risk model do not apply. Use a listed proxy "
                "(IBIT, BITO, COIN, MSTR).")
    return None


def build_tools(broker, db) -> tuple[list[dict], dict]:
    """Return (tool_schemas, handlers) bound to a broker + db."""

    team_name = _team_from_db(db)
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
        bad = unsupported_instrument(inp.get("symbol"))
        if bad:
            return {"ok": False, "error": bad}
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
            adx_decay_exit=inp.get("adx_decay_exit"),
            max_adds=inp.get("max_adds"),
        )
        db.log_agent("trader", "place_trade", str({k: inp.get(k) for k in ("symbol", "side", "qty", "horizon")}))
        return res

    def add_to_position(inp: dict) -> dict:
        """Scale into an existing position, blending to one averaged position."""
        inp = inp or {}
        bad = unsupported_instrument(inp.get("symbol"))
        if bad:
            return {"ok": False, "error": bad}
        try:
            qty = float(inp["qty"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error_code": "bad_qty", "error": "qty must be a number"}
        res = broker.add_to_position(
            symbol=str(inp["symbol"]).upper(), qty=qty,
            stop=inp.get("stop"), target=inp.get("target"),
            auto_scale_r=inp.get("auto_scale_r"),
            auto_scale_frac=inp.get("auto_scale_frac"),
            allow_average_down=bool(inp.get("allow_average_down")),
            rationale=inp.get("rationale", ""))
        db.log_agent("trader", "add_to_position",
                     str({k: inp.get(k) for k in ("symbol", "qty", "stop")}))
        return res

    def declare_strategy(inp: dict) -> dict:
        """Commit to one strategy from the menu for the commitment period."""
        from daytrader.live import mandate
        inp = inp or {}
        return mandate.declare(db, inp.get("strategy") or inp.get("name") or "",
                               plan=inp.get("plan", ""),
                               allocation_pct=inp.get("allocation_pct"))

    def get_platform_updates(inp: dict) -> dict:
        """The complete history of fixes and capability changes shipped."""
        inp = inp or {}
        try:
            limit = max(1, min(int(inp.get("limit") or 25), 100))
        except (TypeError, ValueError):
            limit = 25
        rows = db.platform_updates(limit=limit)
        total = db.platform_update_count()
        return {
            "ok": True, "total": total, "returned": len(rows),
            "updates": [{"ts": r["ts"], "note": r["note"]} for r in rows],
            "note": ("The authoritative record of what this system can do. Every entry "
                     "applies to EVERY desk regardless of which one reported it. If an "
                     "update says a tool, data source or strategy lane now works, RE-TEST "
                     "it — a workaround kept for a bug that was fixed is how a desk "
                     "quietly excludes itself from a lane it could be trading. Read this "
                     "whenever you are about to conclude something is impossible, and "
                     "after any stretch where your desk was idle."),
        }

    def get_risk_state(_inp: dict) -> dict:
        """Remaining risk budget, portfolio heat, cooldown state, declaration."""
        from daytrader.live import mandate
        out = {"ok": True}
        try:
            out.update(broker.risk_state())
        except Exception as e:  # noqa: BLE001
            out["error"] = repr(e)
        out["mandate"] = mandate.status(db, broker)
        return out

    # ---- options ------------------------------------------------------- #
    def get_option_chain(inp: dict) -> dict:
        """Live chain from the owner's tastytrade account (READ-ONLY data)."""
        from daytrader.live import tastytrade_data as tt
        inp = inp or {}
        sym = str(inp.get("symbol", "")).upper().strip()
        if not sym:
            return {"ok": False, "error": "symbol required"}
        min_dte = inp.get("min_dte")
        max_dte = inp.get("max_dte")
        if min_dte is None and max_dte is None and inp.get("target_dte") is not None:
            t = int(inp["target_dte"])
            min_dte, max_dte = max(0, t - 7), t + 7
        env = tt.fetch_option_chain(
            sym,
            max_expirations=int(inp.get("max_expirations") or 2),
            strikes_around_atr=int(inp.get("strikes") or 16),
            min_dte=int(min_dte) if min_dte is not None else None,
            max_dte=int(max_dte) if max_dte is not None else None,
            strike_pct_window=float(inp.get("strike_pct_window") or 15.0),
        )
        if not env.get("ok"):
            # The real reason, not a guess. Also recorded so the snapshot's
            # degraded list warns the next desk before it plans around options.
            try:
                from daytrader.data.feeds.base import record_named_error
                record_named_error(
                    "tastytrade_options", str(env.get("error_code")),
                    f"{sym}: {env.get('error')}",
                    hint=_chain_advice(env.get("error_code")))
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "symbol": sym,
                    "error_code": env.get("error_code"),
                    "error": env.get("error"),
                    "what_to_do": _chain_advice(env.get("error_code"))}
        spot = None
        try:
            spot = round(float(broker.latest_price(sym)), 2)
        except Exception:  # noqa: BLE001
            pass
        # A stale fallback still counts as DEGRADED. Returning ok=True with old
        # prices and a clean health report would let the next desk plan a
        # premium lane believing it has live quotes.
        try:
            from daytrader.data.feeds.base import clear_named_error, record_named_error
            if env.get("stale"):
                record_named_error(
                    "tastytrade_options", "stale_fallback",
                    f"{sym}: live chain unavailable ({env.get('live_error_code')}); serving the "
                    f"historical chain as of {env.get('as_of')}",
                    hint=("Strikes/deltas/IV are usable for structure selection; premiums are "
                          "NOT executable prices. Size conservatively and expect fill slippage."))
            else:
                clear_named_error("tastytrade_options")
        except Exception:  # noqa: BLE001
            pass
        out = {"ok": True, "symbol": sym, "spot": spot,
               "source": env.get("source"), "n_contracts": env.get("n_contracts"),
               "chain": env.get("chain")}
        if env.get("stale"):
            out["stale"] = True
            out["as_of"] = env.get("as_of")
            out["warning"] = (
                f"STALE PRICES — the live stream was unavailable ({env.get('live_error_code')}), "
                f"so this is the HISTORICAL chain as of {env.get('as_of')}. Strikes, deltas and "
                "IV are still sound for choosing a structure; the premiums are NOT executable "
                "prices today. You may trade off them (place_option_trade accepts them and will "
                "record the staleness), but size conservatively and expect fills to differ.")
        else:
            out["note"] = ("Live bid/ask with streaming Greeks. Pass the leg prices you would "
                           "actually trade at — BID on shorts, ASK on longs.")
        return out

    def _chain_advice(code: str | None) -> str:
        return {
            "not_configured": ("tastytrade is not configured. Set ALPHAVANTAGE_API_KEY to at "
                               "least research options off historical chains."),
            "no_session": "the tastytrade session failed — the owner must refresh credentials.",
            "empty_chain": "this underlying appears to have no listed options; pick another.",
            "no_contracts": ("no contracts matched your expiration/strike window — widen "
                             "target_dte or strike_pct_window."),
            "no_quotes": ("the chain loaded but the market-data stream stayed quiet — usually "
                          "outside market hours. Retry during the session."),
            "stream_timeout": "the fetch timed out; retry once, then report it if it persists.",
        }.get(str(code), "retry once; if it persists, file a dev request with this error_code.")

    def place_option_trade(inp: dict) -> dict:
        """Open a multi-leg options position (defined risk only)."""
        inp = inp or {}
        legs = inp.get("legs") or []
        if not legs:
            return {"ok": False, "error_code": "no_legs",
                    "reason": ("legs required: a list of {expiration, strike, right, "
                               "action, qty, price}. One leg is a single option; two make "
                               "a vertical; four make an iron condor.")}
        res = broker.options.open_structure(
            str(inp.get("symbol") or inp.get("underlying") or "").upper(),
            legs,
            strategy=inp.get("strategy") or "options",
            rationale=inp.get("rationale", ""),
            profit_target_pct=inp.get("profit_target_pct", 50.0),
            dte_exit=inp.get("dte_exit", 21),
        )
        db.log_agent("trader", "place_option_trade",
                     f"{inp.get('symbol')} {len(legs)} legs -> "
                     f"{res.get('structure') or res.get('error_code')}")
        return res

    def close_option_position(inp: dict) -> dict:
        inp = inp or {}
        try:
            pid = int(inp.get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "id required — see get_option_positions"}
        return broker.options.close_structure(pid, reason=inp.get("reason", "agent_close"))

    def get_option_positions(_inp: dict) -> dict:
        pos = broker.options.positions()
        return {"ok": True, "count": len(pos), "positions": pos,
                "collateral_held": round(broker.options.collateral_held(), 2),
                "open_risk": round(broker.options.open_risk(), 2),
                "buying_power": round(broker.buying_power(), 2)}

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
        bad = unsupported_instrument(inp.get("symbol"))
        if bad:
            return {"ok": False, "error": bad}
        # Optional general feature conditions (same grammar as backtest_custom_strategy),
        # validated now and re-checked at fire time.
        conditions = inp.get("conditions")
        conditions_json = None
        if conditions:
            from daytrader.strategies.custom import normalize_conditions, StrategyConfigError
            try:
                normalize_conditions(conditions)
            except StrategyConfigError as e:
                return {"ok": False, "error": f"invalid conditions: {e}"}
            import json as _json
            conditions_json = _json.dumps(conditions)
        oid = db.add_staged_order({
            "symbol": inp["symbol"].upper(), "side": side, "qty": qty,
            "stop": inp.get("stop"), "target": inp.get("target"),
            "strategy": inp.get("strategy", "staged"), "rationale": inp.get("rationale", ""),
            "horizon": inp.get("horizon", "day"),
            "fire_after": inp.get("fire_after", "09:35"),
            "max_ema9_dist_atr": inp.get("max_ema9_dist_atr"),
            "min_adx": inp.get("min_adx"),
            "conditions": conditions_json,
        })
        db.log_agent("trader", "stage_order", f"{side} {qty} {inp['symbol'].upper()} @>{inp.get('fire_after','09:35')}")
        return {"ok": True, "id": oid,
                "note": "Staged. Auto-fires at/after fire_after (ET) IF the min_adx / "
                        "max_ema9_dist_atr gates AND any 'conditions' still hold at fire "
                        "time (checked on the ~2-min poll); otherwise it's skipped."}

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
        filters = inp.get("filters")
        rows = analytics.performance_breakdown(trades, group_by=group_by, filters=filters)
        # Echo the dimensions ACTUALLY applied — the previous allowlist here was
        # stale and silently reported "strategy" while grouping by something else.
        applied = [g for g in group_by if g in analytics.VALID_GROUP_BY] or ["strategy"]
        ignored = [g for g in group_by if g not in analytics.VALID_GROUP_BY]
        out = {"ok": True, "group_by": applied, "breakdown": rows,
               "note": ("Sorted by total P&L; time-of-day buckets are ET "
                        "(open 9:30-10:00, morning 10:00-12:00, midday 12:00-14:00, "
                        "late 14:00-16:00). Realized trades only. Tape context "
                        "(breadth_*/sector_*) is recorded AT ENTRY, so trades taken "
                        "before it shipped group as 'unknown' rather than being "
                        "silently bucketed.")}
        if ignored:
            out["ignored_group_by"] = ignored
            out["valid_group_by"] = list(analytics.VALID_GROUP_BY)
        if filters:
            out["filters_applied"] = filters
            out["n_trades_after_filter"] = sum(r["n_trades"] for r in rows)
        return out

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
                horizon=inp.get("horizon", "intraday"),
                max_hold_days=float(inp.get("max_hold_days", 0) or 0),
                trail_atr_mult=float(inp.get("trail_atr_mult", 0) or 0),
                trail_pct=float(inp.get("trail_pct", 0) or 0),
                breakeven_at_r=float(inp.get("breakeven_at_r", 0) or 0),
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
                horizon=inp.get("horizon", "intraday"),
                max_hold_days=float(inp.get("max_hold_days", 0) or 0),
                trail_atr_mult=float(inp.get("trail_atr_mult", 0) or 0),
                trail_pct=float(inp.get("trail_pct", 0) or 0),
                breakeven_at_r=float(inp.get("breakeven_at_r", 0) or 0),
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

    def propose_hypothesis(inp: dict) -> dict:
        """Pre-register a research hypothesis. You will NOT get a result back.

        This is the research loop, not a backtest: the idea is registered with
        its pass/fail bar fixed up front, then judged later by pure compute
        against hard out-of-sample data. You cannot retrofit criteria, retest a
        rejected idea, or see the answer in this call.
        """
        from daytrader.research.hypothesis import HypothesisError
        from daytrader.research.registry import ResearchDB
        inp = inp or {}
        cfg = inp.get("rule") or inp.get("config")
        if not cfg:
            return {"ok": False, "error": "rule (a custom-strategy config) is required"}
        rdb = None
        try:
            rdb = ResearchDB()
            n_periods = int(inp.get("n_periods", 5))
            res = rdb.register(
                {"rule": cfg,
                 "universe": inp.get("universe") or inp.get("symbols") or ["SPY"],
                 "interval": inp.get("interval", "1h"),
                 "name": inp.get("name"),
                 "horizon": inp.get("horizon", "intraday"),
                 "max_hold_days": inp.get("max_hold_days"),
                 "trail_atr_mult": inp.get("trail_atr_mult"),
                 "trail_pct": inp.get("trail_pct"),
                 "breakeven_at_r": inp.get("breakeven_at_r")},
                team=team_name, rationale=inp.get("rationale", ""),
                criteria=inp.get("criteria"), n_periods=n_periods)
            if res.get("ok"):
                summary = rdb.summary()
                db.log_agent("research", "propose_hypothesis",
                             f"#{res['id']} {inp.get('name') or ''}")
                res["note"] = (
                    f"Pre-registered as #{res['id']}. It will be evaluated on {n_periods} "
                    "non-overlapping out-of-sample periods after the close. The bar is "
                    f"corrected for the {summary['tested']} hypotheses already tested "
                    "across ALL desks, so it tightens as the family grows. Expect "
                    "rejection — that is the normal outcome. Do NOT re-propose this idea.")
            return res
        except HypothesisError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        finally:
            if rdb is not None:
                rdb.close()

    def research_log(inp: dict) -> dict:
        """What has already been tested — read this BEFORE proposing."""
        from daytrader.research.registry import ResearchDB
        import json as _json
        rdb = None
        try:
            rdb = ResearchDB()
            rows = rdb.recent(limit=int((inp or {}).get("limit", 25)))
            out = []
            for r in rows:
                res = {}
                if r.get("result"):
                    try:
                        res = _json.loads(r["result"])
                    except Exception:  # noqa: BLE001
                        res = {}
                out.append({
                    "id": r["id"], "team": r["team"], "name": r["name"],
                    "status": r["status"], "rationale": (r["rationale"] or "")[:160],
                    "periods_profitable": (f"{res.get('n_periods_profitable')}/{res.get('n_periods')}"
                                           if res.get("ok") else None),
                    "n_trades": res.get("n_trades"),
                    "net_pnl": res.get("total_net_pnl"),
                    "p_value": r["p_value"], "required_alpha": r["required_alpha"],
                    "why_rejected": (r["reject_reason"] or "")[:200] or None,
                })
            return {"ok": True, "summary": rdb.summary(), "hypotheses": out,
                    "note": "A rejected hypothesis is permanently closed — proposing it "
                            "again is refused by content hash."}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        finally:
            if rdb is not None:
                rdb.close()

    def deploy_strategy(inp: dict) -> dict:
        """Promote one of YOUR accepted hypotheses to live signal generation."""
        from daytrader.research.deploy import deploy
        hid = (inp or {}).get("hypothesis_id")
        if hid is None:
            return {"ok": False, "error": "hypothesis_id is required"}
        try:
            res = deploy(db, int(hid), team_name)
        except (TypeError, ValueError):
            return {"ok": False, "error": "hypothesis_id must be an integer"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        if res.get("ok"):
            db.log_agent("research", "deploy_strategy", f"#{hid} {res.get('name')}")
        return res

    def undeploy_strategy(inp: dict) -> dict:
        """Stop a deployed strategy from generating live signals."""
        from daytrader.research.deploy import undeploy
        hid = (inp or {}).get("hypothesis_id")
        if hid is None:
            return {"ok": False, "error": "hypothesis_id is required"}
        try:
            res = undeploy(db, int(hid))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)}
        if res.get("ok"):
            db.log_agent("research", "undeploy_strategy", f"#{hid}")
        return res

    def list_deployed_strategies(_inp: dict) -> dict:
        from daytrader.research.deploy import active
        rows = active(db)
        return {"ok": True, "count": len(rows), "deployed": [
            {"hypothesis_id": r["hypothesis_id"], "name": r["name"],
             "universe": r["universe"], "deployed_ts": r["deployed_ts"],
             "validation": r["evidence"], "side": r["config"].get("side")}
            for r in rows]}

    def get_contract_specs(inp: dict) -> dict:
        """Futures contract specs — size positions in DOLLARS, not points."""
        from daytrader.core.contracts import describe, supported_symbols
        syms = (inp or {}).get("symbols") or supported_symbols()
        if isinstance(syms, str):
            syms = [syms]
        out = [d for d in (describe(s) for s in syms) if d]
        eq = 0.0
        try:
            eq = float(broker.equity())
        except Exception:  # noqa: BLE001
            pass
        for d in out:
            # What one contract actually costs you at the current risk cap.
            d["notional_note"] = (
                f"1 contract = price x {d['multiplier']:g} dollars of exposure; "
                f"each tick is ${d['tick_value']:g}")
            if eq > 0:
                d["max_contracts_by_margin"] = int(eq // d["initial_margin"]) if d["initial_margin"] else 0
        from daytrader.live.tastytrade_margin import describe as _mdesc
        return {"ok": True, "equity": round(eq, 2), "contracts": out,
                "margin_terms": _mdesc(),
                "note": ("Position size is in CONTRACTS. Risk = (entry - stop) x contracts x "
                         "multiplier, so a 10-point stop on MES risks $50, not $10. Margin is "
                         "pledged (reducing buying_power) but not spent, and a futures "
                         "position contributes only its unrealized P&L to equity. "
                         "Micros (MES/MNQ/MGC/M2K/MYM/MCL) are the only contracts that size "
                         "sensibly against a $25k account.")}

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
        "add_to_position": add_to_position,
        "declare_strategy": declare_strategy,
        "get_risk_state": get_risk_state,
        "get_platform_updates": get_platform_updates,
        "get_option_chain": get_option_chain,
        "place_option_trade": place_option_trade,
        "close_option_position": close_option_position,
        "get_option_positions": get_option_positions,
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
        "get_contract_specs": get_contract_specs,
        "propose_hypothesis": propose_hypothesis,
        "research_log": research_log,
        "deploy_strategy": deploy_strategy,
        "undeploy_strategy": undeploy_strategy,
        "list_deployed_strategies": list_deployed_strategies,
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
                    "max_adds": {"type": "integer", "description": "How many later tranches this position may accept via add_to_position. Encodes a plan like 'core + up to 2 adds' and the engine enforces it. Omit for unlimited."},
                    "adx_decay_exit": {"type": "object", "description": "Server-enforced regime-deterioration exit — the SAME contract as backtest_strategy/backtest_custom_strategy, so a config you validated in a backtest behaves identically live. E.g. {\"adx_drop_from_peak\": 5.0, \"negative_slope_bars\": 3}: force-close this position once its ADX has fallen >= 5.0 from its post-entry peak, OR its ADX slope has been negative for >= 3 consecutive cycles. Checked every cycle AND on the ~2-min stop poll. Use it on trend-continuation entries (MACD/EMA-rollover) where the loss mode is the trend dying under you rather than a hard stop-out."},
                },
                "required": ["symbol", "side", "qty", "stop", "target", "rationale"],
            },
        },
        {
            "name": "add_to_position",
            "description": (
                "SCALE IN to a position you already hold — the way to build in tranches "
                "(initiate on confirmation, add on the first pullback that holds structure) "
                "instead of guessing full size on the first bar of confidence. It blends into "
                "ONE position: volume-weighted average entry, summed quantity, one stop/target. "
                "Closing and re-entering larger is NOT equivalent — that surrenders the runner, "
                "pays two extra sets of spread and slippage, and resets the trailing ratchet. "
                "Returns blended_entry, total_qty and total_planned_risk (plus its % of equity) "
                "so you can verify position-level risk after the add. Risk is re-checked against "
                "the BLENDED entry, and R for auto-scale/breakeven is re-measured from it. "
                "SAME-DIRECTION only: an opposite-side order is a reduce or flip — use "
                "take_partial or close_position."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "qty": {"type": "number", "description": "Size of THIS tranche (added to the existing position)."},
                    "stop": {"type": "number", "description": "New stop for the whole blended position. Omit to keep the current one — but note adding moves your average, so re-check that the old stop still sits the right side of the new entry."},
                    "target": {"type": "number", "description": "New target for the blended position. Omit to keep."},
                    "auto_scale_frac": {"type": "number", "description": "Pass 0 to keep a core hold unmanaged by server-side scaling. Omit to inherit the parent position's setting."},
                    "allow_average_down": {"type": "boolean", "description": "Adding to an UNDERWATER position is blocked by default — it is the most reliable way to turn a small mistake into an account-level one. Set true ONLY if your declared strategy defines this specific adjustment."},
                    "auto_scale_r": {"type": "number", "description": "Override the scale-out R multiple, measured from the blended entry."},
                    "rationale": {"type": "string"},
                },
                "required": ["symbol", "qty"],
            },
        },
        {
            "name": "get_option_chain",
            "description": (
                "LIVE option chain with real bid/ask and streaming Greeks (delta, gamma, "
                "theta, vega, IV) for a symbol. Ask for the DTE window your strategy needs "
                "(target_dte 35 for a 30-45 DTE credit spread) and read the deltas to pick "
                "strikes — a 20-30 delta short strike is what the premium-selling playbook "
                "means. Then pass those exact leg prices to place_option_trade."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Underlying, e.g. SPY"},
                    "target_dte": {"type": "integer", "description": "Preferred days to expiration; returns expirations within +/-7 days of it."},
                    "min_dte": {"type": "integer"},
                    "max_dte": {"type": "integer"},
                    "max_expirations": {"type": "integer", "description": "How many expirations (default 2)"},
                    "strikes": {"type": "integer", "description": "Strikes per expiration, nearest the money first (default 16)"},
                    "strike_pct_window": {"type": "number", "description": "Only strikes within this %% of spot (default 15)"},
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "place_option_trade",
            "description": (
                "Open an OPTIONS position — single leg or multi-leg. This is how you run "
                "cash-secured puts, the wheel, credit spreads, iron condors, LEAPs and "
                "earnings vol-crush trades. Each leg is {expiration, strike, right (call/"
                "put), action (buy/sell), qty (contracts), price (per share, from the "
                "chain)}. The engine handles the 100x multiplier, premium, collateral, "
                "assignment and exercise. DEFINED RISK ONLY: a naked short call is "
                "rejected, and a short call needs 100 shares per contract to cover it. "
                "Risk is the structure's MAX LOSS, never the premium collected; collateral "
                "is what the position ties up (a cash-secured put holds the whole strike). "
                "By default the system auto-closes at 50%% of max profit or 21 DTE."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Underlying, e.g. SPY"},
                    "legs": {
                        "type": "array",
                        "description": "1-4 legs. Example bull put spread: [{expiration:'2026-09-18', strike:570, right:'put', action:'sell', qty:1, price:5.00}, {expiration:'2026-09-18', strike:565, right:'put', action:'buy', qty:1, price:3.00}]",
                        "items": {
                            "type": "object",
                            "properties": {
                                "expiration": {"type": "string", "description": "YYYY-MM-DD"},
                                "strike": {"type": "number"},
                                "right": {"type": "string", "enum": ["call", "put"]},
                                "action": {"type": "string", "enum": ["buy", "sell"]},
                                "qty": {"type": "number", "description": "Contracts (each = 100 shares)"},
                                "price": {"type": "number", "description": "Per-share premium from the chain"},
                            },
                            "required": ["expiration", "strike", "right", "action", "qty"],
                        },
                    },
                    "strategy": {"type": "string", "description": "Tag, e.g. wheel_csp / bull_put_spread / iron_condor"},
                    "rationale": {"type": "string"},
                    "profit_target_pct": {"type": "number", "description": "Auto-close at this %% of max profit (default 50). null to disable."},
                    "dte_exit": {"type": "integer", "description": "Auto-close at this DTE (default 21). null to disable."},
                },
                "required": ["symbol", "legs"],
            },
        },
        {
            "name": "get_option_positions",
            "description": ("Open options structures with mark-to-market P&L, %% of max "
                            "profit captured, DTE, collateral held and remaining buying power."),
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "close_option_position",
            "description": ("Close an open options structure by id (from get_option_positions). "
                            "Buys back shorts and sells longs at current market."),
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "reason": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "declare_strategy",
            "description": (
                "Commit this desk to ONE strategy from the approved menu. Required before "
                "trading. You must keep it for the commitment period (default 5 trading "
                "days) — a lane switched every cycle is not a strategy and produces no "
                "evidence. Trade its rules, tag every trade with the strategy name, and "
                "let the record judge it. ALL menu lanes are executable, options included "
                "(wheel_csp, bull_put_spread, iron_condor, leap_debit, earnings_vol_crush) — "
                "place_option_trade handles the 100x multiplier, premium, collateral, "
                "assignment and exercise. The authoritative list is "
                "get_risk_state().mandate.executable_strategies."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "strategy": {"type": "string", "description": "One of: momentum_20d_high, trend_position, mean_reversion_range, wheel_csp, bull_put_spread, iron_condor, leap_debit, earnings_vol_crush."},
                    "plan": {"type": "string", "description": "How you will run it: entry trigger, stop rule, management, what would make you abandon it."},
                    "allocation_pct": {"type": "number", "description": "Share of buying power this strategy may use (capped at 50)."},
                },
                "required": ["strategy"],
            },
        },
        {
            "name": "get_platform_updates",
            "description": (
                "The FULL history of fixes and new capabilities the developer has shipped — "
                "not just the recent ones the snapshot shows. Every entry applies to every "
                "desk, whoever reported the problem. Call this before concluding that "
                "something is impossible, and after any period where your desk was idle or "
                "paused: the fix that unblocks the lane you gave up on may already be here."),
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many, newest first (default 25, max 100)."}},
            },
        },
        {
            "name": "get_risk_state",
            "description": (
                "The risk budget you actually have RIGHT NOW: open portfolio heat (sum of "
                "entry→stop risk across positions), dollars of new risk still permitted, "
                "per-trade cap, drawdown and whether you are in cooling-off (no new "
                "positions), plus your current strategy declaration. Call this BEFORE "
                "sizing — it tells you the budget instead of you discovering it through a "
                "rejected order."
            ),
            "input_schema": {"type": "object", "properties": {}},
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
            "description": "Pre-stage an order (e.g. before the open, or to catch a bar-resolved trigger between cycles) that AUTO-FIRES at/after a target ET time IF its conditions still hold — removing the calculation step from the time-critical window AND catching triggers that print between 20-min cycles. You specify symbol/side/qty/stop/target now; from fire_after (ET) the system re-checks conditions every ~2 min (stop-poll) and submits when they hold, or SKIPS if they don't. Use with ema_scan / macd_trigger to pre-stage the day's best candidates.",
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
                    "conditions": {"type": "array", "items": {"type": "object"},
                                   "description": "General entry conditions using the SAME grammar as backtest_custom_strategy (each {left, op, right}; features like macd_hist, macd_hist_prev, adx, ema9, rs_stable, etc.). ALL must hold at fire time. E.g. [{\"left\":\"macd_hist\",\"op\":\"<\",\"right\":\"macd_hist_prev\"},{\"left\":\"macd_hist_prev\",\"op\":\"<\",\"right\":0}] to catch a downward MACD-hist re-expansion. Lets you pre-stage the exact validated trigger."},
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
                    "filters": {"type": "object", "description": "Keep only matching trades before grouping. Value forms: exact ({\"breadth_bucket\":\"weak\"}), range ({\"breadth_pct\":{\"max\":35}}), or allowed-list ({\"sector\":[\"semis\",\"mega_tech\"]}). Fields: breadth_pct, breadth_bucket, breadth_change_20m, sector, sector_avg_adx, sector_pct_down, with_trend, side, symbol, strategy."},
                    "group_by": {"type": "array", "items": {"type": "string", "enum": ["strategy", "strategy_raw", "direction", "with_trend", "tod_bucket", "breadth_bucket", "breadth_trend", "sector", "sector_adx_bucket"]},
                                 "description": "Dimensions to group by (default ['strategy']). 'strategy' = canonical built-in bucket; 'strategy_raw' = the exact label; 'direction' = long/short; 'with_trend' = with_trend/counter_trend (from SPY direction at entry); 'tod_bucket' = ET session window; 'breadth_bucket' = weak/mixed/strong breadth AT ENTRY (weak <=35% advancing, strong >=65%); 'breadth_trend' = deteriorating/flat/improving (20-min breadth change at entry); 'sector' = the symbol's sector cluster; 'sector_adx_bucket' = weak/moderate/strong sector ADX. Combine for a setup×tape matrix — e.g. ['strategy','breadth_bucket'] answers whether a short only works on broad-down days."},
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
                    "horizon": {"type": "string", "enum": ["intraday", "swing"], "description": "'intraday' (default) force-flattens every position at the session close, so a multi-day idea is silently re-tested as an intraday one. 'swing' DISABLES the EOD flat: positions carry across sessions until stop/target/max_hold fires, and overnight gaps are priced honestly (a gapped open fills the stop at the OPEN, not at the stop level). Use 'swing' for any hypothesis whose edge is trend persistence beyond one session."},
                    "max_hold_days": {"type": "number", "description": "Time stop for swing tests, in days (0 = none). Without it a swing config that never hits stop or target degenerates into buy-and-hold and tells you nothing."},
                    "trail_atr_mult": {"type": "number", "description": "Trailing stop at this many ATRs behind the close, ratcheting only in your favor. Matches place_trade's live trail_atr_mult, so a runner-based edge can finally be validated instead of being capped by a fixed rr target."},
                    "trail_pct": {"type": "number", "description": "Trailing stop as a percent of price (alternative to trail_atr_mult). Matches place_trade's trail_pct."},
                    "breakeven_at_r": {"type": "number", "description": "Move the stop to entry once the trade reaches +N R (0 = off)."},
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
                "ret1, ret3, rs_vs_spy_pct, rs_slope_20m, rs_persistence, rs_stable (0/1), "
                "adx_rising_nbars, adx_decaying_nbars. Exits (ATR stop, rr target, EOD-flat) are handled by the engine "
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
                    "horizon": {"type": "string", "enum": ["intraday", "swing"], "description": "'intraday' (default) force-flattens every position at the session close, so a multi-day idea is silently re-tested as an intraday one. 'swing' DISABLES the EOD flat: positions carry across sessions until stop/target/max_hold fires, and overnight gaps are priced honestly (a gapped open fills the stop at the OPEN, not at the stop level). Use 'swing' for any hypothesis whose edge is trend persistence beyond one session."},
                    "max_hold_days": {"type": "number", "description": "Time stop for swing tests, in days (0 = none). Without it a swing config that never hits stop or target degenerates into buy-and-hold and tells you nothing."},
                    "trail_atr_mult": {"type": "number", "description": "Trailing stop at this many ATRs behind the close, ratcheting only in your favor. Matches place_trade's live trail_atr_mult, so a runner-based edge can finally be validated instead of being capped by a fixed rr target."},
                    "trail_pct": {"type": "number", "description": "Trailing stop as a percent of price (alternative to trail_atr_mult). Matches place_trade's trail_pct."},
                    "breakeven_at_r": {"type": "number", "description": "Move the stop to entry once the trade reaches +N R (0 = off)."},
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
            "name": "propose_hypothesis",
            "description": (
                "Pre-register a research hypothesis for out-of-sample testing. This is NOT a "
                "backtest and returns NO result — that is the point. You state the rule and the "
                "pass/fail bar up front; code judges it later against hard out-of-sample data "
                "you cannot see, and you cannot change the criteria afterwards. "
                "IMPORTANT: the significance bar is corrected for every hypothesis ever tested "
                "across ALL desks, so it gets harder as the research family grows, and REJECTION "
                "IS THE NORMAL OUTCOME — most real ideas fail, and a run of rejections means the "
                "process is working, not that you should loosen the rule. A rejected idea is "
                "closed permanently (matched by content hash, so renaming it will not get it "
                "re-tested). Call research_log first to see what has already been ruled out. "
                "Propose things you genuinely believe and can justify mechanically, not "
                "variations mined to pass."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "rule": {"type": "object", "description": "Strategy config in the same grammar as backtest_custom_strategy (side, entry conditions, stop_atr_mult, rr, session window)."},
                    "name": {"type": "string", "description": "Short label for the hypothesis."},
                    "rationale": {"type": "string", "description": "WHY you think this edge exists — the mechanism, not the backtest. Recorded permanently alongside the verdict."},
                    "universe": {"type": "array", "items": {"type": "string"}, "description": "Symbols to test (default ['SPY'])."},
                    "interval": {"type": "string", "enum": ["5m", "15m", "1h", "1d"], "description": "Bar interval. Default '1h' — it is the only interval with enough history (730d) for a real multi-period walk-forward; 5m/15m cap at ~60d. '1d' (3650d) suits multi-week position tests."},
                    "horizon": {"type": "string", "enum": ["intraday", "swing"], "description": "'intraday' (default) flattens at every session close. 'swing' carries positions across sessions until stop/target/max_hold, with overnight gaps priced honestly. A swing hypothesis REQUIRES max_hold_days. Horizon is part of the hypothesis identity, so the swing version of a rejected intraday rule is a genuinely new hypothesis, not a re-proposal."},
                    "max_hold_days": {"type": "number", "description": "Time stop in days. Required when horizon='swing' — without it a rule that never hits stop or target degenerates into buy-and-hold and measures the market rather than the rule."},
                    "trail_atr_mult": {"type": "number", "description": "Trailing stop in ATRs behind the close (matches place_trade). Use when the edge is in the runner rather than a fixed target."},
                    "trail_pct": {"type": "number", "description": "Trailing stop as a percent of price (alternative to trail_atr_mult)."},
                    "breakeven_at_r": {"type": "number", "description": "Move stop to entry at +N R (0 = off)."},
                    "n_periods": {"type": "integer", "description": "Non-overlapping out-of-sample periods to test across (default 5, minimum 3)."},
                    "criteria": {"type": "object", "description": "Pre-registered pass bar: {min_periods_profitable, min_trades, base_alpha}. Fixed at registration — it can never be edited to fit the result."},
                },
                "required": ["rule"],
            },
        },
        {
            "name": "research_log",
            "description": (
                "The record of every hypothesis pre-registered across all desks: what was "
                "tested, what was rejected and exactly why, and the running count that sets the "
                "current significance bar. Read this BEFORE proposing so you neither repeat a "
                "closed idea nor re-learn something already ruled out."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "How many recent entries (default 25)."}},
            },
        },
        {
            "name": "get_contract_specs",
            "description": (
                "Futures contract specs: multiplier, tick size, tick value, initial/maintenance "
                "margin, and how many contracts your equity can margin. READ THIS BEFORE SIZING "
                "ANY FUTURES TRADE — position size is in contracts and risk is "
                "(entry-stop) x contracts x multiplier, so a 10-point stop on MES risks $50 "
                "while the same stop on full-size ES risks $500."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"symbols": {"type": "array", "items": {"type": "string"}, "description": "Specific contracts; omit for all supported."}},
            },
        },
        {
            "name": "deploy_strategy",
            "description": (
                "Put one of YOUR accepted hypotheses into live service. From then on its "
                "signals appear in every snapshot under 'deployed_signals', already specified "
                "with symbol, side, stop and target, evaluated by the same code that validated "
                "it. This is how research becomes trades. Only an ACCEPTED hypothesis of your "
                "own desk can be deployed — rejected and pending ideas cannot, and neither can "
                "another desk's."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"hypothesis_id": {"type": "integer", "description": "id from research_log (must be status 'accepted' and yours)."}},
                "required": ["hypothesis_id"],
            },
        },
        {
            "name": "undeploy_strategy",
            "description": "Retire a deployed strategy so it stops generating live signals. Use when its live behavior clearly diverges from its validated record — say why in the journal.",
            "input_schema": {
                "type": "object",
                "properties": {"hypothesis_id": {"type": "integer"}},
                "required": ["hypothesis_id"],
            },
        },
        {
            "name": "list_deployed_strategies",
            "description": "Your currently-deployed strategies with their out-of-sample validation records.",
            "input_schema": {"type": "object", "properties": {}},
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
