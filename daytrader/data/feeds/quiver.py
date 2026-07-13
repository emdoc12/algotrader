"""Quiver Quantitative adapter (read-only): congressional + insider + WSB flow.

RECONSTRUCTED after the source was lost to a .gitignore issue — verify endpoints
against https://api.quiverquant.com/docs if a call misbehaves. Degrades
gracefully. Auth: Bearer token. Key env: QUIVER_API_KEY.
"""
from __future__ import annotations

from typing import Any

from .base import env, http_json

NAME = "quiver"
_BASE = "https://api.quiverquant.com"
_MAX_ROWS = 15


def _key() -> str | None:
    return env("QUIVER_API_KEY", "QUIVER_TOKEN")


def is_configured() -> bool:
    return bool(_key())


def _get(path: str) -> Any:
    key = _key()
    if not key:
        return {"error": "quiver not configured (set QUIVER_API_KEY)"}
    return http_json(_BASE + path, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})


def _rows(data: Any) -> list[dict]:
    return data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])


def _congress(inp: dict) -> dict:
    sym = str((inp or {}).get("symbol", "")).upper()
    path = f"/beta/historical/congresstrading/{sym}" if sym else "/beta/live/congresstrading"
    data = _get(path)
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in _rows(data)[:_MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        out.append({"ticker": r.get("Ticker") or sym, "rep": r.get("Representative") or r.get("Senator"),
                    "transaction": r.get("Transaction"), "amount": r.get("Range") or r.get("Amount"),
                    "date": r.get("TransactionDate") or r.get("Date")})
    return {"symbol": sym or None, "count": len(out), "congress_trades": out}


def _insiders(inp: dict) -> dict:
    sym = str((inp or {}).get("symbol", "")).upper()
    if not sym:
        return {"error": "symbol required"}
    data = _get(f"/beta/historical/insiders/{sym}")
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in _rows(data)[:_MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        out.append({"insider": r.get("Name"), "transaction": r.get("AcquiredDisposedCode") or r.get("Transaction"),
                    "shares": r.get("Shares"), "date": r.get("Date")})
    return {"symbol": sym, "count": len(out), "insider_trades": out}


def _wsb(inp: dict) -> dict:
    data = _get("/beta/live/wallstreetbets")
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in _rows(data)[:_MAX_ROWS]:
        if not isinstance(r, dict):
            continue
        out.append({"ticker": r.get("Ticker"), "mentions": r.get("Mentions"),
                    "rank": r.get("Rank"), "sentiment": r.get("Sentiment")})
    return {"count": len(out), "wsb_trending": out}


def get_tools() -> list[dict]:
    return [
        {"name": "quiver_congress", "description": "Recent US congressional stock trades (optionally for a ticker) via Quiver Quant.",
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}}},
        {"name": "quiver_insiders", "description": "Recent corporate insider transactions for a ticker via Quiver Quant.",
         "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
        {"name": "quiver_wsb", "description": "Trending tickers on r/wallstreetbets (mentions, rank, sentiment) via Quiver Quant.",
         "input_schema": {"type": "object", "properties": {}}},
    ]


def get_handlers() -> dict:
    return {"quiver_congress": _congress, "quiver_insiders": _insiders, "quiver_wsb": _wsb}
