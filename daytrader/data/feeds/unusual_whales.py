"""Unusual Whales options-flow data adapter (read-only).

Base URL : https://api.unusualwhales.com
Auth     : Authorization: Bearer <token>  header

Endpoints used:
  GET /api/option-trades/flow-alerts        recent unusual options-flow alerts
  GET /api/stock/{ticker}/net-prem-ticks     per-minute net call/put premium ticks
  GET /api/stock/{ticker}/flow-recent        recent flow trades for one ticker
  GET /api/darkpool/{ticker}                 recent dark-pool prints for a ticker
  GET /api/market/total-options-volume       market-wide options volume
  GET /api/market/top-net-impact             tickers with largest net premium impact

All handlers degrade gracefully (missing key / HTTP error -> {"error": ...})
and cap payloads to keep the model's context small.
"""
from __future__ import annotations

from typing import Any

from .base import env, http_json

NAME = "unusual_whales"

_BASE = "https://api.unusualwhales.com"
_MAX_ROWS = 15


def _key() -> str | None:
    return env("UNUSUAL_WHALES_API_KEY", "UW_API_KEY", "UNUSUAL_WHALES_TOKEN")


def is_configured() -> bool:
    return bool(_key())


def _get(path: str, params: dict | None = None) -> Any:
    key = _key()
    if not key:
        return {"error": "unusual_whales not configured (set UNUSUAL_WHALES_API_KEY)"}
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    return http_json(_BASE + path, params=params or None, headers=headers)


def _rows(data: Any) -> list[dict]:
    if isinstance(data, dict):
        d = data.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            return [d]
        return []
    if isinstance(data, list):
        return data
    return []


def _num(v: Any) -> Any:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else round(f, 2)
    except (TypeError, ValueError):
        return v


def _side(row: dict) -> str:
    ask = _num(row.get("total_ask_side_prem"))
    bid = _num(row.get("total_bid_side_prem"))
    try:
        if ask is not None and bid is not None:
            if float(ask) > float(bid):
                return "ask"
            if float(bid) > float(ask):
                return "bid"
    except (TypeError, ValueError):
        pass
    return ""


def _flow_alerts(inp: dict) -> dict:
    symbol = (inp or {}).get("symbol")
    try:
        limit = min(int((inp or {}).get("limit", _MAX_ROWS)), _MAX_ROWS)
    except (TypeError, ValueError):
        limit = _MAX_ROWS
    params: dict = {"limit": max(1, limit)}
    if symbol:
        params["ticker_symbol"] = str(symbol).upper()
    data = _get("/api/option-trades/flow-alerts", params)
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in _rows(data)[:limit]:
        if not isinstance(r, dict):
            continue
        out.append({
            "ticker": r.get("ticker"), "type": r.get("type"),
            "strike": _num(r.get("strike")), "expiry": r.get("expiry"),
            "premium": _num(r.get("total_premium")), "size": _num(r.get("total_size")),
            "side": _side(r), "rule": r.get("alert_rule"),
            "sweep": bool(r.get("has_sweep")), "floor": bool(r.get("has_floor")),
            "time": r.get("created_at"),
        })
    return {"symbol": (str(symbol).upper() if symbol else None), "count": len(out), "alerts": out}


