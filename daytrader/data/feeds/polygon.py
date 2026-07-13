"""Polygon.io adapter (read-only): quotes, news, market movers.

RECONSTRUCTED after the source was lost to a .gitignore issue — verify endpoints
against https://polygon.io/docs if a call misbehaves. Degrades gracefully.
Auth: apiKey query param. Key env: POLYGON_API_KEY.
"""
from __future__ import annotations

from typing import Any

from .base import env, http_json

NAME = "polygon"
_BASE = "https://api.polygon.io"
_MAX_ROWS = 12


def _key() -> str | None:
    return env("POLYGON_API_KEY", "POLYGON_KEY")


def is_configured() -> bool:
    return bool(_key())


def _get(path: str, params: dict | None = None) -> Any:
    key = _key()
    if not key:
        return {"error": "polygon not configured (set POLYGON_API_KEY)"}
    p = dict(params or {})
    p["apiKey"] = key
    return http_json(_BASE + path, params=p)


def _quote(inp: dict) -> dict:
    sym = str((inp or {}).get("symbol", "")).upper()
    if not sym:
        return {"error": "symbol required"}
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}")
    if isinstance(data, dict) and data.get("error"):
        return data
    t = (data or {}).get("ticker", {}) if isinstance(data, dict) else {}
    day, last = t.get("day", {}), t.get("lastTrade", {})
    return {"symbol": sym, "price": last.get("p"), "day_open": day.get("o"),
            "day_high": day.get("h"), "day_low": day.get("l"), "volume": day.get("v"),
            "change_pct": t.get("todaysChangePerc")}


def _news(inp: dict) -> dict:
    sym = str((inp or {}).get("symbol", "")).upper()
    try:
        limit = min(int((inp or {}).get("limit", 6)), _MAX_ROWS)
    except (TypeError, ValueError):
        limit = 6
    params = {"limit": limit, "order": "desc", "sort": "published_utc"}
    if sym:
        params["ticker"] = sym
    data = _get("/v2/reference/news", params)
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in (data or {}).get("results", [])[:limit]:
        out.append({"title": r.get("title"), "publisher": (r.get("publisher") or {}).get("name"),
                    "published": r.get("published_utc"), "url": r.get("article_url"),
                    "tickers": r.get("tickers")})
    return {"symbol": sym or None, "count": len(out), "news": out}


def _movers(inp: dict) -> dict:
    direction = str((inp or {}).get("direction", "gainers")).lower()
    if direction not in ("gainers", "losers"):
        direction = "gainers"
    data = _get(f"/v2/snapshot/locale/us/markets/stocks/{direction}")
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in (data or {}).get("tickers", [])[:_MAX_ROWS]:
        out.append({"symbol": r.get("ticker"), "change_pct": r.get("todaysChangePerc"),
                    "price": (r.get("lastTrade") or {}).get("p"),
                    "volume": (r.get("day") or {}).get("v")})
    return {"direction": direction, "count": len(out), "movers": out}


def get_tools() -> list[dict]:
    return [
        {"name": "poly_quote", "description": "Polygon.io snapshot for a ticker: last price, day OHLC, volume, % change.",
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
        {"name": "poly_news", "description": "Recent news articles (optionally for a ticker) from Polygon.io.",
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "limit": {"type": "integer"}}}},
        {"name": "poly_movers", "description": "Market movers (gainers/losers) from Polygon.io.",
         "input_schema": {"type": "object", "properties": {"direction": {"type": "string", "enum": ["gainers", "losers"]}}}},
    ]


def get_handlers() -> dict:
    return {"poly_quote": _quote, "poly_news": _news, "poly_movers": _movers}
