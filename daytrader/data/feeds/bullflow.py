"""BullFlow options-flow alerts adapter (read-only).

RECONSTRUCTED after the source was lost to a .gitignore issue. BullFlow's public
API surface was inferred; verify the base URL/endpoints against your BullFlow
account if a call misbehaves. Degrades gracefully. Auth: Bearer token.
Env: BULLFLOW_API_KEY. Override the base with BULLFLOW_BASE_URL if needed.
"""
from __future__ import annotations

import os
from typing import Any

from .base import env, http_json

NAME = "bullflow"
_MAX_ROWS = 15


def _key() -> str | None:
    return env("BULLFLOW_API_KEY", "BULLFLOW_TOKEN")


def _base() -> str:
    return os.environ.get("BULLFLOW_BASE_URL", "https://api.bullflow.io").rstrip("/")


def is_configured() -> bool:
    return bool(_key())


def _get(path: str, params: dict | None = None) -> Any:
    key = _key()
    if not key:
        return {"error": "bullflow not configured (set BULLFLOW_API_KEY)"}
    return http_json(_base() + path, params=params or None,
                     headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})


def _rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "alerts", "results"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def _alerts(inp: dict) -> dict:
    symbol = (inp or {}).get("symbol")
    try:
        limit = min(int((inp or {}).get("limit", _MAX_ROWS)), _MAX_ROWS)
    except (TypeError, ValueError):
        limit = _MAX_ROWS
    params: dict = {"limit": limit}
    if symbol:
        params["ticker"] = str(symbol).upper()
    data = _get("/v1/flow/alerts", params)
    if isinstance(data, dict) and data.get("error"):
        return data
    out = []
    for r in _rows(data)[:limit]:
        if not isinstance(r, dict):
            continue
        out.append({"ticker": r.get("ticker") or r.get("symbol"),
                    "type": r.get("type") or r.get("side"),
                    "strike": r.get("strike"), "expiry": r.get("expiry"),
                    "premium": r.get("premium") or r.get("total_premium"),
                    "sentiment": r.get("sentiment"), "time": r.get("time") or r.get("created_at")})
    return {"symbol": (str(symbol).upper() if symbol else None), "count": len(out), "alerts": out}


def get_tools() -> list[dict]:
    return [
        {"name": "bullflow_alerts",
         "description": "Recent BullFlow options-flow alerts (optionally for a ticker): type, strike, expiry, premium, sentiment, time.",
         "input_schema": {"type": "object", "properties": {
             "symbol": {"type": "string"}, "limit": {"type": "integer"}}}},
    ]


def get_handlers() -> dict:
    return {"bullflow_alerts": _alerts}