def _ticker_flow(inp: dict) -> dict:
    symbol = (inp or {}).get("symbol")
    if not symbol:
        return {"error": "symbol required"}
    sym = str(symbol).upper()
    ticks = _get(f"/api/stock/{sym}/net-prem-ticks")
    summary: dict = {"symbol": sym}
    if isinstance(ticks, dict) and ticks.get("error"):
        summary["net_premium_error"] = ticks.get("error")
    else:
        rows = _rows(ticks)
        last = rows[-1] if rows else {}
        if isinstance(last, dict):
            ncp = _num(last.get("net_call_premium"))
            npp = _num(last.get("net_put_premium"))
            summary["net_call_premium"] = ncp
            summary["net_put_premium"] = npp
            summary["net_call_volume"] = _num(last.get("net_call_volume"))
            summary["net_put_volume"] = _num(last.get("net_put_volume"))
            try:
                if ncp is not None and npp is not None:
                    summary["bias"] = "calls" if float(ncp) > float(npp) else "puts"
            except (TypeError, ValueError):
                pass
    recent = _get(f"/api/stock/{sym}/flow-recent", {"limit": _MAX_ROWS})
    notable = []
    if not (isinstance(recent, dict) and recent.get("error")):
        for r in _rows(recent)[:_MAX_ROWS]:
            if not isinstance(r, dict):
                continue
            notable.append({
                "type": r.get("type"), "strike": _num(r.get("strike")),
                "expiry": r.get("expiry"),
                "premium": _num(r.get("premium") or r.get("total_premium")),
                "size": _num(r.get("size") or r.get("total_size")),
                "side": r.get("side") or _side(r),
                "time": r.get("executed_at") or r.get("created_at"),
            })
    summary["notable_trades"] = notable
    return summary


def _dark_pool(inp: dict) -> dict:
    symbol = (inp or {}).get("symbol")
    if not symbol:
        return {"error": "symbol required"}
    sym = str(symbol).upper()
    data = _get(f"/api/darkpool/{sym}", {"limit": _MAX_ROWS})
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in _rows(data)[:_MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        out.append({
            "price": _num(r.get("price")), "size": _num(r.get("size")),
            "premium": _num(r.get("premium")), "volume": _num(r.get("volume")),
            "venue": r.get("market_center"), "time": r.get("executed_at"),
        })
    return {"symbol": sym, "count": len(out), "prints": out}


def _market_overview(_inp: dict) -> dict:
    out: dict = {}
    vol = _get("/api/market/total-options-volume")
    if isinstance(vol, dict) and vol.get("error"):
        out["volume_error"] = vol.get("error")
    else:
        rows = _rows(vol)
        last = rows[-1] if rows else (vol if isinstance(vol, dict) else {})
        if isinstance(last, dict):
            out["options_volume"] = {
                "call_volume": _num(last.get("call_volume")),
                "put_volume": _num(last.get("put_volume")),
                "call_premium": _num(last.get("call_premium")),
                "put_premium": _num(last.get("put_premium")),
            }
    impact = _get("/api/market/top-net-impact")
    top = []
    if not (isinstance(impact, dict) and impact.get("error")):
        for r in _rows(impact)[:_MAX_ROWS]:
            if not isinstance(r, dict):
                continue
            top.append({
                "ticker": r.get("ticker"),
                "net_premium": _num(r.get("net_premium") or r.get("net_call_premium")),
                "net_call_premium": _num(r.get("net_call_premium")),
                "net_put_premium": _num(r.get("net_put_premium")),
            })
    else:
        out["impact_error"] = impact.get("error")
    out["top_net_impact"] = top
    return out


def get_tools() -> list[dict]:
    return [
        {"name": "uw_flow_alerts",
         "description": "Recent unusual/notable options-flow alerts from Unusual Whales, optionally filtered by ticker (ticker, type, strike, expiry, premium, size, side, rule, sweep/floor, time).",
         "input_schema": {"type": "object", "properties": {
             "symbol": {"type": "string", "description": "Optional ticker filter, e.g. SPY."},
             "limit": {"type": "integer", "description": "Max rows (<=15)."}}}},
        {"name": "uw_ticker_flow",
         "description": "Options-flow summary for one ticker: net call vs put premium/volume, directional bias, recent notable trades.",
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
        {"name": "uw_dark_pool",
         "description": "Recent dark-pool prints for a ticker: price, size, premium, venue, time.",
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
        {"name": "uw_market_overview",
         "description": "Market-wide options-flow highlights: total call/put volume & premium and tickers with the largest net-premium impact.",
         "input_schema": {"type": "object", "properties": {}}},
    ]


def get_handlers() -> dict:
    return {
        "uw_flow_alerts": _flow_alerts,
        "uw_ticker_flow": _ticker_flow,
        "uw_dark_pool": _dark_pool,
        "uw_market_overview": _market_overview,
    }
